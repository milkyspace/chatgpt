from __future__ import annotations
from typing import Protocol

class PaymentProvider(Protocol):
    """Интерфейс платёжного провайдера."""
    async def create_invoice(self, user_id: int, plan_code: str, amount_rub: int, description: str) -> str:
        """Создает ссылку на оплату и возвращает URL (redirect)."""
        ...

    # Подтверждение/вебхук — реализуется на стороне вебхука (webhooks.py),
    # который вызывает services.subscriptions.activate_paid_plan(...)
