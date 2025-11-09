import logging
from typing import Any

from telegram.ext import (
    CallbackContext
)
from yookassa import Payment

from subscription_config import SubscriptionConfig, SubscriptionType
from utils import db, bot_instance

# Настройка логирования
logger = logging.getLogger(__name__)

# Функции для работы с платежами
async def create_subscription_yookassa_payment(user_id: int, subscription_type: SubscriptionType,
                                               context: CallbackContext) -> str:
    """
    Создает платеж в Yookassa для подписки используя централизованную конфигурацию.
    """
    price = SubscriptionConfig.get_price(subscription_type)
    description_config = SubscriptionConfig.get_description(subscription_type)

    try:
        description = f"Подписка {description_config['name']}"
        payment = Payment.create({
            "amount": {"value": price, "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": "https://t.me/gptducksbot"},
            "capture": True,
            "description": description,
            "receipt": {
                "customer": {
                    "email": "liliatchesnokova@gmail.com",
                },
                "items": [
                    {
                        "description": description,
                        "quantity": "1.00",
                        "amount": {
                            "value": price,
                            "currency": "RUB"
                        },
                        "vat_code": "1",
                        "payment_mode": "full_payment",
                        "payment_subject": "commodity",
                    },
                ]
            },
            "metadata": {"user_id": user_id, "subscription_type": subscription_type.value}
        })

        db.create_payment(
            user_id=user_id,
            payment_id=payment.id,
            amount=price,
            payment_type="subscription",
            description=description
        )

        return payment.confirmation.confirmation_url

    except Exception as e:
        logger.error(f"Error creating Yookassa subscription payment: {e}")
        raise e


async def process_successful_payment(payment_info: Any, user_id: int) -> None:
    """
    Обрабатывает успешный платеж используя централизованную конфигурацию.
    """
    try:
        metadata = payment_info.metadata
        subscription_type = metadata.get('subscription_type')

        logger.info(f"Processing successful payment {payment_info.id} for user {user_id}")

        if subscription_type:
            subscription_type_enum = SubscriptionType(subscription_type)
            duration_days = SubscriptionConfig.get_duration(subscription_type_enum).days

            db.add_subscription(user_id, subscription_type_enum, duration_days)
            await send_subscription_confirmation(user_id, subscription_type_enum)
            logger.info(f"Subscription activated for user {user_id}: {subscription_type}")

    except Exception as e:
        logger.error(f"Error processing successful payment: {e}")


async def send_subscription_confirmation(user_id: int, subscription_type: SubscriptionType) -> None:
    """
    Отправляет подтверждение об активации подписки.
    """
    user = db.user_collection.find_one({"_id": user_id})
    if user:
        chat_id = user["chat_id"]
        duration_days = SubscriptionConfig.get_duration(subscription_type).days

        message = (
            f"🎉 Подписка *{subscription_type.name.replace('_', ' ').title()}* активирована!\n"
            f"📅 Действует *{duration_days} дней*\n\n"
            "Теперь вы можете пользоваться ботом по подписке!"
        )

        await bot_instance.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')

