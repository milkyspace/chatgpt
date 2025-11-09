import logging
from datetime import datetime
from typing import Optional, List

import io
from PIL import Image

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
            # Получаем изображение (самое качественное - последнее в списке)
            photo = update.message.photo[-1]
            file = await context.bot.get_file(photo.file_id)

            # Скачиваем изображение в память
            img_buffer = io.BytesIO()
            await file.download_to_memory(img_buffer)
            img_buffer.seek(0)

            # Получаем промпт (текст сообщения или переданный параметр)
            prompt = message or update.message.caption or "Улучши это изображение"

            # Пробуем разные подходы
            result_url = await self._try_image_generation_methods(prompt, img_buffer, placeholder_message, context)

            # Отправляем результат
            await self._send_edited_image(context, placeholder_message, result_url, prompt)

            # Обновляем статистику использования
            self._update_image_usage_stats(user_id, 1)

        except Exception as e:
            await self._handle_image_generation_error(update, e)

    async def _try_image_generation_methods(self, prompt: str, img_buffer: io.BytesIO,
                                            placeholder_message: telegram.Message,
                                            context: CallbackContext) -> str:
        """Пробует разные методы генерации изображений."""

        # Метод 1: Прямое редактирование с DALL-E 2
        try:
            await context.bot.edit_message_text(
                "🔄 Редактирую изображение...",
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=ParseMode.HTML
            )

            # Конвертируем изображение
            with Image.open(img_buffer) as img:
                # Пробуем разные форматы
                if img.mode not in ['RGBA', 'LA', 'L']:
                    img = img.convert('RGBA')

                # Убедимся, что изображение квадратное (требование DALL-E)
                if img.size[0] != img.size[1]:
                    size = min(img.size[0], img.size[1])
                    img = img.resize((size, size), Image.Resampling.LANCZOS)

                png_buffer = io.BytesIO()
                img.save(png_buffer, format='PNG', optimize=True)
                png_buffer.seek(0)

            return await openai_utils.generate_image_with_input(prompt, png_buffer.getvalue())

        except Exception as e:
            logger.warning(f"Method 1 (DALL-E 2 editing) failed: {e}")

        # Метод 2: Генерация с DALL-E 3 по описанию
        try:
            await context.bot.edit_message_text(
                "🎨 Генерирую новое изображение...",
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=ParseMode.HTML
            )

            image_urls = await openai_utils.generate_images(
                prompt=prompt,
                model="dall-e-3",
                n_images=1,
                size="1024x1024"
            )
            return image_urls[0]

        except Exception as e:
            logger.warning(f"Method 2 (DALL-E 3 generation) failed: {e}")

        # Метод 3: Генерация с DALL-E 2 по описанию
        try:
            await context.bot.edit_message_text(
                "🎨 Генерирую изображение...",
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=ParseMode.HTML
            )

            image_urls = await openai_utils.generate_images(
                prompt=prompt,
                model="dall-e-2",
                n_images=1,
                size="1024x1024"
            )
            return image_urls[0]

        except Exception as e:
            logger.warning(f"Method 3 (DALL-E 2 generation) failed: {e}")
            raise Exception("Все методы генерации изображений не сработали. Пожалуйста, попробуйте другой промпт.")

    async def _handle_image_generation_error(self, update: Update, error: Exception) -> None:
        """Обрабатывает ошибки генерации изображения с полезными подсказками."""
        error_msg = str(error)

        # Более информативные сообщения об ошибках
        if "500" in error_msg or "server_error" in error_msg:
            text = (
                "🔧 <b>Временная проблема с сервером OpenAI</b>\n\n"
                "Это внутренняя ошибка сервера. Пожалуйста:\n"
                "• Попробуйте через несколько минут\n"
                "• Используйте другой промпт\n"
                "• Отправьте другое изображение\n\n"
                "Если проблема повторяется, свяжитесь с поддержкой."
            )
        elif "rejected" in error_msg.lower() or "safety" in error_msg.lower():
            text = (
                "🚫 <b>Запрос отклонён политиками безопасности</b>\n\n"
                "Попробуйте:\n"
                "• Сформулировать промпт мягче\n"
                "• Использовать более нейтральное описание\n"
                "• Выбрать другое изображение"
            )
        elif "billing" in error_msg.lower() or "quota" in error_msg.lower():
            text = (
                "💳 <b>Проблема с биллингом или лимитами</b>\n\n"
                "Проверьте:\n"
                "• Баланс аккаунта OpenAI\n"
                "• Лимиты использования API\n"
                "• Активность подписки"
            )
        elif "invalid_image" in error_msg.lower():
            text = (
                "🖼️ <b>Проблема с форматом изображения</b>\n\n"
                "Попробуйте:\n"
                "• Отправить изображение в формате PNG\n"
                "• Убедиться, что размер меньше 4MB\n"
                "• Использовать квадратное изображение"
            )
        else:
            # Безопасное отображение ошибки
            safe_error_msg = error_msg.replace('<', '&lt;').replace('>', '&gt;')[:200]
            text = (
                "⚠️ <b>Ошибка при генерации изображения</b>\n\n"
                f"<code>{safe_error_msg}</code>\n\n"
                "Попробуйте:\n"
                "• Изменить описание изображения\n"
                "• Использовать более простой промпт\n"
                "• Попробовать позже"
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