from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select, update

from db import AsyncSessionMaker
from models import Payment, User
from payments.yoomoney import YooMoneyProvider
from services.subscriptions import activate_paid_plan
from services.referrals import apply_referral_bonus
from services.notifications import NotificationService
from services.auth import is_admin  # Используем нашу функцию проверки админов

logger = logging.getLogger(__name__)


class PaymentMonitor:
    """Фоновый мониторинг платежей."""

    def __init__(self, interval_min: float = 1, bot=None):
        # Интервал проверки в минутах
        self.interval_min = interval_min
        self.running = False
        self.bot = bot
        self.notification_service = NotificationService(bot) if bot else None

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
        """Останавливает мониторинг."""
        self.running = False

    async def check_pending(self, provider: YooMoneyProvider):
        """Проверяет все незавершенные платежи."""
        async with AsyncSessionMaker() as session:
            payments = (
                await session.execute(
                    select(Payment).where(Payment.status == "pending")
                )
            ).scalars().all()

            if not payments:
                return

            for payment in payments:
                await self._process_payment(session, provider, payment)

    async def _process_payment(
        self,
        session,
        provider: YooMoneyProvider,
        payment: Payment,
    ):
        """Обрабатывает один платеж."""
        try:
            # Для админов автоматически подтверждаем платежи без запроса к провайдеру
            if is_admin(payment.user_id):
                logger.info(
                    f"Админский платеж {payment.provider_payment_id}: "
                    f"пользователь {payment.user_id}"
                )
                status = "succeeded"
            else:
                status = await provider.check_status(payment.provider_payment_id)

            logger.info(
                f"Платеж {payment.provider_payment_id}: "
                f"пользователь {payment.user_id} : статус {status}"
            )

            if status == "succeeded":
                await self._handle_successful_payment(session, payment)
            elif status in ("canceled", "expired"):
                await self._handle_failed_payment(session, payment, status)

        except Exception as e:
            logger.warning(
                f"Ошибка запроса статуса для платежа {payment.id}: {e}"
            )

    async def _handle_successful_payment(self, session, payment: Payment):
        """Обрабатывает успешный платеж."""
        logger.info(f"Платеж {payment.id} ({payment.plan_code}) подтвержден.")

        # 1. Активируем/апгрейдим подписку
        #    activate_paid_plan теперь возвращает SubscriptionUpgradeResult
        upgrade = await activate_paid_plan(
            session,
            payment.user_id,
            payment.plan_code,
        )

        # 2. Начисляем бонус рефереру (если есть)
        user = await session.get(User, payment.user_id)
        if user and user.referred_by:
            await apply_referral_bonus(session, user.referred_by)

        # 3. Обновляем статус платежа
        await session.execute(
            update(Payment)
            .where(Payment.id == payment.id)
            .values(status="succeeded")
        )
        await session.commit()

        # 4. Отправляем уведомления
        if self.notification_service:
            from config import cfg  # Локальный импорт, чтобы избежать циклических зависимостей

            plan = cfg.plans.get(payment.plan_code)
            plan_title = plan.title if plan else payment.plan_code

            # 4.1.1 Базовое уведомление «подписка активирована»
            await self.notification_service.send_subscription_activated(
                user_id=payment.user_id,
                plan_title=plan_title,
                expires_at=upgrade.expires_at,
            )

            # 4.1.2 Уведомление рефералу
            if user and user.referred_by:
                await self.notification_service.send_message(
                    user.referred_by,
                    f"🎉 Ваш реферал оплатил подписку!\nВам начислено +5 дней."
                )

            # 4.2. Детализация апгрейда/даунгрейда
            # Внутри send_subscription_upgrade_info уже учтено:
            # - если old_plan нет
            # - или old_plan == new_plan
            # то подробный расчёт не показывается.
            await self.notification_service.send_subscription_upgrade_info(
                user_id=payment.user_id,
                result=upgrade,
            )

    async def _handle_failed_payment(self, session, payment: Payment, status: str):
        """Обрабатывает неудачный платеж."""
        logger.info(f"Платеж {payment.id} отменен ({status}).")

        await session.execute(
            update(Payment)
            .where(Payment.id == payment.id)
            .values(status=status)
        )
        await session.commit()

        # Отправляем уведомление об ошибке
        if self.notification_service:
            reason = "отменен" if status == "canceled" else "истек срок действия"
            await self.notification_service.send_payment_failed(
                user_id=payment.user_id,
                reason=reason,
            )
