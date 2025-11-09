"""
Класс для обработки сообщений с устранением дублирования.
"""

import logging
import asyncio
import io
import base64
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import emoji
import telegram
from telegram import Update
from telegram.ext import CallbackContext
from telegram.constants import ParseMode

import config
import openai_utils
from .base_handler import BaseHandler

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
        error_text = f"Something went wrong during completion. Reason: {error}"
        logger.error(error_text)
        await update.message.reply_text(error_text)

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

    async def message_handle(self, update: Update, context: CallbackContext,
                             message: Optional[str] = None, use_new_dialog_timeout: bool = True) -> None:
        """Обрабатывает текстовые сообщения."""
        if not await self.is_bot_mentioned(update, context):
            return

        if update.edited_message is not None:
            await self.edited_message_handle(update, context)
            return

        # Проверяем, не является ли сообщение кнопкой главного меню
        if await self._is_main_menu_button(update.message.text):
            await self.handle_main_menu_buttons(update, context)
            return

        processed_message = self._process_message_text(update, context, message)
        await self.register_user_if_not_exists(update, context, update.message.from_user)

        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id

        if not await self.subscription_preprocessor(update, context):
            return

        # Определяем тип обработки сообщения
        chat_mode = self.db.get_user_attribute(user_id, "current_chat_mode")

        # Обработка специальных режимов
        if chat_mode == "photo_editor":
            await self.photo_editor_handle(update, context, message=message)
            return
        elif chat_mode == "artist":
            await self.generate_image_handle(update, context, message=message)
            return
        elif chat_mode == "stenographer":
            await self.voice_message_handle(update, context, message=message)
            return

        await self._handle_text_message(update, context, processed_message, use_new_dialog_timeout)

    async def _handle_text_message(self, update: Update, context: CallbackContext,
                                   message: str, use_new_dialog_timeout: bool) -> None:
        """Обрабатывает текстовое сообщение."""
        user_id = update.message.from_user.id
        current_model = self.db.get_user_attribute(user_id, "current_model")

        # Проверяем необходимость обработки изображений
        if (current_model == "gpt-4-vision-preview" or
                (update.message.photo and len(update.message.photo) > 0)):

            if current_model != "gpt-4-vision-preview":
                current_model = "gpt-4-vision-preview"
                self.db.set_user_attribute(user_id, "current_model", "gpt-4-vision-preview")

            task = asyncio.create_task(
                self._vision_message_handle_fn(update, context, use_new_dialog_timeout)
            )
        else:
            task = asyncio.create_task(
                self._text_message_handle_fn(update, context, message, use_new_dialog_timeout)
            )

        await self.execute_user_task(user_id, task, update)

    async def _text_message_handle_fn(self, update: Update, context: CallbackContext,
                                      message: str, use_new_dialog_timeout: bool) -> None:
        """Обрабатывает текстовое сообщение (внутренняя функция)."""
        user_id = update.message.from_user.id
        chat_mode = self.db.get_user_attribute(user_id, "current_chat_mode")

        await self.prepare_dialog(user_id, use_new_dialog_timeout, chat_mode, update)

        if not message or len(message) == 0:
            await update.message.reply_text("🥲 You sent <b>empty message</b>. Please, try again!",
                                            parse_mode=ParseMode.HTML)
            return

        try:
            async with user_semaphores[user_id]:
                placeholder_message = await update.message.reply_text("<i>Думаю...</i>", parse_mode=ParseMode.HTML)
                await update.message.chat.send_action(action="typing")

                dialog_messages = self.db.get_dialog_messages(user_id, dialog_id=None)
                parse_mode = {
                    "html": ParseMode.HTML,
                    "markdown": ParseMode.MARKDOWN
                }[config.chat_modes[chat_mode]["parse_mode"]]

                current_model = self.db.get_user_attribute(user_id, "current_model")
                chatgpt_instance = openai_utils.ChatGPT(model=current_model)

                if config.enable_message_streaming:
                    await self._handle_streaming_response(
                        update, context, message, dialog_messages, chat_mode,
                        chatgpt_instance, placeholder_message, parse_mode, user_id
                    )
                else:
                    answer, n_input_tokens, n_output_tokens = await self._get_non_streaming_response(
                        chatgpt_instance, message, dialog_messages, chat_mode
                    )

                    await self.edit_message_with_retry(context, placeholder_message, answer, chat_mode)

                    new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": answer,
                                          "date": datetime.now()}
                    self.update_dialog_and_tokens(user_id, new_dialog_message, n_input_tokens, n_output_tokens)

        except Exception as e:
            await self.handle_message_error(update, e)

    async def _handle_streaming_response(self, update: Update, context: CallbackContext, message: str,
                                         dialog_messages: List[Dict], chat_mode: str,
                                         chatgpt_instance: openai_utils.ChatGPT,
                                         placeholder_message: telegram.Message,
                                         parse_mode: str, user_id: int) -> None:
        """Обрабатывает потоковый ответ от ChatGPT."""
        gen = chatgpt_instance.send_message_stream(message, dialog_messages=dialog_messages, chat_mode=chat_mode)

        full_answer = ""
        n_input_tokens, n_output_tokens = 0, 0
        prev_answer = ""
        last_update_time = datetime.now()

        async for gen_item in gen:
            status, answer, (chunk_n_input_tokens, chunk_n_output_tokens), n_first_dialog_messages_removed = gen_item

            full_answer = answer
            n_input_tokens, n_output_tokens = chunk_n_input_tokens, chunk_n_output_tokens

            current_time = datetime.now()
            time_diff = (current_time - last_update_time).total_seconds()

            should_update = (
                    time_diff > 0.5 or
                    abs(len(answer) - len(prev_answer)) > 50 or
                    status == "finished"
            )

            if should_update and answer.strip():
                try:
                    await context.bot.edit_message_text(
                        answer[:4096],
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                        parse_mode=parse_mode,
                        disable_web_page_preview=True
                    )
                    prev_answer = answer
                    last_update_time = current_time
                except telegram.error.BadRequest as e:
                    if not str(e).startswith("Message is not modified"):
                        try:
                            await context.bot.edit_message_text(
                                answer[:4096],
                                chat_id=placeholder_message.chat_id,
                                message_id=placeholder_message.message_id,
                                disable_web_page_preview=True
                            )
                            prev_answer = answer
                            last_update_time = current_time
                        except Exception:
                            pass

            await asyncio.sleep(0.01)

        new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": full_answer, "date": datetime.now()}
        self.update_dialog_and_tokens(user_id, new_dialog_message, n_input_tokens, n_output_tokens)

        if n_first_dialog_messages_removed > 0:
            if n_first_dialog_messages_removed == 1:
                text = "✍️ <i>Note:</i> Your current dialog is too long, so your <b>first message</b> was removed from the context.\n Send /new command to start new dialog"
            else:
                text = f"✍️ <i>Note:</i> Your current dialog is too long, so <b>{n_first_dialog_messages_removed} first messages</b> were removed from the context.\n Send /new command to start new dialog"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _get_non_streaming_response(self, chatgpt_instance: openai_utils.ChatGPT, message: str,
                                          dialog_messages: List[Dict], chat_mode: str) -> Tuple[str, int, int]:
        """Получает непотоковый ответ от ChatGPT."""
        answer, (n_input_tokens, n_output_tokens), _ = await chatgpt_instance.send_message(
            message, dialog_messages=dialog_messages, chat_mode=chat_mode
        )
        return answer, n_input_tokens, n_output_tokens

    async def _vision_message_handle_fn(self, update: Update, context: CallbackContext,
                                        use_new_dialog_timeout: bool = True) -> None:
        """Обрабатывает сообщения с изображениями для GPT-4 Vision."""
        logger.info('_vision_message_handle_fn')
        user_id = update.message.from_user.id
        current_model = self.db.get_user_attribute(user_id, "current_model")

        if current_model != "gpt-4-vision-preview":
            await update.message.reply_text(
                "🥲 Images processing is only available for the <b>GPT-4 Vision</b> model. Please change your settings in /settings",
                parse_mode=ParseMode.HTML,
            )
            return

        chat_mode = self.db.get_user_attribute(user_id, "current_chat_mode")

        await self.prepare_dialog(user_id, use_new_dialog_timeout, chat_mode, update)

        transcribed_text = ''
        buf = None

        # Обработка голосового сообщения
        if update.message.voice:
            voice = update.message.voice
            voice_file = await context.bot.get_file(voice.file_id)

            buf = io.BytesIO()
            await voice_file.download_to_memory(buf)
            buf.name = "voice.oga"
            buf.seek(0)

            transcribed_text = await openai_utils.transcribe_audio(buf)
            transcribed_text = transcribed_text.strip()

        # Обработка изображения
        if update.message.photo:
            photo = update.message.photo[-1]
            photo_file = await context.bot.get_file(photo.file_id)

            buf = io.BytesIO()
            await photo_file.download_to_memory(buf)
            buf.name = "image.jpg"
            buf.seek(0)

        n_input_tokens, n_output_tokens = 0, 0

        try:
            placeholder_message = await update.message.reply_text("<i>Думаю...</i>", parse_mode=ParseMode.HTML)
            message_text = update.message.caption or update.message.text or transcribed_text or ''

            await update.message.chat.send_action(action="typing")

            dialog_messages = self.db.get_dialog_messages(user_id, dialog_id=None)
            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[config.chat_modes[chat_mode]["parse_mode"]]

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)

            if config.enable_message_streaming:
                gen = chatgpt_instance.send_vision_message_stream(
                    message_text,
                    dialog_messages=dialog_messages,
                    image_buffer=buf,
                    chat_mode=chat_mode,
                )
            else:
                answer, (n_input_tokens, n_output_tokens), _ = await chatgpt_instance.send_vision_message(
                    message_text,
                    dialog_messages=dialog_messages,
                    image_buffer=buf,
                    chat_mode=chat_mode,
                )

                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), 0

                gen = fake_gen()

            prev_answer = ""
            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), _ = gen_item
                answer = answer[:4096]

                if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    await context.bot.edit_message_text(
                        answer,
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                        parse_mode=parse_mode,
                    )
                except telegram.error.BadRequest as e:
                    if not str(e).startswith("Message is not modified"):
                        await context.bot.edit_message_text(
                            answer,
                            chat_id=placeholder_message.chat_id,
                            message_id=placeholder_message.message_id,
                        )

                await asyncio.sleep(0.01)
                prev_answer = answer

            # Сохраняем диалог
            if buf is not None:
                base_image = base64.b64encode(buf.getvalue()).decode("utf-8")
                new_dialog_message = {
                    "user": [
                        {"type": "text", "text": message_text},
                        {"type": "image", "image": base_image}
                    ],
                    "bot": answer,
                    "date": datetime.now()
                }
            else:
                new_dialog_message = {"user": message_text, "bot": answer, "date": datetime.now()}

            self.update_dialog_and_tokens(user_id, new_dialog_message, n_input_tokens, n_output_tokens)

        except asyncio.CancelledError:
            self.db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            raise
        except Exception as e:
            error_text = f"Something went wrong during completion_1. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)

    async def voice_message_handle(self, update: Update, context: CallbackContext, message: Optional[str] = None) -> Optional[str]:
        """Обрабатывает голосовые сообщения."""
        if not await self.is_bot_mentioned(update, context):
            return

        await self.register_user_if_not_exists(update, context, update.message.from_user)
        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not await self.subscription_preprocessor(update, context):
            return

        chat_mode = self.db.get_user_attribute(user_id, "current_chat_mode")
        transcribed_text = await self._transcribe_voice_message(update, context, chat_mode)

        if chat_mode == "stenographer":
            return

        await self.message_handle(update, context, message=transcribed_text)
        return transcribed_text

    async def _transcribe_voice_message(self, update: Update, context: CallbackContext, chat_mode: str) -> str:
        """Транскрибирует голосовое сообщение."""
        placeholder_text = "⌨️: <i>Распознаю аудио...</i>" if chat_mode == "stenographer" else "🎤: <i>Распознаю аудио...</i>"
        placeholder_message = await update.message.reply_text(placeholder_text, parse_mode=ParseMode.HTML)

        voice = update.message.voice
        voice_file = await context.bot.get_file(voice.file_id)

        buf = io.BytesIO()
        await voice_file.download_to_memory(buf)
        buf.name = "voice.oga"
        buf.seek(0)

        transcribed_text = await openai_utils.transcribe_audio(buf)
        text = f"🎤: <i>{transcribed_text}</i>"

        user_id = update.message.from_user.id
        audio_duration_minutes = voice.duration / 60.0
        self.db.set_user_attribute(user_id, "n_transcribed_seconds",
                                   voice.duration + self.db.get_user_attribute(user_id, "n_transcribed_seconds"))
        self.db.deduct_cost_for_action(
            user_id=user_id,
            action_type='whisper',
            action_params={'audio_duration_minutes': audio_duration_minutes}
        )

        if chat_mode == "stenographer":
            transcription_message = f"Your transcription is in: \n\n<code>{transcribed_text}</code>"
            await context.bot.edit_message_text(
                transcription_message,
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.edit_message_text(
                text,
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=ParseMode.HTML
            )

        return transcribed_text

    async def edited_message_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает редактированные сообщения."""
        if update.edited_message.chat.type == "private":
            text = "🥲 Unfortunately, message <b>editing</b> is not supported"
            await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def _is_main_menu_button(self, text: str) -> bool:
        """Проверяет, является ли текст кнопкой главного меню."""
        main_menu_buttons = [
            emoji.emojize("Продлить подписку :money_bag:"),
            emoji.emojize("Выбрать режим :red_heart:"),
            emoji.emojize("Пригласить :woman_and_man_holding_hands:"),
            emoji.emojize("Помощь :heart_hands:"),
            emoji.emojize("Админ-панель :smiling_face_with_sunglasses:"),
            emoji.emojize("Назад :right_arrow_curving_left:"),
            emoji.emojize("Вывести пользователей"),
            emoji.emojize("Редактировать пользователя"),
            emoji.emojize("Данные пользователя"),
            emoji.emojize("Отправить рассылку"),
            emoji.emojize("Назад в админ-панель"),
            emoji.emojize("Главное меню"),
        ]
        return text in main_menu_buttons

    def _process_message_text(self, update: Update, context: CallbackContext, message: Optional[str]) -> str:
        """Обрабатывает текст сообщения."""
        _message = message or update.message.text

        if update.message.chat.type != "private":
            _message = _message.replace("@" + context.bot.username, "").strip()

        return _message

    async def photo_message_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает сообщения с фото."""
        logger.info("Photo message received")

        if not await self.is_bot_mentioned(update, context):
            return

        await self.register_user_if_not_exists(update, context, update.message.from_user)

        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not await self.subscription_preprocessor(update, context):
            return

        chat_mode = self.db.get_user_attribute(user_id, "current_chat_mode")
        logger.info(f"Photo received in chat mode: {chat_mode}")

        if chat_mode == "photo_editor":
            await self.photo_editor_handle(update, context)
        elif chat_mode == "artist":
            caption = update.message.caption or "Создай изображение похожее на это фото"
            await self.generate_image_handle(update, context, message=caption)
        else:
            await self._handle_photo_in_regular_mode(update, context)

    async def _handle_photo_in_regular_mode(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает фото в обычных режимах чата."""
        user_id = update.message.from_user.id
        current_model = self.db.get_user_attribute(user_id, "current_model")

        # Если модель поддерживает vision, используем её
        if current_model == "gpt-4-vision-preview":
            await self._vision_message_handle_fn(update, context, use_new_dialog_timeout=True)
        else:
            # Иначе просто уведомляем пользователя
            caption = update.message.caption
            if caption:
                await self.message_handle(update, context, message=caption)
            else:
                await update.message.reply_text(
                    "📸 Фото получено! Если хотите его описать или задать вопрос по фото, "
                    "напишите текст в подписи к фото или следующим сообщением.",
                    parse_mode=ParseMode.HTML
                )