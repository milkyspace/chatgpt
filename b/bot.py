from __future__ import annotations
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties

from config import cfg
from router_public import router as public_router
from router_admin import router as admin_router
from services.payments_monitor import PaymentMonitor

import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


async def _set_commands(bot):
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск и главное меню"),
        BotCommand(command="new", description="Начать новый чат"),
        BotCommand(command="mode", description="Выбрать режим"),
        BotCommand(command="subscription", description="Моя подписка"),
        BotCommand(command="help", description="Помощь"),
    ])


async def main():
    logging.basicConfig(level=logging.INFO)
    logging.info(f"✅ BOT_TOKEN: {cfg.bot_token[:10]}…")

    # Добавляем хранилище для FSM
    storage = MemoryStorage()

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    await _set_commands(bot)

    dp = Dispatcher(storage=storage)

    # Сохраняем хранилище в боте для доступа из хендлеров
    bot["fsm_storage"] = storage

    dp.include_router(public_router)
    dp.include_router(admin_router)

    # Создаем монитор платежей с передачей бота для уведомлений
    monitor = PaymentMonitor(
        interval_min=cfg.payment_check_interval_min,
        bot=bot  # Передаем бота для отправки уведомлений
    )
    asyncio.create_task(monitor.run_forever())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())