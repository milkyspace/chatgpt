from __future__ import annotations
from payments.base import PaymentProvider

class MockPaymentProvider(PaymentProvider):
    async def create_invoice(self, user_id: int, plan_code: str, amount_rub: int, description: str) -> tuple[str, str]:
        # Возвращаем фейковую ссылку и ID платежа
        payment_id = f"mock_payment_{user_id}_{plan_code}"
        return f"https://example.com/pay?user={user_id}&plan={plan_code}&sum={amount_rub}", payment_id