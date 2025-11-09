import logging
import asyncio
from typing import Dict

from telegram import (Update)
from telegram.constants import ParseMode

import bot.database as database

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class ErrorHandler:
    """Централизованная обработка ошибок."""

    ERROR_MESSAGES = {
        "openai_rate_limit": "⚠️ Превышен лимит запросов. Попробуйте через минуту.",
        "openai_quota_exceeded": "❌ Исчерпан лимит токенов. Обратитесь к администратору.",
        "openai_safety_system": "❌ Запрос не соответствует политикам безопасности.",
        "default": "❌ Произошла ошибка. Пожалуйста, попробуйте еще раз."
    }

    @classmethod
    async def handle_ai_error(cls, update: Update, error: Exception) -> None:
        """Обрабатывает ошибки AI с понятными сообщениями."""
        error_message = cls._get_user_friendly_error(error)
        await update.message.reply_text(error_message, parse_mode=ParseMode.HTML)

        # Логируем полную ошибку для разработчика
        logger.error(f"AI Error: {error}", exc_info=True)

    @classmethod
    def _get_user_friendly_error(cls, error: Exception) -> str:
        """Возвращает понятное пользователю сообщение об ошибке."""
        error_str = str(error).lower()

        for key, message in cls.ERROR_MESSAGES.items():
            if key in error_str:
                return message

        return cls.ERROR_MESSAGES["default"]