# services/payments_monitor.py
from __future__ import annotations
import asyncio
import logging
from sqlalchemy import select, update
from b.db import AsyncSessionMaker
from b.models import Payment, User
from b.payments.yoomoney import YooMoneyProvider
from b.services.subscriptions import activate_paid_plan
from b.services.referrals import apply_referral_bonus

logger = logging.getLogger(__name__)

class PaymentMonitor:
    """Фоновый мониторинг платежей."""
    def __init__(self, interval_min: int = 5):
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
                except Exception as e:
                    logger.warning(f"[PaymentMonitor] Ошибка запроса статуса для {p.id}: {e}")
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
