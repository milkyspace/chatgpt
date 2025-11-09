import logging
from datetime import datetime
from typing import Dict, Any

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

from base_handler import BaseHandler
from payment import create_subscription_yookassa_payment
from subscription import SubscriptionType
from subscription_config import SubscriptionConfig
from utils import HELP_MESSAGE

logger = logging.getLogger(__name__)


class SubscriptionHandlers(BaseHandler):
    """Класс для обработки подписок и платежей."""

    # Константы для эмодзи и текста
    _EMOJI_MAP = {
        "current_sub": "📋",
        "expires": "📅",
        "usage": "📊",
        "images": "🎨",
        "subscriptions": "🔔",
        "payment": "💳",
        "back": "⬅️",
        "error": "❌",
        "pending": "⏳",
        "waiting": "🔄",
        "success": "✅",
        "canceled": "❌",
        "unknown": "❓"
    }

    _STATUS_EMOJI = {
        "pending": _EMOJI_MAP["pending"],
        "waiting_for_capture": _EMOJI_MAP["waiting"],
        "succeeded": _EMOJI_MAP["success"],
        "canceled": _EMOJI_MAP["canceled"]
    }

    async def subscription_handle(self, update: Update, context: CallbackContext) -> None:
        """Показывает доступные подписки."""
        try:
            user = self._get_user_from_update(update)
            await self.register_user_if_not_exists(update, context, user)

            user_id = user.id
            self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

            subscription_info = self.db.get_user_subscription_info(user_id)
            text = self._format_subscription_info(subscription_info)
            reply_markup = self._create_subscription_keyboard()

            await self._send_subscription_message(update, text, reply_markup)

        except Exception as e:
            logger.error(f"Error in subscription_handle: {e}")
            await self._handle_subscription_error(update)

    def _get_user_from_update(self, update: Update) -> telegram.User:
        """Получает пользователя из update."""
        return (update.message or update.callback_query).from_user

    def _format_subscription_info(self, subscription_info: Dict[str, Any]) -> str:
        """Форматирует информацию о подписке."""
        text_parts = []

        # Текущая подписка
        if subscription_info["is_active"]:
            if subscription_info["type"] != "free":
                expires_str = subscription_info["expires_at"].strftime("%d.%m.%Y")
                text_parts.extend([
                    f"{self._EMOJI_MAP['current_sub']} <b>Текущая подписка:</b> {subscription_info['type'].upper()}",
                    f"{self._EMOJI_MAP['expires']} <b>Действует до:</b> {expires_str}"
                ])
            else:
                text_parts.append(f"{self._EMOJI_MAP['current_sub']} <b>Текущая подписка:</b> БЕСПЛАТНАЯ")

            usage_text = self._format_usage_info(subscription_info)
            text_parts.append(usage_text)

        # Доступные подписки
        text_parts.extend([
            "",
            f"{self._EMOJI_MAP['subscriptions']} <b>Доступные подписки</b>",
            self._format_available_subscriptions()
        ])

        return "\n".join(text_parts)

    def _format_usage_info(self, subscription_info: Dict[str, Any]) -> str:
        """Форматирует информацию об использовании."""
        subscription_type = SubscriptionType(subscription_info["type"])
        limits = SubscriptionConfig.get_usage_limits(subscription_type)

        max_requests = limits.get("max_requests", 0)
        max_images = limits.get("max_images", 0)

        # Форматирование текста с безлимитными значениями
        requests_text = self._format_limit_text(subscription_info['requests_used'], max_requests)
        images_text = self._format_limit_text(subscription_info['images_used'], max_images)

        return (
            f"{self._EMOJI_MAP['usage']} <b>Запросы использовано:</b> {requests_text}\n"
            f"{self._EMOJI_MAP['images']} <b>Изображения использовано:</b> {images_text}"
        )

    def _format_limit_text(self, used: int, limit: float) -> str:
        """Форматирует текст с лимитом."""
        if limit == float('inf'):
            return f"{used} (безлимитно)"
        return f"{used}/{limit}"

    def _format_available_subscriptions(self) -> str:
        """Форматирует информацию о доступных подписках."""
        text_parts = []

        for sub_type in SubscriptionConfig.get_all_paid_subscriptions():
            description = SubscriptionConfig.get_description(sub_type)
            price = SubscriptionConfig.get_price(sub_type)
            duration = SubscriptionConfig.get_duration(sub_type)

            text_parts.extend([
                f"<b>{description['name']}</b> - {price}₽ / {duration.days} дней",
                f"   {description['features']}",
                ""
            ])

        return "\n".join(text_parts)

    def _create_subscription_keyboard(self) -> InlineKeyboardMarkup:
        """Создает клавиатуру для выбора подписки."""
        buttons = []

        for sub_type in SubscriptionConfig.get_all_paid_subscriptions():
            description = SubscriptionConfig.get_description(sub_type)
            price = SubscriptionConfig.get_price(sub_type)

            name = f"{description['name']} - {price}₽"
            callback_data = f"subscribe|{sub_type.value}"
            buttons.append([InlineKeyboardButton(name, callback_data=callback_data)])

        return InlineKeyboardMarkup(buttons)

    async def _send_subscription_message(self, update: Update, text: str,
                                         reply_markup: InlineKeyboardMarkup) -> None:
        """Отправляет сообщение с информацией о подписках."""
        try:
            if update.message:
                await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            else:
                await update.callback_query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                return
            # Fallback для callback query
            if update.callback_query:
                await update.callback_query.message.reply_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )

    async def _handle_subscription_error(self, update: Update) -> None:
        """Обрабатывает ошибки при работе с подписками."""
        error_text = f"{self._EMOJI_MAP['error']} Произошла ошибка при загрузке подписок. Пожалуйста, попробуйте снова."

        if update.callback_query:
            await update.callback_query.message.reply_text(error_text, parse_mode=ParseMode.HTML)

    async def subscription_callback_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает callback выбора подписки."""
        query = update.callback_query
        await query.answer()

        data = query.data

        if data == "subscription_back":
            await self._handle_subscription_back(query)
        elif data.startswith("subscribe|"):
            await self._handle_subscription_payment(query, context)

    async def _handle_subscription_back(self, query: telegram.CallbackQuery) -> None:
        """Обрабатывает возврат из меню подписок."""
        reply_text = f"Возврат в главное меню...\n\n{HELP_MESSAGE}"

        try:
            await query.edit_message_text(
                reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                await query.message.reply_text(
                    reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )

    async def _handle_subscription_payment(self, query: telegram.CallbackQuery,
                                           context: CallbackContext) -> None:
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
                f"{self._EMOJI_MAP['error']} Произошла ошибка при создании платежа. Пожалуйста, попробуйте позже.",
                parse_mode=ParseMode.HTML
            )

    def _format_payment_message(self, subscription_type: SubscriptionType) -> str:
        """Форматирует сообщение об оплате."""
        price = SubscriptionConfig.get_price(subscription_type)
        duration = SubscriptionConfig.get_duration(subscription_type)
        description = SubscriptionConfig.get_description(subscription_type)

        return (
            f"{self._EMOJI_MAP['payment']} <b>Оформление подписки {description['name']}</b>\n\n"
            f"Стоимость: <b>{price}₽</b>\n"
            f"Период: <b>{duration.days} дней</b>\n"
            f"Возможности: {description['features']}\n\n"
            "Нажмите кнопку ниже для оплаты. После успешной оплаты подписка активируется автоматически!"
        )

    def _create_payment_keyboard(self, payment_url: str) -> InlineKeyboardMarkup:
        """Создает клавиатуру для оплаты."""
        keyboard = [
            [InlineKeyboardButton(f"{self._EMOJI_MAP['payment']} Оплатить", url=payment_url)],
            [InlineKeyboardButton(f"{self._EMOJI_MAP['back']} Назад", callback_data="subscription_back")]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def my_payments_handle(self, update: Update, context: CallbackContext) -> None:
        """Показывает статус pending платежей пользователя"""
        user = update.message.from_user
        await self.register_user_if_not_exists(update, context, user)

        user_id = user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        pending_payments = self.db.get_user_pending_payments(user_id)

        if not pending_payments:
            await update.message.reply_text(
                "У вас нет ожидающих платежей.",
                parse_mode=ParseMode.HTML
            )
            return

        text_lines = [
            f"{self._EMOJI_MAP['current_sub']} <b>Ваши ожидающие платежи:</b>\n"
        ]

        for payment in pending_payments:
            status_emoji = self._STATUS_EMOJI.get(payment["status"], self._EMOJI_MAP["unknown"])
            created_at = payment["created_at"].strftime("%d.%m.%Y %H:%M")

            text_lines.extend([
                f"{status_emoji} <b>{payment['amount']} ₽</b> - {payment['status']}",
                f"   ID: <code>{payment['payment_id']}</code>",
                f"   Создан: {created_at}",
                ""
            ])

        text_lines.append("Платежи проверяются автоматически каждые 30 секунд.")

        await update.message.reply_text("\n".join(text_lines), parse_mode=ParseMode.HTML)