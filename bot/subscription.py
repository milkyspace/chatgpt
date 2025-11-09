from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Any, TYPE_CHECKING

if TYPE_CHECKING:
    from subscription_config import SubscriptionConfig


class SubscriptionType(Enum):
    FREE = "free"
    PRO_LITE = "pro_lite"
    PRO_PLUS = "pro_plus"
    PRO_PREMIUM = "pro_premium"


class Subscription:
    """
    Класс подписки пользователя.
    Использует централизованную конфигурацию из SubscriptionConfig.
    """

    __slots__ = ('user_id', 'type', 'purchased_at', 'expires_at', 'requests_used', 'images_used')

    def __init__(
        self,
        user_id: int,
        subscription_type: SubscriptionType,
        purchased_at: datetime,
        expires_at: datetime,
        requests_used: int = 0,
        images_used: int = 0
    ):
        self.user_id = user_id
        self.type = subscription_type
        self.purchased_at = purchased_at
        self.expires_at = expires_at
        self.requests_used = requests_used
        self.images_used = images_used

    def is_active(self) -> bool:
        """Проверяет, активна ли подписка."""
        return datetime.now() < self.expires_at

    def _get_config(self) -> 'SubscriptionConfig':
        """Ленивая загрузка конфигурации для избежания циклических импортов."""
        from subscription_config import SubscriptionConfig
        return SubscriptionConfig

    def can_make_request(self) -> bool:
        """Проверяет, может ли пользователь сделать запрос."""
        return self.is_active() and self._get_config().can_make_request(self.type, self.requests_used)

    def can_generate_image(self) -> bool:
        """Проверяет, может ли пользователь сгенерировать изображение."""
        return self.is_active() and self._get_config().can_generate_image(self.type, self.images_used)

    def get_max_response_length(self) -> int:
        """Возвращает максимальную длину ответа для подписки."""
        limits = self._get_config().get_usage_limits(self.type)
        return limits.get("max_response_length", 2000)

    def to_dict(self) -> Dict[str, Any]:
        """Преобразует подписку в словарь."""
        return {
            "user_id": self.user_id,
            "type": self.type.value,
            "purchased_at": self.purchased_at,
            "expires_at": self.expires_at,
            "requests_used": self.requests_used,
            "images_used": self.images_used
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Subscription':
        """Создает подписку из словаря."""
        return cls(
            user_id=data["user_id"],
            subscription_type=SubscriptionType(data["type"]),
            purchased_at=data["purchased_at"],
            expires_at=data["expires_at"],
            requests_used=data.get("requests_used", 0),
            images_used=data.get("images_used", 0)
        )


# Устаревшие константы - больше не используются
# Вместо них используйте SubscriptionConfig
SUBSCRIPTION_PRICES = {}
SUBSCRIPTION_DURATIONS = {}