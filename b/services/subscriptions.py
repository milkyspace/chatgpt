from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import cfg, PlanConfig
from models import User, UserSubscription, Usage


async def ensure_user(
    session: AsyncSession,
    tg_user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None,
    referred_by_code: str | None = None,
) -> User:
    """Создаёт пользователя, подписку trial и usage при первом входе."""
    user = await session.scalar(select(User).where(User.id == tg_user_id))
    if user:
        return user

    # генерируем реф. код
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

    # связываем рефера (если есть код)
    if referred_by_code:
        referrer = await session.scalar(
            select(User).where(User.referral_code == referred_by_code)
        )
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
    """Проверка, есть ли активная подписка (учитываем срок действия)."""
    sub = await session.scalar(
        select(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    if not sub or not sub.expires_at:
        return False

    now = datetime.now(timezone.utc)
    expires_at = sub.expires_at

    # Нормализуем к UTC
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    else:
        expires_at = expires_at.astimezone(timezone.utc)

    return expires_at > now


async def get_limits(session: AsyncSession, user_id: int) -> Tuple[int | None, int | None, int]:
    """
    Возвращает (max_requests, max_images, max_text_len) для текущего плана/триала.
    Если нет подписки — всё по нулям.
    """
    sub = await session.scalar(
        select(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    if not sub:
        return (0, 0, 0)

    if sub.is_trial:
        return (cfg.trial_max_requests, cfg.trial_max_images, 4000)

    plan = cfg.plans.get(sub.plan_code or "", None)
    if not plan:
        return (0, 0, 0)

    return (plan.max_requests, plan.max_image_generations, plan.max_text_len)


@dataclass
class SubscriptionUpgradeResult:
    """
    Результат перерасчёта подписки (для аналитики и уведомлений).

    Все значения в днях — float, чтобы можно было красиво форматировать
    в человекочитаемый вид (через format_days_hours).
    """
    old_plan: PlanConfig | None
    new_plan: PlanConfig
    leftover_days: float          # сколько условно "осталось" по старому тарифу (по времени)
    converted_days: float         # сколько дней новой подписки даёт конвертация остатка
    bonus_days_req: float         # "вклад" неиспользованных запросов в эти дни
    bonus_days_img: float         # "вклад" неиспользованных изображений
    total_days: float             # общий срок действия новой подписки (в днях)
    expires_at: datetime          # новая дата окончания


def _compute_plan_change_preview(
    *,
    old_plan: PlanConfig | None,
    expires_at: datetime | None,
    usage: Usage | None,
    new_plan: PlanConfig,
    now: datetime,
) -> SubscriptionUpgradeResult:
    """
    Честный финансовый пересчёт подписки.

    Идея:
    - Считаем долю использованного тарифа по трём измерениям:
      время, запросы, изображения.
    - Берём ИСПОЛЬЗОВАННУЮ долю как максимум по этим измерениям
      (чтобы не "переплачивать" дважды).
    - Деньги за НЕИСПОЛЬЗОВАННУЮ долю старого тарифа конвертируем
      в дни нового тарифа.
    - При этом разбрасываем дополнительные дни по "каналам":
      остаток по времени / запросам / изображениям — только ДЛЯ ОТОБРАЖЕНИЯ
      (сумма компонент = total_extra_days, чисто визуально).

    Важно:
    - Суммарная ценность никогда не превышает price_old.
    - Нельзя получить "бесконечные" бонусы за лимиты.
    """

    # Если старого тарифа по сути нет — просто выдаём новый.
    if not old_plan or not expires_at or expires_at <= now:
        total_days = float(new_plan.duration_days)
        new_expires_at = now + timedelta(days=total_days)
        return SubscriptionUpgradeResult(
            old_plan=None,
            new_plan=new_plan,
            leftover_days=0.0,
            converted_days=0.0,
            bonus_days_req=0.0,
            bonus_days_img=0.0,
            total_days=total_days,
            expires_at=new_expires_at,
        )

    # ----- 1. Остаток по времени -----
    total_duration_old = float(old_plan.duration_days)

    raw_leftover = (expires_at - now).total_seconds() / 86400.0
    # Не даём остатку быть больше исходной длительности и меньше 0
    leftover_days = max(0.0, min(raw_leftover, total_duration_old))

    time_unused_ratio = leftover_days / total_duration_old
    time_used_ratio = 1.0 - time_unused_ratio

    # ----- 2. Остаток по запросам -----
    req_used_ratio = None
    req_unused_ratio = None
    if usage is not None and old_plan.max_requests is not None and old_plan.max_requests > 0:
        req_used_ratio = min(usage.used_requests / old_plan.max_requests, 1.0)
        req_unused_ratio = 1.0 - req_used_ratio

    # ----- 3. Остаток по изображениям -----
    img_used_ratio = None
    img_unused_ratio = None
    if usage is not None and old_plan.max_image_generations is not None and old_plan.max_image_generations > 0:
        img_used_ratio = min(usage.used_images / old_plan.max_image_generations, 1.0)
        img_unused_ratio = 1.0 - img_used_ratio

    # ----- 4. Общая использованная доля тарифа -----
    used_candidates = [time_used_ratio]
    if req_used_ratio is not None:
        used_candidates.append(req_used_ratio)
    if img_used_ratio is not None:
        used_candidates.append(img_used_ratio)

    # На всякий случай защищаемся
    used_fraction = max(used_candidates) if used_candidates else 1.0
    used_fraction = max(0.0, min(used_fraction, 1.0))

    leftover_fraction = 1.0 - used_fraction

    # Если всё использовано — никаких доп. дней
    if leftover_fraction <= 0:
        total_days = float(new_plan.duration_days)
        new_expires_at = now + timedelta(days=total_days)
        return SubscriptionUpgradeResult(
            old_plan=old_plan,
            new_plan=new_plan,
            leftover_days=leftover_days,
            converted_days=0.0,
            bonus_days_req=0.0,
            bonus_days_img=0.0,
            total_days=total_days,
            expires_at=new_expires_at,
        )

    # ----- 5. Перевод остаточной стоимости в дни нового тарифа -----
    price_old = float(old_plan.price_rub)
    price_new = float(new_plan.price_rub)

    # Цена одного дня нового плана
    price_per_day_new = price_new / float(new_plan.duration_days)

    # Денежная ценность неиспользованной доли старого плана
    leftover_value_rub = leftover_fraction * price_old

    # Сколько дней нового плана даёт эта ценность
    extra_total_days = leftover_value_rub / price_per_day_new

    # ----- 6. Разбиваем extra_total_days между "остатком" / "запросами" / "картинками"
    # (для красивой аналитики, сумма компонент = extra_total_days)
    comp_time = time_unused_ratio
    comp_req = req_unused_ratio or 0.0
    comp_img = img_unused_ratio or 0.0

    comp_sum = comp_time + comp_req + comp_img

    if comp_sum > 0 and extra_total_days > 0:
        converted_days = extra_total_days * (comp_time / comp_sum)  # как бы "за время"
        bonus_days_req = extra_total_days * (comp_req / comp_sum)
        bonus_days_img = extra_total_days * (comp_img / comp_sum)
    else:
        converted_days = 0.0
        bonus_days_req = 0.0
        bonus_days_img = 0.0

    total_days = float(new_plan.duration_days) + extra_total_days
    new_expires_at = now + timedelta(days=total_days)

    return SubscriptionUpgradeResult(
        old_plan=old_plan,
        new_plan=new_plan,
        leftover_days=leftover_days,
        converted_days=converted_days,
        bonus_days_req=bonus_days_req,
        bonus_days_img=bonus_days_img,
        total_days=total_days,
        expires_at=new_expires_at,
    )


async def activate_paid_plan(
    session: AsyncSession,
    user_id: int,
    new_code: str,
) -> UserSubscription:
    """
    Активация платного плана (вызывается после оплаты).

    Вариант A: честная финансовая модель.
    - Если не было платного плана / триал / истёк → просто выдаём новый срок.
    - Если был активный платный план → считаем, какая ЧАСТЬ ЕГО ЦЕНЫ
      ещё не "потрачена", и переводим её в дни нового плана.
    """
    now = datetime.now(timezone.utc)
    new_plan = cfg.plans[new_code]

    # Текущая подписка и usage
    sub = await session.scalar(
        select(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    usage = await session.scalar(
        select(Usage).where(Usage.user_id == user_id)
    )

    # ----- Случай 1: платного плана ещё не было / триал / истёк -----
    if (
        not sub
        or not sub.expires_at
        or sub.expires_at <= now
        or sub.is_trial
        or not sub.plan_code
    ):
        expires_at = now + timedelta(days=new_plan.duration_days)

        if not sub:
            sub = UserSubscription(
                user_id=user_id,
                plan_code=new_code,
                is_trial=False,
                expires_at=expires_at,
            )
            session.add(sub)
        else:
            sub.plan_code = new_code
            sub.is_trial = False
            sub.expires_at = expires_at

        # Сбрасываем usage
        if usage:
            usage.used_requests = 0
            usage.used_images = 0

        await session.commit()
        return sub

    # ----- Случай 2: есть активный платный тариф → делаем конвертацию -----
    old_plan = cfg.plans.get(sub.plan_code)
    result = _compute_plan_change_preview(
        old_plan=old_plan,
        expires_at=sub.expires_at,
        usage=usage,
        new_plan=new_plan,
        now=now,
    )

    # Обновляем подписку по расчёту
    sub.plan_code = new_code
    sub.is_trial = False
    sub.expires_at = result.expires_at

    # Сброс usage под новый период
    if usage:
        usage.used_requests = 0
        usage.used_images = 0

    await session.commit()
    return sub


async def preview_plan_change(
    session: AsyncSession,
    user_id: int,
    new_plan_code: str,
) -> dict:
    """
    Возвращает предварительный расчёт для смены плана перед оплатой.

    Структура ответа:
        {
            "old_plan": PlanConfig | None,
            "leftover_days": float,
            "converted_days": float,
            "bonus_days_req": float,
            "bonus_days_img": float,
            "final_days": float,
        }

    ВАЖНО:
    - Этот расчёт использует тот же алгоритм, что и activate_paid_plan,
      чтобы предварительный просмотр совпадал с реальным результатом.
    """
    from models import UserSubscription, Usage  # локальный импорт, чтобы избежать циклов

    new_plan = cfg.plans[new_plan_code]
    now = datetime.now(timezone.utc)

    sub = await session.scalar(
        select(UserSubscription).where(UserSubscription.user_id == user_id)
    )
    usage = await session.scalar(
        select(Usage).where(Usage.user_id == user_id)
    )

    # Нет подписки вообще → просто покупка нового плана
    if not sub or not sub.expires_at:
        return {
            "old_plan": None,
            "leftover_days": 0.0,
            "converted_days": 0.0,
            "bonus_days_req": 0.0,
            "bonus_days_img": 0.0,
            "final_days": float(new_plan.duration_days),
        }

    # Если триал или план не указан → тоже без конверсии
    if sub.is_trial or not sub.plan_code:
        return {
            "old_plan": None,
            "leftover_days": 0.0,
            "converted_days": 0.0,
            "bonus_days_req": 0.0,
            "bonus_days_img": 0.0,
            "final_days": float(new_plan.duration_days),
        }

    old_plan = cfg.plans.get(sub.plan_code)

    result = _compute_plan_change_preview(
        old_plan=old_plan,
        expires_at=sub.expires_at,
        usage=usage,
        new_plan=new_plan,
        now=now,
    )

    return {
        "old_plan": result.old_plan,
        "leftover_days": result.leftover_days,
        "converted_days": result.converted_days,
        "bonus_days_req": result.bonus_days_req,
        "bonus_days_img": result.bonus_days_img,
        "final_days": result.total_days,
    }
