from __future__ import annotations
from b.payments.base import PaymentProvider

class MockPaymentProvider(PaymentProvider):
    async def create_invoice(self, user_id: int, plan_code: str, amount_rub: int, description: str) -> str:
        # Возвращаем фейковую ссылку
        return f"https://example.com/pay?user={user_id}&plan={plan_code}&sum={amount_rub}"
