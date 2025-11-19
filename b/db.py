from __future__ import annotations
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import cfg
from sqlalchemy import event

class Base(DeclarativeBase):
    """Базовый класс моделей."""

engine = create_async_engine(cfg.db_url, echo=False, pool_pre_ping=True)
AsyncSessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

async def get_session() -> AsyncSession:
    """Фабрика сессий для DI."""
    async with AsyncSessionMaker() as session:
        yield session

@event.listens_for(engine.sync_engine.pool, "checkout")
def validate_checkout(dbapi_connection, connection_record, connection_proxy):
    if getattr(dbapi_connection, "_in_use", False):
        raise Exception("Connection leak detected!")
    dbapi_connection._in_use = True

@event.listens_for(engine.sync_engine.pool, "checkin")
def validate_checkin(dbapi_connection, connection_record):
    dbapi_connection._in_use = False
