from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import User, UserSubscription, Usage, ChatSession, Message
from services.subscriptions import get_limits
from typing import Iterable
from datetime import datetime, timezone

async def get_subscription_button_text(session, user_id: int) -> str:
    sub = await session.scalar(
        select(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    now = datetime.now(timezone.utc)  # always aware

    if not sub or not sub.expires_at:
        return "🔴 Подписка: неактивна"

    expires = sub.expires_at

    # -------- FIX --------
    # Приводим дату к offset-aware
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    else:
        expires = expires.astimezone(timezone.utc)
    # ----------------------

    # если уже истекла
    if expires <= now:
        return "🔴 Подписка: неактивна"

    # считаем дни
    days_left = (expires - now).days

    if days_left <= 3:
        icon = "🟡"
    else:
        icon = "🟢"

    return f"{icon} Подписка: {days_left} дн."