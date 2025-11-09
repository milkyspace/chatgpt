import logging
import asyncio
from typing import Dict
from telegram import (Update)
from telegram.ext import (CallbackContext)
from bot.handlers.base_handler import BaseHandler
import bot.database as database

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class MessageRouter(BaseHandler):
    """Маршрутизатор сообщений по типам."""

    MESSAGE_HANDLERS = {
        'text': '_handle_text_message',
        'voice': '_handle_voice_message',
        'photo': '_handle_photo_message',
        'document': '_handle_document_message'
    }

    async def route_message(self, update: Update, context: CallbackContext) -> None:
        """Маршрутизирует сообщение в соответствующий обработчик."""
        if not await self.is_bot_mentioned(update, context):
            return

        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if await self.is_previous_message_not_answered_yet(update, context):
            return

        if not await self.subscription_preprocessor(update, context):
            return

        # Определяем тип сообщения и вызываем соответствующий обработчик
        message_type = self._get_message_type(update)
        handler_name = self.MESSAGE_HANDLERS.get(message_type)

        if handler_name:
            handler = getattr(self, handler_name)
            await handler(update, context, user_id)

    def _get_message_type(self, update: Update) -> str:
        """Определяет тип сообщения."""
        if update.message.voice:
            return 'voice'
        elif update.message.photo:
            return 'photo'
        elif update.message.document:
            return 'document'
        else:
            return 'text'