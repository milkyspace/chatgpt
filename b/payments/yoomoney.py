from __future__ import annotations
from config import cfg
from yookassa import Configuration, Payment as YooPayment
import uuid

class YooMoneyProvider:
    """ЮKassa с ручной проверкой статуса (без вебхуков)."""
    def __init__(self):
        Configuration.account_id = cfg.yookassa_shop_id
        Configuration.secret_key = cfg.yookassa_secret_key

    async def create_invoice(self, user_id: int, plan_code: str, amount_rub: int, description: str) -> str:
        payment = YooPayment.create({
            "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://yoomoney.ru"},
            "capture": True,
            "description": description,
            "metadata": {"user_id": user_id, "plan_code": plan_code}
        }, uuid.uuid4().hex)
        return payment.confirmation.confirmation_url

    async def check_status(self, payment_id: str) -> str:
        payment = YooPayment.find_one(payment_id)
        return payment.status