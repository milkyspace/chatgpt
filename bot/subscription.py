from datetime import datetime, timedelta
from enum import Enum


class SubscriptionType(Enum):
    FREE = "free"
    PRO_LITE = "pro_lite"
    PRO_PLUS = "pro_plus"
    PRO_PREMIUM = "pro_premium"


class Subscription:
    def __init__(self, user_id: int, subscription_type: SubscriptionType,
                 purchased_at: datetime, expires_at: datetime,
                 requests_used: int = 0, images_used: int = 0):
        self.user_id = user_id
        self.type = subscription_type
        self.purchased_at = purchased_at
        self.expires_at = expires_at
        self.requests_used = requests_used
        self.images_used = images_used

    def is_active(self) -> bool:
        return datetime.now() < self.expires_at

    def can_make_request(self) -> bool:
        if not self.is_active():
            return False

        limits = {
            SubscriptionType.FREE: 10,
            SubscriptionType.PRO_LITE: 1000,
            SubscriptionType.PRO_PLUS: float('inf'),
            SubscriptionType.PRO_PREMIUM: float('inf')
        }
        return self.requests_used < limits.get(self.type, 0)

    def can_generate_image(self) -> bool:
        if not self.is_active():
            return False

        limits = {
            SubscriptionType.FREE: 3,
            SubscriptionType.PRO_LITE: 20,
            SubscriptionType.PRO_PLUS: float('inf'),
            SubscriptionType.PRO_PREMIUM: float('inf')
        }
        return self.images_used < limits.get(self.type, 0)

    def get_max_response_length(self) -> int:
        lengths = {
            SubscriptionType.FREE: 2000,
            SubscriptionType.PRO_LITE: 4000,
            SubscriptionType.PRO_PLUS: 32000,
            SubscriptionType.PRO_PREMIUM: 32000
        }
        return lengths.get(self.type, 2000)


SUBSCRIPTION_PRICES = {
    SubscriptionType.PRO_LITE: 10,
    SubscriptionType.PRO_PLUS: 10,
    SubscriptionType.PRO_PREMIUM: 10
}

SUBSCRIPTION_DURATIONS = {
    SubscriptionType.FREE: timedelta(days=10),
    SubscriptionType.PRO_LITE: timedelta(days=10),
    SubscriptionType.PRO_PLUS: timedelta(days=30),
    SubscriptionType.PRO_PREMIUM: timedelta(days=90)
}