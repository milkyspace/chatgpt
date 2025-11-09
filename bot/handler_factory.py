import logging
import asyncio
from typing import Dict, Any
import bot.database as database
import bot.subscription_handlers as SubscriptionHandlers
import bot.image_handlers as ImageHandlers
import bot.chat_mode_handlers as ChatModeHandlers
import bot.admin_handlers as AdminHandlers
import bot.settings_handlers as SettingsHandlers
import bot.message_handlers as MessageHandlers

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class HandlerFactory:
    """Фабрика для создания обработчиков."""

    @staticmethod
    def create_handlers(database: database.Database) -> Dict[str, Any]:
        """Создает все необходимые обработчики."""
        subscription_handlers = SubscriptionHandlers(database)
        image_handlers = ImageHandlers(database)
        chat_mode_handlers = ChatModeHandlers(database)
        admin_handlers = AdminHandlers(database)
        settings_handlers = SettingsHandlers(database)
        message_handlers = MessageHandlers(
            database,
            subscription_handlers,
            chat_mode_handlers,
            admin_handlers,
            image_handlers
        )

        return {
            'message': message_handlers,
            'subscription': subscription_handlers,
            'image': image_handlers,
            'chat_mode': chat_mode_handlers,
            'admin': admin_handlers,
            'settings': settings_handlers
        }