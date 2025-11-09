from yookassa import Payment

import bot.base_handler as BaseHandler
import bot.subscription_config as SubscriptionConfig
import bot.subscription as SubscriptionType

import logging
import asyncio
from typing import Dict, Any
import telegram
from telegram import (Update, User, InlineKeyboardButton, InlineKeyboardMarkup)
from telegram.ext import (CallbackContext)
from telegram.constants import ParseMode
import database
from subscription import SubscriptionType
from subscription_config import SubscriptionConfig

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

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


class SubscriptionHandlers(BaseHandler):
    """Класс для обработки подписок и платежей."""

    async def subscription_handle(self, update: Update, context: CallbackContext) -> None:
        """Показывает доступные подписки."""
        try:
            user = self._get_user_from_update(update)
            user_id = await self.ensure_user_initialized(update, context, user)

            subscription_info = self.db.get_user_subscription_info(user_id)
            text = self._format_subscription_info(subscription_info)
            reply_markup = self._create_subscription_keyboard()

            await self._send_subscription_message(update, text, reply_markup)

        except Exception as e:
            logger.error(f"Error in subscription_handle: {e}")
            await self._handle_subscription_error(update)

    def _get_user_from_update(self, update: Update) -> User:
        """Получает пользователя из update."""
        if update.message is not None:
            return update.message.from_user
        else:
            return update.callback_query.from_user

    def _format_subscription_info(self, subscription_info: Dict[str, Any]) -> str:
        """Форматирует информацию о подписке."""
        text = ""
        if subscription_info["is_active"]:
            if subscription_info["type"] != "free":
                expires_str = subscription_info["expires_at"].strftime("%d.%m.%Y")
                text += f"📋 <b>Текущая подписка:</b> {subscription_info['type'].upper()}\n"
                text += f"📅 <b>Действует до:</b> {expires_str}\n"
            else:
                text += f"📋 <b>Текущая подписка:</b> БЕСПЛАТНАЯ\n"

            usage_text = self._format_usage_info(subscription_info)
            text += usage_text + "\n"

        text += "\n🔔 <b>Доступные подписки</b>\n"
        text += self._format_available_subscriptions()

        return text

    def _format_usage_info(self, subscription_info: Dict[str, Any]) -> str:
        """Форматирует информацию об использовании используя централизованную конфигурацию."""
        subscription_type = SubscriptionType(subscription_info["type"])
        limits = SubscriptionConfig.get_usage_limits(subscription_type)

        max_requests = limits.get("max_requests", 0)
        max_images = limits.get("max_images", 0)

        requests_text = f"{subscription_info['requests_used']}/{max_requests}" if max_requests != float(
            'inf') else f"{subscription_info['requests_used']} (безлимитно)"
        images_text = f"{subscription_info['images_used']}/{max_images}" if max_images != float(
            'inf') else f"{subscription_info['images_used']} (безлимитно)"

        return (
            f"📊 <b>Запросы использовано:</b> {requests_text}\n"
            f"🎨 <b>Изображения использовано:</b> {images_text}"
        )

    def _format_available_subscriptions(self) -> str:
        """Форматирует информацию о доступных подписках используя централизованную конфигурацию."""
        text = ""

        for sub_type in SubscriptionConfig.get_all_paid_subscriptions():
            description = SubscriptionConfig.get_description(sub_type)
            price = SubscriptionConfig.get_price(sub_type)
            duration = SubscriptionConfig.get_duration(sub_type)

            text += f"<b>{description['name']}</b> - {price}₽ / {duration.days} дней\n"
            text += f"   {description['features']}\n\n"

        return text

    def _create_subscription_keyboard(self) -> InlineKeyboardMarkup:
        """Создает клавиатуру для выбора подписки используя централизованную конфигурацию."""
        keyboard = []

        for sub_type in SubscriptionConfig.get_all_paid_subscriptions():
            description = SubscriptionConfig.get_description(sub_type)
            price = SubscriptionConfig.get_price(sub_type)

            name = f"{description['name']} - {price}₽"
            callback_data = f"subscribe|{sub_type.value}"
            keyboard.append([InlineKeyboardButton(name, callback_data=callback_data)])

        return InlineKeyboardMarkup(keyboard)

    async def _send_subscription_message(self, update: Update, text: str,
                                         reply_markup: InlineKeyboardMarkup) -> None:
        """Отправляет сообщение с информацией о подписках."""
        if update.message is not None:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            try:
                await update.callback_query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )
            except telegram.error.BadRequest as e:
                if "Message is not modified" not in str(e):
                    await update.callback_query.message.reply_text(
                        text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                    )

    async def _handle_subscription_error(self, update: Update) -> None:
        """Обрабатывает ошибки при работе с подписками."""
        error_text = "❌ Произошла ошибка при загрузке подписок. Пожалуйста, попробуйте снова."
        if update.callback_query:
            await update.callback_query.message.reply_text(error_text, parse_mode=ParseMode.HTML)

    async def subscription_callback_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает callback выбора подписки."""
        query = update.callback_query
        await query.answer()

        data = query.data

        if data == "subscription_back":
            await self._handle_subscription_back(query)
            return

        if data.startswith("subscribe|"):
            await self._handle_subscription_payment(query, context)

    async def _handle_subscription_back(self, query: telegram.CallbackQuery) -> None:
        """Обрабатывает возврат из меню подписок."""
        reply_text = "Возврат в главное меню...\n\n" + HELP_MESSAGE
        try:
            await query.edit_message_text(
                reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                await query.message.reply_text(
                    reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )

    async def _handle_subscription_payment(self, query: telegram.CallbackQuery, context: CallbackContext) -> None:
        """Обрабатывает создание платежа для подписки."""
        try:
            _, subscription_type_str = query.data.split("|")
            subscription_type = SubscriptionType(subscription_type_str)

            payment_url = await create_subscription_yookassa_payment(
                query.from_user.id, subscription_type, context
            )

            text = self._format_payment_message(subscription_type)
            keyboard = self._create_payment_keyboard(payment_url)

            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

        except Exception as e:
            logger.error(f"Error in subscription payment: {e}")
            await query.edit_message_text(
                "❌ Произошла ошибка при создании платежа. Пожалуйста, попробуйте позже.",
                parse_mode=ParseMode.HTML
            )

    def _format_payment_message(self, subscription_type: SubscriptionType) -> str:
        """Форматирует сообщение об оплате используя централизованную конфигурацию."""
        price = SubscriptionConfig.get_price(subscription_type)
        duration = SubscriptionConfig.get_duration(subscription_type)
        description = SubscriptionConfig.get_description(subscription_type)

        return (
            f"💳 <b>Оформление подписки {description['name']}</b>\n\n"
            f"Стоимость: <b>{price}₽</b>\n"
            f"Период: <b>{duration.days} дней</b>\n"
            f"Возможности: {description['features']}\n\n"
            "Нажмите кнопку ниже для оплаты. После успешной оплаты подписка активируется автоматически!"
        )

    def _create_payment_keyboard(self, payment_url: str) -> InlineKeyboardMarkup:
        """Создает клавиатуру для оплаты."""
        keyboard = [
            [InlineKeyboardButton("💳 Оплатить", url=payment_url)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="subscription_back")]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def my_payments_handle(self, update: Update, context: CallbackContext) -> None:
        """Показывает статус pending платежей пользователя"""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        pending_payments = self.db.get_user_pending_payments(user_id)

        if not pending_payments:
            await update.message.reply_text(
                "У вас нет ожидающих платежей.",
                parse_mode=ParseMode.HTML
            )
            return

        text = "📋 <b>Ваши ожидающие платежи:</b>\n\n"

        for payment in pending_payments:
            amount = payment["amount"]
            payment_id = payment["payment_id"]
            status = payment["status"]
            created_at = payment["created_at"].strftime("%d.%m.%Y %H:%M")

            status_emoji = {
                "pending": "⏳",
                "waiting_for_capture": "🔄",
                "succeeded": "✅",
                "canceled": "❌"
            }.get(status, "❓")

            text += f"{status_emoji} <b>{amount} ₽</b> - {status}\n"
            text += f"   ID: <code>{payment_id}</code>\n"
            text += f"   Создан: {created_at}\n\n"

        text += "Платежи проверяются автоматически каждые 30 секунд."

        await update.message.reply_text(text, parse_mode=ParseMode.HTML)