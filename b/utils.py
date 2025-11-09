from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from models import User, UserSubscription, Usage, ChatSession, Message
from services.subscriptions import get_limits
from typing import Iterable

async def require_active_subscription(session: AsyncSession, user_id: int) -> bool:
    """Возвращает True, если подписка активна и не истёк trial."""
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    if not sub or not sub.expires_at:
        return False
    return sub.expires_at.timestamp() > __import__("time").time()

async def store_message(session: AsyncSession, session_id: int, role: str, content: str) -> None:
    m = Message(session_id=session_id, role=role, content=content)
    session.add(m)
    await session.commit()

async def get_history(session: AsyncSession, session_id: int, limit: int = 30) -> list[dict[str, str]]:
    res = (await session.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.id.desc()).limit(limit)
    )).scalars().all()
    out: list[dict[str, str]] = [{"role": m.role, "content": m.content} for m in reversed(res)]
    return out

def trim_messages(tokens_est: int, messages: list[dict[str, str]], max_len: int) -> list[dict[str, str]]:
    """Простое усечение истории по длине текста (упрощённо)."""
    total = 0
    out: list[dict[str, str]] = []
    for m in reversed(messages):
        l = len(m.get("content", ""))
        if total + l > max_len:
            break
        out.append(m)
        total += l
    return list(reversed(out))
