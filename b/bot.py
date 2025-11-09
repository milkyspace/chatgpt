# bot.py
from __future__ import annotations
import asyncio
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode

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

async def main():
    logger.critical(cfg.bot_token)
    bot = Bot(cfg.bot_token, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(public_router)
    dp.include_router(admin_router)

    # 🔄 Запуск фонового мониторинга платежей
    monitor = PaymentMonitor(interval_min=cfg.payment_check_interval_min)
    asyncio.create_task(monitor.run_forever())

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

