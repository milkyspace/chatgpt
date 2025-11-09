"""
Модуль для централизованной конфигурации подписок.
Устраняет дублирование тарифов и настроек подписок.
"""

from datetime import timedelta
from typing import Dict, Any

from .subscription import SubscriptionType


class SubscriptionConfig:
    """Класс для централизованного управления конфигурацией подписок."""

    # Цены подписок (в рублях)
    PRICES = {
        SubscriptionType.PRO_LITE: 499,
        SubscriptionType.PRO_PLUS: 1290,
        SubscriptionType.PRO_PREMIUM: 2990
    }

    # Длительности подписок
    DURATIONS = {
        SubscriptionType.FREE: timedelta(days=7),  # Бесплатная пробная подписка
        SubscriptionType.PRO_LITE: timedelta(days=10),
        SubscriptionType.PRO_PLUS: timedelta(days=30),
        SubscriptionType.PRO_PREMIUM: timedelta(days=90)
    }

    # Лимиты использования для каждой подписки
    USAGE_LIMITS = {
        SubscriptionType.FREE: {
            "max_requests": 15,
            "max_images": 3,
            "max_response_length": 2000
        },
        SubscriptionType.PRO_LITE: {
            "max_requests": 1000,
            "max_images": 20,
            "max_response_length": 4000
        },
        SubscriptionType.PRO_PLUS: {
            "max_requests": float('inf'),  # безлимитно
            "max_images": float('inf'),  # безлимитно
            "max_response_length": 32000
        },
        SubscriptionType.PRO_PREMIUM: {
            "max_requests": float('inf'),  # безлимитно
            "max_images": float('inf'),  # безлимитно
            "max_response_length": 32000
        }
    }

    # Описания подписок для отображения пользователям
    DESCRIPTIONS = {
        SubscriptionType.FREE: {
            "name": "Бесплатная",
            "features": "15 запросов • 3 генерации изображений • До 2000 символов"
        },
        SubscriptionType.PRO_LITE: {
            "name": "Pro Lite",
            "features": "1000 запросов • 20 генераций изображений • До 4000 символов"
        },
        SubscriptionType.PRO_PLUS: {
            "name": "Pro Plus",
            "features": "Безлимитные запросы • До 32000 символов"
        },
        SubscriptionType.PRO_PREMIUM: {
            "name": "Pro Premium",
            "features": "Безлимитные запросы • До 32000 символов"
        }
    }

    @classmethod
    def get_price(cls, subscription_type: SubscriptionType) -> int:
        """Возвращает цену подписки."""
        return cls.PRICES.get(subscription_type, 0)

    @classmethod
    def get_duration(cls, subscription_type: SubscriptionType) -> timedelta:
        """Возвращает длительность подписки."""
        return cls.DURATIONS.get(subscription_type, timedelta(days=0))

    @classmethod
    def get_usage_limits(cls, subscription_type: SubscriptionType) -> Dict[str, Any]:
        """Возвращает лимиты использования для подписки."""
        return cls.USAGE_LIMITS.get(subscription_type, {})

    @classmethod
    def get_description(cls, subscription_type: SubscriptionType) -> Dict[str, str]:
        """Возвращает описание подписки."""
        return cls.DESCRIPTIONS.get(subscription_type, {"name": "", "features": ""})

    @classmethod
    def get_all_paid_subscriptions(cls) -> list:
        """Возвращает список всех платных подписок."""
        return [
            SubscriptionType.PRO_LITE,
            SubscriptionType.PRO_PLUS,
            SubscriptionType.PRO_PREMIUM
        ]

    @classmethod
    def can_make_request(cls, subscription_type: SubscriptionType, requests_used: int) -> bool:
        """Проверяет, может ли пользователь сделать запрос по текущей подписке."""
        limits = cls.get_usage_limits(subscription_type)
        max_requests = limits.get("max_requests", 0)
        return requests_used < max_requests

    @classmethod
    def can_generate_image(cls, subscription_type: SubscriptionType, images_used: int) -> bool:
        """Проверяет, может ли пользователь сгенерировать изображение по текущей подписке."""
        limits = cls.get_usage_limits(subscription_type)
        max_images = limits.get("max_images", 0)
        return images_used < max_images