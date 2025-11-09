"""
Главный модуль для запуска Telegram бота.
"""

import logging
import asyncio
from datetime import datetime
from typing import Dict, Any

import telegram
from telegram.ext import (
    Application, ApplicationBuilder, CallbackContext, CommandHandler,
    MessageHandler, CallbackQueryHandler, AIORateLimiter, filters
)
from telegram.constants import ParseMode

import config
import database
from keyboards import BotKeyboards
from subscription_config import SubscriptionConfig

# Импорт обработчиков
from handlers.base_handler import BaseHandler
from handlers.message_handlers import MessageHandlers
from handlers.subscription_handlers import SubscriptionHandlers
from handlers.chat_mode_handlers import ChatModeHandlers
from handlers.image_handlers import ImageHandlers
from handlers.settings_handlers import SettingsHandlers
from handlers.admin_handlers import AdminHandlers

from utils.payment_utils import (
    create_subscription_yookassa_payment,
    process_successful_payment,
    check_pending_payments
)

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Настраивает логирование."""
    log_level = logging.DEBUG if config.enable_detailed_logging else logging.CRITICAL
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    )
    logger.setLevel(logging.getLogger().level)


def update_user_roles_from_config(database: database.Database, roles: Dict[str, List[int]]) -> None:
    """Обновляет роли пользователей из конфигурации."""
    for role, user_ids in roles.items():
        for user_id in user_ids:
            database.user_collection.update_one(
                {"_id": user_id},
                {"$set": {"role": role}}
            )
    logger.info("User roles updated from config.")


async def post_init(application: Application) -> None:
    """Функция инициализации после запуска бота."""
    from telegram import BotCommand, BotCommandScopeAllPrivateChats

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


async def check_pending_payments_wrapper(context: CallbackContext) -> None:
    """Обертка для проверки pending платежей."""
    try:
        await check_pending_payments(db, bot_instance)
    except Exception as e:
        logger.error(f"Error in payment checking job: {e}")


async def error_handle(update: Update, context: CallbackContext) -> None:
    """Обрабатывает ошибки бота."""
    import traceback
    import html
    import json

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


def setup_handlers(application, user_filter):
    """Настраивает все обработчики бота."""

    # Создаем экземпляры обработчиков
    subscription_handlers = SubscriptionHandlers(db)
    image_handlers = ImageHandlers(db)
    chat_mode_handlers = ChatModeHandlers(db)
    admin_handlers = AdminHandlers(db)
    message_handlers = MessageHandlers(db, subscription_handlers, chat_mode_handlers, admin_handlers, image_handlers)
    settings_handlers = SettingsHandlers(db)

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", message_handlers.start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", message_handlers.help_handle, filters=user_filter))
    application.add_handler(CommandHandler("settings", settings_handlers.settings_handle, filters=user_filter))
    application.add_handler(
        CommandHandler("help_group_chat", message_handlers.help_group_chat_handle, filters=user_filter))
    application.add_handler(CommandHandler("retry", message_handlers.retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", message_handlers.new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", message_handlers.cancel_handle, filters=user_filter))
    application.add_handler(CommandHandler("mode", chat_mode_handlers.show_chat_modes_handle, filters=user_filter))
    application.add_handler(
        CommandHandler("my_payments", subscription_handlers.my_payments_handle, filters=user_filter))

    # Добавляем обработчики команд админ-панели
    application.add_handler(CommandHandler("edit_user", admin_handlers.edit_user_command, filters=user_filter))
    application.add_handler(CommandHandler("broadcast", admin_handlers.broadcast_command, filters=user_filter))
    application.add_handler(CommandHandler("user_data", admin_handlers.get_user_data_command, filters=user_filter))

    # Добавляем обработчики сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter,
                                           message_handlers.message_handle))
    application.add_handler(MessageHandler(filters.VOICE & user_filter,
                                           message_handlers.voice_message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & user_filter,
                                           message_handlers.photo_message_handle))
    application.add_handler(MessageHandler(filters.Document.IMAGE & user_filter,
                                           message_handlers.photo_message_handle))

    # Добавляем обработчики подписок
    application.add_handler(
        CommandHandler("subscription", subscription_handlers.subscription_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(subscription_handlers.subscription_callback_handle,
                                                 pattern='^subscribe\\|'))
    application.add_handler(CallbackQueryHandler(subscription_handlers.subscription_handle,
                                                 pattern='^subscription_back$'))

    # Добавляем обработчики режимов чата
    application.add_handler(CallbackQueryHandler(chat_mode_handlers.show_chat_modes_callback_handle,
                                                 pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(chat_mode_handlers.set_chat_mode_handle,
                                                 pattern="^set_chat_mode"))

    # Добавляем обработчики для настроек
    application.add_handler(CallbackQueryHandler(
        settings_handlers.model_settings_handler,
        pattern="^model-"
    ))
    application.add_handler(CallbackQueryHandler(
        settings_handlers.set_settings_handle,
        pattern="^model-set_settings\\|"
    ))
    application.add_handler(CallbackQueryHandler(
        settings_handlers.set_settings_handle,
        pattern="^claude-model-set_settings\\|"
    ))

    # Обработчики для настроек художника
    application.add_handler(CallbackQueryHandler(
        settings_handlers.model_settings_handler,
        pattern="^model-artist"
    ))
    application.add_handler(CallbackQueryHandler(
        settings_handlers.model_settings_handler,
        pattern="^model-artist-set_model\\|"
    ))
    application.add_handler(CallbackQueryHandler(
        settings_handlers.model_settings_handler,
        pattern="^model-artist-set_images\\|"
    ))
    application.add_handler(CallbackQueryHandler(
        settings_handlers.model_settings_handler,
        pattern="^model-artist-set_resolution\\|"
    ))
    application.add_handler(CallbackQueryHandler(
        settings_handlers.model_settings_handler,
        pattern="^model-artist-set_quality\\|"
    ))

    # Добавляем обработчики админ-панели (callback)
    application.add_handler(CallbackQueryHandler(admin_handlers.broadcast_confirmation_handler,
                                                 pattern="^confirm_broadcast\\|"))
    application.add_handler(CallbackQueryHandler(admin_handlers.broadcast_confirmation_handler,
                                                 pattern="^cancel_broadcast"))

    # Добавляем обработчик ошибок
    application.add_error_handler(error_handle)


def run_bot() -> None:
    """Запускает бота."""
    global bot_instance

    if config.yookassa_shop_id and config.yookassa_secret_key:
        from yookassa import Configuration
        Configuration.account_id = config.yookassa_shop_id
        Configuration.secret_key = config.yookassa_secret_key

    update_user_roles_from_config(db, config.roles)
    configure_logging()

    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .build()
    )

    bot_instance = application.bot

    # Настройка фильтра пользователей
    user_filter = filters.ALL
    if config.allowed_telegram_usernames:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = (filters.User(username=usernames) |
                       filters.User(user_id=user_ids) |
                       filters.Chat(chat_id=group_ids))

    # Настройка обработчиков
    setup_handlers(application, user_filter)

    application.run_polling()


if __name__ == "__main__":
    run_bot()