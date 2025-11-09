import io
import logging
from datetime import datetime
from typing import Optional, List

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
        """Генерация изображений через новый OpenAI API."""
        prefs = self.db.get_user_attribute(user_id, "image_preferences") or {}

        model = prefs.get("model", "dall-e-3")  # Можно переопределить
        n_images = prefs.get("n_images", 2)
        resolution = prefs.get("resolution", "1024x1024")

        try:
            image_urls = await openai_utils.generate_images(
                prompt=prompt,
                model=model,
                n_images=n_images,
                size=resolution
            )
        except Exception as e:
            # автоматический fallback
            if "rejected" in str(e).lower() or "safety" in str(e).lower():
                logger.warning("FALLBACK dalle-3 → gpt-image-1")
                image_urls = await openai_utils.generate_images(
                    prompt=prompt,
                    model="gpt-image-1",
                    n_images=n_images,
                    size=resolution
                )
            else:
                raise

        self._update_image_usage_stats(user_id, n_images)
        return image_urls

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