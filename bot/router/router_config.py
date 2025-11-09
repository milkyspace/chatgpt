import logging
import asyncio
from typing import Dict
from telegram.ext import (filters)
import bot.database as database

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class RouterConfig:
    """Конфигурация маршрутизации обработчиков."""

    COMMAND_HANDLERS = {
        "start": ("message", "start_handle"),
        "help": ("message", "help_handle"),
        "settings": ("settings", "settings_handle"),
        "retry": ("message", "retry_handle"),
        "new": ("message", "new_dialog_handle"),
        "cancel": ("message", "cancel_handle"),
        "mode": ("chat_mode", "show_chat_modes_handle"),
        "subscription": ("subscription", "subscription_handle"),
        "my_payments": ("subscription", "my_payments_handle"),
        "edit_user": ("admin", "edit_user_command"),
        "broadcast": ("admin", "broadcast_command"),
        "user_data": ("admin", "get_user_data_command"),
    }

    CALLBACK_HANDLERS = {
        r"^subscribe\|": ("subscription", "subscription_callback_handle"),
        r"^subscription_back$": ("subscription", "subscription_handle"),
        r"^show_chat_modes": ("chat_mode", "show_chat_modes_callback_handle"),
        r"^set_chat_mode": ("chat_mode", "set_chat_mode_handle"),
        r"^model-": ("settings", "model_settings_handler"),
        r"^model-set_settings\|": ("settings", "set_settings_handle"),
        r"^confirm_broadcast\|": ("admin", "broadcast_confirmation_handler"),
        r"^cancel_broadcast": ("admin", "broadcast_confirmation_handler"),
    }

    MESSAGE_HANDLERS = {
        filters.TEXT & ~filters.COMMAND: ("message", "message_handle"),
        filters.VOICE: ("message", "voice_message_handle"),
        filters.PHOTO: ("message", "photo_message_handle"),
        filters.Document.IMAGE: ("message", "photo_message_handle"),
    }