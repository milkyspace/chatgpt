"""
Модуль Telegram бота для продажи доступа к ChatGPT.
Оптимизированная версия с улучшенной структурой и читаемостью.
"""

import logging
import asyncio
import traceback
import html
import json
from typing import Dict, Any, List
from telegram import (Update, BotCommand, BotCommandScopeAllPrivateChats)
from telegram.ext import (
    Application, ApplicationBuilder, CallbackContext, CommandHandler,
    MessageHandler, CallbackQueryHandler, AIORateLimiter, filters
)
from yookassa import Payment, Configuration

import config
import database
from subscription import SubscriptionType
from subscription_config import SubscriptionConfig
from handler_factory import HandlerFactory
from router_config import RouterConfig

# Настройка логирования
logger = logging.getLogger(__name__)

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Константы сообщений
HELP_MESSAGE = """<b>Команды:</b>
/new – Начать новый диалог 🆕
/retry – Перегенерировать предыдущий запрос 🔁
/mode – Выбрать режим
/subscription – Управление подписками 🔔
/my_payments – Мои платежи 📋
/help – Помощь ❓

🎤 Вы можете отправлять <b>голосовые сообщения</b> вместо текста

<blockquote>
1. Чат помнит контекст и предыдущие сообщения 10 минут. Чтобы начать заново — /new
2. «Ассистент» — режим по умолчанию. Попробуйте другие режимы: /mode
</blockquote>
"""

HELP_GROUP_CHAT_MESSAGE = """Вы можете добавить бота в любой <b>групповой чат</b> чтобы помогать и развлекать его участников!

Инструкции:
1. Добавьте бота в групповой чат
2. Сделайте его <b>администратором</b>, чтобы он мог видеть сообщения
3. Вы великолепны!

Чтобы получить ответ от бота в чате – @ <b>упомяните</b> его или <b>ответьте</b> на его сообщение.
Например: "{bot_username} напиши стихотворение о Telegram"
"""


def configure_logging() -> None:
    """Настраивает логирование."""
    log_level = logging.DEBUG if config.enable_detailed_logging else logging.CRITICAL
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    )
    logger.setLevel(logging.getLogger().level)


async def check_pending_payments_wrapper(context: CallbackContext) -> None:
    """Обертка для проверки pending платежей."""
    try:
        await check_pending_payments()
    except Exception as e:
        logger.error(f"Error in payment checking job: {e}")


async def check_pending_payments() -> None:
    """Проверяет статус pending платежей."""
    try:
        pending_payments = db.get_pending_payments()
        for payment in pending_payments:
            payment_id = payment["payment_id"]
            user_id = payment["user_id"]

            try:
                payment_info = Payment.find_one(payment_id)
                status = payment_info.status
                db.update_payment_status(payment_id, status)

                if status == 'succeeded':
                    await process_successful_payment(payment_info, user_id)
                elif status == 'canceled':
                    logger.info(f"Payment {payment_id} was canceled")

            except Exception as e:
                logger.error(f"Error checking payment {payment_id}: {e}")

    except Exception as e:
        logger.error(f"Error in payment checking: {e}")


# Инициализация и запуск бота
async def post_init(application: Application) -> None:
    """Функция инициализации после запуска бота."""
    commands = [
        BotCommand("/new", "Начать новый диалог 🆕"),
        BotCommand("/retry", "Перегенерировать предыдущий запрос 🔁"),
        BotCommand("/mode", "Выбрать режим"),
        BotCommand("/subscription", "Управление подписками 🔔"),
        BotCommand("/my_payments", "Мои платежи 📋"),
        BotCommand("/help", "Помощь ❓"),
    ]

    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())

    if config.yookassa_shop_id and config.yookassa_secret_key:
        application.job_queue.run_repeating(
            check_pending_payments_wrapper,
            interval=30,
            first=10
        )


def update_user_roles_from_config(database: database.Database, roles: Dict[str, List[int]]) -> None:
    """Обновляет роли пользователей из конфигурации."""
    for role, user_ids in roles.items():
        for user_id in user_ids:
            database.user_collection.update_one(
                {"_id": user_id},
                {"$set": {"role": role}}
            )
    logger.info("User roles updated from config.")


def setup_handlers(application: Application, handlers: Dict[str, Any]) -> None:
    """Настраивает все обработчики приложения."""
    router_config = RouterConfig()
    user_filter = create_user_filter()

    # Командные обработчики
    for command, (handler_type, method_name) in router_config.COMMAND_HANDLERS.items():
        handler = getattr(handlers[handler_type], method_name)
        application.add_handler(CommandHandler(command, handler, filters=user_filter))

    # Callback обработчики
    for pattern, (handler_type, method_name) in router_config.CALLBACK_HANDLERS.items():
        handler = getattr(handlers[handler_type], method_name)
        application.add_handler(CallbackQueryHandler(handler, pattern=pattern))

    # Обработчики сообщений
    for filters_obj, (handler_type, method_name) in router_config.MESSAGE_HANDLERS.items():
        handler = getattr(handlers[handler_type], method_name)
        application.add_handler(MessageHandler(filters_obj & user_filter, handler))


def create_user_filter() -> filters.BaseFilter:
    """Создает фильтр пользователей."""
    if not config.allowed_telegram_usernames:
        return filters.ALL

    usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
    user_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int) and x > 0]
    group_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int) and x < 0]

    return (filters.User(username=usernames) |
            filters.User(user_id=user_ids) |
            filters.Chat(chat_id=group_ids))


def create_application() -> Application:
    """Создает и настраивает приложение Telegram."""
    return (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .build()
    )


async def error_handle(update: Update, context: CallbackContext) -> None:
    """Обрабатывает ошибки бота."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    try:
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)

        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        error_for_user = (
            f"An unexpected error occurred. "
            f"Please try again or contact support if the issue persists."
        )

        await context.bot.send_message(update.effective_chat.id, error_for_user)

    except Exception as handler_error:
        logger.error("Error in error handler: %s", handler_error)


async def process_successful_payment(payment_info: Any, user_id: int) -> None:
    """
    Обрабатывает успешный платеж используя централизованную конфигурацию.
    """
    try:
        metadata = payment_info.metadata
        subscription_type = metadata.get('subscription_type')

        logger.info(f"Processing successful payment {payment_info.id} for user {user_id}")

        if subscription_type:
            subscription_type_enum = SubscriptionType(subscription_type)
            duration_days = SubscriptionConfig.get_duration(subscription_type_enum).days

            db.add_subscription(user_id, subscription_type_enum, duration_days)
            await send_subscription_confirmation(user_id, subscription_type_enum)
            logger.info(f"Subscription activated for user {user_id}: {subscription_type}")

    except Exception as e:
        logger.error(f"Error processing successful payment: {e}")


async def send_subscription_confirmation(user_id: int, subscription_type: SubscriptionType) -> None:
    """
    Отправляет подтверждение об активации подписки.
    """
    user = db.user_collection.find_one({"_id": user_id})
    if user:
        chat_id = user["chat_id"]
        duration_days = SubscriptionConfig.get_duration(subscription_type).days

        message = (
            f"🎉 Подписка *{subscription_type.name.replace('_', ' ').title()}* активирована!\n"
            f"📅 Действует *{duration_days} дней*\n\n"
            "Теперь вы можете пользоваться ботом по подписке!"
        )

        await bot_instance.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')


def update_user_roles_from_config(database: database.Database, roles: Dict[str, List[int]]) -> None:
    """Обновляет роли пользователей из конфигурации."""
    for role, user_ids in roles.items():
        for user_id in user_ids:
            database.user_collection.update_one(
                {"_id": user_id},
                {"$set": {"role": role}}
            )
    logger.info("User roles updated from config.")


def run_bot() -> None:
    """Запускает бота с оптимизированной конфигурацией."""
    # Инициализация платежной системы
    if config.yookassa_shop_id and config.yookassa_secret_key:
        Configuration.account_id = config.yookassa_shop_id
        Configuration.secret_key = config.yookassa_secret_key

    # Настройка системы
    update_user_roles_from_config(db, config.roles)
    configure_logging()

    # Создание приложения
    application = create_application()

    # Создание обработчиков
    handlers = HandlerFactory.create_handlers(db)

    # Настройка обработчиков
    setup_handlers(application, handlers)

    # Обработчик ошибок
    application.add_error_handler(error_handle)

    # Запуск
    application.run_polling()


if __name__ == "__main__":
    run_bot()
