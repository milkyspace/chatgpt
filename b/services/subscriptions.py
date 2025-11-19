from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from config import cfg
from models import User, UserSubscription, Usage


async def ensure_user(session: AsyncSession, tg_user_id: int, username: str | None, first_name: str | None,
                      last_name: str | None, referred_by_code: str | None = None) -> User:
    """Создаёт пользователя, подписку trial и usage при первом входе."""
    user = await session.scalar(select(User).where(User.id == tg_user_id))
    if user:
        return user

    # генерируем реф. код
    ref_code = f"ref{tg_user_id}"
    user = User(
        id=tg_user_id, username=username, first_name=first_name, last_name=last_name, referral_code=ref_code
    )
    session.add(user)
    await session.flush()

    # связываем рефера
    if referred_by_code:
        referrer = await session.scalar(select(User).where(User.referral_code == referred_by_code))
        if referrer:
            user.referred_by = referrer.id

    # создаем trial подписку
    sub = UserSubscription(
        user_id=user.id,
        plan_code=None,
        is_trial=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=cfg.trial_days),
    )
    session.add(sub)
    # usage
    usage = Usage(user_id=user.id, used_requests=0, used_images=0)
    session.add(usage)

    await session.commit()
    return user


async def has_active_subscription(session: AsyncSession, user_id: int) -> bool:
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    if not sub or not sub.expires_at:
        return False
    # Приводим обе даты к UTC для сравнения
    now = datetime.now(timezone.utc)
    expires_at = sub.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    return expires_at > now


async def get_limits(session: AsyncSession, user_id: int) -> tuple[int | None, int | None, int]:
    """Возвращает (max_requests, max_images, max_text_len) для текущего плана/триала."""
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    if not sub:
        # без подписки — нет доступа
        return (0, 0, 0)
    if sub.is_trial:
        return (cfg.trial_max_requests, cfg.trial_max_images, 4000)
    plan = cfg.plans.get(sub.plan_code or "", None)
    if not plan:
        return (0, 0, 0)
    return (plan.max_requests, plan.max_image_generations, plan.max_text_len)


async def activate_paid_plan(session: AsyncSession, user_id: int, new_code: str) -> UserSubscription:
    """
    Модель 4 (вариант А):
    - конвертация оставшихся дней → в дни нового тарифа
    - конвертация неиспользованных лимитов → в дни нового тарифа
    - затем начинается новый период нового тарифа
    """

    now = datetime.now(timezone.utc)
    new_plan = cfg.plans[new_code]
    new_price_per_day = new_plan.price_rub / new_plan.duration_days

    # Текущая подписка
    sub = await session.scalar(
        select(UserSubscription)
        .where(UserSubscription.user_id == user_id)
    )

    # Usage
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))

    # Если нет подписки (trial или пусто)
    if not sub:
        sub = UserSubscription(
            user_id=user_id,
            plan_code=new_code,
            is_trial=False,
            expires_at=now + timedelta(days=new_plan.duration_days)
        )
        session.add(sub)

        # сброс лимитов
        if usage:
            usage.used_requests = 0
            usage.used_images = 0

        await session.commit()
        return sub

    # ================================
    # 1. Остаток дней старого тарифа
    # ================================
    leftover_days_converted = 0.0

    if sub.expires_at and sub.expires_at > now and sub.plan_code:
        old_plan = cfg.plans.get(sub.plan_code)

        if old_plan:
            old_price_per_day = old_plan.price_rub / old_plan.duration_days
            leftover_days = (sub.expires_at - now).total_seconds() / 86400.0
            leftover_value_rub = leftover_days * old_price_per_day
            leftover_days_converted = leftover_value_rub / new_price_per_day

    # ============================================
    # 2. Конвертация неиспользованных лимитов в дни
    # ============================================

    bonus_days_req = 0.0
    bonus_days_img = 0.0

    if usage and sub.plan_code:
        old_plan = cfg.plans.get(sub.plan_code)

        if old_plan:
            # запросы
            max_req_old = old_plan.max_requests
            if max_req_old > 0:
                unused_req = max(max_req_old - usage.used_requests, 0)
                unused_ratio_req = unused_req / max_req_old
                bonus_days_req = unused_ratio_req * new_plan.duration_days

            # изображения
            max_img_old = old_plan.max_image_generations
            if max_img_old > 0:
                unused_img = max(max_img_old - usage.used_images, 0)
                unused_ratio_img = unused_img / max_img_old
                bonus_days_img = unused_ratio_img * new_plan.duration_days

    # ==========================
    # 3. Общие дни новой подписки
    # ==========================

    total_days = (
        new_plan.duration_days +
        leftover_days_converted +
        bonus_days_req +
        bonus_days_img
    )

    sub.plan_code = new_code
    sub.is_trial = False
    sub.expires_at = now + timedelta(days=total_days)

    # сброс лимитов
    if usage:
        usage.used_requests = 0
        usage.used_images = 0

    await session.commit()
    return sub