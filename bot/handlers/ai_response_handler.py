import logging
import asyncio
import telegram
from typing import Dict, Any
from telegram.ext import (CallbackContext)
from telegram.constants import ParseMode
from ..database import database

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)


class AIResponseHandler:
    """Унифицированная обработка ответов от AI."""

    @staticmethod
    async def send_ai_response(
            context: CallbackContext,
            chat_id: int,
            response_data: Dict[str, Any],
            parse_mode: str = ParseMode.HTML
    ) -> telegram.Message:
        """Универсальный метод отправки ответов AI."""
        answer = response_data.get('answer', '')
        response_type = response_data.get('type', 'text')

        if response_type == 'text':
            return await context.bot.send_message(
                chat_id=chat_id,
                text=answer[:4096],
                parse_mode=parse_mode,
                disable_web_page_preview=True
            )
        elif response_type == 'photo':
            return await context.bot.send_photo(
                chat_id=chat_id,
                photo=response_data['image_url'],
                caption=answer
            )

    @staticmethod
    async def edit_ai_response(
            context: CallbackContext,
            message: telegram.Message,
            answer: str,
            parse_mode: str = ParseMode.HTML
    ) -> None:
        """Редактирует сообщение с ответом AI."""
        try:
            await context.bot.edit_message_text(
                answer[:4096],
                chat_id=message.chat_id,
                message_id=message.message_id,
                parse_mode=parse_mode,
                disable_web_page_preview=True
            )
        except telegram.error.BadRequest as e:
            if not str(e).startswith("Message is not modified"):
                await context.bot.edit_message_text(
                    answer[:4096],
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    disable_web_page_preview=True
                )