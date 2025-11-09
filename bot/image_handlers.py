import io
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

import aiohttp
import telegram
from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

import openai_utils
from base_handler import BaseHandler

# Настройка логирования
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
            image_urls = await self._generate_images(user_id, prompt)
            await self._send_generated_images(update, context, prompt, image_urls, placeholder_message)

        except Exception as e:
            await self._handle_image_generation_error(update, e)

    async def _generate_images(self, user_id: int, prompt: str) -> List[str]:
        """Генерирует изображения через OpenAI."""
        user_preferences = self.db.get_user_attribute(user_id, "image_preferences") or {}

        model = user_preferences.get("model", "dalle-2")
        n_images = user_preferences.get("n_images", 3)
        resolution = user_preferences.get("resolution", "1024x1024")

        image_urls = await openai_utils.generate_images(
            prompt=prompt,
            model=model,
            n_images=n_images,
            size=resolution
        )

        self._update_image_usage_stats(user_id, n_images)
        return image_urls

    def _update_image_usage_stats(self, user_id: int, n_images: int) -> None:
        """Обновляет статистику использования изображений."""
        current_count = self.db.get_user_attribute(user_id, "n_generated_images") or 0
        self.db.set_user_attribute(user_id, "n_generated_images", current_count + n_images)

    async def _send_generated_images(self, update: Update, context: CallbackContext, prompt: str,
                                     image_urls: List[str], placeholder_message: telegram.Message) -> None:
        """Отправляет сгенерированные изображения."""
        chat_id = placeholder_message.chat_id
        message_id = placeholder_message.message_id

        # Предварительное сообщение
        pre_message = f"Нарисовали 🎨:\n\n  <i>{prompt or ''}</i>  \n\nПодождите немного, изображение почти готово!"
        await context.bot.edit_message_text(
            pre_message,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode=ParseMode.HTML
        )

        # Отправка изображений
        for image_url in image_urls:
            await update.message.chat.send_action(action="upload_photo")
            await self._upload_image_from_url(context.bot, chat_id, image_url)

        # Финальное сообщение
        post_message = f"Нарисовали 🎨:\n\n  <i>{prompt or ''}</i>  \n\nКак вам??"
        await context.bot.edit_message_text(
            post_message,
            chat_id=chat_id,
            message_id=message_id,
            parse_mode=ParseMode.HTML
        )

    async def _upload_image_from_url(self, bot: telegram.Bot, chat_id: int, image_url: str) -> None:
        """Загружает изображение по URL и отправляет его асинхронно."""
        async with aiohttp.ClientSession() as session:
            async with session.get(image_url) as response:
                if response.status == 200:
                    image_data = await response.read()
                    image_buffer = io.BytesIO(image_data)
                    image_buffer.name = "image.jpg"
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=InputFile(image_buffer, "image.jpg")
                    )
                else:
                    logger.error(f"Failed to download image. Status: {response.status}")

    async def _handle_image_generation_error(self, update: Update, error: Exception) -> None:
        """Обрабатывает ошибки генерации изображений."""
        error_msg = str(error)

        if error_msg.startswith("Your request was rejected as a result of our safety system"):
            error_text = "🥲 Your request <b>doesn't comply</b> with OpenAI's usage policies.\nWhat did you write there, huh??"
        else:
            error_text = f"⚠️ There was an issue with your request. Please try again.\n\n<b>Reason</b>: {error_msg}"

        await update.message.reply_text(error_text, parse_mode=ParseMode.HTML)