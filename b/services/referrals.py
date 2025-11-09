from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import timedelta, datetime
from b.config import cfg
from b.models import UserSubscription

async def apply_referral_bonus(session: AsyncSession, referrer_user_id: int) -> None:
    """Дарим рефереру +N дней к текущей подписке."""
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == referrer_user_id))
    now = datetime.now(datetime.now().astimezone().tzinfo)
    bonus = timedelta(days=cfg.referral_bonus_days)

    if not sub:
        sub = UserSubscription(user_id=referrer_user_id, is_trial=False, plan_code="pro_lite")
        session.add(sub)

    if sub.expires_at and sub.expires_at > now:
        sub.expires_at = sub.expires_at + bonus
    else:
        sub.expires_at = now + bonus

    await session.commit()
