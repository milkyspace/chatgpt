from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import timedelta, datetime, timezone
from config import cfg
from models import UserSubscription


async def apply_referral_bonus(session: AsyncSession, referrer_user_id: int) -> None:
    """Дарим рефереру +N дней к текущей подписке."""
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == referrer_user_id))
    now = datetime.now(timezone.utc)  # Исправлено: всегда используем UTC
    bonus = timedelta(days=cfg.referral_bonus_days)

    if not sub:
        sub = UserSubscription(user_id=referrer_user_id, is_trial=False, plan_code="pro_lite")
        session.add(sub)

    # Приводим expires_at к UTC для корректного сравнения
    if sub.expires_at:
        expires_at = sub.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)

        if expires_at > now:
            sub.expires_at = expires_at + bonus
        else:
            sub.expires_at = now + bonus
    else:
        sub.expires_at = now + bonus

    await session.commit()