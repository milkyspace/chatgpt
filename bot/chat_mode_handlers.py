import logging
from datetime import datetime

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

import config
from base_handler import BaseHandler

logger = logging.getLogger(__name__)


class ChatModeHandlers(BaseHandler):
    """Класс для обработки режимов чата."""

    @staticmethod
    def get_chat_mode_menu(page_index: int) -> tuple[str, InlineKeyboardMarkup]:
        """Создает меню выбора режима чата."""
        n_chat_modes_per_page = config.n_chat_modes_per_page
        chat_mode_keys = list(config.chat_modes.keys())
        total_modes = len(chat_mode_keys)

        text = f"Выберите <b>режим чата</b> (Доступно {total_modes} режимов):"

        # Получаем режимы для текущей страницы
        start_idx = page_index * n_chat_modes_per_page
        end_idx = start_idx + n_chat_modes_per_page
        page_chat_mode_keys = chat_mode_keys[start_idx:end_idx]

        # Создаем кнопки режимов (по 2 в строке)
        keyboard = [
            [InlineKeyboardButton(config.chat_modes[key]["name"],
                                  callback_data=f"set_chat_mode|{key}")
             for key in page_chat_mode_keys[i:i + 2]]
            for i in range(0, len(page_chat_mode_keys), 2)
        ]

        # Добавляем пагинацию если нужно
        if total_modes > n_chat_modes_per_page:
            is_first_page = (page_index == 0)
            is_last_page = (end_idx >= total_modes)

            pagination_buttons = []
            if not is_first_page:
                pagination_buttons.append(
                    InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}")
                )
            if not is_last_page:
                pagination_buttons.append(
                    InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}")
                )

            if pagination_buttons:
                keyboard.append(pagination_buttons)

        return text, InlineKeyboardMarkup(keyboard)

    async def _process_user_interaction(self, update: Update, context: CallbackContext, from_callback: bool = False):
        """Общая логика обработки пользовательского взаимодействия."""
        user_data = update.callback_query.from_user if from_callback else update.message.from_user
        await self.register_user_if_not_exists(update, context, user_data)

        if not from_callback and await self.is_previous_message_not_answered_yet(update, context):
            return False

        user_id = user_data.id
        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())
        return True

    async def show_chat_modes_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /mode."""
        if not await self._process_user_interaction(update, context, from_callback=False):
            return

        text, reply_markup = self.get_chat_mode_menu(0)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def show_chat_modes_callback_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает callback пагинации режимов чата."""
        if not await self._process_user_interaction(update, context, from_callback=True):
            return

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
                raise e

    async def set_chat_mode_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает выбор режима чата."""
        await self._process_user_interaction(update, context, from_callback=True)

        query = update.callback_query
        await query.answer()

        chat_mode = query.data.split("|")[1]
        user_id = query.from_user.id

        self.db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
        self.db.start_new_dialog(user_id)

        welcome_message = config.chat_modes[chat_mode]["welcome_message"]
        await context.bot.send_message(
            query.message.chat.id,
            welcome_message,
            parse_mode=ParseMode.HTML
        )