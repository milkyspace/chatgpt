from __future__ import annotations
from config import cfg
from db import AsyncSessionMaker
from sqlalchemy import select
from models import User
import logging

logger = logging.getLogger(__name__)

def is_admin(user_id: int) -> bool:
    """
    Проверяет, является ли пользователь администратором

    Args:
        user_id: ID пользователя в Telegram

    Returns:
        bool: True если пользователь администратор
    """
    logger.debug("is_admin: %s", user_id in cfg.admin_ids)
    return user_id in cfg.admin_ids


async def admin_required(user_id: int) -> bool:
    """
    Асинхронная версия проверки администратора (для фильтров)

    Args:
        user_id: ID пользователя в Telegram

    Returns:
        bool: True если пользователь администратор
    """
    return is_admin(user_id)

async def is_user_blocked(user_id: int) -> bool:
    async with AsyncSessionMaker() as session:
        user = await session.scalar(select(User).where(User.id == user_id))
        return bool(user and user.is_blocked)