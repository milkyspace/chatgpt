import logging
from datetime import datetime

import telegram
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import CallbackContext

import config
from base_handler import BaseHandler

logger = logging.getLogger(__name__)


class SettingsHandlers(BaseHandler):
    """Класс для обработки настроек."""

    def get_settings_menu(self, user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        """Создает меню настроек."""
        text = "⚙️ Настройки:"
        keyboard = [
            [InlineKeyboardButton("🧠 Модель нейросети", callback_data='model-ai_model')],
            [InlineKeyboardButton("🎨 Модель художника", callback_data='model-artist_model')]
        ]
        return text, InlineKeyboardMarkup(keyboard)

    async def _check_access(self, update: Update, user_id: int) -> bool:
        """Проверяет права доступа пользователя."""
        if str(user_id) not in config.roles.get('admin', []):
            await update.message.reply_text("❌ У вас нет доступа к админ-панели.")
            return False
        return True

    async def settings_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /settings."""
        await self.register_user_if_not_exists(update, context, update.message.from_user)

        if await self.is_previous_message_not_answered_yet(update, context):
            return

        user_id = update.message.from_user.id

        if not await self._check_access(update, user_id):
            return

        self.db.set_user_attribute(user_id, "last_interaction", datetime.now())
        text, reply_markup = self.get_settings_menu(user_id)
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    def _create_model_buttons(self, available_models: list, current_model: str, prefix: str = "model") -> tuple[
        list, list]:
        """Создает кнопки для выбора модели."""
        claude_buttons = []
        other_buttons = []

        for model_key in available_models:
            title = config.models["info"][model_key]["name"]
            if model_key == current_model:
                title = "✅ " + title

            callback_data = f"{prefix}-set_settings|{model_key}"
            button = InlineKeyboardButton(title, callback_data=callback_data)

            if "claude" in model_key.lower():
                claude_buttons.append(button)
            else:
                other_buttons.append(button)

        return other_buttons, claude_buttons

    def _format_model_info(self, model_key: str) -> str:
        """Форматирует информацию о модели."""
        model_info = config.models["info"][model_key]
        description = model_info["description"]
        scores = model_info["scores"]

        details_text = f"{description}\n\n"
        for score_key, score_value in scores.items():
            details_text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

        details_text += "\nВыберите <b>модель</b>:"
        return details_text

    async def display_model_info(self, query, user_id, context):
        """Отображает информацию о модели."""
        current_model = self.db.get_user_attribute(user_id, "current_model")
        details_text = self._format_model_info(current_model)

        other_buttons, claude_buttons = self._create_model_buttons(
            config.models["available_text_models"], current_model
        )

        # Разделяем кнопки на два ряда
        half_size = len(other_buttons) // 2
        first_row = other_buttons[:half_size]
        second_row = other_buttons[half_size:]

        back_button = [InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')]
        reply_markup = InlineKeyboardMarkup([first_row, second_row, claude_buttons, back_button])

        try:
            await query.edit_message_text(
                text=details_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e

    async def _handle_model_selection(self, query, user_id: int, model_key: str, context: CallbackContext) -> None:
        """Обрабатывает выбор модели."""
        if "claude" in model_key.lower() and not config.anthropic_api_key:
            await context.bot.send_message(
                chat_id=user_id,
                text="This bot does not have the Anthropic models available :(",
                parse_mode='Markdown'
            )
            return

        self.db.set_user_attribute(user_id, "current_model", model_key)
        await self.display_model_info(query, user_id, context)

    async def _handle_artist_model_selection(self, query, user_id: int, model_key: str) -> None:
        """Обрабатывает выбор модели художника."""
        preferences = self.db.get_user_attribute(user_id, "image_preferences")
        preferences["model"] = model_key

        # Сбрасываем настройки при смене модели
        if model_key == "dalle-2":
            preferences["quality"] = "standard"
        elif model_key == "dalle-3":
            preferences["n_images"] = 1
        preferences["resolution"] = "1024x1024"

        self.db.set_user_attribute(user_id, "image_preferences", preferences)
        await self.artist_model_settings_handler(query, user_id)

    async def _update_artist_preference(self, user_id: int, preference_key: str, value: str) -> None:
        """Обновляет настройки художника."""
        preferences = self.db.get_user_attribute(user_id, "image_preferences")
        # Преобразуем значение к правильному типу
        if preference_key == "n_images":
            value = int(value)
        preferences[preference_key] = value
        self.db.set_user_attribute(user_id, "image_preferences", preferences)

    async def model_settings_handler(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает настройки моделей."""
        query = update.callback_query
        await query.answer()

        data = query.data
        user_id = query.from_user.id

        if data == 'model-ai_model':
            await self._handle_ai_model_settings(query, user_id)
        elif data.startswith(('claude-model-set_settings|', 'model-set_settings|')):
            _, model_key = data.split("|")
            await self._handle_model_selection(query, user_id, model_key, context)
        elif data == 'model-artist_model':
            await self.artist_model_settings_handler(query, user_id)
        elif data.startswith('model-artist-set_model|'):
            _, model_key = data.split("|")
            await self._handle_artist_model_selection(query, user_id, model_key)
        elif data.startswith("model-artist-set_"):
            await self._handle_artist_preference_update(query, user_id, data)
        elif data == 'model-back_to_settings':
            text, reply_markup = self.get_settings_menu(user_id)
            await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def _handle_ai_model_settings(self, query, user_id: int) -> None:
        """Обрабатывает настройки AI модели."""
        current_model = self.db.get_user_attribute(user_id, "current_model")
        text = self._format_model_info(current_model)

        other_buttons, claude_buttons = self._create_model_buttons(
            config.models["available_text_models"], current_model
        )

        half_size = len(other_buttons) // 2
        first_row = other_buttons[:half_size]
        second_row = other_buttons[half_size:]
        back_button = [InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')]

        reply_markup = InlineKeyboardMarkup([first_row, second_row, claude_buttons, back_button])
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def _handle_artist_preference_update(self, query, user_id: int, data: str) -> None:
        """Обрабатывает обновление настроек художника."""
        _, action_data = data.split("|")

        if data.startswith("model-artist-set_images|"):
            await self._update_artist_preference(user_id, "n_images", action_data)
        elif data.startswith("model-artist-set_resolution|"):
            await self._update_artist_preference(user_id, "resolution", action_data)
        elif data.startswith("model-artist-set_quality|"):
            await self._update_artist_preference(user_id, "quality", action_data)

        await self.artist_model_settings_handler(query, user_id)

    def _create_artist_buttons(self, user_id: int) -> list[list[InlineKeyboardButton]]:
        """Создает кнопки для выбора модели художника."""
        current_preferences = self.db.get_user_attribute(user_id, "image_preferences")
        current_model = current_preferences.get("model", "dalle-2")

        buttons = []
        for model_key in config.models["available_image_models"]:
            title = config.models["info"][model_key]["name"]
            if model_key == current_model:
                title = "✅ " + title
            buttons.append(InlineKeyboardButton(title, callback_data=f"model-artist-set_model|{model_key}"))

        keyboard = [buttons]

        # Добавляем специфичные настройки для каждой модели
        if current_model == "dalle-2":
            n_images = current_preferences.get("n_images", 1)
            images_buttons = [
                InlineKeyboardButton(
                    f"✅ {i} изображение" if i == n_images and i == 1 else
                    f"✅ {i} изображения" if i == n_images else
                    f"{i} изображение" if i == 1 else f"{i} изображения",
                    callback_data=f"model-artist-set_images|{i}"
                ) for i in range(1, 4)
            ]
            current_resolution = current_preferences.get("resolution", "1024x1024")
            resolution_buttons = [
                InlineKeyboardButton(
                    f"✅ {res_key}" if res_key == current_resolution else res_key,
                    callback_data=f"model-artist-set_resolution|{res_key}"
                ) for res_key in config.models["info"]["dalle-2"]["resolutions"]
            ]
            keyboard.extend([images_buttons, resolution_buttons])

        elif current_model == "dalle-3":
            current_quality = current_preferences.get("quality", "standard")
            quality_buttons = [
                InlineKeyboardButton(
                    f"✅ {quality_key}" if quality_key == current_quality else quality_key,
                    callback_data=f"model-artist-set_quality|{quality_key}"
                ) for quality_key in config.models["info"]["dalle-3"]["qualities"]
            ]
            current_resolution = current_preferences.get("resolution", "1024x1024")
            resolution_buttons = [
                InlineKeyboardButton(
                    f"✅ {res_key}" if res_key == current_resolution else res_key,
                    callback_data=f"model-artist-set_resolution|{res_key}"
                ) for res_key in config.models["info"]["dalle-3"]["qualities"][current_quality]["resolutions"]
            ]
            keyboard.extend([quality_buttons, resolution_buttons])

        keyboard.append([InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')])
        return keyboard

    async def artist_model_settings_handler(self, query, user_id: int) -> None:
        """Обрабатывает настройки модели художника."""
        current_preferences = self.db.get_user_attribute(user_id, "image_preferences")
        current_model = current_preferences.get("model", "dalle-2")

        details_text = self._format_model_info(current_model)

        # Добавляем пояснение для конкретной модели
        if current_model == "dalle-2":
            details_text += "\nДля этой модели выберите количество изображений и разрешение:"
        elif current_model == "dalle-3":
            details_text += "\nДля этой модели выберите качество изображений и разрешение:"

        keyboard = self._create_artist_buttons(user_id)
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.edit_message_text(
                text=details_text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" not in str(e):
                raise e