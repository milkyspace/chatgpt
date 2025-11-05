"""
Модуль Telegram бота для продажи доступа к ChatGPT.
Оптимизированная версия с улучшенной структурой и читаемостью.
"""

import logging
import asyncio
import traceback
import html
import json
import base64
import io
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple

import requests
import emoji
import pytz
import openai
import telegram
from telegram import (
    Update, User, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeAllPrivateChats, InputFile
)
from telegram.ext import (
    Application, ApplicationBuilder, CallbackContext, CommandHandler,
    MessageHandler, CallbackQueryHandler, AIORateLimiter, filters
)
from telegram.constants import ParseMode
from yookassa import Payment, Configuration

import config
import database
import openai_utils
from keyboards import BotKeyboards
from subscription import SubscriptionType
from subscription_config import SubscriptionConfig

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
/settings – Настройки ⚙️
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


class CustomEncoder(json.JSONEncoder):
    """Кастомный JSON энкодер для обработки datetime объектов."""

    def default(self, obj: Any) -> Any:
        """Обрабатывает специальные типы данных для JSON сериализации."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class BotHandlers:
    """Базовый класс для обработчиков бота."""

    def __init__(self, database: database.Database):
        self.db = database

    async def register_user_if_not_exists(self, update: Update, context: CallbackContext, user: User) -> bool:
        """
        Регистрирует пользователя если он не существует.
        """
        user_registered_now = False

        print(user.id)
        print(self.db.check_if_user_exists(user.id))
        if not self.db.check_if_user_exists(user.id):
            self.db.add_new_user(
                user.id,
                update.message.chat_id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name
            )
            self.db.add_subscription(user.id, SubscriptionType.FREE, 7)
            user_registered_now = True
            self.db.start_new_dialog(user.id)

        # Инициализация необходимых атрибутов пользователя
        await self._initialize_user_attributes(user.id)

        if user_registered_now:
            await self._send_registration_notification(context, user)

        return user_registered_now

    async def _initialize_user_attributes(self, user_id: int) -> None:
        """Инициализирует необходимые атрибуты пользователя."""
        if self.db.get_user_attribute(user_id, "current_dialog_id") is None:
            self.db.start_new_dialog(user_id)

        if user_id not in user_semaphores:
            user_semaphores[user_id] = asyncio.Semaphore(1)

        attributes_to_init = [
            ("current_model", config.models["available_text_models"][0]),
            ("n_used_tokens", {}),
            ("n_transcribed_seconds", 0.0),
            ("n_generated_images", 0)
        ]

        for attr, default_value in attributes_to_init:
            if self.db.get_user_attribute(user_id, attr) is None:
                self.db.set_user_attribute(user_id, attr, default_value)

    async def _send_registration_notification(self, context: CallbackContext, user: User) -> None:
        """Отправляет уведомление о новой регистрации администраторам."""
        username = user.username or "No username"
        first_name = user.first_name or "No first name"
        last_name = user.last_name or "No last name"

        notification_text = (
            f"A new user has just registered!\n\n"
            f"Username: {username}\n"
            f"First Name: {first_name}\n"
            f"Last Name: {last_name}"
        )

        for admin_id in config.roles.get('admin', []):
            try:
                await context.bot.send_message(chat_id=admin_id, text=notification_text)
            except Exception as e:
                logger.warning(f"Failed to send registration to admin {admin_id}: {e}")

    async def is_bot_mentioned(self, update: Update, context: CallbackContext) -> bool:
        """
        Проверяет, упомянут ли бот в сообщении.
        """
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

    async def is_previous_message_not_answered_yet(self, update: Update, context: CallbackContext) -> bool:
        """
        Проверяет, обрабатывается ли предыдущее сообщение.
        """
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id

        if user_semaphores[user_id].locked():
            text = "⏳ Please <b>wait</b> for a reply to the previous message\nOr you can /cancel it"
            await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
            return True
        return False

    async def subscription_preprocessor(self, update: Update, context: CallbackContext) -> bool:
        """
        Проверяет возможность выполнения запроса по подписке.
        """
        user_id = update.effective_user.id
        subscription_info = self.db.get_user_subscription_info(user_id)

        if not subscription_info["is_active"]:
            await update.message.reply_text(
                "❌ Для использования бота требуется активная подписка. "
                "Пожалуйста, приобретите подписку через /subscription",
                parse_mode=ParseMode.HTML
            )
            return False

        return await self._check_subscription_limits(subscription_info, update)

    async def _check_subscription_limits(self, subscription_info: Dict[str, Any], update: Update) -> bool:
        """Проверяет лимиты подписки используя централизованную конфигурацию."""
        subscription_type = SubscriptionType(subscription_info["type"])

        # Проверяем лимит запросов
        if not SubscriptionConfig.can_make_request(subscription_type, subscription_info["requests_used"]):
            description = SubscriptionConfig.get_description(subscription_type)
            await update.message.reply_text(
                f"❌ Лимит запросов подписки {description['name']} исчерпан. "
                "Пожалуйста, обновите подписку через /subscription",
                parse_mode=ParseMode.HTML
            )
            return False

        return True


class MessageHandlers(BotHandlers):
    """Класс для обработки сообщений."""

    def __init__(self, database: database.Database, subscription_handlers: Any, chat_mode_handlers: Any):
        super().__init__(database)
        self.subscription_handlers = subscription_handlers
        self.chat_mode_handlers = chat_mode_handlers

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

    def _get_welcome_message(self) -> str:
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

    def _get_no_subscription_message(self) -> str:
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
            self.db.set_user_attribute(user_id, "current_model", "gpt-4-turbo-2024-04-09")

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

        if chat_mode == "artist":
            await self.generate_image_handle(update, context, message=message)
            return
        elif chat_mode == "stenographer":
            await self.voice_message_handle(update, context, message=message)
            return

        await self._handle_text_message(update, context, processed_message, use_new_dialog_timeout)

    async def _is_main_menu_button(self, text: str) -> bool:
        """Проверяет, является ли текст кнопкой главного меню."""
        main_menu_buttons = [
            emoji.emojize("Продлить подписку :money_bag:"),
            emoji.emojize("Выбрать режим :red_heart:"),
            emoji.emojize("Пригласить :woman_and_man_holding_hands:"),
            emoji.emojize("Помощь :heart_hands:"),
            emoji.emojize("Админ-панель :smiling_face_with_sunglasses:"),
            emoji.emojize("Назад :right_arrow_curving_left:"),
        ]
        return text in main_menu_buttons

    async def handle_main_menu_buttons(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает нажатия кнопок главного меню."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        text = update.message.text

        if text == emoji.emojize("Продлить подписку :money_bag:"):
            await self.subscription_handlers.subscription_handle(update, context)
        elif text == emoji.emojize("Выбрать режим :red_heart:"):
            await self.chat_mode_handlers.show_chat_modes_handle(update, context)
        elif text == emoji.emojize("Пригласить :woman_and_man_holding_hands:"):
            await self._handle_invite(update, context)
        elif text == emoji.emojize("Помощь :heart_hands:"):
            await self.help_handle(update, context)
        elif text == emoji.emojize("Админ-панель :smiling_face_with_sunglasses:"):
            await self._handle_admin_panel(update, context)
        elif text == emoji.emojize("Назад :right_arrow_curving_left:"):
            await self._handle_back(update, context)
        elif emoji.emojize(":green_circle:") in text or emoji.emojize(":red_circle:") in text:
            await self.subscription_handlers.subscription_handle(update, context)

    async def _handle_invite(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает кнопку приглашения друзей."""
        await update.message.reply_text(
            "👥 <b>Пригласите друзей!</b>\n\n"
            "Поделитесь ссылкой на бота с друзьями:\n"
            f"https://t.me/{context.bot.username}\n\n"
            "Чем больше друзей - тем лучше!",
            parse_mode=ParseMode.HTML
        )

    async def _handle_admin_panel(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает кнопку админ-панели."""
        user_id = update.message.from_user.id
        if user_id in config.roles.get('admin', []):
            await self._show_admin_panel(update, context)
        else:
            await update.message.reply_text("У вас нет доступа к админ-панели.")

    async def _show_admin_panel(self, update: Update, context: CallbackContext) -> None:
        """Показывает админ-панель."""
        text = "🛠️ <b>Админ-панель</b>\n\nВыберите действие:"
        reply_markup = BotKeyboards.get_admin_keyboard()
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

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

    def _process_message_text(self, update: Update, context: CallbackContext, message: Optional[str]) -> str:
        """Обрабатывает текст сообщения."""
        _message = message or update.message.text

        if update.message.chat.type != "private":
            _message = _message.replace("@" + context.bot.username, "").strip()

        return _message

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

        await self._execute_user_task(user_id, task, update)

    async def _execute_user_task(self, user_id: int, task: asyncio.Task, update: Update) -> None:
        """Выполняет задачу пользователя с обработкой отмены."""
        user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]

    async def _text_message_handle_fn(self, update: Update, context: CallbackContext,
                                      message: str, use_new_dialog_timeout: bool) -> None:
        """Обрабатывает текстовое сообщение (внутренняя функция)."""
        user_id = update.message.from_user.id
        chat_mode = self.db.get_user_attribute(user_id, "current_chat_mode")

        await self._prepare_dialog(user_id, use_new_dialog_timeout, chat_mode, update)

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
                    # Потоковый режим - отправляем части ответа по мере поступления
                    await self._handle_streaming_response(
                        update, context, message, dialog_messages, chat_mode,
                        chatgpt_instance, placeholder_message, parse_mode, user_id
                    )
                else:
                    # Непотоковый режим - получаем весь ответ сразу
                    answer, n_input_tokens, n_output_tokens = await self._get_non_streaming_response(
                        chatgpt_instance, message, dialog_messages, chat_mode
                    )

                    # Отправляем полный ответ
                    await self._edit_message_with_retry(context, placeholder_message, answer, chat_mode)

                    # Сохраняем диалог и токены
                    new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": answer,
                                          "date": datetime.now()}
                    self._update_dialog_and_tokens(user_id, new_dialog_message, n_input_tokens, n_output_tokens)

        except Exception as e:
            await self._handle_message_error(update, e)

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

            # Обновляем полный ответ
            full_answer = answer
            n_input_tokens, n_output_tokens = chunk_n_input_tokens, chunk_n_output_tokens

            # Проверяем, нужно ли обновлять сообщение
            current_time = datetime.now()
            time_diff = (current_time - last_update_time).total_seconds()

            # Обновляем сообщение если:
            # 1. Прошло больше 0.5 секунд с последнего обновления ИЛИ
            # 2. Ответ значительно изменился ИЛИ
            # 3. Это финальный статус
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
                        # Если ошибка не связана с неизмененным сообщением, пытаемся отправить без форматирования
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

            # Небольшая задержка для плавности
            await asyncio.sleep(0.01)

        # Сохраняем финальный ответ в диалог
        new_dialog_message = {"user": [{"type": "text", "text": message}], "bot": full_answer, "date": datetime.now()}
        self._update_dialog_and_tokens(user_id, new_dialog_message, n_input_tokens, n_output_tokens)

        # Уведомление если были удалены сообщения из контекста
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
                # Потоковый режим для vision
                gen = chatgpt_instance.send_vision_message_stream(
                    message_text,
                    dialog_messages=dialog_messages,
                    image_buffer=buf,
                    chat_mode=chat_mode,
                )

                full_answer = ""
                prev_answer = ""
                last_update_time = datetime.now()

                async for gen_item in gen:
                    status, answer, (
                    chunk_n_input_tokens, chunk_n_output_tokens), n_first_dialog_messages_removed = gen_item

                    full_answer = answer
                    n_input_tokens, n_output_tokens = chunk_n_input_tokens, chunk_n_output_tokens

                    # Проверяем, нужно ли обновлять сообщение
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
                                    )
                                    prev_answer = answer
                                    last_update_time = current_time
                                except Exception:
                                    pass

                    await asyncio.sleep(0.01)

            else:
                # Непотоковый режим для vision
                answer, (n_input_tokens, n_output_tokens), _ = await chatgpt_instance.send_vision_message(
                    message_text,
                    dialog_messages=dialog_messages,
                    image_buffer=buf,
                    chat_mode=chat_mode,
                )

                await context.bot.edit_message_text(
                    answer[:4096],
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=parse_mode,
                )
                full_answer = answer

            # Сохраняем диалог
            if buf is not None:
                base_image = base64.b64encode(buf.getvalue()).decode("utf-8")
                new_dialog_message = {
                    "user": [
                        {"type": "text", "text": message_text},
                        {"type": "image", "image": base_image}
                    ],
                    "bot": full_answer,
                    "date": datetime.now()
                }
            else:
                new_dialog_message = {"user": message_text, "bot": full_answer, "date": datetime.now()}

            self._update_dialog_and_tokens(user_id, new_dialog_message, n_input_tokens, n_output_tokens)

        except asyncio.CancelledError:
            self.db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            raise
        except Exception as e:
            error_text = f"Something went wrong during completion_1. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)


    async def _get_chatgpt_response(self, message: str, dialog_messages: List[Dict],
                                    chat_mode: str, user_id: str) -> Tuple[str, int, int]:
        """Получает ответ от ChatGPT."""
        current_model = self.db.get_user_attribute(user_id, "current_model")
        chatgpt_instance = openai_utils.ChatGPT(model=current_model)

        if config.enable_message_streaming:
            return await self._get_streamed_response(chatgpt_instance, message, dialog_messages, chat_mode)
        else:
            answer, (n_input_tokens, n_output_tokens), _ = await chatgpt_instance.send_message(
                message, dialog_messages=dialog_messages, chat_mode=chat_mode
            )
            return answer, n_input_tokens, n_output_tokens

    async def _get_streamed_response(self, chatgpt_instance: openai_utils.ChatGPT, message: str,
                                     dialog_messages: List[Dict], chat_mode: str) -> Tuple[str, int, int]:
        """Получает потоковый ответ от ChatGPT."""
        gen = chatgpt_instance.send_message_stream(message, dialog_messages=dialog_messages, chat_mode=chat_mode)
        answer = ""
        n_input_tokens, n_output_tokens = 0, 0

        async for gen_item in gen:
            status, chunk_answer, (chunk_n_input_tokens, chunk_n_output_tokens), _ = gen_item

            # Исправление: не конкатенируем, а заменяем ответ
            # В потоковом режиме каждый чанк содержит полный ответ на данный момент
            answer = chunk_answer
            n_input_tokens, n_output_tokens = chunk_n_input_tokens, chunk_n_output_tokens

            if status == "finished":
                break

        return answer, n_input_tokens, n_output_tokens

    async def _prepare_dialog(self, user_id: int, use_new_dialog_timeout: bool,
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

    def _update_dialog_and_tokens(self, user_id: int, new_dialog_message: Dict,
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

    async def _edit_message_with_retry(self, context: CallbackContext, placeholder_message: telegram.Message,
                                       answer: str, chat_mode: str) -> None:
        """Редактирует сообщение с повторными попытками при ошибках."""
        parse_mode = {
            "html": ParseMode.HTML,
            "markdown": ParseMode.MARKDOWN
        }[config.chat_modes[chat_mode]["parse_mode"]]

        try:
            await context.bot.edit_message_text(
                answer[:4096],  # Ограничение длины сообщения в Telegram
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

    async def _handle_message_error(self, update: Update, error: Exception) -> None:
        """Обрабатывает ошибки при обработке сообщений."""
        error_text = f"Something went wrong during completion. Reason: {error}"
        logger.error(error_text)
        await update.message.reply_text(error_text)

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

            self._update_dialog_and_tokens(user_id, new_dialog_message, n_input_tokens, n_output_tokens)

        except asyncio.CancelledError:
            self.db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
            raise
        except Exception as e:
            error_text = f"Something went wrong during completion_1. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)

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
            return  # Обработка завершена в _transcribe_voice_message

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

        # Обновляем статистику использования
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

    async def cancel_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /cancel."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if user_id in user_tasks:
            user_tasks[user_id].cancel()
        else:
            await update.message.reply_text("<i>Нечего отменять...</i>", parse_mode=ParseMode.HTML)


class ChatModeHandlers(BotHandlers):
    """Класс для обработки режимов чата."""

    def get_chat_mode_menu(self, page_index: int):
        """
        Создает меню выбора режима чата.
        """
        n_chat_modes_per_page = config.n_chat_modes_per_page
        text = f"Выберите <b>режим чата</b> (Доступно {len(config.chat_modes)} режимов):"

        chat_mode_keys = list(config.chat_modes.keys())
        page_chat_mode_keys = chat_mode_keys[
                              page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page
                              ]

        keyboard = []
        row = []
        for chat_mode_key in page_chat_mode_keys:
            name = config.chat_modes[chat_mode_key]["name"]
            row.append(InlineKeyboardButton(name, callback_data=f"set_chat_mode|{chat_mode_key}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

        # Добавляем пагинацию если нужно
        if len(chat_mode_keys) > n_chat_modes_per_page:
            is_first_page = (page_index == 0)
            is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

            pagination_row = []
            if not is_first_page:
                pagination_row.append(InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"))
            if not is_last_page:
                pagination_row.append(InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}"))
            if pagination_row:
                keyboard.append(pagination_row)

        reply_markup = InlineKeyboardMarkup(keyboard)
        return text, reply_markup

    async def show_chat_modes_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /mode."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        text, reply_markup = self.get_chat_mode_menu(0)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def show_chat_modes_callback_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает callback пагинации режимов чата."""
        await self.register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
        user_id = update.callback_query.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        query = update.callback_query
        await query.answer()

        page_index = int(query.data.split("|")[1])
        if page_index < 0:
            return

        text, reply_markup = self.get_chat_mode_menu(page_index)
        try:
            await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except telegram.error.BadRequest as e:
            if not str(e).startswith("Message is not modified"):
                raise

    async def set_chat_mode_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает выбор режима чата."""
        await self.register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
        user_id = update.callback_query.from_user.id

        query = update.callback_query
        await query.answer()

        chat_mode = query.data.split("|")[1]

        self.db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
        self.db.start_new_dialog(user_id)

        await context.bot.send_message(
            update.callback_query.message.chat.id,
            f"{config.chat_modes[chat_mode]['welcome_message']}",
            parse_mode=ParseMode.HTML
        )


class SettingsHandlers(BotHandlers):
    """Класс для обработки настроек."""

    def get_settings_menu(self, user_id: int):
        """
        Создает меню настроек.
        """
        text = "⚙️ Настройки:"

        keyboard = [
            [InlineKeyboardButton("🧠 Модель нейросети", callback_data='model-ai_model')],
            [InlineKeyboardButton("🎨 Модель художника", callback_data='model-artist_model')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        return text, reply_markup

    async def settings_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /settings."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        text, reply_markup = self.get_settings_menu(user_id)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def set_settings_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает выбор настроек."""
        await self.register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
        user_id = update.callback_query.from_user.id

        query = update.callback_query
        await query.answer()

        _, model_key = query.data.split("|")
        self.db.set_user_attribute(user_id, "current_model", model_key)

        await self.display_model_info(query, user_id, context)

    async def display_model_info(self, query, user_id, context):
        """Отображает информацию о модели."""
        current_model = self.db.get_user_attribute(user_id, "current_model")
        model_info = config.models["info"][current_model]
        description = model_info["description"]
        scores = model_info["scores"]

        details_text = f"{description}\n\n"
        for score_key, score_value in scores.items():
            details_text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

        details_text += "\nВыберите <b>модель</b>:"

        buttons = []
        claude_buttons = []
        other_buttons = []

        for model_key in config.models["available_text_models"]:
            title = config.models["info"][model_key]["name"]
            if model_key == current_model:
                title = "✅ " + title

            if "claude" in model_key.lower():
                callback_data = f"claude-model-set_settings|{model_key}"
                claude_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))
            else:
                callback_data = f"model-set_settings|{model_key}"
                other_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))

        half_size = len(other_buttons) // 2
        first_row = other_buttons[:half_size]
        second_row = other_buttons[half_size:]
        back_button = [InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')]

        reply_markup = InlineKeyboardMarkup([first_row, second_row, claude_buttons, back_button])

        try:
            await query.edit_message_text(text=details_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                pass

    async def model_settings_handler(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает настройки моделей."""
        query = update.callback_query
        await query.answer()

        data = query.data
        user_id = query.from_user.id

        if data == 'model-ai_model':
            current_model = self.db.get_user_attribute(user_id, "current_model")
            text = f"{config.models['info'][current_model]['description']}\n\n"

            score_dict = config.models["info"][current_model]["scores"]
            for score_key, score_value in score_dict.items():
                text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

            text += "\nSelect <b>model</b>:\n"

            buttons = []
            claude_buttons = []
            other_buttons = []

            for model_key in config.models["available_text_models"]:
                title = config.models["info"][model_key]["name"]
                if model_key == current_model:
                    title = "✅ " + title

                if "claude" in model_key.lower():
                    callback_data = f"claude-model-set_settings|{model_key}"
                    claude_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))
                else:
                    callback_data = f"model-set_settings|{model_key}"
                    other_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))

            half_size = len(other_buttons) // 2
            first_row = other_buttons[:half_size]
            second_row = other_buttons[half_size:]
            back_button = [InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')]

            reply_markup = InlineKeyboardMarkup([first_row, second_row, claude_buttons, back_button])

            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

        elif data.startswith('claude-model-set_settings|'):
            if config.anthropic_api_key is None or config.anthropic_api_key == "":
                await context.bot.send_message(
                    chat_id=user_id,
                    text="This bot does not have the Anthropic models available :(",
                    parse_mode='Markdown'
                )
                return
            _, model_key = data.split("|")
            self.db.set_user_attribute(user_id, "current_model", model_key)
            await self.display_model_info(query, user_id, context)

        elif data.startswith('model-set_settings|'):
            _, model_key = data.split("|")
            if "claude" in model_key.lower() and (config.anthropic_api_key is None or config.anthropic_api_key == ""):
                await context.bot.send_message(
                    chat_id=user_id,
                    text="This bot does not have the Anthropic models available :(",
                    parse_mode='Markdown'
                )
                return
            self.db.set_user_attribute(user_id, "current_model", model_key)
            await self.display_model_info(query, user_id, context)

        elif data.startswith('model-artist-set_model|'):
            _, model_key = data.split("|")
            await self.switch_between_artist_handler(query, user_id, model_key)

        elif data == 'model-artist_model':
            await self.artist_model_settings_handler(query, user_id)

        elif data.startswith('model-artist-set_model|'):
            _, model_key = data.split("|")
            preferences = self.db.get_user_attribute(user_id, "image_preferences")
            preferences["model"] = model_key
            self.db.set_user_attribute(user_id, "image_preferences", preferences)
            await self.artist_model_settings_handler(query, user_id)

        elif data.startswith("model-artist-set_images|"):
            _, n_images = data.split("|")
            preferences = self.db.get_user_attribute(user_id, "image_preferences")
            preferences["n_images"] = int(n_images)
            self.db.set_user_attribute(user_id, "image_preferences", preferences)
            await self.artist_model_settings_handler(query, user_id)

        elif data.startswith("model-artist-set_resolution|"):
            _, resolution = data.split("|")
            preferences = self.db.get_user_attribute(user_id, "image_preferences")
            preferences["resolution"] = resolution
            self.db.set_user_attribute(user_id, "image_preferences", preferences)
            await self.artist_model_settings_handler(query, user_id)

        elif data.startswith("model-artist-set_quality|"):
            _, quality = data.split("|")
            preferences = self.db.get_user_attribute(user_id, "image_preferences")
            preferences["quality"] = quality
            self.db.set_user_attribute(user_id, "image_preferences", preferences)
            await self.artist_model_settings_handler(query, user_id)

        elif data == 'model-back_to_settings':
            text, reply_markup = self.get_settings_menu(user_id)
            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def artist_model_settings_handler(self, query, user_id):
        """Обрабатывает настройки модели художника."""
        current_preferences = self.db.get_user_attribute(user_id, "image_preferences")
        current_model = current_preferences.get("model", "dalle-2")

        model_info = config.models["info"][current_model]
        description = model_info["description"]
        scores = model_info["scores"]

        details_text = f"{description}\n\n"
        for score_key, score_value in scores.items():
            details_text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

        buttons = []
        for model_key in config.models["available_image_models"]:
            title = config.models["info"][model_key]["name"]
            if model_key == current_model:
                title = "✅ " + title
            buttons.append(InlineKeyboardButton(title, callback_data=f"model-artist-set_model|{model_key}"))

        if current_model == "dalle-2":
            details_text += "\nFor this model, choose the number of images to generate and the resolution:"
            n_images = current_preferences.get("n_images", 1)
            images_buttons = [
                InlineKeyboardButton(
                    f"✅ {i} image" if i == n_images and i == 1 else f"✅ {i} images" if i == n_images else f"{i} image" if i == 1 else f"{i} images",
                    callback_data=f"model-artist-set_images|{i}")
                for i in range(1, 4)
            ]
            current_resolution = current_preferences.get("resolution", "1024x1024")
            resolution_buttons = [
                InlineKeyboardButton(f"✅ {res_key}" if res_key == current_resolution else f"{res_key}",
                                     callback_data=f"model-artist-set_resolution|{res_key}")
                for res_key in config.models["info"]["dalle-2"]["resolutions"].keys()
            ]
            keyboard = [buttons] + [images_buttons] + [resolution_buttons]

        elif current_model == "dalle-3":
            details_text += "\nFor this model, choose the quality of the images and the resolution:"
            current_quality = current_preferences.get("quality", "standard")
            quality_buttons = [
                InlineKeyboardButton(f"✅ {quality_key}" if quality_key == current_quality else f"{quality_key}",
                                     callback_data=f"model-artist-set_quality|{quality_key}")
                for quality_key in config.models["info"]["dalle-3"]["qualities"].keys()
            ]
            current_resolution = current_preferences.get("resolution", "1024x1024")
            resolution_buttons = [
                InlineKeyboardButton(f"✅ {res_key}" if res_key == current_resolution else f"{res_key}",
                                     callback_data=f"model-artist-set_resolution|{res_key}")
                for res_key in config.models["info"]["dalle-3"]["qualities"][current_quality]["resolutions"].keys()
            ]
            keyboard = [buttons] + [quality_buttons] + [resolution_buttons]
        else:
            keyboard = [buttons]

        keyboard.append([InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')])
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.edit_message_text(text=details_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                pass

    async def switch_between_artist_handler(self, query, user_id, model_key):
        """Переключает между моделями художника."""
        preferences = self.db.get_user_attribute(user_id, "image_preferences")
        preferences["model"] = model_key
        if model_key == "dalle-2":
            preferences["quality"] = "standard"
        elif model_key == "dalle-3":
            preferences["n_images"] = 1
        preferences["resolution"] = "1024x1024"
        self.db.set_user_attribute(user_id, "image_preferences", preferences)
        await self.artist_model_settings_handler(query, user_id)


class SubscriptionHandlers(BotHandlers):
    """Класс для обработки подписок и платежей."""

    async def subscription_handle(self, update: Update, context: CallbackContext) -> None:
        """Показывает доступные подписки."""
        try:
            user = self._get_user_from_update(update)
            await self.register_user_if_not_exists(update, context, user)
            user_id = user.id
            self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

            subscription_info = self.db.get_user_subscription_info(user_id)
            text = self._format_subscription_info(subscription_info)
            reply_markup = self._create_subscription_keyboard()

            await self._send_subscription_message(update, text, reply_markup)

        except Exception as e:
            logger.error(f"Error in subscription_handle: {e}")
            await self._handle_subscription_error(update)

    def _get_user_from_update(self, update: Update):
        """Получает пользователя из update."""
        if update.message is not None:
            return update.message.from_user
        else:
            return update.callback_query.from_user

    def _format_subscription_info(self, subscription_info: Dict[str, Any]) -> str:
        """Форматирует информацию о подписке."""
        text = ""
        if subscription_info["is_active"]:
            if subscription_info["type"] != "free":
                expires_str = subscription_info["expires_at"].strftime("%d.%m.%Y")
                text += f"📋 <b>Текущая подписка:</b> {subscription_info['type'].upper()}\n"
                text += f"📅 <b>Действует до:</b> {expires_str}\n"
            else:
                text += f"📋 <b>Текущая подписка:</b> БЕСПЛАТНАЯ\n"

            usage_text = self._format_usage_info(subscription_info)
            text += usage_text + "\n"

        text += "\n🔔 <b>Доступные подписки</b>\n"
        text += self._format_available_subscriptions()

        return text

    def _format_usage_info(self, subscription_info: Dict[str, Any]) -> str:
        """Форматирует информацию об использовании используя централизованную конфигурацию."""
        subscription_type = SubscriptionType(subscription_info["type"])
        limits = SubscriptionConfig.get_usage_limits(subscription_type)

        max_requests = limits.get("max_requests", 0)
        max_images = limits.get("max_images", 0)

        # Для безлимитных подписок показываем соответствующий текст
        requests_text = f"{subscription_info['requests_used']}/{max_requests}" if max_requests != float(
            'inf') else f"{subscription_info['requests_used']} (безлимитно)"
        images_text = f"{subscription_info['images_used']}/{max_images}" if max_images != float(
            'inf') else f"{subscription_info['images_used']} (безлимитно)"

        return (
            f"📊 <b>Запросы использовано:</b> {requests_text}\n"
            f"🎨 <b>Изображения использовано:</b> {images_text}"
        )

    def _format_available_subscriptions(self) -> str:
        """Форматирует информацию о доступных подписках используя централизованную конфигурацию."""
        text = ""

        for sub_type in SubscriptionConfig.get_all_paid_subscriptions():
            description = SubscriptionConfig.get_description(sub_type)
            price = SubscriptionConfig.get_price(sub_type)
            duration = SubscriptionConfig.get_duration(sub_type)

            text += f"<b>{description['name']}</b> - {price}₽ / {duration.days} дней\n"
            text += f"   {description['features']}\n\n"

        return text

    def _create_subscription_keyboard(self):
        """Создает клавиатуру для выбора подписки используя централизованную конфигурацию."""
        keyboard = []

        for sub_type in SubscriptionConfig.get_all_paid_subscriptions():
            description = SubscriptionConfig.get_description(sub_type)
            price = SubscriptionConfig.get_price(sub_type)

            name = f"{description['name']} - {price}₽"
            callback_data = f"subscribe|{sub_type.value}"
            keyboard.append([InlineKeyboardButton(name, callback_data=callback_data)])

        return InlineKeyboardMarkup(keyboard)

    async def _send_subscription_message(self, update: Update, text: str,
                                         reply_markup: InlineKeyboardMarkup) -> None:
        """Отправляет сообщение с информацией о подписках."""
        if update.message is not None:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            try:
                await update.callback_query.edit_message_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                )
            except telegram.error.BadRequest as e:
                if "Message is not modified" not in str(e):
                    await update.callback_query.message.reply_text(
                        text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
                    )

    async def _handle_subscription_error(self, update: Update) -> None:
        """Обрабатывает ошибки при работе с подписками."""
        error_text = "❌ Произошла ошибка при загрузке подписок. Пожалуйста, попробуйте снова."
        if update.callback_query:
            await update.callback_query.message.reply_text(error_text, parse_mode=ParseMode.HTML)

    async def subscription_callback_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает callback выбора подписки."""
        query = update.callback_query
        await query.answer()

        data = query.data

        if data == "subscription_back":
            await self._handle_subscription_back(query)
            return

        if data.startswith("subscribe|"):
            await self._handle_subscription_payment(query, context)

    async def _handle_subscription_back(self, query: telegram.CallbackQuery) -> None:
        """Обрабатывает возврат из меню подписок."""
        reply_text = "Возврат в главное меню...\n\n" + HELP_MESSAGE
        try:
            await query.edit_message_text(
                reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                await query.message.reply_text(
                    reply_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )

    async def _handle_subscription_payment(self, query: telegram.CallbackQuery, context: CallbackContext) -> None:
        """Обрабатывает создание платежа для подписки."""
        try:
            _, subscription_type_str = query.data.split("|")
            subscription_type = SubscriptionType(subscription_type_str)

            payment_url = await create_subscription_yookassa_payment(
                query.from_user.id, subscription_type, context
            )

            text = self._format_payment_message(subscription_type)
            keyboard = self._create_payment_keyboard(payment_url)

            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)

        except Exception as e:
            logger.error(f"Error in subscription payment: {e}")
            await query.edit_message_text(
                "❌ Произошла ошибка при создании платежа. Пожалуйста, попробуйте позже.",
                parse_mode=ParseMode.HTML
            )

    def _format_payment_message(self, subscription_type: SubscriptionType) -> str:
        """Форматирует сообщение об оплате используя централизованную конфигурацию."""
        price = SubscriptionConfig.get_price(subscription_type)
        duration = SubscriptionConfig.get_duration(subscription_type)
        description = SubscriptionConfig.get_description(subscription_type)

        return (
            f"💳 <b>Оформление подписки {description['name']}</b>\n\n"
            f"Стоимость: <b>{price}₽</b>\n"
            f"Период: <b>{duration.days} дней</b>\n"
            f"Возможности: {description['features']}\n\n"
            "Нажмите кнопку ниже для оплаты. После успешной оплаты подписка активируется автоматически!"
        )

    def _create_payment_keyboard(self, payment_url: str):
        """Создает клавиатуру для оплаты."""
        keyboard = [
            [InlineKeyboardButton("💳 Оплатить", url=payment_url)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="subscription_back")]
        ]
        return InlineKeyboardMarkup(keyboard)

    async def my_payments_handle(self, update: Update, context: CallbackContext) -> None:
        """Показывает статус pending платежей пользователя"""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        pending_payments = self.db.get_user_pending_payments(user_id)

        if not pending_payments:
            await update.message.reply_text(
                "У вас нет ожидающих платежей.",
                parse_mode=ParseMode.HTML
            )
            return

        text = "📋 <b>Ваши ожидающие платежи:</b>\n\n"

        for payment in pending_payments:
            amount = payment["amount"]
            payment_id = payment["payment_id"]
            status = payment["status"]
            created_at = payment["created_at"].strftime("%d.%m.%Y %H:%M")

            status_emoji = {
                "pending": "⏳",
                "waiting_for_capture": "🔄",
                "succeeded": "✅",
                "canceled": "❌"
            }.get(status, "❓")

            text += f"{status_emoji} <b>{amount} ₽</b> - {status}\n"
            text += f"   ID: <code>{payment_id}</code>\n"
            text += f"   Создан: {created_at}\n\n"

        text += "Платежи проверяются автоматически каждые 30 секунд."

        await update.message.reply_text(text, parse_mode=ParseMode.HTML)


class ImageHandlers(BotHandlers):
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

        except openai.error.InvalidRequestError as e:
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

        # Обновляем статистику использования
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


# Функции для работы с платежами
async def create_subscription_yookassa_payment(user_id: int, subscription_type: SubscriptionType,
                                               context: CallbackContext) -> str:
    """
    Создает платеж в Yookassa для подписки используя централизованную конфигурацию.
    """
    price = SubscriptionConfig.get_price(subscription_type)
    description_config = SubscriptionConfig.get_description(subscription_type)

    try:
        description = f"Подписка {description_config['name']}"
        payment = Payment.create({
            "amount": {"value": price, "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/gptducksbot"},
            "capture": True,
            "description": description,
            "receipt": {
                "customer": {
                    "email": "liliatchesnokova@gmail.com",
                },
                "items": [
                    {
                        "description": description,
                        "quantity": "1.00",
                        "amount": {
                            "value": price,
                            "currency": "RUB"
                        },
                        "vat_code": "1",
                        "payment_mode": "full_payment",
                        "payment_subject": "commodity",
                    },
                ]
            },
            "metadata": {"user_id": user_id, "subscription_type": subscription_type.value}
        })

        db.create_payment(
            user_id=user_id,
            payment_id=payment.id,
            amount=price,
            payment_type="subscription",
            description=description
        )

        return payment.confirmation.confirmation_url

    except Exception as e:
        logger.error(f"Error creating Yookassa subscription payment: {e}")
        raise e


async def process_successful_payment(payment_info: Any, user_id: int) -> None:
    """
    Обрабатывает успешный платеж используя централизованную конфигурацию.
    """
    try:
        metadata = payment_info.metadata
        subscription_type = metadata.get('subscription_type')

        logger.info(f"Processing successful payment {payment_info.id} for user {user_id}")

        if subscription_type:
            subscription_type_enum = SubscriptionType(subscription_type)
            duration_days = SubscriptionConfig.get_duration(subscription_type_enum).days

            db.add_subscription(user_id, subscription_type_enum, duration_days)
            await send_subscription_confirmation(user_id, subscription_type_enum)
            logger.info(f"Subscription activated for user {user_id}: {subscription_type}")

    except Exception as e:
        logger.error(f"Error processing successful payment: {e}")


async def send_subscription_confirmation(user_id: int, subscription_type: SubscriptionType) -> None:
    """
    Отправляет подтверждение об активации подписки.
    """
    user = db.user_collection.find_one({"_id": user_id})
    if user:
        chat_id = user["chat_id"]
        duration_days = SubscriptionConfig.get_duration(subscription_type).days

        message = (
            f"🎉 Подписка *{subscription_type.name.replace('_', ' ').title()}* активирована!\n"
            f"📅 Действует *{duration_days} дней*\n\n"
            "Теперь вы можете пользоваться ботом по подписке!"
        )

        await bot_instance.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')


# Вспомогательные функции
def split_text_into_chunks(text: str, chunk_size: int):
    """
    Разделяет текст на части заданного размера.
    """
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


def update_user_roles_from_config(database: database.Database, roles: Dict[str, List[int]]) -> None:
    """
    Обновляет роли пользователей из конфигурации.
    """
    for role, user_ids in roles.items():
        for user_id in user_ids:
            database.user_collection.update_one(
                {"_id": user_id},
                {"$set": {"role": role}}
            )
    logger.info("User roles updated from config.")


def configure_logging() -> None:
    """Настраивает логирование."""
    log_level = logging.DEBUG if config.enable_detailed_logging else logging.CRITICAL
    logging.basicConfig(
        level=log_level,
        format='%(asctime)s - %(levelname)s - %(name)s - %(message)s'
    )
    logger.setLevel(logging.getLogger().level)


# Инициализация и запуск бота
async def post_init(application: Application) -> None:
    """
    Функция инициализации после запуска бота.
    """
    commands = [
        BotCommand("/new", "Начать новый диалог 🆕"),
        BotCommand("/retry", "Перегенерировать предыдущий запрос 🔁"),
        BotCommand("/mode", "Выбрать режим"),
        BotCommand("/subscription", "Управление подписками 🔔"),
        BotCommand("/my_payments", "Мои платежи 📋"),
        BotCommand("/settings", "Настройки ⚙️"),
        BotCommand("/help", "Помощь ❓"),
    ]

    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())

    # Добавляем фоновую задачу для проверки платежей
    if config.yookassa_shop_id and config.yookassa_secret_key:
        application.job_queue.run_repeating(
            check_pending_payments_wrapper,
            interval=30,
            first=10
        )


async def check_pending_payments_wrapper(context: CallbackContext) -> None:
    """
    Обертка для проверки pending платежей.
    """
    try:
        await check_pending_payments()
    except Exception as e:
        logger.error(f"Error in payment checking job: {e}")


async def check_pending_payments() -> None:
    """Проверяет статус pending платежей."""
    try:
        pending_payments = db.get_pending_payments()
        for payment in pending_payments:
            payment_id = payment["payment_id"]
            user_id = payment["user_id"]

            try:
                payment_info = Payment.find_one(payment_id)
                status = payment_info.status
                db.update_payment_status(payment_id, status)

                if status == 'succeeded':
                    await process_successful_payment(payment_info, user_id)
                elif status == 'canceled':
                    logger.info(f"Payment {payment_id} was canceled")

            except Exception as e:
                logger.error(f"Error checking payment {payment_id}: {e}")

    except Exception as e:
        logger.error(f"Error in payment checking: {e}")


def run_bot() -> None:
    """Запускает бота."""
    global bot_instance

    # Инициализация Yookassa
    if config.yookassa_shop_id and config.yookassa_secret_key:
        Configuration.account_id = config.yookassa_shop_id
        Configuration.secret_key = config.yookassa_secret_key

    update_user_roles_from_config(db, config.roles)
    configure_logging()

    # Создаем application
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .build()
    )

    bot_instance = application.bot

    # Создаем обработчики
    subscription_handlers = SubscriptionHandlers(db)
    image_handlers = ImageHandlers(db)
    chat_mode_handlers = ChatModeHandlers(db)
    settings_handlers = SettingsHandlers(db)
    message_handlers = MessageHandlers(db, subscription_handlers, chat_mode_handlers)

    # Настраиваем фильтр пользователей
    user_filter = filters.ALL
    if config.allowed_telegram_usernames:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = (filters.User(username=usernames) |
                      filters.User(user_id=user_ids) |
                      filters.Chat(chat_id=group_ids))

    # Добавляем обработчики команд
    application.add_handler(CommandHandler("start", message_handlers.start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", message_handlers.help_handle, filters=user_filter))
    application.add_handler(CommandHandler("help_group_chat", message_handlers.help_group_chat_handle, filters=user_filter))
    application.add_handler(CommandHandler("retry", message_handlers.retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", message_handlers.new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", message_handlers.cancel_handle, filters=user_filter))
    application.add_handler(CommandHandler("mode", chat_mode_handlers.show_chat_modes_handle, filters=user_filter))
    application.add_handler(CommandHandler("settings", settings_handlers.settings_handle, filters=user_filter))
    application.add_handler(CommandHandler("my_payments", subscription_handlers.my_payments_handle, filters=user_filter))

    # Добавляем обработчики сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter,
                                         message_handlers.message_handle))
    application.add_handler(MessageHandler(filters.VOICE & user_filter,
                                         message_handlers.voice_message_handle))

    # Добавляем обработчики подписок
    application.add_handler(CommandHandler("subscription", subscription_handlers.subscription_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(subscription_handlers.subscription_callback_handle,
                                               pattern='^subscribe\\|'))
    application.add_handler(CallbackQueryHandler(subscription_handlers.subscription_handle,
                                               pattern='^subscription_back$'))

    # Добавляем обработчики режимов чата
    application.add_handler(CallbackQueryHandler(chat_mode_handlers.show_chat_modes_callback_handle,
                                               pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(chat_mode_handlers.set_chat_mode_handle,
                                               pattern="^set_chat_mode"))

    # Добавляем обработчики настроек
    application.add_handler(CallbackQueryHandler(settings_handlers.set_settings_handle, pattern="^set_settings"))
    application.add_handler(CallbackQueryHandler(settings_handlers.model_settings_handler, pattern='^model-'))
    application.add_handler(CallbackQueryHandler(settings_handlers.model_settings_handler, pattern='^claude-model-'))

    # Добавляем обработчик ошибок
    application.add_error_handler(error_handle)

    # Запускаем бота
    application.run_polling()


async def error_handle(update: Update, context: CallbackContext) -> None:
    """Обрабатывает ошибки бота."""
    logger.error("Exception while handling an update:", exc_info=context.error)

    try:
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)

        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # Отправляем сообщение об ошибке
        error_for_user = (
            f"An unexpected error occurred. "
            f"Please try again or contact support if the issue persists."
        )

        await context.bot.send_message(update.effective_chat.id, error_for_user)

    except Exception as handler_error:
        logger.error("Error in error handler: %s", handler_error)


if __name__ == "__main__":
    run_bot()
