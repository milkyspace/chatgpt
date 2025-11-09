import bot.base_handler as BaseHandler

import logging
import asyncio
import io
from typing import Optional, Dict, Any, List
import requests
import telegram
from telegram import (Update, InputFile)
from telegram.ext import (CallbackContext)
from telegram.constants import ParseMode
from bot.error_handler import ErrorHandler
from bot.ai_response_handler import AIResponseHandler

import database
import openai_utils

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class ImageHandlers(BaseHandler):
    """Класс для обработки генерации изображений."""

    async def generate_image_handle(self, update: Update, context: CallbackContext,
                                    message: Optional[str] = None) -> None:
        """Обрабатывает генерацию изображений."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if await self.is_previous_message_not_answered_yet(update, context):
            return

        if not await self.subscription_preprocessor(update, context):
            return

        await update.message.chat.send_action(action="upload_photo")

        prompt = message or update.message.text
        placeholder_message = await update.message.reply_text("<i>Рисуем...</i>", parse_mode=ParseMode.HTML)

        try:
            image_urls = await self._generate_images(user_id, prompt)
            await self._send_generated_images(update, context, prompt, image_urls, placeholder_message)

        except Exception as e:
            await ErrorHandler.handle_ai_error(update, e)

    async def _generate_images(self, user_id: int, prompt: str) -> List[str]:
        """Генерирует изображения через OpenAI."""
        user_preferences = self.db.get_user_attribute(user_id, "image_preferences")
        model = user_preferences.get("model", "dalle-2")
        n_images = user_preferences.get("n_images", 3)
        resolution = user_preferences.get("resolution", "1024x1024")

        image_urls = await openai_utils.generate_images(
            prompt=prompt, model=model, n_images=n_images, size=resolution
        )

        self._update_image_usage_stats(user_id, user_preferences, n_images)
        return image_urls

    def _update_image_usage_stats(self, user_id: int, user_preferences: Dict[str, Any], n_images: int) -> None:
        """Обновляет статистику использования изображений."""
        self.db.set_user_attribute(
            user_id, "n_generated_images",
            n_images + self.db.get_user_attribute(user_id, "n_generated_images")
        )

        action_type = user_preferences.get("model", "dalle-3")
        action_params = {
            "model": user_preferences.get("model", "dalle-2"),
            "quality": user_preferences.get("quality", "standard"),
            "resolution": user_preferences.get("resolution", "1024x1024"),
            "n_images": n_images
        }

        self.db.deduct_cost_for_action(
            user_id=user_id,
            action_type=action_type,
            action_params=action_params
        )

    async def _send_generated_images(self, update: Update, context: CallbackContext, prompt: str,
                                     image_urls: List[str], placeholder_message: telegram.Message) -> None:
        """Отправляет сгенерированные изображения."""
        pre_generation_message = f"Нарисовали 🎨:\n\n  <i>{prompt or ''}</i>  \n\n Подождите немного, изображение почти готово!"
        await AIResponseHandler.edit_ai_response(
            context, placeholder_message, pre_generation_message
        )

        for image_url in image_urls:
            await update.message.chat.send_action(action="upload_photo")
            await self._upload_image_from_url(context.bot, update.message.chat_id, image_url)

        post_generation_message = f"Нарисовали 🎨:\n\n  <i>{prompt or ''}</i>  \n\n Как вам??"
        await AIResponseHandler.edit_ai_response(
            context, placeholder_message, post_generation_message
        )

    async def _upload_image_from_url(self, bot: telegram.Bot, chat_id: int, image_url: str) -> None:
        """Загружает изображение по URL и отправляет его."""
        response = requests.get(image_url, stream=True)
        if response.status_code == 200:
            image_buffer = io.BytesIO(response.content)
            image_buffer.name = "image.jpg"
            await bot.send_photo(chat_id=chat_id, photo=InputFile(image_buffer, "image.jpg"))