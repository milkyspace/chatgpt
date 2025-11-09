import asyncio
import logging
from datetime import datetime
from typing import Dict

import telegram
from telegram import (
    Update
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackContext
)

import config
import database
from base_handler import BaseHandler

# Настройка логирования
logger = logging.getLogger(__name__)

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}


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

    async def edit_message_with_retry(self, context: CallbackContext, placeholder_message: telegram.Message,
                                      answer: str, chat_mode: str) -> None:
        """Редактирует сообщение с повторными попытками при ошибках."""
        parse_mode = {
            "html": ParseMode.HTML,
            "markdown": ParseMode.MARKDOWN
        }[config.chat_modes[chat_mode]["parse_mode"]]

        try:
            await context.bot.edit_message_text(
                answer[:4096],
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=parse_mode,
                disable_web_page_preview=True
            )
        except telegram.error.BadRequest as e:
            if not str(e).startswith("Message is not modified"):
                await context.bot.edit_message_text(
                    answer[:4096],
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    disable_web_page_preview=True
                )

    async def handle_message_error(self, update: Update, error: Exception) -> None:
        """Обрабатывает ошибки при обработке сообщений."""
        try:
            # Логируем полную информацию об ошибке
            logger.error(f"Error during message completion: {error}", exc_info=True)

            # Формируем понятное сообщение для пользователя
            if hasattr(error, '__class__') and error.__class__.__name__ != 'int':
                error_text = f"⚠️ Произошла ошибка при обработке сообщения. Пожалуйста, попробуйте еще раз."
            else:
                error_text = f"⚠️ Произошла ошибка (код: {error}). Пожалуйста, попробуйте еще раз."

            await update.message.reply_text(error_text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error in error handler: {e}")
            # Резервное сообщение
            try:
                await update.message.reply_text("⚠️ Произошла непредвиденная ошибка.")
            except:
                pass

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
