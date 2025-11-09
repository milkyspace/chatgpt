import io
import logging
from datetime import datetime
from typing import Optional, List

import asyncio
import aiohttp
import telegram
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

import openai_utils
from base_handler import BaseHandler

logger = logging.getLogger(__name__)


class ImageHandlers(BaseHandler):
    """Класс для обработки генерации изображений."""

    async def generate_image_handle(self, update: Update, context: CallbackContext,
                                    message: Optional[str] = None) -> None:
        """Обрабатывает генерацию изображений."""
        user = update.message.from_user

        await self.register_user_if_not_exists(update, context, user)
        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not await self.subscription_preprocessor(update, context):
            return

        await update.message.chat.send_action(action="upload_photo")

        prompt = message or update.message.text

        placeholder_message = await update.message.reply_text(
            "<i>Рисуем...</i>",
            parse_mode=ParseMode.HTML
        )

        try:
            # Этот вызов должен быть с await
            image_urls = await self._generate_images(user_id, prompt)

            await self._send_generated_images(
                update,
                context,
                prompt,
                image_urls,
                placeholder_message
            )

        except Exception as e:
            await self._handle_image_generation_error(update, e)

    async def _generate_images(self, user_id: int, prompt: str) -> List[str]:
        """Генерация изображений через OpenAI API."""
        prefs = self.db.get_user_attribute(user_id, "image_preferences") or {}

        model = prefs.get("model", "dall-e-3")
        resolution = prefs.get("resolution", "1024x1024")

        try:
            # Убедитесь, что здесь есть await
            image_urls = await openai_utils.generate_images(
                prompt=prompt,
                model=model,
                size=resolution
            )
            return image_urls

        except Exception as e:
            # Fallback для DALL-E 3 → DALL-E 2
            if any(keyword in str(e).lower() for keyword in ["rejected", "safety", "billing", "quota"]):
                logger.warning("FALLBACK dalle-3 → dalle-2")
                try:
                    # И здесь тоже должен быть await
                    image_urls = await openai_utils.generate_images(
                        prompt=prompt,
                        model="dall-e-2",
                        size="1024x1024"
                    )
                    return image_urls
                except Exception:
                    raise e
            else:
                raise

    def _update_image_usage_stats(self, user_id: int, n_images: int) -> None:
        count = self.db.get_user_attribute(user_id, "n_generated_images") or 0
        self.db.set_user_attribute(user_id, "n_generated_images", count + n_images)

    async def _send_generated_images(self, update: Update, context: CallbackContext,
                                     prompt: str, image_urls: List[str],
                                     placeholder_message: telegram.Message) -> None:
        chat_id = placeholder_message.chat_id
        m_id = placeholder_message.message_id

        try:
            await context.bot.edit_message_text(
                f"🖼 Генерируем...\n\n<i>{prompt}</i>",
                chat_id=chat_id,
                message_id=m_id,
                parse_mode=ParseMode.HTML
            )
        except telegram.error.BadRequest:
            pass

        async with aiohttp.ClientSession() as session:
            for url in image_urls:
                await update.message.chat.send_action(action="upload_photo")
                await self._send_one_image(session, context.bot, chat_id, url)

        await context.bot.edit_message_text(
            f"Готово 🎨\n\n<i>{prompt}</i>",
            chat_id=chat_id,
            message_id=m_id,
            parse_mode=ParseMode.HTML
        )

    async def _send_one_image(self, session: aiohttp.ClientSession, bot: telegram.Bot, chat_id: int, url: str):
        """Скачивание и отправка изображения."""
        async with session.get(url) as resp:
            if resp.status != 200:
                logger.error(f"Failed download {url} — {resp.status}")
                return

            img = io.BytesIO(await resp.read())
            img.name = "image.jpg"

            await bot.send_photo(chat_id=chat_id, photo=InputFile(img))

    async def _handle_image_generation_error(self, update: Update, error: Exception) -> None:
        msg = str(error)

        if msg.startswith("Your request was rejected"):
            text = (
                "🚫 <b>Запрос отклонён политиками OpenAI.</b>\n"
                "Попробуй сформулировать мягче 🫣"
            )
        else:
            text = (
                "⚠️ Ошибка при генерации изображения.\n"
                f"<b>Причина:</b> {msg}"
            )

        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def process_image_message_handle(self, update: Update, context: CallbackContext,
                                           message: Optional[str] = None) -> None:
        """Обрабатывает сообщения с изображениями для редактирования/улучшения."""
        user = update.message.from_user

        await self.register_user_if_not_exists(update, context, user)
        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not await self.subscription_preprocessor(update, context):
            return

        # Проверяем, есть ли фото в сообщении
        if not update.message.photo:
            await update.message.reply_text(
                "❌ Пожалуйста, отправьте изображение для обработки.",
                parse_mode=ParseMode.HTML
            )
            return

        await update.message.chat.send_action(action="upload_photo")

        placeholder_message = await update.message.reply_text(
            "<i>Обрабатываю изображение...</i>",
            parse_mode=ParseMode.HTML
        )

        try:
            # Получаем изображение
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)
            img_bytes = await file.download_as_bytearray()

            # Конвертируем bytearray в bytes для OpenAI API
            img_bytes = bytes(img_bytes)

            # Получаем промпт (текст сообщения или переданный параметр)
            prompt = message or update.message.caption or "Улучши это изображение"

            # Генерируем новое изображение на основе загруженного
            result_url = await openai_utils.generate_image_with_input(prompt, img_bytes)

            # Отправляем результат
            await self._send_edited_image(context, placeholder_message, result_url, prompt)

            # Обновляем статистику использования
            self._update_image_usage_stats(user_id, 1)

        except Exception as e:
            await self._handle_image_generation_error(update, e)

    async def _handle_image_generation_error(self, update: Update, error: Exception) -> None:
        """Обрабатывает ошибки генерации изображения с безопасным HTML."""
        error_msg = str(error)

        # Безопасное форматирование сообщения об ошибке
        if "rejected" in error_msg.lower() or "safety" in error_msg.lower():
            text = (
                "🚫 <b>Запрос отклонён политиками OpenAI.</b>\n"
                "Попробуй сформулировать мягче 🫣"
            )
        elif "billing" in error_msg.lower() or "quota" in error_msg.lower():
            text = (
                "💳 <b>Проблема с биллингом OpenAI.</b>\n"
                "Пожалуйста, попробуйте позже или свяжитесь с поддержкой."
            )
        else:
            # Экранируем специальные символы для безопасного отображения
            safe_error_msg = error_msg.replace('<', '&lt;').replace('>', '&gt;')
            text = (
                "⚠️ <b>Ошибка при генерации изображения.</b>\n"
                f"<b>Причина:</b> {safe_error_msg}"
            )

        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _send_edited_image(self, context: CallbackContext, placeholder_message: telegram.Message,
                                 image_url: str, prompt: str) -> None:
        """Отправляет отредактированное изображение."""
        chat_id = placeholder_message.chat_id
        message_id = placeholder_message.message_id

        try:
            await context.bot.edit_message_text(
                f"🎨 Обрабатываю...\n\n<i>{prompt}</i>",
                chat_id=chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML
            )
        except telegram.error.BadRequest:
            pass

        # Скачиваем и отправляем изображение
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as resp:
                if resp.status == 200:
                    img = io.BytesIO(await resp.read())
                    img.name = "edited_image.jpg"

                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=InputFile(img),
                        caption=f"Готово 🎨\n\n<i>{prompt}</i>",
                        parse_mode=ParseMode.HTML
                    )

        # Удаляем placeholder сообщение
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)