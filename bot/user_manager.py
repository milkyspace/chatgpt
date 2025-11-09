from datetime import datetime
from telegram import (Update, User)
from telegram.ext import (CallbackContext)
from telegram.constants import ParseMode
from keyboards import BotKeyboards
from bot.base_handler import BaseHandler
from bot.message_handlers import HELP_MESSAGE


class UserManager(BaseHandler):
    """Централизованное управление пользователями."""

    async def ensure_user_initialized(self, update: Update, context: CallbackContext, user: User) -> int:
        """Гарантирует инициализацию пользователя одним вызовом."""
        user_registered = await self.register_user_if_not_exists(update, context, user)

        if user_registered:
            await self._send_welcome_message(update, context)

        user_id = user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())
        return user_id

    async def _send_welcome_message(self, update: Update, context: CallbackContext) -> None:
        """Отправляет приветственное сообщение новым пользователям."""
        welcome_text = self._get_welcome_message()
        reply_markup = await BotKeyboards.get_main_keyboard(update.message.from_user.id)

        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )

    def _get_welcome_message(self) -> str:
        """Возвращает приветственное сообщение."""
        return (
                "👋 Привет! Мы <b>Ducks GPT</b>\n"
                "Компактный чат-бот на базе <b>ChatGPT</b>\n"
                "Рады знакомству!\n\n"
                "Доступны в <b>РФ</b>🇷🇺\n"
                "<b>Дарим подписку на 7 дней:</b>\n"
                "- 15 запросов\n"
                "- 3 генерации изображения\n\n"
                + HELP_MESSAGE
        )