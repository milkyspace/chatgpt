"""
Обработчики сообщений для Telegram бота.
"""

import logging
import asyncio
import io
import base64
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import requests
import emoji
import telegram
from telegram import (
    Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import CallbackContext
from telegram.constants import ParseMode

import config
import openai_utils
from keyboards import BotKeyboards
from .base_handler import BaseHandler
from .message_processor import MessageProcessor
from .photo_editor_mixin import PhotoEditorMixin

logger = logging.getLogger(__name__)


class MessageHandlers(MessageProcessor, PhotoEditorMixin):
    """Класс для обработки сообщений."""

    def __init__(self, database, subscription_handlers, chat_mode_handlers, admin_handlers, image_handlers):
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

    async def cancel_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /cancel."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)
        user_id = update.message.from_user.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())

        if user_id in user_tasks:
            user_tasks[user_id].cancel()
        else:
            await update.message.reply_text("<i>Нечего отменять...</i>", parse_mode=ParseMode.HTML)

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