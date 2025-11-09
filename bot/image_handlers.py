import asyncio
import io
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

import requests
import telegram
from telegram import (
    Update, InputFile
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackContext
)

import database
import openai_utils
from base_handler import BaseHandler

# Настройка логирования
logger = logging.getLogger(__name__)

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Константы сообщений
HELP_MESSAGE = """<b>Команды:</b>
/new – Начать новый диалог 🆕
/retry – Перегенерировать предыдущий запрос 🔁
/mode – Выбрать режим
/subscription – Управление подписками 🔔
/my_payments – Мои платежи 📋
/help – Помощь ❓

🎤 Вы можете отправлять <b>голосовые сообщения</b> вместо текста

<blockquote>
1. Чат помнит контекст и предыдущие сообщения 10 минут. Чтобы начать заново — /new
2. «Ассистент» — режим по умолчанию. Попробуйте другие режимы: /mode
</blockquote>
"""

HELP_GROUP_CHAT_MESSAGE = """Вы можете добавить бота в любой <b>групповой чат</b> чтобы помогать и развлекать его участников!

Инструкции:
1. Добавьте бота в групповой чат
2. Сделайте его <b>администратором</b>, чтобы он мог видеть сообщения
3. Вы великолепны!

Чтобы получить ответ от бота в чате – @ <b>упомяните</b> его или <b>ответьте</b> на его сообщение.
Например: "{bot_username} напиши стихотворение о Telegram"
"""


class ImageHandlers(BaseHandler):
    """Класс для обработки генерации изображений."""

    async def generate_image_handle(self, update: Update, context: CallbackContext,
                                    message: Optional[str] = None) -> None:
        """Обрабатывает генерацию изображений."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not await self.subscription_preprocessor(update, context):
            return

        await update.message.chat.send_action(action="upload_photo")

        prompt = message or update.message.text
        placeholder_message = await update.message.reply_text("<i>Рисуем...</i>", parse_mode=ParseMode.HTML)

        try:
            image_urls = await self._generate_images(user_id, prompt)
            await self._send_generated_images(update, context, prompt, image_urls, placeholder_message)

        except Exception as e:
            await self._handle_image_generation_error(update, e)
        except Exception as e:
            await self._handle_image_generation_error(update, e, is_unexpected=True)

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
        await context.bot.edit_message_text(
            pre_generation_message,
            chat_id=placeholder_message.chat_id,
            message_id=placeholder_message.message_id,
            parse_mode=ParseMode.HTML
        )

        for image_url in image_urls:
            await update.message.chat.send_action(action="upload_photo")
            await self._upload_image_from_url(context.bot, update.message.chat_id, image_url)

        post_generation_message = f"Нарисовали 🎨:\n\n  <i>{prompt or ''}</i>  \n\n Как вам??"
        await context.bot.edit_message_text(
            post_generation_message,
            chat_id=placeholder_message.chat_id,
            message_id=placeholder_message.message_id,
            parse_mode=ParseMode.HTML
        )

    async def _upload_image_from_url(self, bot: telegram.Bot, chat_id: int, image_url: str) -> None:
        """Загружает изображение по URL и отправляет его."""
        response = requests.get(image_url, stream=True)
        if response.status_code == 200:
            image_buffer = io.BytesIO(response.content)
            image_buffer.name = "image.jpg"
            await bot.send_photo(chat_id=chat_id, photo=InputFile(image_buffer, "image.jpg"))

    async def _handle_image_generation_error(self, update: Update, error: Exception,
                                             is_unexpected: bool = False) -> None:
        """Обрабатывает ошибки генерации изображений."""
        if is_unexpected:
            error_text = f"⚠️ An unexpected error occurred. Please try again. \n\n<b>Reason</b>: {str(error)}"
        else:
            if str(error).startswith("Your request was rejected as a result of our safety system"):
                error_text = "🥲 Your request <b>doesn't comply</b> with OpenAI's usage policies.\nWhat did you write there, huh??"
            else:
                error_text = f"⚠️ There was an issue with your request. Please try again.\n\n<b>Reason</b>: {str(error)}"

        await update.message.reply_text(error_text, parse_mode=ParseMode.HTML)