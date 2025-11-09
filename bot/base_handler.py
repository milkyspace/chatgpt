import asyncio
import logging
from abc import ABC
from typing import Dict, Any

from telegram import (
    Update, User
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackContext
)

import config
import database
from subscription import SubscriptionType
from subscription_config import SubscriptionConfig

# Настройка логирования
logger = logging.getLogger(__name__)

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

class BaseHandler(ABC):
    """Базовый класс для всех обработчиков."""

    def __init__(self, database: database.Database):
        self.db = database

    async def register_user_if_not_exists(self, update: Update, context: CallbackContext, user: User) -> bool:
        """Регистрирует пользователя если он не существует."""
        user_registered_now = False

        if not self.db.check_if_user_exists(user.id):
            self.db.add_new_user(
                user.id,
                update.message.chat_id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name
            )
            self.db.add_subscription(user.id, SubscriptionType.FREE, 7)
            user_registered_now = True
            self.db.start_new_dialog(user.id)

        await self._initialize_user_attributes(user.id)

        if user_registered_now:
            await self._send_registration_notification(context, user)

        return user_registered_now

    async def _initialize_user_attributes(self, user_id: int) -> None:
        """Инициализирует необходимые атрибуты пользователя."""
        if self.db.get_user_attribute(user_id, "current_dialog_id") is None:
            self.db.start_new_dialog(user_id)

        if user_id not in user_semaphores:
            user_semaphores[user_id] = asyncio.Semaphore(1)

        attributes_to_init = [
            ("current_model", config.models["available_text_models"][0]),
            ("n_used_tokens", {}),
            ("n_transcribed_seconds", 0.0),
            ("n_generated_images", 0)
        ]

        for attr, default_value in attributes_to_init:
            if self.db.get_user_attribute(user_id, attr) is None:
                self.db.set_user_attribute(user_id, attr, default_value)

    async def _send_registration_notification(self, context: CallbackContext, user: User) -> None:
        """Отправляет уведомление о новой регистрации администраторам."""
        username = user.username or "No username"
        first_name = user.first_name or "No first name"
        last_name = user.last_name or "No last name"

        notification_text = (
            f"A new user has just registered!\n\n"
            f"Username: {username}\n"
            f"First Name: {first_name}\n"
            f"Last Name: {last_name}"
        )

        for admin_id in config.roles.get('admin', []):
            try:
                await context.bot.send_message(chat_id=admin_id, text=notification_text)
            except Exception as e:
                logger.warning(f"Failed to send registration to admin {admin_id}: {e}")

    async def is_previous_message_not_answered_yet(self, update: Update, context: CallbackContext) -> bool:
        """Проверяет, обрабатывается ли предыдущее сообщение."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id

        if user_semaphores[user_id].locked():
            text = "⏳ Пожалуйста, <b>подождите</b> ответ на предыдущее сообщение\nИли отмените его командой /cancel"
            await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
            return True
        return False

    async def subscription_preprocessor(self, update: Update, context: CallbackContext) -> bool:
        """Проверяет возможность выполнения запроса по подписке."""
        try:
            user_id = update.effective_user.id
            subscription_info = self.db.get_user_subscription_info(user_id)

            if not subscription_info["is_active"]:
                await update.message.reply_text(
                    "❌ Для использования бота требуется активная подписка. "
                    "Пожалуйста, приобретите подписку через /subscription",
                    parse_mode=ParseMode.HTML
                )
                return False

            return await self._check_subscription_limits(subscription_info, update)
        except Exception as e:
            logger.error(f"Error in subscription preprocessor: {e}", exc_info=True)
            await update.message.reply_text(
                "❌ Ошибка проверки подписки. Пожалуйста, попробуйте еще раз.",
                parse_mode=ParseMode.HTML
            )
            return False

    async def _check_subscription_limits(self, subscription_info: Dict[str, Any], update: Update) -> bool:
        """Проверяет лимиты подписки используя централизованную конфигурацию."""
        subscription_type = SubscriptionType(subscription_info["type"])

        if not SubscriptionConfig.can_make_request(subscription_type, subscription_info["requests_used"]):
            description = SubscriptionConfig.get_description(subscription_type)
            await update.message.reply_text(
                f"❌ Лимит запросов подписки {description['name']} исчерпан. "
                "Пожалуйста, обновите подписку через /subscription",
                parse_mode=ParseMode.HTML
            )
            return False

        return True