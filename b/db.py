from __future__ import annotations
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import cfg

class Base(DeclarativeBase):
    """Базовый класс моделей."""

engine = create_async_engine(cfg.db_url, echo=False, pool_pre_ping=True)
AsyncSessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_session() -> AsyncSession:
    """Фабрика сессий для DI."""
    async with AsyncSessionMaker() as session:
        yield session
