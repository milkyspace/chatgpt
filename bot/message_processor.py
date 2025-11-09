from datetime import datetime
import config

import logging
import asyncio

from typing import Dict
from telegram import (Update)
from telegram.ext import (CallbackContext)
from telegram.constants import ParseMode

import database
import bot.base_handler as BaseHandler

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class MessageProcessor(BaseHandler):
    """Класс для обработки сообщений с устранением дублирования."""

    async def is_bot_mentioned(self, update: Update, context: CallbackContext) -> bool:
        """Проверяет, упомянут ли бот в сообщении."""
        try:
            message = update.message

            if message.chat.type == "private":
                return True

            if message.text and ("@" + context.bot.username) in message.text:
                return True

            if (message.reply_to_message and
                    message.reply_to_message.from_user.id == context.bot.id):
                return True

        except Exception:
            return True

        return False

    async def prepare_dialog(self, user_id: int, use_new_dialog_timeout: bool,
                             chat_mode: str, update: Update) -> None:
        """Подготавливает диалог для нового сообщения."""
        if use_new_dialog_timeout:
            last_interaction = self.db.get_user_attribute(user_id, "last_interaction")
            dialog_messages = self.db.get_dialog_messages(user_id)

            if (datetime.now() - last_interaction).seconds > config.new_dialog_timeout and len(dialog_messages) > 0:
                self.db.start_new_dialog(user_id)
                await update.message.reply_text(
                    f"Запуск нового диалога (<b>{config.chat_modes[chat_mode]['name']}</b>) ✅",
                    parse_mode=ParseMode.HTML
                )

        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

    def update_dialog_and_tokens(self, user_id: int, new_dialog_message: Dict,
                                 n_input_tokens: int, n_output_tokens: int) -> None:
        """Обновляет диалог и счетчики токенов."""
        current_model = self.db.get_user_attribute(user_id, "current_model")
        current_dialog_messages = self.db.get_dialog_messages(user_id, dialog_id=None)
        self.db.set_dialog_messages(user_id, current_dialog_messages + [new_dialog_message], dialog_id=None)

        self.db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

        action_type = self.db.get_user_attribute(user_id, "current_model")
        self.db.deduct_cost_for_action(
            user_id=user_id,
            action_type=action_type,
            action_params={'n_input_tokens': n_input_tokens, 'n_output_tokens': n_output_tokens}
        )

    async def execute_user_task(self, user_id: int, task: asyncio.Task, update: Update) -> None:
        """Выполняет задачу пользователя с обработкой отмены."""
        user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Приостановлено", parse_mode=ParseMode.HTML)
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]