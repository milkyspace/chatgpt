import asyncio
import base64
import io
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import emoji
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
import openai_utils
from base_handler import BaseHandler
from keyboards import BotKeyboards
from message_processor import MessageProcessor
from utils import get_user_semaphore, HELP_MESSAGE, HELP_GROUP_CHAT_MESSAGE

# Настройка логирования
logger = logging.getLogger(__name__)

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Константы сообщений
class MessageHandlers(MessageProcessor):
    """Класс для обработки сообщений."""

    def __init__(self, database: database.Database, subscription_handlers: Any,
                 chat_mode_handlers: Any, admin_handlers: Any, image_handlers: Any):
        # Инициализируем BaseHandler
        BaseHandler.__init__(self, database)
        self.subscription_handlers = subscription_handlers
        self.chat_mode_handlers = chat_mode_handlers
        self.admin_handlers = admin_handlers
        self.image_handlers = image_handlers

    async def start_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /start."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        try:
            self.db.start_new_dialog(user_id)
            reply_text = self._get_welcome_message()
        except PermissionError:
            reply_text = self._get_no_subscription_message()

        reply_markup = await BotKeyboards.get_main_keyboard(user_id)
        await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    @staticmethod
    def _get_welcome_message() -> str:
        """Возвращает приветственное сообщение."""
        return (
                "👋 Привет! Мы <b>Ducks GPT</b>\n"
                "Компактный чат-бот на базе <b>ChatGPT</b>\n"
                "Рады знакомству!\n\n"
                "Доступны в <b>РФ</b>🇷🇺\n"
                "<b>Дарим подписку на 7 дней:</b>\n"
                "- 15 запросов\n"
                "- 3 генерации изображения\n\n"
                + HELP_MESSAGE
        )

    @staticmethod
    def _get_no_subscription_message() -> str:
        """Возвращает сообщение об отсутствии подписки."""
        return (
                "👋 Привет! Мы <b>Ducks GPT</b>\n"
                "Компактный чат-бот на базе <b>ChatGPT</b>\n"
                "Рады знакомству!\n\n"
                "❌ <b>Для использования бота требуется активная подписка</b>\n\n"
                "🎁 <b>100 ₽ за наш счёт при регистрации!</b>\n\n"
                "Используйте команду /subscription чтобы посмотреть доступные подписки\n\n"
                + HELP_MESSAGE
        )

    async def help_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /help."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())
        await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)

    async def help_group_chat_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /help_group_chat."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + context.bot.username)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def retry_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /retry."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not await self.subscription_preprocessor(update, context):
            return

        dialog_messages = self.db.get_dialog_messages(user_id, dialog_id=None)
        if not dialog_messages:
            await update.message.reply_text("Нет сообщений для перегенерации 🤷‍♂️")
            return

        last_dialog_message = dialog_messages.pop()
        self.db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)

        await self.message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)

    async def new_dialog_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /new для начала нового диалога."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        # Сбрасываем модель с vision на текстовую по умолчанию
        current_model = self.db.get_user_attribute(user_id, "current_model")
        if current_model == "gpt-4-vision-preview":
            self.db.set_user_attribute(user_id, "current_model", "gpt-4o")

        try:
            self.db.start_new_dialog(user_id)
            await update.message.reply_text("Начинаем новый диалог ✅")

            # Отправляем приветственное сообщение для текущего режима чата
            chat_mode = self.db.get_user_attribute(user_id, "current_chat_mode")
            await update.message.reply_text(
                f"{config.chat_modes[chat_mode]['welcome_message']}",
                parse_mode=ParseMode.HTML
            )
        except PermissionError:
            await update.message.reply_text(
                "❌ <b>Для начала нового диалога требуется активная подписка</b>\n\n"
                "Используйте /subscription для управления подписками",
                parse_mode=ParseMode.HTML
            )

    async def message_handle(self, update: Update, context: CallbackContext,
                             message: Optional[str] = None, use_new_dialog_timeout: bool = True) -> None:
        """Обрабатывает текстовые сообщения."""

        # Добавьте проверку API ключей
        if not config.openai_api_key:
            logger.error("OpenAI API key is not configured!")
            await update.message.reply_text(
                "❌ Бот не настроен. Отсутствует API ключ OpenAI.",
                parse_mode=ParseMode.HTML
            )
            return

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
        if chat_mode == "artist":
            await self.image_handlers.generate_image_handle(update, context, message=processed_message)
            return
        elif chat_mode == "stenographer":
            await self.voice_message_handle(update, context, message=processed_message)
            return

        await self._handle_text_message(update, context, processed_message, use_new_dialog_timeout)

    async def _handle_text_message(self, update: Update, context: CallbackContext,
                                   message: str, use_new_dialog_timeout: bool) -> None:
        """Обрабатывает текстовое сообщение."""
        user_id = update.message.from_user.id

        task = asyncio.create_task(
            self._text_message_handle_fn(update, context, message, use_new_dialog_timeout)
        )

        await self.execute_user_task(user_id, task, update)

    async def _text_message_handle_fn(self, update: Update, context: CallbackContext,
                                      message: str, use_new_dialog_timeout: bool) -> None:
        """Обрабатывает текстовое сообщение (внутренняя функция)."""
        user_id = update.message.from_user.id

        try:
            logger.info(f"=== START TEXT MESSAGE PROCESSING ===")
            logger.info(f"User: {user_id}, Message: '{message}'")

            # Безопасный доступ к семафору
            if user_id not in user_semaphores:
                logger.warning(f"Semaphore not found for user {user_id}, initializing...")
                user_semaphores[user_id] = asyncio.Semaphore(1)

            chat_mode = self.db.get_user_attribute(user_id, "current_chat_mode")
            logger.info(f"Chat mode: {chat_mode}")

            current_model = self.db.get_user_attribute(user_id, "current_model")
            logger.info(f"Current model: {current_model}")

            # Проверка подписки
            logger.info("Checking subscription...")
            subscription_info = self.db.get_user_subscription_info(user_id)
            logger.info(f"Subscription info: {subscription_info}")

            await self.prepare_dialog(user_id, use_new_dialog_timeout, chat_mode, update)
            logger.info("Dialog prepared")

            if not message or len(message) == 0:
                await update.message.reply_text("🥲 You sent <b>empty message</b>. Please, try again!",
                                                parse_mode=ParseMode.HTML)
                return

            async with get_user_semaphore(user_id):
                logger.info("Acquired user semaphore")
                placeholder_message = await update.message.reply_text("<i>Думаю...</i>", parse_mode=ParseMode.HTML)
                await update.message.chat.send_action(action="typing")
                logger.info("Sent typing action")

                dialog_messages = self.db.get_dialog_messages(user_id, dialog_id=None)
                logger.info(f"Retrieved {len(dialog_messages)} dialog messages")

                parse_mode = {
                    "html": ParseMode.HTML,
                    "markdown": ParseMode.MARKDOWN
                }[config.chat_modes[chat_mode]["parse_mode"]]

                logger.info(f"Using parse mode: {parse_mode}")

                chatgpt_instance = openai_utils.ChatGPT(model=current_model)
                logger.info("Created ChatGPT instance")

                if config.enable_message_streaming:
                    logger.info("Using streaming response")
                    await self._handle_streaming_response(
                        update, context, message, dialog_messages, chat_mode,
                        chatgpt_instance, placeholder_message, parse_mode, user_id
                    )
                else:
                    logger.info("Using non-streaming response")
                    answer, n_input_tokens, n_output_tokens = await self._get_non_streaming_response(
                        chatgpt_instance, message, dialog_messages, chat_mode
                    )
                    logger.info(f"Got response: {answer[:100]}...")

                    await self.edit_message_with_retry(context, placeholder_message, answer, chat_mode)
                    logger.info("Message edited")

                    new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": answer,
                                          "date": datetime.now()}
                    self.update_dialog_and_tokens(user_id, new_dialog_message, n_input_tokens, n_output_tokens)
                    logger.info("Dialog updated")

            logger.info("=== TEXT MESSAGE PROCESSING COMPLETED ===")

        except Exception as e:
            logger.error(f"=== ERROR IN TEXT MESSAGE HANDLING ===", exc_info=True)
            logger.error(f"Error type: {type(e)}")
            logger.error(f"Error message: {str(e)}")
            logger.error(f"=== END ERROR ===")
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

    @staticmethod
    async def _get_non_streaming_response(chatgpt_instance: openai_utils.ChatGPT, message: str,
                                          dialog_messages: List[Dict], chat_mode: str) -> Tuple[str, int, int]:
        """Получает непотоковый ответ от ChatGPT."""
        answer, (n_input_tokens, n_output_tokens), _ = await chatgpt_instance.send_message(
            message, dialog_messages=dialog_messages, chat_mode=chat_mode
        )
        return answer, n_input_tokens, n_output_tokens

    async def voice_message_handle(self, update: Update, context: CallbackContext, message: Optional[str] = None) -> \
            Optional[str]:
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

        # Обертываем синхронный вызов в asyncio.to_thread
        transcribed_text = await asyncio.to_thread(openai_utils.transcribe_audio, buf)

        text = f"🎤: <i>{transcribed_text}</i>"

        user_id = update.message.from_user.id
        audio_duration_minutes = voice.duration / 60.0
        self.db.set_user_attribute(user_id, "n_transcribed_seconds",
                                   voice.duration + self.db.get_user_attribute(user_id, "n_transcribed_seconds"))

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

    @staticmethod
    async def edited_message_handle(update: Update, context: CallbackContext) -> None:
        """Обрабатывает редактированные сообщения."""
        if update.edited_message.chat.type == "private":
            text = "🥲 Unfortunately, message <b>editing</b> is not supported"
            await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cancel_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /cancel."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if user_id in user_tasks:
            user_tasks[user_id].cancel()
        else:
            await update.message.reply_text("<i>Нечего отменять...</i>", parse_mode=ParseMode.HTML)

    @staticmethod
    async def _is_main_menu_button(text: str) -> bool:
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

    async def handle_main_menu_buttons(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает нажатия кнопок главного меню."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        text = update.message.text

        button_handlers = {
            emoji.emojize("Продлить подписку :money_bag:"): self.subscription_handlers.subscription_handle,
            emoji.emojize("Выбрать режим :red_heart:"): self.chat_mode_handlers.show_chat_modes_handle,
            emoji.emojize("Пригласить :woman_and_man_holding_hands:"): self._handle_invite,
            emoji.emojize("Помощь :heart_hands:"): self.help_handle,
            emoji.emojize("Админ-панель :smiling_face_with_sunglasses:"): self.admin_handlers.admin_panel_handle,
            emoji.emojize("Назад :right_arrow_curving_left:"): self._handle_back,
            emoji.emojize("Вывести пользователей"): self.admin_handlers.show_users_handle,
            emoji.emojize("Редактировать пользователя"): self.admin_handlers.edit_user_handle,
            emoji.emojize("Данные пользователя"): self.admin_handlers.get_user_data_handle,
            emoji.emojize("Отправить рассылку"): self.admin_handlers.broadcast_handle,
            emoji.emojize("Назад в админ-панель"): self.admin_handlers.handle_admin_panel_back,
            emoji.emojize("Главное меню"): self.admin_handlers.handle_main_menu_back,
        }

        handler = button_handlers.get(text)
        if handler:
            await handler(update, context)
        elif emoji.emojize(":green_circle:") in text or emoji.emojize(":red_circle:") in text:
            await self.subscription_handlers.subscription_handle(update, context)

    @staticmethod
    async def _handle_invite(update: Update, context: CallbackContext) -> None:
        """Обрабатывает кнопку приглашения друзей."""
        await update.message.reply_text(
            "👥 <b>Пригласите друзей!</b>\n\n"
            "Поделитесь ссылкой на бота с друзьями:\n"
            f"https://t.me/{context.bot.username}\n\n"
            "Чем больше друзей - тем лучше!",
            parse_mode=ParseMode.HTML
        )

    async def _handle_back(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает кнопку 'Назад'."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        reply_markup = await BotKeyboards.get_main_keyboard(user_id)
        await update.message.reply_text(
            "Возврат в главное меню...",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )

    @staticmethod
    def _process_message_text(update: Update, context: CallbackContext, message: Optional[str]) -> str:
        """Обрабатывает текст сообщения."""
        _message = message or update.message.text

        if update.message.chat.type != "private":
            _message = _message.replace("@" + context.bot.username, "").strip()

        return _message