import os
import sys
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler

bot_dir = Path(__file__).parent.parent
sys.path.append(str(bot_dir))

from flask import Flask, request, jsonify
import yookassa
from yookassa import Payment, Configuration
from telegram import Bot
import config
from database import Database
import redis
import json

# Настройка логирования для продакшена
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        RotatingFileHandler('/var/log/yookassa-webhook.log', maxBytes=10000000, backupCount=5),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

db = Database()
app = Flask(__name__)

# Инициализация Yookassa
if config.yookassa_shop_id and config.yookassa_secret_key:
    Configuration.account_id = config.yookassa_shop_id
    Configuration.secret_key = config.yookassa_secret_key
    logger.info("Yookassa configured successfully")
else:
    logger.error("Yookassa credentials not provided!")

# Инициализация бота Telegram
try:
    bot = Bot(token=config.telegram_token)
    logger.info("Telegram bot initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Telegram bot: {e}")
    bot = None


@app.before_request
def log_request_info():
    """Логируем входящие запросы"""
    logger.info(f"Request: {request.method} {request.url}")
    logger.info(f"Headers: {dict(request.headers)}")
    if request.data:
        logger.info(f"Body: {request.get_data()}")


@app.after_request
def log_response_info(response):
    """Логируем исходящие ответы"""
    logger.info(f"Response: {response.status}")
    return response


@app.route('/yookassa-webhook', methods=['POST'])
def yookassa_webhook():
    """
    Вебхук для обработки уведомлений от Yookassa
    """
    try:
        # Получаем JSON из запроса
        event_json = request.json

        if not event_json:
            logger.error("Empty webhook payload received")
            return jsonify({'error': 'Empty payload'}), 400

        event_type = event_json.get('event')
        payment_object = event_json.get('object', {})

        logger.info(f"Received Yookassa webhook: {event_type}, payment_id: {payment_object.get('id')}")

        # Обрабатываем только успешные платежи
        if event_type == 'payment.succeeded':
            return handle_successful_payment(payment_object)
        elif event_type == 'payment.waiting_for_capture':
            return handle_pending_payment(payment_object)
        elif event_type == 'payment.canceled':
            return handle_canceled_payment(payment_object)
        else:
            logger.warning(f"Unhandled event type: {event_type}")
            return jsonify({'status': 'ignored'}), 200

    except Exception as e:
        logger.error(f"Error processing Yookassa webhook: {e}", exc_info=True)
        return jsonify({'error': 'Internal server error'}), 500


def handle_successful_payment(payment_object):
    """Обрабатывает успешный платеж"""
    try:
        payment_id = payment_object.get('id')
        amount = payment_object.get('amount', {})
        metadata = payment_object.get('metadata', {})

        amount_value = float(amount.get('value', 0))
        user_id = int(metadata.get('user_id', 0))
        is_donation = metadata.get('is_donation', 'false') == 'true'
        subscription_type = metadata.get('subscription_type')

        if not user_id:
            logger.error(f"No user_id in payment metadata: {payment_id}")
            return jsonify({'error': 'No user_id'}), 400

        # Проверяем, не обрабатывали ли мы уже этот платеж
        last_payment_id = db.get_user_attribute(user_id, "last_payment_id")
        if last_payment_id == payment_id:
            logger.info(f"Payment {payment_id} already processed for user {user_id}")
            return jsonify({'status': 'already_processed'}), 200

        logger.info(f"Processing successful payment {payment_id} for user {user_id}, amount: {amount_value} RUB")

        if subscription_type:
            # Это платеж за подписку
            from subscription import SubscriptionType, SUBSCRIPTION_DURATIONS
            subscription_type_enum = SubscriptionType(subscription_type)
            duration_days = SUBSCRIPTION_DURATIONS[subscription_type_enum].days

            db.add_subscription(user_id, subscription_type_enum, duration_days)
            send_subscription_confirmation(user_id, subscription_type_enum)
            logger.info(f"Subscription activated for user {user_id}: {subscription_type}")

        else:
            # Это пополнение баланса
            if not is_donation:
                db.update_rub_balance(user_id, amount_value)
                db.update_total_topup(user_id, amount_value)
                logger.info(f"Balance updated for user {user_id}: +{amount_value} RUB")
            else:
                db.update_total_donated(user_id, amount_value)
                logger.info(f"Donation received from user {user_id}: {amount_value} RUB")

            send_confirmation_message(user_id, amount_value, is_donation)

        # Сохраняем ID платежа чтобы избежать повторной обработки
        db.set_user_attribute(user_id, "last_payment_id", payment_id)

        return jsonify({'status': 'success'}), 200

    except Exception as e:
        logger.error(f"Error handling successful payment: {e}", exc_info=True)
        return jsonify({'error': 'Payment processing failed'}), 500


def handle_pending_payment(payment_object):
    """Обрабатывает платеж ожидающий подтверждения"""
    payment_id = payment_object.get('id')
    logger.info(f"Payment waiting for capture: {payment_id}")

    try:
        # Автоматически подтверждаем платеж
        Payment.capture(payment_id)
        logger.info(f"Payment captured: {payment_id}")
    except Exception as e:
        logger.error(f"Error capturing payment {payment_id}: {e}")

    return jsonify({'status': 'captured'}), 200


def handle_canceled_payment(payment_object):
    """Обрабатывает отмененный платеж"""
    payment_id = payment_object.get('id')
    logger.info(f"Payment canceled: {payment_id}")
    return jsonify({'status': 'canceled'}), 200


def send_confirmation_message(user_id, amount_rub, is_donation):
    """Отправляет подтверждение об успешной оплате через Redis"""
    try:
        redis_client = redis.Redis(host='redis', port=6379, db=0, decode_responses=True)
        data = {
            'user_id': user_id,
            'amount_rub': amount_rub,
            'is_donation': is_donation
        }
        redis_client.publish('payment_notifications', json.dumps(data))
        logger.info(f"Confirmation message sent for user {user_id}")
    except Exception as e:
        logger.error(f"Error sending confirmation message: {e}")


def send_subscription_confirmation(user_id, subscription_type):
    """Отправляет подтверждение об активации подписки"""
    try:
        redis_client = redis.Redis(host='redis', port=6379, db=0, decode_responses=True)
        data = {
            'user_id': user_id,
            'subscription_type': subscription_type.value,
            'message_type': 'subscription'
        }
        redis_client.publish('payment_notifications', json.dumps(data))
        logger.info(f"Subscription confirmation sent for user {user_id}")
    except Exception as e:
        logger.error(f"Error sending subscription confirmation: {e}")


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        # Проверяем соединение с MongoDB
        db.client.admin.command('ismaster')

        # Проверяем соединение с Redis
        redis_client = redis.Redis(host='redis', port=6379, db=0)
        redis_client.ping()

        return jsonify({
            'status': 'healthy',
            'service': 'yookassa-webhook',
            'database': 'connected',
            'redis': 'connected'
        }), 200
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'error': str(e)
        }), 500


@app.route('/', methods=['GET'])
def index():
    """Основная страница"""
    return jsonify({
        'service': 'Yookassa Webhook Handler',
        'status': 'running',
        'endpoints': {
            'webhook': '/yookassa-webhook (POST)',
            'health': '/health (GET)'
        }
    })


if __name__ == '__main__':
    # Проверяем наличие необходимых переменных окружения
    required_env_vars = ['YOOKASSA_SHOP_ID', 'YOOKASSA_SECRET_KEY', 'TELEGRAM_TOKEN']
    missing_vars = [var for var in required_env_vars if not os.getenv(var)]

    if missing_vars:
        logger.error(f"Missing required environment variables: {missing_vars}")
        sys.exit(1)

    # Запускаем сервер
    logger.info("Starting Yookassa webhook server in production mode")
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False,
        threaded=True
    )