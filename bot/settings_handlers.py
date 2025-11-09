from typing import Tuple
import telegram

import logging
import asyncio

from typing import Dict
from telegram import (Update, InlineKeyboardButton, InlineKeyboardMarkup,)
from telegram.ext import (CallbackContext)
from telegram.constants import ParseMode

import config
import database
import bot.base_handler as BaseHandler

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class SettingsHandlers(BaseHandler):
    """Класс для обработки настроек."""

    def get_settings_menu(self, user_id: int) -> Tuple[str, InlineKeyboardMarkup]:
        """Создает меню настроек."""
        text = "⚙️ Настройки:"

        keyboard = [
            [InlineKeyboardButton("🧠 Модель нейросети", callback_data='model-ai_model')],
            [InlineKeyboardButton("🎨 Модель художника", callback_data='model-artist_model')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        return text, reply_markup

    async def settings_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /settings."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if await self.is_previous_message_not_answered_yet(update, context):
            return

        # Проверяем права
        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к настройкам.")
            return

        text, reply_markup = self.get_settings_menu(user_id)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    def _is_admin(self, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором."""
        return str(user_id) in config.roles.get('admin', [])

    async def set_settings_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает выбор настроек."""
        user_id = await self.ensure_user_initialized(update.callback_query, context, update.callback_query.from_user)

        query = update.callback_query
        await query.answer()

        _, model_key = query.data.split("|")
        self.db.set_user_attribute(user_id, "current_model", model_key)

        await self.display_model_info(query, user_id, context)

    async def display_model_info(self, query: telegram.CallbackQuery, user_id: int, context: CallbackContext) -> None:
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
            await self._handle_ai_model_settings(query, user_id)
        elif data.startswith('claude-model-set_settings|'):
            await self._handle_claude_model_settings(query, user_id, data, context)
        elif data.startswith('model-set_settings|'):
            await self._handle_model_settings(query, user_id, data, context)
        elif data == 'model-artist_model':
            await self.artist_model_settings_handler(query, user_id)
        elif data.startswith('model-artist-set_model|'):
            await self._handle_artist_model_settings(query, user_id, data)
        elif data.startswith('model-artist-set_images|'):
            await self._handle_artist_images_settings(query, user_id, data)
        elif data.startswith('model-artist-set_resolution|'):
            await self._handle_artist_resolution_settings(query, user_id, data)
        elif data.startswith('model-artist-set_quality|'):
            await self._handle_artist_quality_settings(query, user_id, data)
        elif data == 'model-back_to_settings':
            await self._handle_back_to_settings(query, user_id)

    async def _handle_ai_model_settings(self, query: telegram.CallbackQuery, user_id: int) -> None:
        """Обрабатывает настройки AI модели."""
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

    async def _handle_claude_model_settings(self, query: telegram.CallbackQuery, user_id: int,
                                            data: str, context: CallbackContext) -> None:
        """Обрабатывает настройки Claude модели."""
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

    async def _handle_model_settings(self, query: telegram.CallbackQuery, user_id: int,
                                     data: str, context: CallbackContext) -> None:
        """Обрабатывает настройки обычных моделей."""
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

    async def _handle_artist_model_settings(self, query: telegram.CallbackQuery, user_id: int, data: str) -> None:
        """Обрабатывает настройки модели художника."""
        _, model_key = data.split("|")
        preferences = self.db.get_user_attribute(user_id, "image_preferences")
        preferences["model"] = model_key
        self.db.set_user_attribute(user_id, "image_preferences", preferences)
        await self.artist_model_settings_handler(query, user_id)

    async def _handle_artist_images_settings(self, query: telegram.CallbackQuery, user_id: int, data: str) -> None:
        """Обрабатывает настройки количества изображений."""
        _, n_images = data.split("|")
        preferences = self.db.get_user_attribute(user_id, "image_preferences")
        preferences["n_images"] = int(n_images)
        self.db.set_user_attribute(user_id, "image_preferences", preferences)
        await self.artist_model_settings_handler(query, user_id)

    async def _handle_artist_resolution_settings(self, query: telegram.CallbackQuery, user_id: int, data: str) -> None:
        """Обрабатывает настройки разрешения изображений."""
        _, resolution = data.split("|")
        preferences = self.db.get_user_attribute(user_id, "image_preferences")
        preferences["resolution"] = resolution
        self.db.set_user_attribute(user_id, "image_preferences", preferences)
        await self.artist_model_settings_handler(query, user_id)

    async def _handle_artist_quality_settings(self, query: telegram.CallbackQuery, user_id: int, data: str) -> None:
        """Обрабатывает настройки качества изображений."""
        _, quality = data.split("|")
        preferences = self.db.get_user_attribute(user_id, "image_preferences")
        preferences["quality"] = quality
        self.db.set_user_attribute(user_id, "image_preferences", preferences)
        await self.artist_model_settings_handler(query, user_id)

    async def _handle_back_to_settings(self, query: telegram.CallbackQuery, user_id: int) -> None:
        """Обрабатывает возврат к настройкам."""
        text, reply_markup = self.get_settings_menu(user_id)
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def artist_model_settings_handler(self, query: telegram.CallbackQuery, user_id: int) -> None:
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