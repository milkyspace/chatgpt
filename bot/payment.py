import logging
from typing import Any, Dict

from telegram.ext import CallbackContext
from yookassa import Payment

from subscription_config import SubscriptionConfig, SubscriptionType

from utils import db, bot_instance

# Настройка логирования
logger = logging.getLogger(__name__)

# Константы для избежания магических строк
PAYMENT_RETURN_URL = "https://t.me/gptducksbot"
PAYMENT_CURRENCY = "RUB"
PAYMENT_CUSTOMER_EMAIL = "liliatchesnokova@gmail.com"
PAYMENT_TYPE_SUBSCRIPTION = "subscription"


def _create_payment_data(user_id: int, subscription_type: SubscriptionType) -> Dict[str, Any]:
    """Создает данные для платежа Yookassa."""
    price = SubscriptionConfig.get_price(subscription_type)
    description_config = SubscriptionConfig.get_description(subscription_type)
    description = f"Подписка {description_config['name']}"

    return {
        "amount": {"value": price, "currency": PAYMENT_CURRENCY},
        "confirmation": {"type": "redirect", "return_url": PAYMENT_RETURN_URL},
        "capture": True,
        "description": description,
        "receipt": {
            "customer": {"email": PAYMENT_CUSTOMER_EMAIL},
            "items": [
                {
                    "description": description,
                    "quantity": "1.00",
                    "amount": {"value": price, "currency": PAYMENT_CURRENCY},
                    "vat_code": "1",
                    "payment_mode": "full_payment",
                    "payment_subject": "commodity",
                },
            ]
        },
        "metadata": {"user_id": user_id, "subscription_type": subscription_type.value}
    }


async def create_subscription_yookassa_payment(
        user_id: int,
        subscription_type: SubscriptionType,
        context: CallbackContext
) -> str:
    """
    Создает платеж в Yookassa для подписки используя централизованную конфигурацию.
    """
    try:
        payment_data = _create_payment_data(user_id, subscription_type)
        payment = Payment.create(payment_data)

        db.create_payment(
            user_id=user_id,
            payment_id=payment.id,
            amount=payment_data["amount"]["value"],
            payment_type=PAYMENT_TYPE_SUBSCRIPTION,
            description=payment_data["description"]
        )

        return payment.confirmation.confirmation_url

    except Exception as e:
        logger.error(f"Error creating Yookassa subscription payment for user {user_id}: {e}")
        raise


async def process_successful_payment(payment_info: Any, user_id: int) -> None:
    """
    Обрабатывает успешный платеж используя централизованную конфигурацию.
    """
    try:
        metadata = payment_info.metadata
        subscription_type = metadata.get('subscription_type')

        if not subscription_type:
            logger.warning(f"No subscription type in payment {payment_info.id} for user {user_id}")
            return

        logger.info(f"Processing successful payment {payment_info.id} for user {user_id}")

        subscription_type_enum = SubscriptionType(subscription_type)
        duration_days = SubscriptionConfig.get_duration(subscription_type_enum).days

        db.add_subscription(user_id, subscription_type_enum, duration_days)
        await send_subscription_confirmation(user_id, subscription_type_enum)
        logger.info(f"Subscription activated for user {user_id}: {subscription_type}")

    except Exception as e:
        logger.error(f"Error processing successful payment {getattr(payment_info, 'id', 'unknown')}: {e}")


async def send_subscription_confirmation(user_id: int, subscription_type: SubscriptionType) -> None:
    """
    Отправляет подтверждение об активации подписки.
    """
    try:
        user = db.user_collection.find_one({"_id": user_id})
        if not user:
            logger.warning(f"User {user_id} not found for subscription confirmation")
            return

        chat_id = user["chat_id"]
        duration_days = SubscriptionConfig.get_duration(subscription_type).days

        subscription_name = subscription_type.name.replace('_', ' ').title()
        message = (
            f"🎉 Подписка *{subscription_name}* активирована!\n"
            f"📅 Действует *{duration_days} дней*\n\n"
            "Теперь вы можете пользоваться ботом по подписке!"
        )

        await bot_instance.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Error sending subscription confirmation to user {user_id}: {e}")