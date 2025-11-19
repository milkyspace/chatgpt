from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Tuple, Optional, Dict, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import cfg, PlanConfig
from models import User, UserSubscription, Usage
from tools.utils import normalize_to_utc, format_days_hours


# =============================================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =============================================================================

def _zero_block(new_plan: PlanConfig) -> Dict[str, Any]:
    """Шаблон нулевых значений для preview."""
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


def _calc_bonus_days(unused: int, price_one_item_old: float, price_one_item_new: float) -> float:
    """
    Общая формула бонусов
    """
    price_old = unused * price_one_item_old
    items = price_old/price_one_item_new
    return items


def _normalize_expires(sub: Optional[UserSubscription]):
    """Возвращает expires_at в UTC или None."""
    if not sub or not sub.expires_at:
        return None
    return normalize_to_utc(sub.expires_at)


def _calculate_conversion(
        old_plan: Optional[PlanConfig],
        new_plan: PlanConfig,
        leftover_days: float,
        usage: Optional[Usage]
) -> Dict[str, float]:
    """
    Единая функция конверсии:
    - leftover → руб → converted_days
    - bonus_req
    - bonus_img
    """
    if not old_plan:
        return {
            "converted": 0.0,
            "bonus_req": 0.0,
            "bonus_img": 0.0
        }

    new_price = new_plan.price_rub / new_plan.duration_days # цена одного дня новая
    old_price = old_plan.price_rub / old_plan.duration_days # цена одного дня старая

    # Конвертация остатка
    leftover_rub = leftover_days * old_price # цена остатка дней в старой подписке
    converted_days = (new_plan.price_rub / leftover_rub) * 0.3

    bonus_req = 0.0
    bonus_img = 0.0

    if usage:
        # запросы
        if old_plan.max_requests:
            unused = old_plan.max_requests - usage.used_requests
            bonus_req = _calc_bonus_days(unused, old_plan.price_rub/old_plan.max_requests, new_plan.price_rub/new_plan.max_requests)

        # изображения
        if old_plan.max_image_generations:
            unused = old_plan.max_image_generations - usage.used_images
            bonus_img = _calc_bonus_days(unused, old_plan.price_rub/old_plan.max_image_generations, new_plan.price_rub/new_plan.max_image_generations)

    return {
        "converted": converted_days,
        "bonus_req": bonus_req * 0.2,
        "bonus_img": bonus_img * 0.2
    }


# =============================================================================
# ОСНОВНАЯ ЛОГИКА СЕРВИСА
# =============================================================================

@dataclass
class SubscriptionUpgradeResult:
    old_plan: PlanConfig | None
    new_plan: PlanConfig
    leftover_days: float
    converted_days: float
    bonus_days_req: float
    bonus_days_img: float
    total_days: float
    expires_at: datetime


async def ensure_user(
        session: AsyncSession,
        tg_user_id: int,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
        referred_by_code: str | None = None,
) -> User:
    """Создаёт пользователя и trial-подписку при первом входе."""
    user = await session.scalar(select(User).where(User.id == tg_user_id))
    if user:
        return user

    ref_code = f"ref{tg_user_id}"
    user = User(
        id=tg_user_id,
        username=username,
        first_name=first_name,
        last_name=last_name,
        referral_code=ref_code,
    )
    session.add(user)
    await session.flush()

    # рефералка
    if referred_by_code:
        referrer = await session.scalar(select(User).where(User.referral_code == referred_by_code))
        if referrer:
            user.referred_by = referrer.id

    # trial
    sub = UserSubscription(
        user_id=user.id,
        plan_code=None,
        is_trial=True,
        expires_at=datetime.now(timezone.utc) + timedelta(days=cfg.trial_days),
    )
    session.add(sub)

    session.add(Usage(user_id=user.id, used_requests=0, used_images=0))

    await session.commit()
    return user


async def has_active_subscription(session: AsyncSession, user_id: int) -> bool:
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    if not sub or not sub.expires_at:
        return False
    return normalize_to_utc(sub.expires_at) > datetime.now(timezone.utc)


async def get_limits(session: AsyncSession, user_id: int) -> Tuple[int | None, int | None, int]:
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    if not sub:
        return (0, 0, 0)

    if sub.is_trial:
        return (cfg.trial_max_requests, cfg.trial_max_images, 4000)

    plan = cfg.plans.get(sub.plan_code or "", None)
    if not plan:
        return (0, 0, 0)

    return (plan.max_requests, plan.max_image_generations, plan.max_text_len)


# =============================================================================
# АКТИВАЦИЯ / АПГРЕЙД
# =============================================================================

async def activate_paid_plan(session: AsyncSession, user_id: int, new_code: str) -> SubscriptionUpgradeResult:
    now = datetime.now(timezone.utc)
    new_plan = cfg.plans[new_code]

    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))

    expires_at = _normalize_expires(sub)

    # 1 — trial или expired или нет подписки → нулевое начало
    if (not sub) or (sub.is_trial) or (not expires_at) or (expires_at <= now):
        new_exp = now + timedelta(days=new_plan.duration_days)

        if not sub:
            sub = UserSubscription(user_id=user_id, plan_code=new_code, is_trial=False, expires_at=new_exp)
            session.add(sub)
        else:
            sub.plan_code = new_code
            sub.is_trial = False
            sub.expires_at = new_exp

        if usage:
            usage.used_requests = 0
            usage.used_images = 0

        await session.commit()

        return SubscriptionUpgradeResult(
            old_plan=None,
            new_plan=new_plan,
            leftover_days=0,
            converted_days=0,
            bonus_days_req=0,
            bonus_days_img=0,
            total_days=new_plan.duration_days,
            expires_at=new_exp
        )

    # 2 — честная конверсия
    leftover = max((expires_at - now).total_seconds() / 86400, 0)
    old_plan = cfg.plans.get(sub.plan_code)

    conv = _calculate_conversion(old_plan, new_plan, leftover, usage)

    total = new_plan.duration_days + conv["converted"] + conv["bonus_req"] + conv["bonus_img"]
    new_exp = now + timedelta(days=total)

    sub.plan_code = new_code
    sub.is_trial = False
    sub.expires_at = new_exp

    if usage:
        usage.used_requests = 0
        usage.used_images = 0

    await session.commit()

    return SubscriptionUpgradeResult(
        old_plan=old_plan,
        new_plan=new_plan,
        leftover_days=leftover,
        converted_days=conv["converted"],
        bonus_days_req=conv["bonus_req"],
        bonus_days_img=conv["bonus_img"],
        total_days=total,
        expires_at=new_exp
    )


# =============================================================================
# ПРЕДВАРИТЕЛЬНЫЙ ПРОСЧЁТ
# =============================================================================

async def preview_plan_change(session: AsyncSession, user_id: int, new_plan_code: str):
    new_plan = cfg.plans[new_plan_code]
    now = datetime.now(timezone.utc)

    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))

    # 0 — подписки нет
    if not sub or not sub.expires_at:
        return _zero_block(new_plan)

    # 1 — trial (не конвертируем)
    if sub.is_trial:
        return _zero_block(new_plan)

    expires = _normalize_expires(sub)
    leftover = max((expires - now).total_seconds() / 86400, 0)

    # 2 — остаток == 0 → прямое новое начало
    if leftover <= 0:
        return _zero_block(new_plan)

    old_plan = cfg.plans.get(sub.plan_code)

    conv = _calculate_conversion(old_plan, new_plan, leftover, usage)

    final = new_plan.duration_days + conv["converted"] + conv["bonus_req"] + conv["bonus_img"]

    return {
        "old_plan": old_plan,

        "leftover_days": leftover,
        "leftover_str": format_days_hours(leftover),

        "converted_days": conv["converted"],
        "converted_str": format_days_hours(conv["converted"]),

        "bonus_days_req": conv["bonus_req"],
        "bonus_str_req": format_days_hours(conv["bonus_req"]),

        "bonus_days_img": conv["bonus_img"],
        "bonus_str_img": format_days_hours(conv["bonus_img"]),

        "final_days": final,
        "final_str": format_days_hours(final),
    }
