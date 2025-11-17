from __future__ import annotations

import requests
from config import cfg
from yookassa import Configuration, Payment as YooPayment
import uuid

import logging

logger = logging.getLogger(__name__)


class YooMoneyProvider:
    """ЮKassa с ручной проверкой статуса (без вебхуков)."""

    def __init__(self):
        Configuration.account_id = cfg.yookassa_shop_id
        Configuration.secret_key = cfg.yookassa_secret_key
        self.email = cfg.yookassa_invoice_email

    async def create_invoice(self, user_id: int, plan_code: str, amount_rub: int, description: str) -> tuple[str, str]:
        """Создает платёж и возвращает (redirect_url, payment_id)"""
        idempotence_key = str(uuid.uuid4())
        try:
            payment = YooPayment.create({
                "amount": {
                    "value": f"{amount_rub:.2f}",
                    "currency": "RUB"
                },
                "confirmation": {
                    "type": "redirect",
                    "return_url": "https://yoomoney.ru"
                },
                "capture": True,
                "description": description or f"План {plan_code}",
                "receipt": {
                    "customer": {
                        "email": self.email,
                    },
                    "items": [
                        {
                            "description": description,
                            "quantity": "1.00",
                            "amount": {
                                "value": f"{amount_rub:.2f}",
                                "currency": "RUB"
                            },
                            "vat_code": "1",
                            "payment_mode": "full_payment",
                            "payment_subject": "commodity",
                        },
                    ]
                },
                "metadata": {
                    "user_id": str(user_id),
                    "plan_code": plan_code
                }
            }, idempotence_key)

            # Возвращаем и URL для редиректа, и ID платежа
            return payment.confirmation.confirmation_url, payment.id

        except requests.exceptions.HTTPError as e:
            logger.error(f"[YooKassa] Ошибка HTTP: {e.response.text}")
            raise

    async def check_status(self, payment_id: str) -> str:
        payment = YooPayment.find_one(payment_id)
        return payment.status
