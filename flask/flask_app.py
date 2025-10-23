import os
import sys
from pathlib import Path

bot_dir = Path(__file__).parent.parent / "bot"
sys.path.append(str(bot_dir))
#import yaml
from flask import Flask, request, jsonify
import stripe
from telegram import Bot
import config
from database import Database
import redis
import json
import subscription

db = Database()
app = Flask(__name__)
bot = Bot(token=config.telegram_token)

stripe.api_key = config.stripe_secret_key
STRIPE_WEBHOOK_SECRET = config.stripe_webhook_secret


@app.route('/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature')

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        return 'Invalid payload', 400
    except stripe.error.SignatureVerificationError as e:
        return 'Invalid signature', 400

    # Handle the event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        user_id = int(session['metadata']['user_id'])

        # Проверяем, это подписка или пополнение баланса
        if 'subscription_type' in session['metadata']:
            # Это платеж за подписку
            subscription_type_str = session['metadata']['subscription_type']
            subscription_type = subscription.SubscriptionType(subscription_type_str)

            # Добавляем подписку пользователю
            duration_days = subscription.SUBSCRIPTION_DURATIONS[subscription_type].days
            db.add_subscription(user_id, subscription_type, duration_days)

            # Отправляем подтверждение
            send_subscription_confirmation(user_id, subscription_type)
        else:
            # Это пополнение баланса (существующая логика)
            is_donation = session['metadata'].get('is_donation', 'false') == 'true'
            total_amount_paid_cents = session['amount_total']
            total_amount_paid_euros = total_amount_paid_cents / 100.0
            net_euro_amount = total_amount_paid_euros

            if not is_donation:
                if total_amount_paid_cents == 125:
                    net_euro_amount = 1.0
                else:
                    net_euro_amount = total_amount_paid_euros

                db.update_euro_balance(user_id, net_euro_amount)
                db.update_total_topup(user_id, total_amount_paid_euros)
            else:
                net_euro_amount = total_amount_paid_euros
                db.update_total_donated(user_id, net_euro_amount)

            send_confirmation_message(user_id, net_euro_amount, is_donation)

    return jsonify({'status': 'success'}), 200


def send_subscription_confirmation(user_id, subscription_type):
    redis_client = redis.Redis(host='redis', port=6379, db=0)
    data = {
        'user_id': user_id,
        'subscription_type': subscription_type.value,
        'message_type': 'subscription'
    }
    redis_client.publish('payment_notifications', json.dumps(data))

def send_confirmation_message(user_id, euro_amount, is_donation):

    if not config.stripe_webhook_secret or config.stripe_webhook_secret.strip() == "":
        print("Stripe webhook secret not set, skipping Redis operations.")
        return

    redis_client = redis.Redis(host='redis', port=6379, db=0)
    data = {
        'user_id': user_id,
        'euro_amount': euro_amount,
        'is_donation': is_donation
    }
    redis_client.publish('payment_notifications', json.dumps(data))


if __name__ == '__main__':
    if not config.stripe_webhook_secret or config.stripe_webhook_secret.strip() == "":
        print("No Stripe webhook secret provided. Exiting.")
        sys.exit(0)
    else:
        app.run(host='0.0.0.0', port=5000)