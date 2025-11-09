from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from b.models import Usage
from b.services.subscriptions import get_limits

async def can_spend_request(session: AsyncSession, user_id: int) -> bool:
    max_req, _, _ = await get_limits(session, user_id)
    if max_req is None:
        return True
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))
    return usage.used_requests < max_req

async def can_spend_image(session: AsyncSession, user_id: int) -> bool:
    _, max_img, _ = await get_limits(session, user_id)
    if max_img is None:
        return True
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))
    return usage.used_images < max_img

async def spend_request(session: AsyncSession, user_id: int) -> None:
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))
    usage.used_requests += 1
    await session.commit()

async def spend_image(session: AsyncSession, user_id: int) -> None:
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))
    usage.used_images += 1
    await session.commit()
