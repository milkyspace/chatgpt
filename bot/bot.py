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
from typing import Optional, Dict, Any, List, Tuple, Union
from abc import ABC, abstractmethod
from PIL import Image

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
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class BaseHandler(ABC):
    """Базовый класс для всех обработчиков."""

    def __init__(self, database: database.Database):
        self.db = database

    async def register_user_if_not_exists(self, update: Update, context: CallbackContext, user: User) -> bool:
        """Регистрирует пользователя если он не существует."""
        user_registered_now = False

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

    async def is_previous_message_not_answered_yet(self, update: Update, context: CallbackContext) -> bool:
        """Проверяет, обрабатывается ли предыдущее сообщение."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id

        if user_semaphores[user_id].locked():
            text = "⏳ Пожалуйста, <b>подождите</b> ответ на предыдущее сообщение\nИли отмените его командой /cancel"
            await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
            return True
        return False

    async def subscription_preprocessor(self, update: Update, context: CallbackContext) -> bool:
        """Проверяет возможность выполнения запроса по подписке."""
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

        if not SubscriptionConfig.can_make_request(subscription_type, subscription_info["requests_used"]):
            description = SubscriptionConfig.get_description(subscription_type)
            await update.message.reply_text(
                f"❌ Лимит запросов подписки {description['name']} исчерпан. "
                "Пожалуйста, обновите подписку через /subscription",
                parse_mode=ParseMode.HTML
            )
            return False

        return True


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


class PhotoEditorMixin(BaseHandler):
    """Миксин для обработки фоторедактора."""

    async def photo_editor_handle(self, update: Update, context: CallbackContext,
                                  message: Optional[str] = None) -> None:
        """Обрабатывает запросы в режиме фоторедактора."""
        logger.info(
            f"Photo editor handle: photo={bool(update.message.photo)}, caption='{update.message.caption}', text='{update.message.text}'")

        await self.register_user_if_not_exists(update, context, update.message.from_user)

        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

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

        # Сохраняем фото
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)

        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)

        # Важно: устанавливаем правильное имя файла с расширением
        buf.name = "photo_to_edit.png"  # Изменяем на PNG для OpenAI
        buf.seek(0)

        # Конвертируем в PNG если нужно
        try:
            image = Image.open(buf)
            if image.format != 'PNG':
                # Конвертируем в PNG
                png_buf = io.BytesIO()
                image.save(png_buf, format='PNG')
                png_buf.name = "photo_to_edit.png"
                png_buf.seek(0)
                context.user_data['photo_to_edit'] = png_buf.getvalue()
            else:
                context.user_data['photo_to_edit'] = buf.getvalue()
        except ImportError:
            # Если PIL не установлен, используем оригинальный буфер
            logger.warning("PIL not available, using original image format")
            context.user_data['photo_to_edit'] = buf.getvalue()

        if edit_description:
            await self._perform_photo_editing(update, context, edit_description)
        else:
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
            "🎨 <i>Редактирую фото...</i>",
            parse_mode=ParseMode.HTML
        )

        try:
            photo_data = context.user_data['photo_to_edit']
            photo_buffer = io.BytesIO(photo_data)
            photo_buffer.name = "image.png"  # Обязательно .png для OpenAI

            logger.info(f"Starting photo editing with prompt: {edit_description}")

            edited_image_url = await openai_utils.edit_image(
                image=photo_buffer,
                prompt=edit_description,
                size="1024x1024"
            )

            if edited_image_url:
                logger.info("Photo editing successful")
                await self._send_edited_photo(update, context, edited_image_url,
                                              edit_description, placeholder_message)
                self._update_photo_editor_usage(user_id)
                self._cleanup_photo_context(context)
            else:
                logger.error("Photo editing returned no URL")
                await context.bot.edit_message_text(
                    "❌ Не удалось отредактировать фото. Попробуйте другое описание.",
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=ParseMode.HTML
                )

        except Exception as e:
            logger.error(f"Error in photo editing: {e}")
            error_message = self._get_user_friendly_error(e)

            await context.bot.edit_message_text(
                error_message,
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=ParseMode.HTML
            )


    def _get_user_friendly_error(self, error: Exception) -> str:
        """Возвращает понятное пользователю сообщение об ошибке."""
        error_str = str(error).lower()

        error_messages = {
            "unsupported mimetype": "❌ Формат изображения не поддерживается. Попробуйте другое фото (JPEG, PNG).",
            "invalid image": "❌ Формат изображения не поддерживается. Попробуйте другое фото (JPEG, PNG).",
            "safety system": "❌ Запрос не соответствует политикам безопасности OpenAI. Попробуйте другое описание.",
            "billing": "❌ Проблемы с биллингом OpenAI. Обратитесь к администратору.",
            "size": "❌ Изображение слишком большое. Попробуйте фото меньшего размера."
        }

        for key, message in error_messages.items():
            if key in error_str:
                return message

        return f"❌ Ошибка при редактировании фото: {str(error)}"

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

                await context.bot.edit_message_text(
                    f"✅ <b>Фото отредактировано!</b>\n\n"
                    f"<i>Запрос:</i> {edit_description}\n\n"
                    f"Как вам результат? 🎨",
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=ParseMode.HTML
                )

                await update.message.chat.send_photo(
                    photo=InputFile(image_buffer, "edited_image.png"),
                    caption=f"🎨 Отредактировано: {edit_description}"
                )
            else:
                await context.bot.edit_message_text(
                    "❌ Не удалось загрузить отредактированное изображение.",
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=ParseMode.HTML
                )

        except Exception as e:
            logger.error(f"Error sending edited photo: {e}")
            await context.bot.edit_message_text(
                "❌ Ошибка при отправке отредактированного фото.",
                chat_id=placeholder_message.chat_id,
                message_id=placeholder_message.message_id,
                parse_mode=ParseMode.HTML
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


class MessageHandlers(MessageProcessor, PhotoEditorMixin):
    """Класс для обработки сообщений."""

    def __init__(self, database: database.Database, subscription_handlers: Any,
                 chat_mode_handlers: Any, admin_handlers: Any, image_handlers: Any):
        # Инициализируем BaseHandler
        BaseHandler.__init__(self, database)
        self.subscription_handlers = subscription_handlers
        self.chat_mode_handlers = chat_mode_handlers
        self.admin_handlers = admin_handlers
        self.image_handlers = image_handlers

    async def photo_editor_handle(self, update: Update, context: CallbackContext,
                                  message: Optional[str] = None) -> None:
        """Прокси-метод для обработки фоторедактора."""
        # Вызываем метод миксина напрямую
        await PhotoEditorMixin.photo_editor_handle(self, update, context, message)

    async def generate_image_handle(self, update: Update, context: CallbackContext,
                                    message: Optional[str] = None) -> None:
        """Прокси-метод для генерации изображений."""
        await self.image_handlers.generate_image_handle(update, context, message=message)

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

    async def cancel_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /cancel."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if user_id in user_tasks:
            user_tasks[user_id].cancel()
        else:
            await update.message.reply_text("<i>Нечего отменять...</i>", parse_mode=ParseMode.HTML)

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

    async def _handle_invite(self, update: Update, context: CallbackContext) -> None:
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

    def _process_message_text(self, update: Update, context: CallbackContext, message: Optional[str]) -> str:
        """Обрабатывает текст сообщения."""
        _message = message or update.message.text

        if update.message.chat.type != "private":
            _message = _message.replace("@" + context.bot.username, "").strip()

        return _message

    async def photo_editor_handle(self, update: Update, context: CallbackContext,
                                  message: Optional[str] = None) -> None:
        """Прокси-метод для обработки фоторедактора."""
        await PhotoEditorMixin.photo_editor_handle(self, update, context, message)

    async def generate_image_handle(self, update: Update, context: CallbackContext,
                                    message: Optional[str] = None) -> None:
        """Прокси-метод для генерации изображений."""
        await self.image_handlers.generate_image_handle(update, context, message=message)

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


class ChatModeHandlers(BaseHandler):
    """Класс для обработки режимов чата."""

    def get_chat_mode_menu(self, page_index: int):
        """Создает меню выбора режима чата."""
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

        # Пагинация
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


class SubscriptionHandlers(BaseHandler):
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


class AdminHandlers(BaseHandler):
    """Класс для обработки админ-панели."""

    async def admin_panel_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду админ-панели."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к админ-панели.")
            return

        await self._show_admin_panel(update, context)

    def _is_admin(self, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором."""
        return str(user_id) in config.roles.get('admin', [])

    async def _show_admin_panel(self, update: Update, context: CallbackContext) -> None:
        """Показывает админ-панель."""
        text = "🛠️ <b>Админ-панель</b>\n\nВыберите действие:"
        reply_markup = BotKeyboards.get_admin_keyboard()

        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def show_users_handle(self, update: Update, context: CallbackContext) -> None:
        """Показывает список пользователей."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        users = self.db.get_users_and_roles()

        if not users:
            await update.message.reply_text("📝 Пользователей не найдено.")
            return

        text = "👥 <b>Список пользователей:</b>\n\n"
        for i, user in enumerate(users[:50], 1):
            username = user.get('username', 'Нет username')
            first_name = user.get('first_name', 'Нет имени')
            role = user.get('role', 'Не указана')
            last_interaction = user.get('last_interaction', 'Неизвестно')

            if isinstance(last_interaction, datetime):
                last_interaction = last_interaction.strftime("%d.%m.%Y %H:%M")

            text += f"{i}. ID: {user['_id']}\n"
            text += f"   👤: {first_name} (@{username})\n"
            text += f"   🏷️: {role}\n"
            text += f"   ⏰: {last_interaction}\n\n"

        if len(users) > 50:
            text += f"\n... и еще {len(users) - 50} пользователей"

        reply_markup = BotKeyboards.get_back_to_admin_keyboard()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def edit_user_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает запрос на редактирование пользователя."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        text = (
            "✏️ <b>Редактирование пользователя</b>\n\n"
            "Для редактирования пользователя отправьте команду в формате:\n"
            "<code>/edit_user USER_ID ROLE</code>\n\n"
            "Пример:\n"
            "<code>/edit_user 123456789 admin</code>\n\n"
            "Доступные роли: admin, beta_tester, friend, regular_user, trial_user"
        )

        reply_markup = BotKeyboards.get_back_to_admin_keyboard()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def broadcast_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает запрос на рассылку."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        text = (
            "📢 <b>Рассылка сообщений</b>\n\n"
            "Для отправки рассылки отправьте команду в формате:\n"
            "<code>/broadcast ТЕКСТ_СООБЩЕНИЯ</code>\n\n"
            "Пример:\n"
            "<code>/broadcast Всем привет! Это тестовая рассылка.</code>"
        )

        reply_markup = BotKeyboards.get_back_to_admin_keyboard()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def handle_main_menu_back(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает возврат в главное меню из админ-панели."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        reply_markup = await BotKeyboards.get_main_keyboard(user_id)
        await update.message.reply_text(
            "Возврат в главное меню...",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )

    async def handle_admin_panel_back(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает возврат в админ-панель."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к админ-панели.")
            return

        await self._show_admin_panel(update, context)

    async def edit_user_command(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /edit_user."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "❌ Неправильный формат команды.\n"
                "Используйте: /edit_user USER_ID ROLE\n"
                "Пример: /edit_user 123456789 admin"
            )
            return

        try:
            target_user_id = int(context.args[0])
            new_role = context.args[1]

            if not self.db.check_if_user_exists(target_user_id):
                await update.message.reply_text(f"❌ Пользователь с ID {target_user_id} не найден.")
                return

            valid_roles = ['admin', 'beta_tester', 'friend', 'regular_user', 'trial_user']
            if new_role not in valid_roles:
                await update.message.reply_text(
                    f"❌ Неверная роль. Допустимые роли: {', '.join(valid_roles)}"
                )
                return

            self.db.set_user_attribute(target_user_id, "role", new_role)

            await update.message.reply_text(
                f"✅ Роль пользователя {target_user_id} успешно изменена на '{new_role}'"
            )

        except ValueError:
            await update.message.reply_text("❌ ID пользователя должен быть числом.")
        except Exception as e:
            logger.error(f"Error editing user: {e}")
            await update.message.reply_text("❌ Произошла ошибка при изменении роли пользователя.")

    async def broadcast_command(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /broadcast."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        if not context.args:
            await update.message.reply_text(
                "❌ Неправильный формат команды.\n"
                "Используйте: /broadcast ТЕКСТ_СООБЩЕНИЯ\n"
                "Пример: /broadcast Всем привет! Это тестовая рассылка."
            )
            return

        message_text = ' '.join(context.args)

        confirmation_text = (
            f"📢 <b>Подтверждение рассылки</b>\n\n"
            f"Текст сообщения:\n{message_text}\n\n"
            f"Отправить это сообщение всем пользователям?"
        )

        keyboard = [
            [
                InlineKeyboardButton("✅ Да, отправить", callback_data=f"confirm_broadcast|{message_text}"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_broadcast")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(confirmation_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def broadcast_confirmation_handler(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает подтверждение рассылки."""
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        if not self._is_admin(user_id):
            await query.edit_message_text("❌ У вас нет доступа к этой команде.")
            return

        data = query.data

        if data == "cancel_broadcast":
            await query.edit_message_text("❌ Рассылка отменена.")
            return

        if data.startswith("confirm_broadcast|"):
            message_text = data.split("|", 1)[1]

            await query.edit_message_text("🔄 Начинаю рассылку...")

            all_user_ids = self.db.get_all_user_ids()
            success_count = 0
            fail_count = 0

            for target_user_id in all_user_ids:
                try:
                    user_data = self.db.get_user_by_id(target_user_id)
                    if user_data and 'chat_id' in user_data:
                        await context.bot.send_message(
                            chat_id=user_data['chat_id'],
                            text=f"📢 <b>Рассылка от администратора:</b>\n\n{message_text}",
                            parse_mode=ParseMode.HTML
                        )
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    logger.error(f"Error sending broadcast to {target_user_id}: {e}")
                    fail_count += 1

                await asyncio.sleep(0.1)

            result_text = (
                f"✅ <b>Рассылка завершена</b>\n\n"
                f"✅ Успешно: {success_count}\n"
                f"❌ Не удалось: {fail_count}\n"
                f"📊 Всего: {len(all_user_ids)}"
            )

            await query.edit_message_text(result_text, parse_mode=ParseMode.HTML)

    async def get_user_data_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает запрос на получение данных пользователя."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        text = (
            "👤 <b>Получение данных пользователя</b>\n\n"
            "Для получения данных отправьте команду в формате:\n"
            "<code>/user_data USER_ID</code>\n\n"
            "Пример:\n"
            "<code>/user_data 123456789</code>\n\n"
            "Или отправьте username пользователя:\n"
            "<code>/user_data @username</code>"
        )

        reply_markup = BotKeyboards.get_back_to_admin_keyboard()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def get_user_data_command(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /user_data."""
        try:
            user = self._get_user_from_update(update)
            await self.register_user_if_not_exists(update, context, user)
            user_id = user.id

            if not self._is_admin(user_id):
                await self._send_reply(update, "❌ У вас нет доступа к этой команде.")
                return

            if not context.args:
                error_text = (
                    "❌ Неправильный формат команды.\n"
                    "Используйте: /user_data USER_ID\n"
                    "Пример: /user_data 123456789"
                )
                await self._send_reply(update, error_text)
                return

            user_identifier = context.args[0]
            target_user = self._find_user_by_identifier(user_identifier)

            if not target_user:
                await self._send_reply(update, f"❌ Пользователь '{user_identifier}' не найден.")
                return

            user_info = await self._format_user_details(target_user)
            await self._send_reply(update, user_info)

        except Exception as e:
            logger.error(f"Error getting user data: {e}")
            error_text = "❌ Произошла ошибка при получении данных пользователя."
            await self._send_reply(update, error_text)

    def _get_user_from_update(self, update: Update):
        """Получает пользователя из update."""
        if update.message:
            return update.message.from_user
        elif update.callback_query:
            return update.callback_query.from_user
        return None

    def _find_user_by_identifier(self, user_identifier: str) -> Optional[Dict[str, Any]]:
        """Находит пользователя по ID или username."""
        if user_identifier.startswith('@'):
            username = user_identifier[1:]
            return self.db.find_user_by_username(username)
        else:
            try:
                target_user_id = int(user_identifier)
                return self.db.get_user_by_id(target_user_id)
            except ValueError:
                return None

    async def _send_reply(self, update: Update, text: str, parse_mode: str = ParseMode.HTML) -> None:
        """Вспомогательный метод для отправки ответа."""
        try:
            if update.message:
                await update.message.reply_text(text, parse_mode=parse_mode)
            elif update.callback_query:
                await update.callback_query.message.reply_text(text, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Error sending reply: {e}")

    async def _format_user_details(self, user_data: Dict[str, Any]) -> str:
        """Форматирует подробную информацию о пользователе."""
        user_id = user_data['_id']

        text = f"👤 <b>Данные пользователя</b>\n\n"
        text += f"<b>ID:</b> <code>{user_id}</code>\n"
        text += f"<b>Username:</b> @{user_data.get('username', 'не указан')}\n"
        text += f"<b>Имя:</b> {user_data.get('first_name', 'не указано')}\n"
        text += f"<b>Фамилия:</b> {user_data.get('last_name', 'не указана')}\n"
        text += f"<b>Chat ID:</b> <code>{user_data.get('chat_id', 'не указан')}</code>\n"
        text += f"<b>Роль:</b> {user_data.get('role', 'не указана')}\n\n"

        # Информация о подписке
        subscription_info = self.db.get_user_subscription_info(user_id)
        if subscription_info["is_active"]:
            expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y %H:%M")
            text += f"<b>Подписка:</b> {subscription_info['type']}\n"
            text += f"<b>Действует до:</b> {expires_at}\n"
            text += f"<b>Запросов использовано:</b> {subscription_info['requests_used']}\n"
            text += f"<b>Изображений использовано:</b> {subscription_info['images_used']}\n\n"
        else:
            text += "<b>Подписка:</b> не активна\n\n"

        # Статистика использования
        text += "<b>Статистика использования:</b>\n"

        n_used_tokens = user_data.get('n_used_tokens', {})
        if n_used_tokens:
            for model, tokens in n_used_tokens.items():
                input_tokens = tokens.get('n_input_tokens', 0)
                output_tokens = tokens.get('n_output_tokens', 0)
                text += f"  {model}: {input_tokens} ввод / {output_tokens} вывод\n"
        else:
            text += "  Токены: не использовались\n"

        n_generated_images = user_data.get('n_generated_images', 0)
        text += f"  Сгенерировано изображений: {n_generated_images}\n"

        n_transcribed_seconds = user_data.get('n_transcribed_seconds', 0)
        text += f"  Расшифровано аудио: {n_transcribed_seconds} сек.\n\n"

        # Финансовая информация
        financials = self.db.get_user_financials(user_id)
        text += "<b>Финансовая информация:</b>\n"
        text += f"  Баланс RUB: {user_data.get('rub_balance', 0)}₽\n"
        text += f"  Баланс EUR: {user_data.get('euro_balance', 0)}€\n"
        text += f"  Всего пополнено: {financials.get('total_topup', 0)}₽\n"
        text += f"  Всего потрачено: {user_data.get('total_spent', 0)}₽\n"
        text += f"  Пожертвовано: {financials.get('total_donated', 0)}₽\n\n"

        # Информация о активности
        first_seen = user_data.get('first_seen', 'неизвестно')
        last_interaction = user_data.get('last_interaction', 'неизвестно')

        if isinstance(first_seen, datetime):
            first_seen = first_seen.strftime("%d.%m.%Y %H:%M")
        if isinstance(last_interaction, datetime):
            last_interaction = last_interaction.strftime("%d.%m.%Y %H:%M")

        text += f"<b>Первое посещение:</b> {first_seen}\n"
        text += f"<b>Последняя активность:</b> {last_interaction}\n"

        current_model = user_data.get('current_model', 'не указана')
        current_chat_mode = user_data.get('current_chat_mode', 'не указан')
        text += f"<b>Текущая модель:</b> {current_model}\n"
        text += f"<b>Режим чата:</b> {current_chat_mode}\n"

        return text


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
    """Разделяет текст на части заданного размера."""
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


def update_user_roles_from_config(database: database.Database, roles: Dict[str, List[int]]) -> None:
    """Обновляет роли пользователей из конфигурации."""
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
    """Функция инициализации после запуска бота."""
    commands = [
        BotCommand("/new", "Начать новый диалог 🆕"),
        BotCommand("/retry", "Перегенерировать предыдущий запрос 🔁"),
        BotCommand("/mode", "Выбрать режим"),
        BotCommand("/subscription", "Управление подписками 🔔"),
        BotCommand("/my_payments", "Мои платежи 📋"),
        BotCommand("/help", "Помощь ❓"),
    ]

    await application.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())

    if config.yookassa_shop_id and config.yookassa_secret_key:
        application.job_queue.run_repeating(
            check_pending_payments_wrapper,
            interval=30,
            first=10
        )


async def check_pending_payments_wrapper(context: CallbackContext) -> None:
    """Обертка для проверки pending платежей."""
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

    if config.yookassa_shop_id and config.yookassa_secret_key:
        Configuration.account_id = config.yookassa_shop_id
        Configuration.secret_key = config.yookassa_secret_key

    update_user_roles_from_config(db, config.roles)
    configure_logging()

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

    subscription_handlers = SubscriptionHandlers(db)
    image_handlers = ImageHandlers(db)
    chat_mode_handlers = ChatModeHandlers(db)
    admin_handlers = AdminHandlers(db)
    message_handlers = MessageHandlers(db, subscription_handlers, chat_mode_handlers, admin_handlers, image_handlers)

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
    application.add_handler(
        CommandHandler("help_group_chat", message_handlers.help_group_chat_handle, filters=user_filter))
    application.add_handler(CommandHandler("retry", message_handlers.retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", message_handlers.new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", message_handlers.cancel_handle, filters=user_filter))
    application.add_handler(CommandHandler("mode", chat_mode_handlers.show_chat_modes_handle, filters=user_filter))
    application.add_handler(
        CommandHandler("my_payments", subscription_handlers.my_payments_handle, filters=user_filter))

    # Добавляем обработчики команд админ-панели
    application.add_handler(CommandHandler("edit_user", admin_handlers.edit_user_command, filters=user_filter))
    application.add_handler(CommandHandler("broadcast", admin_handlers.broadcast_command, filters=user_filter))
    application.add_handler(CommandHandler("user_data", admin_handlers.get_user_data_command, filters=user_filter))

    # Добавляем обработчики сообщений
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter,
                                           message_handlers.message_handle))
    application.add_handler(MessageHandler(filters.VOICE & user_filter,
                                           message_handlers.voice_message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & user_filter,
                                           message_handlers.photo_message_handle))
    application.add_handler(MessageHandler(filters.Document.IMAGE & user_filter,
                                           message_handlers.photo_message_handle))

    # Добавляем обработчики подписок
    application.add_handler(
        CommandHandler("subscription", subscription_handlers.subscription_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(subscription_handlers.subscription_callback_handle,
                                                 pattern='^subscribe\\|'))
    application.add_handler(CallbackQueryHandler(subscription_handlers.subscription_handle,
                                                 pattern='^subscription_back$'))

    # Добавляем обработчики режимов чата
    application.add_handler(CallbackQueryHandler(chat_mode_handlers.show_chat_modes_callback_handle,
                                                 pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(chat_mode_handlers.set_chat_mode_handle,
                                                 pattern="^set_chat_mode"))

    # Добавляем обработчики админ-панели (callback)
    application.add_handler(CallbackQueryHandler(admin_handlers.broadcast_confirmation_handler,
                                                 pattern="^confirm_broadcast\\|"))
    application.add_handler(CallbackQueryHandler(admin_handlers.broadcast_confirmation_handler,
                                                 pattern="^cancel_broadcast"))

    # Добавляем обработчик ошибок
    application.add_error_handler(error_handle)

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

        error_for_user = (
            f"An unexpected error occurred. "
            f"Please try again or contact support if the issue persists."
        )

        await context.bot.send_message(update.effective_chat.id, error_for_user)

    except Exception as handler_error:
        logger.error("Error in error handler: %s", handler_error)


if __name__ == "__main__":
    run_bot()
