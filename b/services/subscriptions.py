from __future__ import annotations
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta, timezone
from config import cfg, PlanConfig
from models import User, UserSubscription, Usage
from tools.utils import format_days_hours
from dataclasses import dataclass


async def ensure_user(session: AsyncSession, tg_user_id: int, username: str | None, first_name: str | None,
                      last_name: str | None, referred_by_code: str | None = None) -> User:
    """Создаёт пользователя, подписку trial и usage при первом входе."""
    user = await session.scalar(select(User).where(User.id == tg_user_id))
    if user:
        return user

    # генерируем реф. код
    ref_code = f"ref{tg_user_id}"
    user = User(
        id=tg_user_id, username=username, first_name=first_name, last_name=last_name, referral_code=ref_code
    )
    session.add(user)
    await session.flush()

    # связываем рефера
    if referred_by_code:
        referrer = await session.scalar(select(User).where(User.referral_code == referred_by_code))
        if referrer:
            user.referred_by = referrer.id

    # создаем trial подписку
    sub = UserSubscription(
        user_id=user.id,
        plan_code=None,
        is_trial=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=cfg.trial_days),
    )
    session.add(sub)
    # usage
    usage = Usage(user_id=user.id, used_requests=0, used_images=0)
    session.add(usage)

    await session.commit()
    return user


async def has_active_subscription(session: AsyncSession, user_id: int) -> bool:
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    if not sub or not sub.expires_at:
        return False
    # Приводим обе даты к UTC для сравнения
    now = datetime.now(timezone.utc)
    expires_at = sub.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)
    return expires_at > now


async def get_limits(session: AsyncSession, user_id: int) -> tuple[int | None, int | None, int]:
    """Возвращает (max_requests, max_images, max_text_len) для текущего плана/триала."""
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    if not sub:
        # без подписки — нет доступа
        return (0, 0, 0)
    if sub.is_trial:
        return (cfg.trial_max_requests, cfg.trial_max_images, 4000)
    plan = cfg.plans.get(sub.plan_code or "", None)
    if not plan:
        return (0, 0, 0)
    return (plan.max_requests, plan.max_image_generations, plan.max_text_len)

@dataclass
class SubscriptionUpgradeResult:
    old_plan: PlanConfig | None
    new_plan: PlanConfig
    converted_days: float
    bonus_days_req: float
    bonus_days_img: float
    total_days: float
    expires_at: datetime

async def activate_paid_plan(session: AsyncSession, user_id: int, new_code: str) -> SubscriptionUpgradeResult:
    """
    Активирует подписку с учетом честного пересчёта (ап/даун-грейд).
    Теперь возвращает красиво отформатированные строки.
    """
    now = datetime.now(timezone.utc)
    new_plan = cfg.plans[new_code]
    new_price_per_day = new_plan.price_rub / new_plan.duration_days

    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))

    # Чистое обновление (trial или нет подписки)
    if not sub or not sub.plan_code or not sub.expires_at or sub.expires_at <= now:
        expires_at = now + timedelta(days=new_plan.duration_days)

        if not sub:
            sub = UserSubscription(
                user_id=user_id,
                plan_code=new_code,
                is_trial=False,
                expires_at=expires_at
            )
            session.add(sub)
        else:
            sub.plan_code = new_code
            sub.is_trial = False
            sub.expires_at = expires_at

        if usage:
            usage.used_requests = 0
            usage.used_images = 0

        await session.commit()

        return SubscriptionUpgradeResult(
            old_plan=None,
            new_plan=new_plan,
            converted_days=format_days_hours(0),
            bonus_days_req=format_days_hours(0),
            bonus_days_img=format_days_hours(0),
            total_days=format_days_hours(new_plan.duration_days),
            expires_at=expires_at
        )

    # ---------------------------
    # Продвинутый апгрейд/даунгрейд
    # ---------------------------
    old_plan = cfg.plans.get(sub.plan_code)
    old_price_per_day = old_plan.price_rub / old_plan.duration_days

    # Остаток
    leftover_days = max((sub.expires_at - now).total_seconds() / 86400, 0)
    leftover_value = leftover_days * old_price_per_day
    converted_days = leftover_value / new_price_per_day

    # Бонусы
    bonus_req = 0
    bonus_img = 0

    if usage:

        if old_plan.max_requests:
            unused = max(old_plan.max_requests - usage.used_requests, 0)
            ratio = unused / old_plan.max_requests
            bonus_req = ratio * new_plan.duration_days

        if old_plan.max_image_generations:
            unused = max(old_plan.max_image_generations - usage.used_images, 0)
            ratio = unused / old_plan.max_image_generations
            bonus_img = ratio * new_plan.duration_days

    total_days = new_plan.duration_days + converted_days + bonus_req + bonus_img
    new_expires_at = now + timedelta(days=total_days)

    # Сохранение
    sub.plan_code = new_code
    sub.is_trial = False
    sub.expires_at = new_expires_at

    if usage:
        usage.used_requests = 0
        usage.used_images = 0

    await session.commit()

    return SubscriptionUpgradeResult(
        old_plan=old_plan,
        new_plan=new_plan,
        converted_days=format_days_hours(converted_days),
        bonus_days_req=format_days_hours(bonus_req),
        bonus_days_img=format_days_hours(bonus_img),
        total_days=format_days_hours(total_days),
        expires_at=new_expires_at
    )

async def preview_plan_change(session: AsyncSession, user_id: int, new_plan_code: str):
    """
    Возвращает предварительный расчёт для смены плана (все значения — уже в строках).
    """
    new_plan = cfg.plans[new_plan_code]
    now = datetime.now(timezone.utc)

    sub = await session.scalar(
        select(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    usage = await session.scalar(
        select(Usage).where(Usage.user_id == user_id)
    )

    # Новый тариф
    new_price_per_day = new_plan.price_rub / new_plan.duration_days

    # Нет подписки → чистая покупка
    if not sub or not sub.expires_at:
        return {
            "old_plan": None,
            "leftover_days": 0,
            "leftover_str": "0 часов",
            "converted_days": 0,
            "converted_str": "0 часов",
            "bonus_days_req": 0,
            "bonus_str_req": "0 часов",
            "bonus_days_img": 0,
            "bonus_str_img": "0 часов",
            "final_days": new_plan.duration_days,
            "final_str": format_days_hours(new_plan.duration_days),
        }

    # Текущий тариф
    old_plan = None
    if not sub.is_trial and sub.plan_code:
        old_plan = cfg.plans.get(sub.plan_code)

    # Остаток
    expires = sub.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)

    leftover_days = max((expires - now).total_seconds() / 86400, 0)
    leftover_str = format_days_hours(leftover_days)

    # Если trial → только новая подписка, без конверсий
    if sub.is_trial or old_plan is None:
        return {
            "old_plan": old_plan,
            "leftover_days": leftover_days,
            "leftover_str": leftover_str,
            "converted_days": 0,
            "converted_str": "0 часов",
            "bonus_days_req": 0,
            "bonus_str_req": "0 часов",
            "bonus_days_img": 0,
            "bonus_str_img": "0 часов",
            "final_days": new_plan.duration_days,
            "final_str": format_days_hours(new_plan.duration_days),
        }

    # Стоимость остатка
    old_price_per_day = old_plan.price_rub / old_plan.duration_days
    leftover_value = leftover_days * old_price_per_day
    converted_days = leftover_value / new_price_per_day
    converted_str = format_days_hours(converted_days)

    # Бонусы
    bonus_req = 0
    bonus_img = 0

    if old_plan.max_requests and usage.used_requests < old_plan.max_requests:
        unused = old_plan.max_requests - usage.used_requests
        ratio = unused / old_plan.max_requests
        bonus_req = ratio * new_plan.duration_days

    if old_plan.max_image_generations and usage.used_images < old_plan.max_image_generations:
        unused = old_plan.max_image_generations - usage.used_images
        ratio = unused / old_plan.max_image_generations
        bonus_img = ratio * new_plan.duration_days

    bonus_req_str = format_days_hours(bonus_req)
    bonus_img_str = format_days_hours(bonus_img)

    final_days = new_plan.duration_days + converted_days + bonus_req + bonus_img
    final_str = format_days_hours(final_days)

    return {
        "old_plan": old_plan,
        "leftover_days": leftover_days,
        "leftover_str": leftover_str,
        "converted_days": converted_days,
        "converted_str": converted_str,
        "bonus_days_req": bonus_req,
        "bonus_str_req": bonus_req_str,
        "bonus_days_img": bonus_img,
        "bonus_str_img": bonus_img_str,
        "final_days": final_days,
        "final_str": final_str,
    }