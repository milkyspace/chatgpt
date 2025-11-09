"""
Модуль для централизованной конфигурации подписок.
Устраняет дублирование тарифов и настроек подписок.
"""

from datetime import timedelta
from typing import Dict, Any, List
from subscription import SubscriptionType


class SubscriptionConfig:
    """Класс для централизованного управления конфигурацией подписок."""

    # Базовые конфигурации для каждого типа подписки
    _CONFIG = {
        SubscriptionType.FREE: {
            "price": 0,
            "duration": timedelta(days=7),
            "usage_limits": {
                "max_requests": 15,
                "max_images": 3,
                "max_response_length": 2000
            },
            "description": {
                "name": "Бесплатная",
                "features": "15 запросов • 3 генерации изображений • До 2000 символов"
            }
        },
        SubscriptionType.PRO_LITE: {
            "price": 499,
            "duration": timedelta(days=10),
            "usage_limits": {
                "max_requests": 1000,
                "max_images": 20,
                "max_response_length": 4000
            },
            "description": {
                "name": "Pro Lite",
                "features": "1000 запросов • 20 генераций изображений • До 4000 символов"
            }
        },
        SubscriptionType.PRO_PLUS: {
            "price": 1290,
            "duration": timedelta(days=30),
            "usage_limits": {
                "max_requests": float('inf'),
                "max_images": float('inf'),
                "max_response_length": 32000
            },
            "description": {
                "name": "Pro Plus",
                "features": "Безлимитные запросы • До 32000 символов"
            }
        },
        SubscriptionType.PRO_PREMIUM: {
            "price": 2990,
            "duration": timedelta(days=90),
            "usage_limits": {
                "max_requests": float('inf'),
                "max_images": float('inf'),
                "max_response_length": 32000
            },
            "description": {
                "name": "Pro Premium",
                "features": "Безлимитные запросы • До 32000 символов"
            }
        }
    }

    # Кэш для платных подписок
    _PAID_SUBSCRIPTIONS = None

    @classmethod
    def get_price(cls, subscription_type: SubscriptionType) -> int:
        """Возвращает цену подписки."""
        return cls._get_config_value(subscription_type, "price", 0)

    @classmethod
    def get_duration(cls, subscription_type: SubscriptionType) -> timedelta:
        """Возвращает длительность подписки."""
        return cls._get_config_value(subscription_type, "duration", timedelta(days=0))

    @classmethod
    def get_usage_limits(cls, subscription_type: SubscriptionType) -> Dict[str, Any]:
        """Возвращает лимиты использования для подписки."""
        return cls._get_config_value(subscription_type, "usage_limits", {})

    @classmethod
    def get_description(cls, subscription_type: SubscriptionType) -> Dict[str, str]:
        """Возвращает описание подписки."""
        return cls._get_config_value(subscription_type, "description", {"name": "", "features": ""})

    @classmethod
    def _get_config_value(cls, subscription_type: SubscriptionType, key: str, default: Any) -> Any:
        """Вспомогательный метод для получения значения из конфигурации."""
        config = cls._CONFIG.get(subscription_type, {})
        return config.get(key, default)

    @classmethod
    def get_all_paid_subscriptions(cls) -> List[SubscriptionType]:
        """Возвращает список всех платных подписок."""
        if cls._PAID_SUBSCRIPTIONS is None:
            cls._PAID_SUBSCRIPTIONS = [
                sub_type for sub_type in cls._CONFIG.keys()
                if sub_type != SubscriptionType.FREE and cls.get_price(sub_type) > 0
            ]
        return cls._PAID_SUBSCRIPTIONS

    @classmethod
    def can_make_request(cls, subscription_type: SubscriptionType, requests_used: int) -> bool:
        """Проверяет, может ли пользователь сделать запрос по текущей подписке."""
        max_requests = cls.get_usage_limits(subscription_type).get("max_requests", 0)
        return requests_used < max_requests

    @classmethod
    def can_generate_image(cls, subscription_type: SubscriptionType, images_used: int) -> bool:
        """Проверяет, может ли пользователь сгенерировать изображение по текущей подписке."""
        max_images = cls.get_usage_limits(subscription_type).get("max_images", 0)
        return images_used < max_images