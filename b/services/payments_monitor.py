from __future__ import annotations
import asyncio
import logging
from sqlalchemy import select, update
from db import AsyncSessionMaker
from models import Payment, User
from payments.yoomoney import YooMoneyProvider
from services.subscriptions import activate_paid_plan
from services.referrals import apply_referral_bonus
from router_admin import is_admin

logger = logging.getLogger(__name__)


class PaymentMonitor:
    """Фоновый мониторинг платежей."""

    def __init__(self, interval_min: float = 1):
        self.interval_min = interval_min
        self.running = False

    async def run_forever(self):
        """Запускает бесконечный цикл проверки."""
        self.running = True
        provider = YooMoneyProvider()
        logger.info(f"[PaymentMonitor] Запущен. Интервал: {self.interval_min} минут.")
        while self.running:
            try:
                await self.check_pending(provider)
            except Exception as e:
                logger.exception(f"[PaymentMonitor] Ошибка проверки: {e}")
            await asyncio.sleep(self.interval_min * 60)

    async def stop(self):
        self.running = False

    async def check_pending(self, provider: YooMoneyProvider):
        """Проверяет все незавершенные платежи."""
        async with AsyncSessionMaker() as session:
            payments = (await session.execute(
                select(Payment).where(Payment.status == "pending")
            )).scalars().all()

            if not payments:
                logger.info("[PaymentMonitor] Нет ожидающих платежей.")
                return

            for p in payments:
                try:
                    status = await provider.check_status(p.provider_payment_id)
                    logger.info(f"Платеж {p.provider_payment_id}: пользователь {p.user_id} : статус {status}")  # Исправлено
                    if is_admin(p.user_id):
                        logger.info(f"Админский платеж {p.provider_payment_id}: пользователь {p.user_id}")  # Исправлено
                        status = "succeeded"
                except Exception as e:
                    logger.warning(f"[PaymentMonitor] Ошибка запроса статуса для платежа {p.id}: {e}")  # Исправлено
                    continue

                if status == "succeeded":
                    logger.info(f"[PaymentMonitor] Платеж {p.id} ({p.plan_code}) подтвержден.")
                    await activate_paid_plan(session, p.user_id, p.plan_code)

                    # Бонус за рефералку
                    user = await session.get(User, p.user_id)
                    if user and user.referred_by:
                        await apply_referral_bonus(session, user.referred_by)

                    await session.execute(update(Payment)
                                          .where(Payment.id == p.id)
                                          .values(status="succeeded"))
                    await session.commit()

                elif status in ("canceled", "expired"):
                    logger.info(f"[PaymentMonitor] Платеж {p.id} отменен ({status}).")
                    await session.execute(update(Payment)
                                          .where(Payment.id == p.id)
                                          .values(status=status))
                    await session.commit()