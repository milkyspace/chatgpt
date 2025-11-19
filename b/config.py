from __future__ import annotations
from dataclasses import dataclass, field

from typing import List

from pydantic import BaseModel
import os
from dotenv import load_dotenv

load_dotenv()

class PlanConfig(BaseModel):
    code: str
    title: str
    price_rub: int
    duration_days: int
    max_requests: int | None       # None = безлимит
    max_image_generations: int | None
    max_text_len: int              # макс. символов в одном запросе

class AppConfig(BaseModel):
    # Telegram
    admins: List[int] = [os.getenv("ADMIN_IDS", "")]
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: set[int] = set(map(int, os.getenv("ADMIN_IDS", "0").split(","))) if os.getenv("ADMIN_IDS") else set()

    # DB
    db_url: str = os.getenv("DATABASE_URL", "mysql+aiomysql://root:password@mariadb:3306/ai_bot_db")

    # OpenAI
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_api_base: str | None = os.getenv("OPENAI_API_BASE")

    # AITUNNEL API настройки
    aitunnel_api_key: str = field(default_factory=lambda: os.getenv("AITUNNEL_API_KEY", ""))
    aitunnel_api_base: str = field(default_factory=lambda: os.getenv("AITUNNEL_API_BASE", "https://api.aitunnel.ru/v1"))

    # Платежи (YooMoney/ЮKassa)
    payment_check_interval_min: float = float(os.getenv("PAYMENT_CHECK_INTERVAL_MIN", "1"))
    yookassa_shop_id: str | None = os.getenv("YOOKASSA_SHOP_ID")
    yookassa_secret_key: str | None = os.getenv("YOOKASSA_SECRET_KEY")
    yookassa_invoice_email: str | None = os.getenv("YOOKASSA_INVOICE_EMAIL")
    payment_provider: str = os.getenv("PAYMENT_PROVIDER", "yoomoney")  # yoomoney|mock

    # Тестовый период
    trial_days: int = 3
    trial_max_requests: int = 15
    trial_max_images: int = 3

    # Очереди
    workers_chat: int = 4
    workers_images: int = 2

    # Рефералка
    referral_bonus_days: int = 5

    # Разрешенные режимы
    modes: tuple[str, ...] = ("assistant", "image", "editor", "celebrity_selfie", "add_people")

    # Планы (легко менять)
    plans: dict[str, PlanConfig] = {
        "pro_lite": PlanConfig(
            code="pro_lite", title="Pro Lite", price_rub=499, duration_days=10,
            max_requests=1000, max_image_generations=20, max_text_len=4000
        ),
        "pro_plus": PlanConfig(
            code="pro_plus", title="Pro Plus", price_rub=1290, duration_days=30,
            max_requests=None, max_image_generations=30, max_text_len=32000
        ),
        "pro_premium": PlanConfig(
            code="pro_premium", title="Pro Premium", price_rub=2990, duration_days=90,
            max_requests=None, max_image_generations=50, max_text_len=32000
        ),
    }

cfg = AppConfig()
