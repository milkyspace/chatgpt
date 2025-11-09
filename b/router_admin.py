from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message as TgMessage, CallbackQuery
from sqlalchemy import func
from sqlalchemy import select, update

from config import cfg
from db import AsyncSessionMaker
from keyboards import admin_menu
from models import Payment
from models import User
from payments.yoomoney import YooMoneyProvider
from services.subscriptions import activate_paid_plan

router = Router()

def is_admin(user_id: int) -> bool:
    return user_id in cfg.admin_ids

@router.message(Command("admin"))
async def admin_entry(m: TgMessage):
    if not is_admin(m.from_user.id):
        return
    await m.answer("Админ-панель:", reply_markup=admin_menu())

@router.callback_query(F.data == "admin:users")
async def admin_users(cq: CallbackQuery):
    if not is_admin(cq.from_user.id): return
    async with AsyncSessionMaker() as session:
        total = await session.scalar(select(func.count()).select_from(User))
    await cq.message.answer(f"Пользователей: {total}")
    await cq.answer()

@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast(cq: CallbackQuery):
    if not is_admin(cq.from_user.id): return
    await cq.message.answer("Отправьте текст рассылки ответом на это сообщение.")
    await cq.answer()

@router.message(Command("check_payments"))
async def check_payments(m: TgMessage):
    if not is_admin(m.from_user.id):
        return
    provider = YooMoneyProvider()
    async with AsyncSessionMaker() as session:
        payments = (await session.execute(
            select(Payment).where(Payment.status == "pending")
        )).scalars().all()
        for p in payments:
            status = await provider.check_status(p.provider_payment_id)
            if status == "succeeded":
                await activate_paid_plan(session, p.user_id, p.plan_code)
                await session.execute(update(Payment).where(Payment.id == p.id).values(status="succeeded"))
        await session.commit()
    await m.answer("✅ Проверка платежей завершена.")
