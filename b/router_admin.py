from __future__ import annotations

from aiogram import Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message as TgMessage, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import func
from sqlalchemy import select, update
import asyncio
import logging

from config import cfg
from db import AsyncSessionMaker
from keyboards import admin_menu, admin_back_keyboard
from models import Payment, User, UserSubscription
from payments.yoomoney import YooMoneyProvider
from services.subscriptions import activate_paid_plan
from services.auth import is_admin

logger = logging.getLogger(__name__)

# Создаем фильтр для администраторов
admin_filter = F.from_user.func(lambda user: is_admin(user.id))

router = Router()

# Применяем фильтр ко всем хендлерам этого роутера
router.message.filter(admin_filter)
router.callback_query.filter(admin_filter)


# FSM состояния для рассылки
class BroadcastStates(StatesGroup):
    waiting_for_broadcast_text = State()


@router.message(Command("admin"))
async def admin_entry(m: TgMessage):
    """Главное меню админ-панели"""
    await m.answer(
        "🛡 <b>Админ-панель</b>\n\n"
        "Доступные функции:\n"
        "• 👤 Просмотр пользователей\n"
        "• 📊 Статистика\n"
        "• 💳 Платежи\n"
        "• 📣 Рассылка сообщений\n"
        "• 🔄 Проверка платежей",
        reply_markup=admin_menu()
    )


@router.callback_query(F.data == "admin:main")
async def admin_main(cq: CallbackQuery):
    """Возврат в главное меню админ-панели"""
    await cq.message.edit_text(
        "🛡 <b>Админ-панель</b>\n\n"
        "Доступные функции:\n"
        "• 👤 Просмотр пользователей\n"
        "• 📊 Статистика\n"
        "• 💳 Платежи\n"
        "• 📣 Рассылка сообщений\n"
        "• 🔄 Проверка платежей",
        reply_markup=admin_menu()
    )
    await cq.answer()


@router.callback_query(F.data == "admin:users")
async def admin_users(cq: CallbackQuery):
    """Показывает количество пользователей"""
    async with AsyncSessionMaker() as session:
        total_users = await session.scalar(select(func.count()).select_from(User))
        active_subs = await session.scalar(
            select(func.count()).select_from(UserSubscription).where(
                UserSubscription.expires_at > func.now()
            )
        )
        trial_users = await session.scalar(
            select(func.count()).select_from(UserSubscription).where(
                UserSubscription.is_trial == True
            )
        )

    text = (
        f"👥 <b>Статистика пользователей</b>\n\n"
        f"• Всего пользователей: <b>{total_users}</b>\n"
        f"• Активных подписок: <b>{active_subs}</b>\n"
        f"• Пробных периодов: <b>{trial_users}</b>"
    )
    await cq.message.edit_text(text, reply_markup=admin_back_keyboard())
    await cq.answer()


@router.callback_query(F.data == "admin:stats")
async def admin_stats(cq: CallbackQuery):
    """Подробная статистика"""
    async with AsyncSessionMaker() as session:
        # Общая статистика
        total_users = await session.scalar(select(func.count()).select_from(User))
        total_payments = await session.scalar(select(func.count()).select_from(Payment))
        successful_payments = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.status == "succeeded")
        )
        total_revenue = await session.scalar(
            select(func.sum(Payment.amount_rub)).where(Payment.status == "succeeded")
        ) or 0

        # Статистика по планам
        plan_stats = {}
        for plan_code in cfg.plans.keys():
            count = await session.scalar(
                select(func.count()).select_from(Payment).where(
                    Payment.plan_code == plan_code,
                    Payment.status == "succeeded"
                )
            )
            plan_stats[plan_code] = count

    text = (
        "📊 <b>Общая статистика</b>\n\n"
        f"• Пользователей: <b>{total_users}</b>\n"
        f"• Всего платежей: <b>{total_payments}</b>\n"
        f"• Успешных платежей: <b>{successful_payments}</b>\n"
        f"• Общая выручка: <b>{total_revenue} ₽</b>\n\n"
        "<b>Статистика по тарифам:</b>\n"
    )

    for plan_code, count in plan_stats.items():
        plan = cfg.plans.get(plan_code)
        plan_name = plan.title if plan else plan_code
        text += f"• {plan_name}: <b>{count}</b>\n"

    await cq.message.edit_text(text, reply_markup=admin_back_keyboard())
    await cq.answer()


@router.callback_query(F.data == "admin:payments")
async def admin_payments(cq: CallbackQuery):
    """Статистика по платежам"""
    async with AsyncSessionMaker() as session:
        # Статистика по статусам платежей
        status_stats = await session.execute(
            select(Payment.status, func.count(Payment.id))
            .group_by(Payment.status)
        )
        status_counts = dict(status_stats.all())

        # Ожидающие платежи
        pending_count = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.status == "pending")
        )

    text = "💳 <b>Статистика платежей</b>\n\n<b>По статусам:</b>\n"

    for status, count in status_counts.items():
        text += f"• {status}: <b>{count}</b>\n"

    text += f"\n<b>Ожидающие проверки:</b> <b>{pending_count}</b>"

    await cq.message.edit_text(text, reply_markup=admin_back_keyboard())
    await cq.answer()


@router.callback_query(F.data == "admin:check_payments")
async def admin_check_payments(cq: CallbackQuery):
    """Проверка ожидающих платежей"""
    provider = YooMoneyProvider()

    async with AsyncSessionMaker() as session:
        payments = await session.scalars(
            select(Payment).where(Payment.status == "pending")
        )
        pending_payments = payments.all()

        if not pending_payments:
            await cq.message.edit_text(
                "✅ Нет ожидающих платежей",
                reply_markup=admin_back_keyboard()
            )
            await cq.answer()
            return

        processed = 0
        succeeded = 0

        for payment in pending_payments:
            try:
                # Для админов автоматически подтверждаем платежи
                if is_admin(payment.user_id):
                    status = "succeeded"
                else:
                    status = await provider.check_status(payment.provider_payment_id)

                if status == "succeeded":
                    await activate_paid_plan(session, payment.user_id, payment.plan_code)
                    payment.status = "succeeded"
                    succeeded += 1
                elif status in ("canceled", "expired"):
                    payment.status = status

                processed += 1

            except Exception as e:
                logger.error(f"Ошибка проверки платежа {payment.id}: {e}")
                continue

        await session.commit()

    await cq.message.edit_text(
        f"🔍 Проверка платежей завершена:\n"
        f"• Обработано: {processed}\n"
        f"• Успешных: {succeeded}\n"
        f"• Всего в очереди: {len(pending_payments)}",
        reply_markup=admin_back_keyboard()
    )
    await cq.answer()


@router.callback_query(F.data == "admin:broadcast")
async def admin_broadcast(cq: CallbackQuery, state: FSMContext):
    """Начало процесса рассылки"""
    await state.set_state(BroadcastStates.waiting_for_broadcast_text)

    await cq.message.edit_text(
        "📣 <b>Рассылка сообщений</b>\n\n"
        "Отправьте текст для рассылки.\n"
        "Сообщение будет отправлено всем пользователям бота.\n\n"
        "<i>Используйте HTML-разметку для форматирования.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:main")]
        ])
    )
    await cq.answer()


@router.message(BroadcastStates.waiting_for_broadcast_text)
async def process_broadcast_text(m: TgMessage, state: FSMContext):
    """
    Обрабатывает текст для рассылки в состоянии ожидания.

    Args:
        m: Сообщение от пользователя
        state: Состояние FSM
    """
    try:
        # Проверяем наличие текста
        if not m.text or not m.text.strip():
            await m.answer("❌ Пожалуйста, отправьте текст для рассылки")
            return

        broadcast_text = m.text.strip()

        if len(broadcast_text) < 5:
            await m.answer("❌ Текст рассылки слишком короткий (минимум 5 символов)")
            return

        # Сбрасываем состояние
        await state.clear()

        # Получаем всех пользователей
        async with AsyncSessionMaker() as session:
            users_result = await session.execute(select(User))
            user_list = users_result.scalars().all()

        if not user_list:
            await m.answer("❌ В базе данных нет пользователей для рассылки")
            return

        processing_msg = await m.answer(f"🔄 Начинаю рассылку для {len(user_list)} пользователей...")

        success_count = 0
        fail_count = 0
        blocked_count = 0

        # Отправляем сообщения с обработкой ошибок
        for i, user in enumerate(user_list):
            try:
                await m.bot.send_message(
                    chat_id=user.id,
                    text=broadcast_text,
                    parse_mode="HTML"
                )
                success_count += 1

                # Задержка чтобы не превысить лимиты Telegram (30 сообщений в секунду)
                if (i + 1) % 25 == 0:
                    await asyncio.sleep(1)
                else:
                    await asyncio.sleep(0.05)

            except Exception as e:
                error_msg = str(e).lower()
                if "bot was blocked" in error_msg or "user is deactivated" in error_msg:
                    blocked_count += 1
                else:
                    logger.error(f"Ошибка отправки пользователю {user.id}: {e}")
                    fail_count += 1

        # Формируем отчет
        report_text = (
            f"✅ <b>Рассылка завершена</b>\n\n"
            f"• 📊 Всего пользователей: <b>{len(user_list)}</b>\n"
            f"• ✅ Успешно отправлено: <b>{success_count}</b>\n"
            f"• ❌ Не удалось отправить: <b>{fail_count}</b>\n"
            f"• 🚫 Заблокировали бота: <b>{blocked_count}</b>"
        )

        await processing_msg.edit_text(
            report_text,
            reply_markup=admin_back_keyboard(),
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Критическая ошибка в процессе рассылки: {e}")
        await state.clear()
        await m.answer(
            "❌ Произошла критическая ошибка при рассылке",
            reply_markup=admin_back_keyboard()
        )


@router.message(Command("check_payments"))
async def check_payments_command(m: TgMessage):
    """Ручная проверка платежей через команду"""
    provider = YooMoneyProvider()

    async with AsyncSessionMaker() as session:
        payments = await session.scalars(
            select(Payment).where(Payment.status == "pending")
        )
        pending_payments = payments.all()

        if not pending_payments:
            await m.answer("✅ Нет ожидающих платежей")
            return

        processed = 0
        succeeded = 0

        for payment in pending_payments:
            try:
                # Для админов автоматически подтверждаем платежи
                if is_admin(payment.user_id):
                    status = "succeeded"
                else:
                    status = await provider.check_status(payment.provider_payment_id)

                if status == "succeeded":
                    await activate_paid_plan(session, payment.user_id, payment.plan_code)
                    payment.status = "succeeded"
                    succeeded += 1
                elif status in ("canceled", "expired"):
                    payment.status = status

                processed += 1

            except Exception as e:
                logger.error(f"Ошибка проверки платежа {payment.id}: {e}")
                continue

        await session.commit()

    await m.answer(
        f"🔍 Проверка платежей завершена:\n"
        f"• Обработано: {processed}\n"
        f"• Успешных: {succeeded}\n"
        f"• Всего в очереди: {len(pending_payments)}"
    )


@router.callback_query(F.data == "panel:admin")
async def panel_admin(cq: CallbackQuery):
    """Переход в админ-панель"""
    if not is_admin(cq.from_user.id):
        await cq.answer("🚫 Нет доступа", show_alert=True)
        return

    await cq.message.edit_text(
        "🛡 <b>Админ-панель</b>\n\n"
        "Доступные функции:\n"
        "• 👤 Просмотр пользователей\n"
        "• 📊 Статистика\n"
        "• 💳 Платежи\n"
        "• 📣 Рассылка сообщений\n"
        "• 🔄 Проверка платежей",
        reply_markup=admin_menu()
    )
    await cq.answer()


@router.callback_query(F.data == "cancel_broadcast")
async def cancel_broadcast(cq: CallbackQuery, state: FSMContext):
    """Отмена рассылки"""
    await state.clear()

    await cq.message.edit_text(
        "❌ Рассылка отменена",
        reply_markup=admin_back_keyboard()
    )
    await cq.answer()