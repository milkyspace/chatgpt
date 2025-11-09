import requests
from telegram import (InputFile)
import io
from typing import Optional
import openai_utils
import telegram

import logging
import asyncio

from typing import Dict
from telegram import (Update)
from telegram.ext import (CallbackContext)
from telegram.constants import ParseMode

import database
import bot.base_handler as BaseHandler
import bot.ai_response_handler as AIResponseHandler

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class PhotoEditorMixin(BaseHandler):
    """Миксин для обработки фоторедактора."""

    async def photo_editor_handle(self, update: Update, context: CallbackContext,
                                  message: Optional[str] = None) -> None:
        """Обрабатывает запросы в режиме фоторедактора."""
        logger.info(
            f"Photo editor handle: photo={bool(update.message.photo)}, caption='{update.message.caption}', text='{update.message.text}'")

        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if await self.is_previous_message_not_answered_yet(update, context):
            return

        if not await self.subscription_preprocessor(update, context):
            return

        edit_description = self._get_edit_description(update, message)

        if update.message.photo:
            await self._handle_photo_for_editing(update, context, edit_description)
        elif context.user_data.get('waiting_for_edit_description') and edit_description:
            await self._perform_photo_editing(update, context, edit_description)
        else:
            await self._request_photo_for_editing(update, context, edit_description)

    def _get_edit_description(self, update: Update, message: Optional[str]) -> Optional[str]:
        """Получает описание редактирования из различных источников."""
        if update.message.caption:
            return update.message.caption
        elif message:
            return message
        elif update.message.text and not update.message.photo:
            return update.message.text
        return None

    async def _handle_photo_for_editing(self, update: Update, context: CallbackContext,
                                        edit_description: Optional[str] = None) -> None:
        """Обрабатывает фото для редактирования."""
        user_id = update.message.from_user.id

        # ✅ Получаем файл
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)

        # ✅ Скачиваем в память
        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)
        buf.seek(0)

        # ✅ Сохраняем сразу байты без конвертации
        context.user_data['photo_to_edit'] = buf.getvalue()

        # ✅ Если есть описание — редактируем фото
        if edit_description:
            await self._perform_photo_editing(update, context, edit_description)
            return

        # ✅ Иначе спрашиваем описание
        context.user_data['waiting_for_edit_description'] = True

        await update.message.reply_text(
            "📸 <b>Фото получено!</b>\n\n"
            "Теперь опишите что нужно изменить на фото:\n"
            "• Что добавить\n• Что убрать\n• Какие изменения сделать\n\n"
            "<i>Пример: \"Добавь кота на диван\" или \"Поменяй цвет стены на синий\"</i>",
            parse_mode=ParseMode.HTML
        )

    async def _request_photo_for_editing(self, update: Update, context: CallbackContext,
                                         message: Optional[str] = None) -> None:
        """Запрашивает фото для редактирования."""
        if message and context.user_data.get('waiting_for_edit_description'):
            context.user_data['waiting_for_edit_description'] = False
            await self._perform_photo_editing(update, context, message)
        else:
            await update.message.reply_text(
                "🎨 <b>Режим фоторедактора</b>\n\n"
                "Для редактирования фото:\n"
                "1. 📸 <b>Отправьте фото</b> которое нужно изменить\n"
                "2. ✍️ <b>Опишите</b> что нужно добавить/изменить\n\n"
                "Я могу:\n"
                "• Добавлять объекты и людей\n"
                "• Убирать ненужные элементы\n"
                "• Менять цвета и фон\n"
                "• Улучшать качество\n\n"
                "<i>Просто отправьте фото чтобы начать!</i>",
                parse_mode=ParseMode.HTML
            )

    async def _perform_photo_editing(self, update: Update, context: CallbackContext,
                                     edit_description: str) -> None:
        """Выполняет редактирование фото через DALL-E."""
        user_id = update.message.from_user.id

        if 'photo_to_edit' not in context.user_data:
            await update.message.reply_text(
                "❌ Сначала отправьте фото для редактирования!",
                parse_mode=ParseMode.HTML
            )
            return

        if not edit_description or not edit_description.strip():
            await update.message.reply_text(
                "❌ Пожалуйста, опишите что нужно изменить на фото!",
                parse_mode=ParseMode.HTML
            )
            return

        placeholder_message = await update.message.reply_text(
            "🎨 <i>Редактирую фото... Это может занять до 2х минут</i>",
            parse_mode=ParseMode.HTML
        )

        try:
            image_bytes = context.user_data['photo_to_edit']
            image_buf = io.BytesIO(image_bytes)

            edited_image_url = await openai_utils.generate_photo(
                image=image_buf,
                prompt=edit_description
            )

            if edited_image_url:
                logger.info("Photo editing successful")
                await self._send_edited_photo(update, context, edited_image_url,
                                              edit_description, placeholder_message)
                self._update_photo_editor_usage(user_id)
                self._cleanup_photo_context(context)
            else:
                logger.error("Photo editing returned no URL")
                await AIResponseHandler.edit_ai_response(
                    context, placeholder_message,
                    "❌ Не удалось отредактировать фото. Попробуйте другое описание."
                )

        except Exception as e:
            logger.error(f"Error in photo editing: {e}")
            error_message = self._get_user_friendly_error(e)

            await AIResponseHandler.edit_ai_response(
                context, placeholder_message, error_message
            )

    def _get_user_friendly_error(self, error: Exception) -> str:
        """Возвращает понятное пользователю сообщение об ошибке."""
        error_str = str(error).lower()

        error_messages = {
            "unsupported mimetype": "❌ Формат изображения не поддерживается. Попробуйте другое фото (JPEG, PNG).",
            "invalid image": "❌ Не удалось обработать изображение. Попробуйте другое фото.",
            "safety system": "❌ Запрос не соответствует политикам безопасности OpenAI. Попробуйте другое описание.",
            "billing": "❌ Проблемы с биллингом OpenAI. Обратитесь к администратору.",
            "size": "❌ Изображение слишком большое. Попробуйте фото меньшего размера.",
            "mask": "❌ Проблема с обработкой изображения. Попробуйте другое фото.",
            "edit": "❌ Не удалось отредактировать фото. Попробуйте другое описание или изображение."
        }

        for key, message in error_messages.items():
            if key in error_str:
                return message

        # Для ошибок OpenAI API
        if hasattr(error, 'code'):
            if error.code == 'billing_hard_limit_reached':
                return "❌ Лимит расходов OpenAI исчерпан. Обратитесь к администратору."

        return "❌ Произошла ошибка при редактировании фото. Пожалуйста, попробуйте еще раз."

    def _cleanup_photo_context(self, context: CallbackContext) -> None:
        """Очищает временные данные фото из контекста."""
        keys_to_remove = ['photo_to_edit', 'waiting_for_edit_description']
        for key in keys_to_remove:
            if key in context.user_data:
                del context.user_data[key]

    async def _send_edited_photo(self, update: Update, context: CallbackContext,
                                 image_url: str, edit_description: str,
                                 placeholder_message: telegram.Message) -> None:
        """Отправляет отредактированное фото."""
        try:
            response = requests.get(image_url, stream=True)
            if response.status_code == 200:
                image_buffer = io.BytesIO(response.content)
                image_buffer.name = "edited_image.png"

                await AIResponseHandler.edit_ai_response(
                    context, placeholder_message,
                    f"✅ <b>Фото отредактировано!</b>\n\n"
                    f"<i>Запрос:</i> {edit_description}\n\n"
                    f"Как вам результат? 🎨"
                )

                await update.message.chat.send_photo(
                    photo=InputFile(image_buffer, "edited_image.png"),
                    caption=f"🎨 Отредактировано: {edit_description}"
                )
            else:
                await AIResponseHandler.edit_ai_response(
                    context, placeholder_message,
                    "❌ Не удалось загрузить отредактированное изображение."
                )

        except Exception as e:
            logger.error(f"Error sending edited photo: {e}")
            await AIResponseHandler.edit_ai_response(
                context, placeholder_message,
                "❌ Ошибка при отправке отредактированного фото."
            )

    def _update_photo_editor_usage(self, user_id: int) -> None:
        """Обновляет статистику использования фоторедактора."""
        current_usage = self.db.get_user_attribute(user_id, "n_photo_edits") or 0
        self.db.set_user_attribute(user_id, "n_photo_edits", current_usage + 1)

        self.db.deduct_cost_for_action(
            user_id=user_id,
            action_type='photo_edit',
            action_params={'n_edits': 1}
        )