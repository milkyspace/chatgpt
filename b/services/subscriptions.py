from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from b.config import cfg
from b.models import User, UserSubscription, Usage

async def ensure_user(session: AsyncSession, tg_user_id: int, username: str | None, first_name: str | None, last_name: str | None, referred_by_code: str | None = None) -> User:
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
    return sub.expires_at > datetime.now(datetime.now().astimezone().tzinfo).astimezone().replace(tzinfo=None)

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

async def activate_paid_plan(session: AsyncSession, user_id: int, plan_code: str) -> None:
    """Активация платного плана (вызывается после оплаты). Продлевает время и сбрасывает usage."""
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    now = datetime.now(datetime.now().astimezone().tzinfo)
    if not sub:
        sub = UserSubscription(user_id=user_id)
        session.add(sub)

    plan = cfg.plans[plan_code]
    if sub.expires_at and sub.expires_at > now:
        sub.expires_at = sub.expires_at + timedelta(days=plan.duration_days)
    else:
        sub.expires_at = now + timedelta(days=plan.duration_days)
    sub.plan_code = plan_code
    sub.is_trial = False

    # Сброс usage под новый период
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))
    if usage:
        usage.used_requests = 0
        usage.used_images = 0

    await session.commit()
