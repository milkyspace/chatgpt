import logging
from datetime import datetime

import telegram
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackContext
)

import config
from base_handler import BaseHandler

# Настройка логирования
logger = logging.getLogger(__name__)


class ChatModeHandlers(BaseHandler):
    """Класс для обработки режимов чата."""

    @staticmethod
    def get_chat_mode_menu(page_index: int):
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
