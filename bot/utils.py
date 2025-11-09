import asyncio
import logging
from typing import Dict, List

import config
import database

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
    logging.getLogger('pymongo').setLevel(logging.WARNING)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
    logging.getLogger('urllib3').setLevel(logging.WARNING)


def get_user_semaphore(user_id: int) -> asyncio.Semaphore:
    """Безопасно получает семафор пользователя, создавая его при необходимости."""
    if user_id not in user_semaphores:
        user_semaphores[user_id] = asyncio.Semaphore(1)
        logger.info(f"Created semaphore for user {user_id}")
    return user_semaphores[user_id]