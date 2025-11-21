from __future__ import annotations
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import String, Integer, DateTime, ForeignKey, Boolean, Text, BigInteger
from sqlalchemy.sql import func
from db import Base
from typing import Optional
from datetime import datetime

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[Optional[str]] = mapped_column(String(255))
    first_name: Mapped[Optional[str]] = mapped_column(String(255))
    last_name: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    referral_code: Mapped[str] = mapped_column(String(32), unique=True)

    referred_by: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("users.id")
    )

    # отношения
    subscription: Mapped[Optional["UserSubscription"]] = relationship(back_populates="user", uselist=False)
    usage: Mapped[Optional["Usage"]] = relationship(back_populates="user", uselist=False)

class UserSubscription(Base):
    __tablename__ = "user_subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    plan_code: Mapped[Optional[str]] = mapped_column(String(64))     # None означает тестовую
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_trial: Mapped[bool] = mapped_column(Boolean, default=True)

    user: Mapped["User"] = relationship(back_populates="subscription")

class Usage(Base):
    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), unique=True)
    used_requests: Mapped[int] = mapped_column(Integer, default=0)
    used_images: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped["User"] = relationship(back_populates="usage")

class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    provider: Mapped[str] = mapped_column(String(64))
    provider_payment_id: Mapped[str] = mapped_column(String(128), index=True)
    plan_code: Mapped[str] = mapped_column(String(64))
    amount_rub: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/succeeded/canceled
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32), default="assistant")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    session_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("chat_sessions.id"))
    role: Mapped[str] = mapped_column(String(32))  # user|assistant|system
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
