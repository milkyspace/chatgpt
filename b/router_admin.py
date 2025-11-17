from __future__ import annotations
import asyncio
import logging
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message as TgMessage, CallbackQuery
from sqlalchemy import func
from sqlalchemy import select, update

from config import cfg
from db import AsyncSessionMaker
from keyboards import admin_menu, admin_back_keyboard
from models import Payment, User, UserSubscription
from payments.yoomoney import YooMoneyProvider
from services.subscriptions import activate_paid_plan
from services.auth import is_admin, admin_required

# Создаем фильтр для администраторов
admin_filter = F.from_user.func(lambda user: is_admin(user.id))

router = Router()

# Применяем фильтр ко всем хендлерам этого роутера
router.message.filter(admin_filter)
router.callback_query.filter(admin_filter)

logger = logging.getLogger(__name__)

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

        # Последние успешные платежи
        recent_payments = await session.scalars(
            select(Payment)
            .where(Payment.status == "succeeded")
            .order_by(Payment.created_at.desc())
            .limit(5)
        )

    text = "💳 <b>Статистика платежей</b>\n\n<b>По статусам:</b>\n"

    for status, count in status_counts.items():
        text += f"• {status}: <b>{count}</b>\n"

    text += "\n<b>Последние успешные платежи:</b>\n"
    for payment in recent_payments:
        plan = cfg.plans.get(payment.plan_code, None)
        plan_name = plan.title if plan else payment.plan_code
        text += f"• {plan_name} - {payment.amount_rub}₽\n"

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
async def admin_broadcast(cq: CallbackQuery):
    """Начало процесса рассылки"""
    await cq.message.edit_text(
        "📣 <b>Рассылка сообщений</b>\n\n"
        "Отправьте текст для рассылки ответом на это сообщение.\n"
        "Сообщение будет отправлено всем пользователям бота.\n\n"
        "<i>Используйте HTML-разметку для форматирования.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Отмена", callback_data="admin:main")]
        ])
    )
    await cq.answer()


@router.message(F.reply_to_message & F.reply_to_message.text.contains("Рассылка"))
async def process_broadcast(m: TgMessage):
    """Обрабатывает рассылку"""
    broadcast_text = m.text

    if not broadcast_text or len(broadcast_text.strip()) < 5:
        await m.answer("❌ Текст рассылки слишком короткий")
        return

    # Получаем всех пользователей
    async with AsyncSessionMaker() as session:
        users = await session.scalars(select(User))
        user_list = users.all()

    processing_msg = await m.answer(f"🔄 Начинаю рассылку для {len(user_list)} пользователей...")

    success_count = 0
    fail_count = 0

    for user in user_list:
        try:
            await m.bot.send_message(
                chat_id=user.id,
                text=broadcast_text,
                parse_mode="HTML"
            )
            success_count += 1
            # Небольшая задержка чтобы не превысить лимиты Telegram
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user.id}: {e}")
            fail_count += 1

    await processing_msg.edit_text(
        f"✅ Рассылка завершена:\n"
        f"• Успешно: {success_count}\n"
        f"• Не удалось: {fail_count}\n"
        f"• Всего: {len(user_list)}",
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
        reply_markup=admin_menu()  # Теперь используется!
    )
    await cq.answer()