import logging
import asyncio
import traceback
import html
from datetime import datetime, timedelta
import openai
from subscription import SubscriptionType, SUBSCRIPTION_PRICES, SUBSCRIPTION_DURATIONS

import yookassa
from yookassa import Payment, Configuration
import telegram
from telegram import (
    Update,
    User,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackContext,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    AIORateLimiter,
    filters
)
from telegram.constants import ParseMode

import config
import database
import openai_utils

import base64
import json
from json import JSONEncoder
import io
import requests
from telegram import InputFile
import pytz

# setup
db = database.Database()

# Инициализация Yookassa
if config.yookassa_shop_id and config.yookassa_secret_key:
    Configuration.account_id = config.yookassa_shop_id
    Configuration.secret_key = config.yookassa_secret_key

logger = logging.getLogger(__name__)

user_semaphores = {}
user_tasks = {}

HELP_MESSAGE = """<b>Команды:</b>
/new – Начать новый диалог
/mode – Выбрать режим чата
/retry – Перегенерировать ответ бота
/balance – Показать баланс
/topup – Пополнить счёт
/subscription – Управление подписками
/my_payments – Мои платежи
/settings – Показать настройки
/help – Помощь

🎨 Генерация изображений /mode_art
⌨️ Расшифровка голосовых сообщений /mode_mesageaudio
🎤 Вы можете отправлять <b>голосовые Сообщения</b> вместо текста
👥 Добавить бота в <b>групповой чат</b>: /help_group_chat

<blockquote>
1. Чем длиннее диалог — тем выше расходы, потому что я помню контекст. Чтобы начать заново — /new
2. «Ассистент» — режим по умолчанию. Попробуйте другие режимы: /mode
</blockquote>
"""

HELP_GROUP_CHAT_MESSAGE = """Вы можете добавить бота в любой <b>групповой чат</b>, чтобы помогать и развлекать его участников!

Инструкции:
1. Добавьте бота в групповой чат
2. Сделайте его <b>администратором</b>, чтобы он мог видеть сообщения (все остальные права можно ограничить)
3. Вы великолепны!

Чтобы получить ответ от бота в чате – @ <b>упомяните</b> его или <b>ответьте</b> на его сообщение.
Например: "{bot_username} напиши стихотворение о Telegram"
"""


def update_user_roles_from_config(db, roles):
    for role, user_ids in roles.items():
        for user_id in user_ids:
            db.user_collection.update_one(
                {"_id": user_id},
                {"$set": {"role": role}}
            )
    print("User roles updated from config.")


def split_text_into_chunks(text, chunk_size):
    for i in range(0, len(text), chunk_size):
        yield text[i:i + chunk_size]


def configure_logging():
    if config.enable_detailed_logging:
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    else:
        logging.basicConfig(level=logging.CRITICAL, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    logger.setLevel(logging.getLogger().level)


async def register_user_if_not_exists(update: Update, context: CallbackContext, user: User):
    user_registered_now = False
    if not db.check_if_user_exists(user.id):
        db.add_new_user(
            user.id,
            update.message.chat_id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name
        )
        db.add_subscription(
            user.id,
            subscription_type=SubscriptionType.FREE,
            duration_days=7
        )
        user_registered_now = True
        db.start_new_dialog(user.id)

    if db.get_user_attribute(user.id, "current_dialog_id") is None:
        db.start_new_dialog(user.id)

    if user.id not in user_semaphores:
        user_semaphores[user.id] = asyncio.Semaphore(1)

    if db.get_user_attribute(user.id, "current_model") is None:
        db.set_user_attribute(user.id, "current_model", config.models["available_text_models"][0])

    n_used_tokens = db.get_user_attribute(user.id, "n_used_tokens")
    if isinstance(n_used_tokens, int) or isinstance(n_used_tokens, float):
        new_n_used_tokens = {
            "gpt-4-1106-preview": {
                "n_input_tokens": 0,
                "n_output_tokens": n_used_tokens
            }
        }
        db.set_user_attribute(user.id, "n_used_tokens", new_n_used_tokens)

    if db.get_user_attribute(user.id, "n_transcribed_seconds") is None:
        db.set_user_attribute(user.id, "n_transcribed_seconds", 0.0)

    if db.get_user_attribute(user.id, "n_generated_images") is None:
        db.set_user_attribute(user.id, "n_generated_images", 0)

    if user_registered_now:
        username = user.username or "No username"
        first_name = user.first_name or "No first name"
        last_name = user.last_name or "No last name"
        notification_text = f"A new user has just registered!\n\nUsername: {username}\nFirst Name: {first_name}\nLast Name: {last_name}"
        for admin_id in config.roles['admin']:
            try:
                await context.bot.send_message(chat_id=admin_id, text=notification_text)
            except Exception as e:
                print(
                    f"Failed to send registration to admin: {str(e)}\n\n Don't worry, this doesn't affect you in anyway!")


async def is_bot_mentioned(update: Update, context: CallbackContext):
    try:
        message = update.message

        if message.chat.type == "private":
            return True

        if message.text is not None and ("@" + context.bot.username) in message.text:
            return True

        if message.reply_to_message is not None:
            if message.reply_to_message.from_user.id == context.bot.id:
                return True
    except:
        return True
    else:
        return False


async def start_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id

    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    try:
        db.start_new_dialog(user_id)
    except PermissionError as e:
        reply_text = "👋 Привет! Мы <b>Ducks GPT</b>\n"
        reply_text += "Компактный чат-бот на базе <b>ChatGPT</b>\n"
        reply_text += "Рады знакомству!\n\n"
        reply_text += "❌ <b>Для использования бота требуется активная подписка</b>\n\n"
        reply_text += "🎁 <b>100 ₽ за наш счёт при регистрации!</b>\n\n"
        reply_text += "Используйте команду /subscription чтобы посмотреть доступные подписки\n"
        reply_text += "Или /topup чтобы пополнить баланс\n\n"
        reply_text += HELP_MESSAGE

        await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)
        return

    reply_text = "👋 Привет! Мы <b>Ducks GPT</b>\n"
    reply_text += "Компактный чат-бот на базе <b>ChatGPT</b>\n"
    reply_text += "Рады знакомству!\n\n"
    reply_text += "- Доступны в <b>РФ</b>🇷🇺\n"
    reply_text += "- <b>Без месячной подписки</b> — платишь только за использование\n\n"
    reply_text += "🎁 <b>100 ₽ за наш счёт!</b>\n\n"
    reply_text += HELP_MESSAGE

    await update.message.reply_text(reply_text, parse_mode=ParseMode.HTML)


async def help_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())
    await update.message.reply_text(HELP_MESSAGE, parse_mode=ParseMode.HTML)


async def help_group_chat_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text = HELP_GROUP_CHAT_MESSAGE.format(bot_username="@" + context.bot.username)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def process_successful_payment(payment_info, user_id):
    """Обрабатывает успешный платеж"""
    try:
        amount = float(payment_info.amount.value)
        metadata = payment_info.metadata
        is_donation = metadata.get('is_donation', 'false') == 'true'
        subscription_type = metadata.get('subscription_type')

        logger.info(f"Processing successful payment {payment_info.id} for user {user_id}, amount: {amount} RUB")

        if subscription_type:
            subscription_type_enum = SubscriptionType(subscription_type)
            duration_days = SUBSCRIPTION_DURATIONS[subscription_type_enum].days

            db.add_subscription(user_id, subscription_type_enum, duration_days)
            await send_subscription_confirmation(user_id, subscription_type_enum)
            logger.info(f"Subscription activated for user {user_id}: {subscription_type}")

        else:
            if not is_donation:
                db.update_rub_balance(user_id, amount)
                db.update_total_topup(user_id, amount)
                logger.info(f"Balance updated for user {user_id}: +{amount} RUB")
            else:
                db.update_total_donated(user_id, amount)
                logger.info(f"Donation received from user {user_id}: {amount} RUB")

            await send_payment_confirmation(user_id, amount, is_donation)

    except Exception as e:
        logger.error(f"Error processing successful payment: {e}")


async def send_payment_confirmation(user_id, amount_rub, is_donation):
    """Отправляет подтверждение об успешной оплате"""
    user = db.user_collection.find_one({"_id": user_id})
    if user:
        chat_id = user["chat_id"]

        if is_donation:
            message = f"Спасибо за ваше пожертвование *{amount_rub} ₽*! Ваша поддержка очень важна для нас! ❤️❤️"
        else:
            message = f"Пополнение на *{amount_rub} ₽* прошло успешно! 🎉\n\nБаланс обновлен."
            if user.get("role") == "trial_user":
                db.user_collection.update_one(
                    {"_id": user_id},
                    {"$set": {"role": "regular_user"}}
                )
                message += "\n\nВаш статус изменен на *обычного пользователя*! Спасибо за поддержку! ❤️"

        await bot_instance.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')


async def send_subscription_confirmation(user_id, subscription_type):
    """Отправляет подтверждение об активации подписки"""
    user = db.user_collection.find_one({"_id": user_id})
    if user:
        chat_id = user["chat_id"]

        duration_days = SUBSCRIPTION_DURATIONS[subscription_type].days

        message = f"🎉 Подписка *{subscription_type.name.replace('_', ' ').title()}* активирована!\n"
        message += f"📅 Действует *{duration_days} дней*\n\n"
        message += "Теперь вы можете пользоваться ботом по подписке!"

        await bot_instance.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')


async def create_yookassa_payment(user_id: int, amount_rub: int, context: CallbackContext):
    """Создает платеж в Yookassa и возвращает URL для оплаты"""
    try:
        description = "Пополнение баланса"
        if context.user_data.get('is_donation'):
            description = "Добровольное пожертвование"

        currency = "RUB"
        payment = Payment.create({
            "amount": {
                "value": amount_rub,
                "currency": currency
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/gptducksbot"
            },
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
                            "value": amount_rub,
                            "currency": currency
                        },
                        "vat_code": "1",
                        "payment_mode": "full_payment",
                        "payment_subject": "commodity",
                    },
                ]
            },
            "metadata": {
                "user_id": user_id,
                "is_donation": str(context.user_data.get('is_donation', False)).lower()
            }
        })

        db.create_payment(
            user_id=user_id,
            payment_id=payment.id,
            amount=amount_rub,
            payment_type="donation" if context.user_data.get('is_donation') else "topup",
            description=description
        )

        return payment.confirmation.confirmation_url, payment.id

    except Exception as e:
        logger.error(f"Error creating Yookassa payment: {e}")
        raise e


async def create_subscription_yookassa_payment(user_id: int, subscription_type: SubscriptionType,
                                               context: CallbackContext):
    """Создает платеж в Yookassa для подписки"""
    price = SUBSCRIPTION_PRICES[subscription_type]

    try:
        currency = "RUB"
        label = f"Подписка {subscription_type.name.replace('_', ' ').title()}"
        payment = Payment.create({
            "amount": {
                "value": price,
                "currency": currency
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://t.me/gptducksbot"
            },
            "capture": True,
            "description": label,
            "receipt": {
                "customer": {
                    "email": "liliatchesnokova@gmail.com",
                },
                "items": [
                    {
                        "description": label,
                        "quantity": "1.00",
                        "amount": {
                            "value": price,
                            "currency": currency
                        },
                        "vat_code": "1",
                        "payment_mode": "full_payment",
                        "payment_subject": "commodity",
                    },
                ]
            },
            "metadata": {
                "user_id": user_id,
                "subscription_type": subscription_type.value
            }
        })

        db.create_payment(
            user_id=user_id,
            payment_id=payment.id,
            amount=price,
            payment_type="subscription",
            description=f"Подписка {subscription_type.name.replace('_', ' ').title()}"
        )

        return payment.confirmation.confirmation_url

    except Exception as e:
        logger.error(f"Error creating Yookassa subscription payment: {e}")
        raise e


async def subscription_preprocessor(update: Update, context: CallbackContext) -> bool:
    """Проверяет возможность выполнения запроса по подписке"""
    user_id = update.effective_user.id
    subscription_info = db.get_user_subscription_info(user_id)

    if subscription_info["is_active"]:
        if subscription_info["type"] == "pro_lite":
            if subscription_info["requests_used"] >= 1000:
                await update.message.reply_text(
                    "❌ Лимит запросов по вашей подписке исчерпан. "
                    "Пожалуйста, обновите подписку или пополните баланс.",
                    parse_mode=ParseMode.HTML
                )
                return False
        context.user_data['process_allowed'] = True
        return True
    else:
        return await rub_balance_preprocessor(update, context)


async def rub_balance_preprocessor(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    current_rub_balance = db.get_user_rub_balance(user_id)
    minimum_rub_required = 1

    if current_rub_balance < minimum_rub_required:
        context.user_data['process_allowed'] = False
        await update.message.reply_text(
            f"Ваш баланс слишком мал :( Пожалуйста, пополните баланс для продолжения.\nВаш баланс ₽{current_rub_balance:.2f}",
            parse_mode='Markdown'
        )
        return False
    else:
        context.user_data['process_allowed'] = True
        return True


async def retry_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if not await rub_balance_preprocessor(update, context):
        return

    dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
    if len(dialog_messages) == 0:
        await update.message.reply_text("Нет сообщений для перегенерации 🤷‍♂️")
        return

    last_dialog_message = dialog_messages.pop()
    db.set_dialog_messages(user_id, dialog_messages, dialog_id=None)

    await message_handle(update, context, message=last_dialog_message["user"], use_new_dialog_timeout=False)


class CustomEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return JSONEncoder.default(self, obj)


async def _vision_message_handle_fn(
        update: Update, context: CallbackContext, use_new_dialog_timeout: bool = True
):
    logger.info('_vision_message_handle_fn')
    user_id = update.message.from_user.id
    current_model = db.get_user_attribute(user_id, "current_model")

    if current_model != "gpt-4-vision-preview":
        await update.message.reply_text(
            "🥲 Images processing is only available for the <b>GPT-4 Vision</b> model. Please change your settings in /settings",
            parse_mode=ParseMode.HTML,
        )
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    if use_new_dialog_timeout:
        if (datetime.now() - db.get_user_attribute(user_id,
                                                   "last_interaction")).seconds > config.new_dialog_timeout and len(
            db.get_dialog_messages(user_id)) > 0:
            db.start_new_dialog(user_id)
            await update.message.reply_text(f"Запуск нового диалога (<b>{config.chat_modes[chat_mode]['name']}</b>) ✅",
                                            parse_mode=ParseMode.HTML)
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    transcribed_text = ''

    if update.message.voice:
        voice = update.message.voice
        voice_file = await context.bot.get_file(voice.file_id)

        buf = io.BytesIO()
        await voice_file.download_to_memory(buf)
        buf.name = "voice.oga"
        buf.seek(0)

        transcribed_text = await openai_utils.transcribe_audio(buf)
        transcribed_text = transcribed_text.strip()

    buf = None

    if update.message.photo:
        photo = update.message.photo[-1]
        photo_file = await context.bot.get_file(photo.file_id)

        buf = io.BytesIO()
        await photo_file.download_to_memory(buf)
        buf.name = "image.jpg"
        buf.seek(0)

    n_input_tokens, n_output_tokens = 0, 0

    try:
        placeholder_message = await update.message.reply_text("<i>Думаю...</i>", parse_mode=ParseMode.HTML)
        message = update.message.caption or update.message.text or transcribed_text or ''

        await update.message.chat.send_action(action="typing")

        dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
        parse_mode = {"html": ParseMode.HTML, "markdown": ParseMode.MARKDOWN}[
            config.chat_modes[chat_mode]["parse_mode"]
        ]

        chatgpt_instance = openai_utils.ChatGPT(model=current_model)
        if config.enable_message_streaming:
            gen = chatgpt_instance.send_vision_message_stream(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )
        else:
            (
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = await chatgpt_instance.send_vision_message(
                message,
                dialog_messages=dialog_messages,
                image_buffer=buf,
                chat_mode=chat_mode,
            )

            async def fake_gen():
                yield "finished", answer, (
                    n_input_tokens,
                    n_output_tokens,
                ), n_first_dialog_messages_removed

            gen = fake_gen()

        prev_answer = ""
        async for gen_item in gen:
            (
                status,
                answer,
                (n_input_tokens, n_output_tokens),
                n_first_dialog_messages_removed,
            ) = gen_item

            answer = answer[:4096]

            if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                continue

            try:
                await context.bot.edit_message_text(
                    answer,
                    chat_id=placeholder_message.chat_id,
                    message_id=placeholder_message.message_id,
                    parse_mode=parse_mode,
                )
            except telegram.error.BadRequest as e:
                if str(e).startswith("Message is not modified"):
                    continue
                else:
                    await context.bot.edit_message_text(
                        answer,
                        chat_id=placeholder_message.chat_id,
                        message_id=placeholder_message.message_id,
                    )

            await asyncio.sleep(0.01)
            prev_answer = answer

        if buf is not None:
            base_image = base64.b64encode(buf.getvalue()).decode("utf-8")
            new_dialog_message = {"user": [
                {
                    "type": "text",
                    "text": message,
                },
                {
                    "type": "image",
                    "image": base_image,
                }
            ]
                , "bot": answer, "date": datetime.now()}
        else:
            new_dialog_message = {"user": message, "bot": answer, "date": datetime.now()}

        db.set_dialog_messages(
            user_id,
            db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
            dialog_id=None
        )

        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

        action_type = db.get_user_attribute(user_id, "current_model")
        db.deduct_cost_for_action(user_id=user_id, action_type=action_type,
                                  action_params={'n_input_tokens': n_input_tokens, 'n_output_tokens': n_output_tokens})

    except asyncio.CancelledError:
        db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)
        raise

    except Exception as e:
        error_text = f"Something went wrong during completion_1. Reason: {e}"
        logger.error(error_text)
        await update.message.reply_text(error_text)
        return


async def unsupport_message_handle(update: Update, context: CallbackContext, message=None):
    if not await is_bot_mentioned(update, context):
        return

    error_text = f"I don't know how to read files or videos. Send the picture in normal mode (Quick Mode)."
    logger.error(error_text)
    await update.message.reply_text(error_text)
    return


async def show_user_role(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user_role = db.get_user_role(user_id)
    await update.message.reply_text(f"Your current role is ~ `{user_role}` ~  \n\n Pretty neat huh?",
                                    parse_mode='Markdown')


async def show_user_model(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    user_model = db.get_user_model(user_id)
    await update.message.reply_text(f"Your current model is ~ `{user_model}` ~", parse_mode='Markdown')


async def token_balance_command(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    token_balance = db.check_token_balance(user_id)
    await update.message.reply_text(f"Your current token balance is: `{token_balance}`", parse_mode='Markdown')


async def topup_handle(update: Update, context: CallbackContext, chat_id=None):
    user_id = chat_id if chat_id else update.effective_user.id

    if config.yookassa_shop_id is None or config.yookassa_secret_key is None:
        await context.bot.send_message(
            chat_id=user_id,
            text="Система оплаты недоступна :(",
            parse_mode='Markdown'
        )
        return

    rub_amount_options = {
        "₽100": 100,
        "₽300": 300,
        "₽500": 500,
        "₽1000": 1000,
        "₽2000": 2000,
        "₽5000": 5000,
        "Другая сумма...": "custom",
        "Пожертвование ❤️": "donation"
    }

    keyboard = [
        [InlineKeyboardButton(text, callback_data=f"topup|topup_{amount}")]
        for text, amount in rub_amount_options.items()
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=user_id,
        text="Выберите сумму для пополнения баланса:",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )


async def topup_callback_handle(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "topup|topup_custom" or data == "topup|topup_donation":
        is_donation = "donation" in data
        prompt_text = "Спасибо за желание *пожертвовать*! \n\nВведите сумму в рублях:" if is_donation else "Введите *сумму* в рублях:"

        await query.edit_message_text(
            text=prompt_text,
            reply_markup=InlineKeyboardMarkup([]),
            parse_mode='Markdown'
        )

        context.user_data['awaiting_custom_topup'] = "donation" if is_donation else "custom"
        context.user_data['is_donation'] = is_donation
        return

    elif data == "topup|back_to_topup_options":
        context.user_data['awaiting_custom_topup'] = False
        context.user_data.pop('is_donation', None)

        rub_amount_options = {
            "₽100": 100,
            "₽300": 300,
            "₽500": 500,
            "₽1000": 1000,
            "₽2000": 2000,
            "₽5000": 5000,
            "Другая сумма...": "custom",
            "Пожертвование ❤️": "donation"
        }

        keyboard = [
            [InlineKeyboardButton(text, callback_data=f"topup|topup_{amount if amount != 'custom' else 'custom'}")]
            for text, amount in rub_amount_options.items()
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text="Выберите сумму для пополнения баланса:",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    else:
        await query.edit_message_text("⏳ Создаем платеж...")
        context.user_data.pop('is_donation', None)
        user_id = update.effective_user.id
        _, amount_str = query.data.split("_")
        amount_rub = int(amount_str)

        payment_url, payment_id = await create_yookassa_payment(user_id, amount_rub, context)

        payment_text = (
            f"Для оплаты *{amount_rub} ₽* нажмите на кнопку ниже:\n\n"
            "🔐 Платежи обрабатываются через <b>ЮKassa</b> - надежную платежную систему.\n"
            "После успешной оплаты баланс пополнится автоматически в течение 1-2 минут!"
        )
        keyboard = [
            [InlineKeyboardButton("💳 Оплатить", url=payment_url)],
            [InlineKeyboardButton("⬅️ Назад", callback_data="topup|back_to_topup_options")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=payment_text, parse_mode='Markdown', reply_markup=reply_markup,
                                      disable_web_page_preview=True)


async def subscription_handle(update: Update, context: CallbackContext):
    """Показывает доступные подписки"""
    try:
        # Получаем пользователя в зависимости от типа update
        if update.message is not None:
            user = update.message.from_user
            chat_id = update.message.chat_id
        else:
            user = update.callback_query.from_user
            chat_id = update.callback_query.message.chat_id

        await register_user_if_not_exists(update, context, user)
        user_id = user.id
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        subscription_info = db.get_user_subscription_info(user_id)

        text = "🔔 <b>Доступные подписки</b>\n\n"

        if subscription_info["is_active"]:
            expires_str = subscription_info["expires_at"].strftime("%d.%m.%Y")
            text += f"📋 <b>Текущая подписка:</b> {subscription_info['type'].upper()}\n"
            text += f"📅 <b>Действует до:</b> {expires_str}\n"
            if subscription_info["type"] == "pro_lite":
                text += f"📊 <b>Запросы использовано:</b> {subscription_info['requests_used']}/1000\n"
                text += f"🎨 <b>Изображения использовано:</b> {subscription_info['images_used']}/20\n"
            text += "\n"

        subscriptions = [
            {
                "name": "Pro Lite",
                "type": SubscriptionType.PRO_LITE,
                "price": 499,
                "duration": "10 дней",
                "features": "1000 запросов • 20 генераций изображений • До 4000 символов"
            },
            {
                "name": "Pro Plus",
                "type": SubscriptionType.PRO_PLUS,
                "price": 1290,
                "duration": "1 месяц",
                "features": "Безлимитные запросы • До 32000 символов"
            },
            {
                "name": "Pro Premium",
                "type": SubscriptionType.PRO_PREMIUM,
                "price": 2990,
                "duration": "3 месяца",
                "features": "Безлимитные запросы • До 32000 символов"
            }
        ]

        keyboard = []
        for sub in subscriptions:
            btn_text = f"{sub['name']} - {sub['price']}₽"
            callback_data = f"subscribe|{sub['type'].value}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=callback_data)])

        reply_markup = InlineKeyboardMarkup(keyboard)

        for sub in subscriptions:
            text += f"<b>{sub['name']}</b> - {sub['price']}₽ / {sub['duration']}\n"
            text += f"   {sub['features']}\n\n"

        # Отправляем сообщение в зависимости от типа update
        if update.message is not None:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
        else:
            # Используем более безопасный подход для редактирования сообщения
            try:
                await update.callback_query.edit_message_text(
                    text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=reply_markup
                )
            except telegram.error.BadRequest as e:
                if "Message is not modified" in str(e):
                    # Сообщение не изменилось, это нормально
                    pass
                else:
                    # Другая ошибка - переотправляем сообщение
                    await update.callback_query.message.reply_text(
                        text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=reply_markup
                    )

    except Exception as e:
        logger.error(f"Error in subscription_handle: {e}")
        # Отправляем сообщение об ошибке
        if update.callback_query:
            await update.callback_query.message.reply_text(
                "❌ Произошла ошибка при загрузке подписок. Пожалуйста, попробуйте снова.",
                parse_mode=ParseMode.HTML
            )


async def subscription_callback_handle(update: Update, context: CallbackContext):
    """Обрабатывает выбор подписки"""
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "subscription_back":
        try:
            # Возвращаемся в главное меню
            reply_text = "Возврат в главное меню...\n\n" + HELP_MESSAGE

            # Пытаемся отредактировать сообщение
            await query.edit_message_text(
                reply_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        except telegram.error.BadRequest as e:
            if "Message is not modified" in str(e):
                # Сообщение не изменилось - игнорируем
                pass
            else:
                # Другая ошибка - отправляем новое сообщение
                await query.message.reply_text(
                    "Возврат в главное меню...\n\n" + HELP_MESSAGE,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True
                )
        return

    if data.startswith("subscribe|"):
        try:
            _, subscription_type_str = data.split("|")
            subscription_type = SubscriptionType(subscription_type_str)

            price = SUBSCRIPTION_PRICES[subscription_type]
            duration = SUBSCRIPTION_DURATIONS[subscription_type]

            payment_url = await create_subscription_yookassa_payment(
                query.from_user.id, subscription_type, context
            )

            text = f"💳 <b>Оформление подписки {subscription_type.name.replace('_', ' ').title()}</b>\n\n"
            text += f"Стоимость: <b>{price}₽</b>\n"
            text += f"Период: <b>{duration.days} дней</b>\n\n"
            text += "Нажмите кнопку ниже для оплаты. После успешной оплаты подписка активируется автоматически в течение 1-2 минут!"

            keyboard = [
                [InlineKeyboardButton("💳 Оплатить", url=payment_url)],
                [InlineKeyboardButton("⬅️ Назад", callback_data="subscription_back")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

        except Exception as e:
            logger.error(f"Error in subscription payment: {e}")
            await query.edit_message_text(
                "❌ Произошла ошибка при создании платежа. Пожалуйста, попробуйте позже.",
                parse_mode=ParseMode.HTML
            )


async def check_my_payments_handle(update: Update, context: CallbackContext):
    """Показывает статус pending платежей пользователя"""
    await register_user_if_not_exists(update, context, update.message.from_user)
    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    pending_payments = db.get_user_pending_payments(user_id)

    if not pending_payments:
        await update.message.reply_text(
            "У вас нет ожидающих платежей.",
            parse_mode=ParseMode.HTML
        )
        return

    text = "📋 <b>Ваши ожидающие платежи:</b>\n\n"

    for payment in pending_payments:
        amount = payment["amount"]
        payment_id = payment["payment_id"]
        status = payment["status"]
        created_at = payment["created_at"].strftime("%d.%m.%Y %H:%M")

        status_emoji = {
            "pending": "⏳",
            "waiting_for_capture": "🔄",
            "succeeded": "✅",
            "canceled": "❌"
        }.get(status, "❓")

        text += f"{status_emoji} <b>{amount} ₽</b> - {status}\n"
        text += f"   ID: <code>{payment_id}</code>\n"
        text += f"   Создан: {created_at}\n\n"

    text += "Платежи проверяются автоматически каждые 30 секунд."

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


bot_instance = None


async def message_handle(update: Update, context: CallbackContext, message=None, use_new_dialog_timeout=True):
    if not await is_bot_mentioned(update, context):
        return

    if update.edited_message is not None:
        await edited_message_handle(update, context)
        return

    _message = message or update.message.text

    if update.message.chat.type != "private":
        _message = _message.replace("@" + context.bot.username, "").strip()

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    if not await subscription_preprocessor(update, context):
        return

    if chat_mode == "artist":
        await generate_image_handle(update, context, message=message)
        return

    if chat_mode == "stenographer":
        await voice_message_handle(update, context, message=message)
        return

    current_model = db.get_user_attribute(user_id, "current_model")

    if 'awaiting_custom_topup' in context.user_data and context.user_data['awaiting_custom_topup']:
        user_input = update.message.text.replace(',', '.').strip()
        try:
            custom_amount = float(user_input)
            min_amount = 10
            error_message = f"Минимальная сумма пополнения *{min_amount} ₽*. Введите другую сумму."

            if context.user_data['awaiting_custom_topup'] == "donation":
                min_amount = 1
                error_message = f"Минимальная сумма пожертвования *{min_amount} ₽*. Введите другую сумму."

            if custom_amount < min_amount:
                keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="topup|back_to_topup_options")]]
                await context.bot.send_message(
                    chat_id=update.effective_user.id,
                    text=f"{error_message}\n\nНажмите кнопку *назад* чтобы вернуться к выбору суммы",
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='Markdown'
                )
                return

            await update.message.reply_text("⏳ Создаем платеж...", parse_mode='Markdown')

            payment_url, payment_id = await create_yookassa_payment(
                update.effective_user.id, int(custom_amount), context
            )

            thank_you_message = "\n\nСпасибо за вашу поддержку! ❤️" if context.user_data[
                                                                           'awaiting_custom_topup'] == "donation" else ""

            payment_text = (
                f"Для оплаты *{custom_amount:.0f} ₽* нажмите на кнопку ниже:{thank_you_message}\n\n"
                "🔐 Платежи обрабатываются через <b>ЮKassa</b>.\n"
                "После успешной оплаты баланс пополнится автоматически в течение 1-2 минут!"
            )
            keyboard = [
                [InlineKeyboardButton("💳 Оплатить", url=payment_url)],
                [InlineKeyboardButton("⬅️ Назад", callback_data="topup|back_to_topup_options")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=payment_text,
                parse_mode='Markdown',
                reply_markup=reply_markup,
                disable_web_page_preview=True
            )

            context.user_data['awaiting_custom_topup'] = False
            return

        except ValueError:
            keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="topup|back_to_topup_options")]]
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text="*Неверная сумма*. Введите числовое значение в рублях.\n\nНажмите кнопку *назад* чтобы вернуться к выбору суммы",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode='Markdown'
            )
            return

    async def message_handle_fn():
        if use_new_dialog_timeout:
            if (datetime.now() - db.get_user_attribute(user_id,
                                                       "last_interaction")).seconds > config.new_dialog_timeout and len(
                db.get_dialog_messages(user_id)) > 0:
                db.start_new_dialog(user_id)
                await update.message.reply_text(
                    f"Запуск нового диалога(<b>{config.chat_modes[chat_mode]['name']}</b>) ✅",
                    parse_mode=ParseMode.HTML)
        db.set_user_attribute(user_id, "last_interaction", datetime.now())

        n_input_tokens, n_output_tokens = 0, 0

        try:
            placeholder_message = await update.message.reply_text("<i>Думаю...</i>", parse_mode=ParseMode.HTML)

            await update.message.chat.send_action(action="typing")

            if _message is None or len(_message) == 0:
                await update.message.reply_text("🥲 You sent <b>empty message</b>. Please, try again!",
                                                parse_mode=ParseMode.HTML)
                return

            dialog_messages = db.get_dialog_messages(user_id, dialog_id=None)
            parse_mode = {
                "html": ParseMode.HTML,
                "markdown": ParseMode.MARKDOWN
            }[config.chat_modes[chat_mode]["parse_mode"]]

            chatgpt_instance = openai_utils.ChatGPT(model=current_model)

            if config.enable_message_streaming:
                gen = chatgpt_instance.send_message_stream(_message, dialog_messages=dialog_messages,
                                                           chat_mode=chat_mode)

            else:
                answer, (
                    n_input_tokens,
                    n_output_tokens), n_first_dialog_messages_removed = await chatgpt_instance.send_message(
                    _message,
                    dialog_messages=dialog_messages,
                    chat_mode=chat_mode
                )

                async def fake_gen():
                    yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                gen = fake_gen()

            prev_answer = ""

            async for gen_item in gen:
                status, answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed = gen_item

                answer = answer[:4096]

                if abs(len(answer) - len(prev_answer)) < 100 and status != "finished":
                    continue

                try:
                    await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id,
                                                        message_id=placeholder_message.message_id,
                                                        parse_mode=parse_mode, disable_web_page_preview=True)
                except telegram.error.BadRequest as e:
                    if str(e).startswith("Message is not modified"):
                        continue

                    else:
                        await context.bot.edit_message_text(answer, chat_id=placeholder_message.chat_id,
                                                            message_id=placeholder_message.message_id,
                                                            disable_web_page_preview=True)

                await asyncio.sleep(0.01)
                prev_answer = answer

            new_dialog_message = {"user": [{"type": "text", "text": _message}], "bot": answer,
                                  "date": datetime.now()}

            db.set_dialog_messages(
                user_id,
                db.get_dialog_messages(user_id, dialog_id=None) + [new_dialog_message],
                dialog_id=None
            )

            action_type = db.get_user_attribute(user_id, "current_model")
            db.deduct_cost_for_action(user_id=user_id, action_type=action_type,
                                      action_params={'n_input_tokens': n_input_tokens,
                                                     'n_output_tokens': n_output_tokens})

            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

        except asyncio.CancelledError:
            db.update_n_used_tokens(user_id, current_model, n_input_tokens, n_output_tokens)

            action_type = db.get_user_attribute(user_id, "current_model")
            db.deduct_cost_for_action(user_id=user_id, action_type=action_type,
                                      action_params={'n_input_tokens': n_input_tokens,
                                                     'n_output_tokens': n_output_tokens})

            raise

        except Exception as e:
            error_text = f"Something went wrong during completion 2. Reason: {e}"
            logger.error(error_text)
            await update.message.reply_text(error_text)
            return

        if n_first_dialog_messages_removed > 0:
            if n_first_dialog_messages_removed == 1:
                text = "✍️ <i>Note:</i> Your current dialog is too long, so your <b>first message</b> was removed from the context.\n Send /new command to start new dialog"
            else:
                text = f"✍️ <i>Note:</i> Your current dialog is too long, so <b>{n_first_dialog_messages_removed} first messages</b> were removed from the context.\n Send /new command to start new dialog"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async with user_semaphores[user_id]:
        if current_model == "gpt-4-vision-preview" or update.message.photo is not None and len(
                update.message.photo) > 0:
            logger.error('gpt-4-vision-preview')
            if current_model != "gpt-4-vision-preview":
                current_model = "gpt-4-vision-preview"
                db.set_user_attribute(user_id, "current_model", "gpt-4-vision-preview")
            task = asyncio.create_task(
                _vision_message_handle_fn(update, context, use_new_dialog_timeout=use_new_dialog_timeout)
            )
        else:
            task = asyncio.create_task(
                message_handle_fn()
            )

        user_tasks[user_id] = task

        try:
            await task
        except asyncio.CancelledError:
            await update.message.reply_text("✅ Canceled", parse_mode=ParseMode.HTML)
        else:
            pass
        finally:
            if user_id in user_tasks:
                del user_tasks[user_id]


async def is_previous_message_not_answered_yet(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    if user_semaphores[user_id].locked():
        text = "⏳ Please <b>wait</b> for a reply to the previous message\n"
        text += "Or you can /cancel it"
        await update.message.reply_text(text, reply_to_message_id=update.message.id, parse_mode=ParseMode.HTML)
        return True
    else:
        return False


async def voice_message_handle(update: Update, context: CallbackContext):
    if not await is_bot_mentioned(update, context):
        return

    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if not await subscription_preprocessor(update, context):
        return

    chat_mode = db.get_user_attribute(user_id, "current_chat_mode")

    if chat_mode == "stenographer":
        placeholder_message = await update.message.reply_text("⌨️: <i>Распознаю аудио...</i>",
                                                              parse_mode=ParseMode.HTML)
    else:
        placeholder_message = await update.message.reply_text("🎤: <i>Распознаю аудио...</i>", parse_mode=ParseMode.HTML)

    voice = update.message.voice
    voice_file = await context.bot.get_file(voice.file_id)

    buf = io.BytesIO()
    await voice_file.download_to_memory(buf)
    buf.name = "voice.oga"
    buf.seek(0)

    transcribed_text = await openai_utils.transcribe_audio(buf)
    text = f"🎤: <i>{transcribed_text}</i>"

    audio_duration_minutes = voice.duration / 60.0

    db.set_user_attribute(user_id, "n_transcribed_seconds",
                          voice.duration + db.get_user_attribute(user_id, "n_transcribed_seconds"))
    db.deduct_cost_for_action(user_id=user_id, action_type='whisper',
                              action_params={'audio_duration_minutes': audio_duration_minutes})

    if chat_mode == "stenographer":
        transcription_message = f"Your transcription is in: \n\n<code>{transcribed_text}</code>"
        await context.bot.edit_message_text(transcription_message, chat_id=placeholder_message.chat_id,
                                            message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)
        return
    else:
        await context.bot.edit_message_text(text, chat_id=placeholder_message.chat_id,
                                            message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)

    await message_handle(update, context, message=transcribed_text)

    return transcribed_text


async def generate_image_handle(update: Update, context: CallbackContext, message=None):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    user_preferences = db.get_user_attribute(user_id, "image_preferences")

    model = user_preferences.get("model", "dalle-2")
    n_images = user_preferences.get("n_images", 3)
    resolution = user_preferences.get("resolution", "1024x1024")

    if not await subscription_preprocessor(update, context):
        return

    await update.message.chat.send_action(action="upload_photo")

    message = message or update.message.text

    placeholder_message = await update.message.reply_text("<i>Рисуем...</i>", parse_mode=ParseMode.HTML)

    try:
        image_urls = await openai_utils.generate_images(prompt=message or update.message.text, model=model,
                                                        n_images=n_images, size=resolution)
    except openai.error.InvalidRequestError as e:
        if str(e).startswith("Your request was rejected as a result of our safety system"):
            text = "🥲 Your request <b>doesn't comply</b> with OpenAI's usage policies.\nWhat did you write there, huh??"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
            return
        else:
            logging.error(f"OpenAI Invalid Request Error: {str(e)}")
            text = f"⚠️ There was an issue with your request. Please try again.\n\n<b>Reason</b>: {str(e)}"
            await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    except Exception as e:
        logging.error(f"Unexpected Error: {str(e)}")
        text = f"⚠️ An unexpected error occurred. Please try again. \n\n<b>Reason</b>: {str(e)}"
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)
        return

    action_params = {
        "model": model,
        "quality": user_preferences.get("quality", "standard"),
        "resolution": resolution,
        "n_images": n_images
    }

    db.set_user_attribute(user_id, "n_generated_images",
                          n_images + db.get_user_attribute(user_id, "n_generated_images"))
    action_type = user_preferences.get("model", "dalle-3")
    db.deduct_cost_for_action(user_id=user_id, action_type=action_type, action_params=action_params)

    pre_generation_message = f"Нарисовали 🎨:\n\n  <i>{message or ''}</i>  \n\n Подождите немного, изображение почти готово!"
    await context.bot.edit_message_text(pre_generation_message, chat_id=placeholder_message.chat_id,
                                        message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)

    for image_url in image_urls:
        await update.message.chat.send_action(action="upload_photo")
        await upload_image_from_memory(
            bot=context.bot,
            chat_id=update.message.chat_id,
            image_url=image_url
        )

    post_generation_message = f"Нарисовали 🎨:\n\n  <i>{message or ''}</i>  \n\n Как вам??"
    await context.bot.edit_message_text(post_generation_message, chat_id=placeholder_message.chat_id,
                                        message_id=placeholder_message.message_id, parse_mode=ParseMode.HTML)


async def upload_image_from_memory(bot, chat_id, image_url):
    response = requests.get(image_url, stream=True)
    if response.status_code == 200:
        image_buffer = io.BytesIO(response.content)
        image_buffer.name = "image.jpg"
        await bot.send_photo(chat_id=chat_id, photo=InputFile(image_buffer, "image.jpg"))


async def new_dialog_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    current_model = db.get_user_attribute(user_id, "current_model")
    if current_model == "gpt-4-vision-preview":
        db.set_user_attribute(user_id, "current_model", "gpt-4-turbo-2024-04-09")

    try:
        db.start_new_dialog(user_id)
        await update.message.reply_text("Начинаем новый диалог ✅")

        chat_mode = db.get_user_attribute(user_id, "current_chat_mode")
        await update.message.reply_text(f"{config.chat_modes[chat_mode]['welcome_message']}", parse_mode=ParseMode.HTML)
    except PermissionError as e:
        await update.message.reply_text(
            "❌ <b>Для начала нового диалога требуется активная подписка</b>\n\n"
            "Используйте /subscription для управления подписками",
            parse_mode=ParseMode.HTML
        )


async def cancel_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    if user_id in user_tasks:
        task = user_tasks[user_id]
        task.cancel()
    else:
        await update.message.reply_text("<i>Нечего отменять...</i>", parse_mode=ParseMode.HTML)


def get_chat_mode_menu(page_index: int):
    n_chat_modes_per_page = config.n_chat_modes_per_page
    text = f"Выберите <b>режим чата</b> (Доступно {len(config.chat_modes)} режимов):"

    chat_mode_keys = list(config.chat_modes.keys())
    page_chat_mode_keys = chat_mode_keys[page_index * n_chat_modes_per_page:(page_index + 1) * n_chat_modes_per_page]

    keyboard = []
    row = []
    for i, chat_mode_key in enumerate(page_chat_mode_keys):
        name = config.chat_modes[chat_mode_key]["name"]
        row.append(InlineKeyboardButton(name, callback_data=f"set_chat_mode|{chat_mode_key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    if len(chat_mode_keys) > n_chat_modes_per_page:
        is_first_page = (page_index == 0)
        is_last_page = ((page_index + 1) * n_chat_modes_per_page >= len(chat_mode_keys))

        pagination_row = []
        if not is_first_page:
            pagination_row.append(InlineKeyboardButton("«", callback_data=f"show_chat_modes|{page_index - 1}"))
        if not is_last_page:
            pagination_row.append(InlineKeyboardButton("»", callback_data=f"show_chat_modes|{page_index + 1}"))
        if pagination_row:
            keyboard.append(pagination_row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


async def show_chat_modes_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context): return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_chat_mode_menu(0)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def show_chat_modes_callback_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    if await is_previous_message_not_answered_yet(update.callback_query, context): return

    user_id = update.callback_query.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    query = update.callback_query
    await query.answer()

    page_index = int(query.data.split("|")[1])
    if page_index < 0:
        return

    text, reply_markup = get_chat_mode_menu(page_index)
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    except telegram.error.BadRequest as e:
        if str(e).startswith("Message is not modified"):
            pass


async def set_chat_mode_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    chat_mode = query.data.split("|")[1]

    db.set_user_attribute(user_id, "current_chat_mode", chat_mode)
    db.start_new_dialog(user_id)

    await context.bot.send_message(
        update.callback_query.message.chat.id,
        f"{config.chat_modes[chat_mode]['welcome_message']}",
        parse_mode=ParseMode.HTML
    )


def get_settings_menu(user_id: int):
    text = "⚙️ Настройки:"

    keyboard = [
        [InlineKeyboardButton("🧠 Модель нейросети", callback_data='model-ai_model')],
        [InlineKeyboardButton("🎨 Модель художника", callback_data='model-artist_model')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, reply_markup


async def settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)
    if await is_previous_message_not_answered_yet(update, context):
        return

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    text, reply_markup = get_settings_menu(user_id)
    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)


async def set_settings_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update.callback_query, context, update.callback_query.from_user)
    user_id = update.callback_query.from_user.id

    query = update.callback_query
    await query.answer()

    _, model_key = query.data.split("|")
    db.set_user_attribute(user_id, "current_model", model_key)

    await display_model_info(query, user_id, context)


async def display_model_info(query, user_id, context):
    current_model = db.get_user_attribute(user_id, "current_model")
    model_info = config.models["info"][current_model]
    description = model_info["description"]
    scores = model_info["scores"]

    details_text = f"{description}\n\n"
    for score_key, score_value in scores.items():
        details_text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

    details_text += "\nВыберите <b>модель</b>:"

    buttons = []
    claude_buttons = []
    other_buttons = []

    for model_key in config.models["available_text_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title

        if "claude" in model_key.lower():
            callback_data = f"claude-model-set_settings|{model_key}"
            claude_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))
        else:
            callback_data = f"model-set_settings|{model_key}"
            other_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))

    half_size = len(other_buttons) // 2
    first_row = other_buttons[:half_size]
    second_row = other_buttons[half_size:]
    back_button = [InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')]

    reply_markup = InlineKeyboardMarkup([first_row, second_row, claude_buttons, back_button])

    try:
        await query.edit_message_text(text=details_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            pass


async def model_settings_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    data = query.data
    user_id = query.from_user.id

    if data == 'model-ai_model':
        current_model = db.get_user_attribute(user_id, "current_model")
        text = f"{config.models['info'][current_model]['description']}\n\n"

        score_dict = config.models["info"][current_model]["scores"]
        for score_key, score_value in score_dict.items():
            text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

        text += "\nSelect <b>model</b>:\n"

        buttons = []
        claude_buttons = []
        other_buttons = []

        for model_key in config.models["available_text_models"]:
            title = config.models["info"][model_key]["name"]
            if model_key == current_model:
                title = "✅ " + title

            if "claude" in model_key.lower():
                callback_data = f"claude-model-set_settings|{model_key}"
                claude_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))
            else:
                callback_data = f"model-set_settings|{model_key}"
                other_buttons.append(InlineKeyboardButton(title, callback_data=callback_data))

        half_size = len(other_buttons) // 2
        first_row = other_buttons[:half_size]
        second_row = other_buttons[half_size:]
        back_button = [InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')]

        reply_markup = InlineKeyboardMarkup([first_row, second_row, claude_buttons, back_button])

        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    elif data.startswith('claude-model-set_settings|'):
        if config.anthropic_api_key is None or config.anthropic_api_key == "":
            await context.bot.send_message(
                chat_id=user_id,
                text="This bot does not have the Anthropic models available :(",
                parse_mode='Markdown'
            )
            return
        _, model_key = data.split("|")
        db.set_user_attribute(user_id, "current_model", model_key)
        await display_model_info(query, user_id, context)

    elif data.startswith('model-set_settings|'):
        _, model_key = data.split("|")
        if "claude" in model_key.lower() and (config.anthropic_api_key is None or config.anthropic_api_key == ""):
            await context.bot.send_message(
                chat_id=user_id,
                text="This bot does not have the Anthropic models available :(",
                parse_mode='Markdown'
            )
            return
        db.set_user_attribute(user_id, "current_model", model_key)
        await display_model_info(query, user_id, context)

    elif data.startswith('model-artist-set_model|'):
        _, model_key = data.split("|")
        await switch_between_artist_handler(query, user_id, model_key)

    elif data == 'model-artist_model':
        await artist_model_settings_handler(query, user_id)

    elif data.startswith('model-artist-set_model|'):
        _, model_key = data.split("|")
        preferences = db.get_user_attribute(user_id, "image_preferences")
        preferences["model"] = model_key
        db.set_user_attribute(user_id, "image_preferences", preferences)
        await artist_model_settings_handler(query, user_id)

    elif data.startswith("model-artist-set_images|"):
        _, n_images = data.split("|")
        preferences = db.get_user_attribute(user_id, "image_preferences")
        preferences["n_images"] = int(n_images)
        db.set_user_attribute(user_id, "image_preferences", preferences)
        await artist_model_settings_handler(query, user_id)

    elif data.startswith("model-artist-set_resolution|"):
        _, resolution = data.split("|")
        preferences = db.get_user_attribute(user_id, "image_preferences")
        preferences["resolution"] = resolution
        db.set_user_attribute(user_id, "image_preferences", preferences)
        await artist_model_settings_handler(query, user_id)

    elif data.startswith("model-artist-set_quality|"):
        _, quality = data.split("|")
        preferences = db.get_user_attribute(user_id, "image_preferences")
        preferences["quality"] = quality
        db.set_user_attribute(user_id, "image_preferences", preferences)
        await artist_model_settings_handler(query, user_id)

    elif data == 'model-back_to_settings':
        text, reply_markup = get_settings_menu(user_id)
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def artist_model_settings_handler(query, user_id):
    current_preferences = db.get_user_attribute(user_id, "image_preferences")
    current_model = current_preferences.get("model", "dalle-2")

    model_info = config.models["info"][current_model]
    description = model_info["description"]
    scores = model_info["scores"]

    details_text = f"{description}\n\n"
    for score_key, score_value in scores.items():
        details_text += f"{'🟢' * score_value}{'⚪️' * (5 - score_value)} – {score_key}\n"

    buttons = []
    for model_key in config.models["available_image_models"]:
        title = config.models["info"][model_key]["name"]
        if model_key == current_model:
            title = "✅ " + title
        buttons.append(InlineKeyboardButton(title, callback_data=f"model-artist-set_model|{model_key}"))

    if current_model == "dalle-2":
        details_text += "\nFor this model, choose the number of images to generate and the resolution:"
        n_images = current_preferences.get("n_images", 1)
        images_buttons = [
            InlineKeyboardButton(
                f"✅ {i} image" if i == n_images and i == 1 else f"✅ {i} images" if i == n_images else f"{i} image" if i == 1 else f"{i} images",
                callback_data=f"model-artist-set_images|{i}")
            for i in range(1, 4)
        ]
        current_resolution = current_preferences.get("resolution", "1024x1024")
        resolution_buttons = [
            InlineKeyboardButton(f"✅ {res_key}" if res_key == current_resolution else f"{res_key}",
                                 callback_data=f"model-artist-set_resolution|{res_key}")
            for res_key in config.models["info"]["dalle-2"]["resolutions"].keys()
        ]
        keyboard = [buttons] + [images_buttons] + [resolution_buttons]

    elif current_model == "dalle-3":
        details_text += "\nFor this model, choose the quality of the images and the resolution:"
        current_quality = current_preferences.get("quality", "standard")
        quality_buttons = [
            InlineKeyboardButton(f"✅ {quality_key}" if quality_key == current_quality else f"{quality_key}",
                                 callback_data=f"model-artist-set_quality|{quality_key}")
            for quality_key in config.models["info"]["dalle-3"]["qualities"].keys()
        ]
        current_resolution = current_preferences.get("resolution", "1024x1024")
        resolution_buttons = [
            InlineKeyboardButton(f"✅ {res_key}" if res_key == current_resolution else f"{res_key}",
                                 callback_data=f"model-artist-set_resolution|{res_key}")
            for res_key in config.models["info"]["dalle-3"]["qualities"][current_quality]["resolutions"].keys()
        ]
        keyboard = [buttons] + [quality_buttons] + [resolution_buttons]
    else:
        keyboard = [buttons]

    keyboard.append([InlineKeyboardButton("⬅️", callback_data='model-back_to_settings')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text=details_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except telegram.error.BadRequest as e:
        if "Message is not modified" in str(e):
            pass


async def switch_between_artist_handler(query, user_id, model_key):
    preferences = db.get_user_attribute(user_id, "image_preferences")
    preferences["model"] = model_key
    if model_key == "dalle-2":
        preferences["quality"] = "standard"
    elif model_key == "dalle-3":
        preferences["n_images"] = 1
    preferences["resolution"] = "1024x1024"
    db.set_user_attribute(user_id, "image_preferences", preferences)
    await artist_model_settings_handler(query, user_id)


async def show_balance_handle(update: Update, context: CallbackContext):
    await register_user_if_not_exists(update, context, update.message.from_user)

    user_id = update.message.from_user.id
    db.set_user_attribute(user_id, "last_interaction", datetime.now())

    current_rub_balance = db.get_user_rub_balance(user_id)

    text = f"Ваш баланс <b>₽{current_rub_balance:.2f}</b> 💶\n\n"
    text += "Нажмите «🏷️ Детально» для полной информации.\n"

    keyboard = [
        [InlineKeyboardButton("🏷️ Детально", callback_data='show_details')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def callback_show_details(update: Update, context: CallbackContext):
    print("Details button pressed")
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    current_rub_balance = db.get_user_rub_balance(user_id)
    n_used_tokens_dict = db.get_user_attribute(user_id, "n_used_tokens")
    n_generated_images = db.get_user_attribute(user_id, "n_generated_images")
    n_transcribed_seconds = db.get_user_attribute(user_id, "n_transcribed_seconds")
    financials = db.get_user_financials(user_id)
    total_topup = financials['total_topup']
    total_donated = financials['total_donated']
    total_spent = db.get_user_attribute(user_id, "total_spent")

    dalle_2_data = db.get_user_attribute(user_id, "dalle_2") or {"images": 0, "cost": 0.0}
    dalle_3_data = db.get_user_attribute(user_id, "dalle_3") or {"images": 0, "cost": 0.0}

    details_text = "🏷️ Детально:\n"
    total_n_spent_dollars = 0
    total_n_used_tokens = 0

    for model_key in sorted(n_used_tokens_dict.keys()):
        n_input_tokens, n_output_tokens = n_used_tokens_dict[model_key]["n_input_tokens"], \
            n_used_tokens_dict[model_key]["n_output_tokens"]
        total_n_used_tokens += n_input_tokens + n_output_tokens

        n_input_spent_dollars = config.models["info"][model_key]["price_per_1000_input_tokens"] * (
                n_input_tokens / 1000)
        n_output_spent_dollars = config.models["info"][model_key]["price_per_1000_output_tokens"] * (
                n_output_tokens / 1000)
        total_n_spent_dollars += n_input_spent_dollars + n_output_spent_dollars

        details_text += f"- {model_key}: <b>{n_input_spent_dollars + n_output_spent_dollars:.03f}₽</b> / <b>{n_input_tokens + n_output_tokens} tokens</b>\n"

    details_text += f"- DALL·E 2 (генерация изображений): <b>{dalle_2_data['cost']:.03f}₽</b> / <b>{dalle_2_data['images']} images</b>\n"
    details_text += f"- DALL·E 3 (генерация изображений): <b>{dalle_3_data['cost']:.03f}₽</b> / <b>{dalle_3_data['images']} images</b>\n"

    voice_recognition_n_spent_dollars = config.models["info"]["whisper"]["price_per_1_min"] * (
            n_transcribed_seconds / 60)
    total_n_spent_dollars += voice_recognition_n_spent_dollars

    details_text += f"- Whisper (распознавание голоса): <b>{voice_recognition_n_spent_dollars:.03f}₽</b> / <b>{n_transcribed_seconds:.01f} seconds</b>\n"

    text = f"Ваш баланс <b>₽{current_rub_balance:.3f}</b> 💶\n\n"
    text += "Ты:\n\n"
    text += f"   Ещё не сделал(а) первый платёж 😢\n" if total_topup == 0 else f"   Пополнил(а) баланс на <b>{total_topup:.02f}₽</b> ❤️\n" if total_topup < 30 else f"   Пополнил(а) баланс на <b>{total_topup:.02f}₽</b>. Рад, что тебе действительно нравится пользоваться ботом! ❤️\n"
    text += f"   Ещё не делал(а) донатов.\n\n" if total_donated == 0 else f"   Задонатил(а) <b>{total_donated:.02f}₽</b>. Ты — легенда! ❤️\n\n" if total_donated < 10 else f"   \nЗадонатил(а) <b>{total_donated:.02f}₽</b>! Мы очень ценим твою постоянную поддержку! ❤️❤️\n\n"
    text += f"   Потратил(а) ≈ <b>{total_spent:.03f}₽</b> 💵\n"
    text += f"   Использовал(а) <b>{total_n_used_tokens}</b> токенов 🪙\n\n"
    text += details_text

    print("Attempting to edit message")
    try:
        await query.edit_message_text(text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        print(f"Failed to edit message: {e}")
    print("Message edit attempted")


async def edited_message_handle(update: Update, context: CallbackContext):
    if update.edited_message.chat.type == "private":
        text = "🥲 Unfortunately, message <b>editing</b> is not supported"
        await update.edited_message.reply_text(text, parse_mode=ParseMode.HTML)


async def error_handle(update: Update, context: CallbackContext) -> None:
    logger.error(msg="Exception while handling an update:", exc_info=context.error)

    user_id = None
    if update and update.effective_user:
        user_id = update.effective_user.id

    admin_ids = config.roles.get('admin', [])
    is_admin = user_id in admin_ids
    developer = config.developer_username

    try:
        tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
        tb_string = "".join(tb_list)
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            f"An exception was raised while handling an update\n"
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        if is_admin:
            for message_chunk in split_text_into_chunks(message, 4096):
                try:
                    await context.bot.send_message(update.effective_chat.id, message_chunk, parse_mode=ParseMode.HTML)
                except telegram.error.BadRequest:
                    await context.bot.send_message(update.effective_chat.id, message_chunk)
        else:
            error_for_user = (
                f"An unexpected error occurred. "
                f"{'Please try again, or contact ' + ', '.join(developer) + ' if the issue persists.' if developer else 'Please try again or contact the support if the issue persists.'} \n\n"
            )

            await context.bot.send_message(
                update.effective_chat.id,
                error_for_user
            )
    except Exception as handler_error:
        logger.error("Error in error handler: %s", handler_error)
        await context.bot.send_message(update.effective_chat.id, "Some error in error handler")


async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("/new", "Начать новый диалог 🆕"),
        BotCommand("/retry", "Перегенерировать предыдущий запрос 🔁"),
        BotCommand("/mode", "Выбрать режим"),
        BotCommand("/balance", "Показать баланс 💰"),
        BotCommand("/topup", "Пополнить баланс 💳"),
        BotCommand("/subscription", "Управление подписками 🔔"),
        BotCommand("/my_payments", "Мои платежи 📋"),
        BotCommand("/settings", "Настройки ⚙️"),
        BotCommand("/help", "Помощь ❓"),
        BotCommand("/role", "Моя роль 🎫"),
        BotCommand("/model", "Выбрать модель нейросети 🔍"),
    ])


def run_bot() -> None:
    global bot_instance

    # Инициализация Yookassa
    if config.yookassa_shop_id and config.yookassa_secret_key:
        Configuration.account_id = config.yookassa_shop_id
        Configuration.secret_key = config.yookassa_secret_key

    update_user_roles_from_config(db, config.roles)
    configure_logging()

    # Создаем application с обработчиком post_init для настройки фоновых задач
    application = (
        ApplicationBuilder()
        .token(config.telegram_token)
        .concurrent_updates(True)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .http_version("1.1")
        .get_updates_http_version("1.1")
        .post_init(post_init)
        .build()
    )

    bot_instance = application.bot

    # add handlers
    user_filter = filters.ALL
    if len(config.allowed_telegram_usernames) > 0:
        usernames = [x for x in config.allowed_telegram_usernames if isinstance(x, str)]
        any_ids = [x for x in config.allowed_telegram_usernames if isinstance(x, int)]
        user_ids = [x for x in any_ids if x > 0]
        group_ids = [x for x in any_ids if x < 0]
        user_filter = filters.User(username=usernames) | filters.User(user_id=user_ids) | filters.Chat(
            chat_id=group_ids)

    application.add_handler(CommandHandler("start", start_handle, filters=user_filter))
    application.add_handler(CommandHandler("help", help_handle, filters=user_filter))
    application.add_handler(CommandHandler("help_group_chat", help_group_chat_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND & user_filter, message_handle))
    application.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.COMMAND & user_filter, unsupport_message_handle))
    application.add_handler(CommandHandler("retry", retry_handle, filters=user_filter))
    application.add_handler(CommandHandler("new", new_dialog_handle, filters=user_filter))
    application.add_handler(CommandHandler("cancel", cancel_handle, filters=user_filter))

    application.add_handler(MessageHandler(filters.VOICE & user_filter, voice_message_handle))

    application.add_handler(CommandHandler("mode", show_chat_modes_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(show_chat_modes_callback_handle, pattern="^show_chat_modes"))
    application.add_handler(CallbackQueryHandler(set_chat_mode_handle, pattern="^set_chat_mode"))

    application.add_handler(CommandHandler("settings", settings_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(set_settings_handle, pattern="^set_settings"))
    application.add_handler(CallbackQueryHandler(model_settings_handler, pattern='^model-'))
    application.add_handler(CallbackQueryHandler(model_settings_handler, pattern='^claude-model-'))

    application.add_handler(CommandHandler("balance", show_balance_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(callback_show_details, pattern='^show_details$'))

    # payment commands
    application.add_handler(CommandHandler("topup", topup_handle, filters=filters.ALL))
    application.add_handler(CallbackQueryHandler(topup_callback_handle, pattern='^topup\\|'))

    # subscription commands
    application.add_handler(CommandHandler("subscription", subscription_handle, filters=user_filter))
    application.add_handler(CallbackQueryHandler(subscription_callback_handle, pattern='^subscribe\\|'))
    application.add_handler(CallbackQueryHandler(subscription_handle, pattern='^subscription_back$'))

    # payment status command
    application.add_handler(CommandHandler("my_payments", check_my_payments_handle, filters=user_filter))

    # custom commands
    application.add_handler(CommandHandler('role', show_user_role))
    application.add_handler(CommandHandler('model', show_user_model))
    application.add_handler(CommandHandler('token_balance', token_balance_command))

    # admin commands (оставлены для совместимости, можно удалить если не нужны)
    application.add_handler(
        CommandHandler("admin", lambda update, context: update.message.reply_text("Admin commands disabled")))
    application.add_handler(
        CommandHandler('get_user_count', lambda update, context: update.message.reply_text("Admin commands disabled")))
    application.add_handler(
        CommandHandler('list_user_roles', lambda update, context: update.message.reply_text("Admin commands disabled")))

    application.add_error_handler(error_handle)

    # start the bot
    application.run_polling()


# Обновленная функция post_init для добавления фоновых задач
async def post_init(application: Application):
    await application.bot.set_my_commands([
        BotCommand("/new", "Начать новый диалог 🆕"),
        BotCommand("/retry", "Перегенерировать предыдущий запрос 🔁"),
        BotCommand("/mode", "Выбрать режим"),
        BotCommand("/balance", "Показать баланс 💰"),
        BotCommand("/topup", "Пополнить баланс 💳"),
        BotCommand("/subscription", "Управление подписками 🔔"),
        BotCommand("/my_payments", "Мои платежи 📋"),
        BotCommand("/settings", "Настройки ⚙️"),
        BotCommand("/help", "Помощь ❓"),
        BotCommand("/role", "Моя роль 🎫"),
        BotCommand("/model", "Выбрать модель нейросети 🔍"),
    ])

    # Добавляем фоновую задачу для проверки платежей через job_queue
    if config.yookassa_shop_id and config.yookassa_secret_key:
        application.job_queue.run_repeating(
            check_pending_payments_wrapper,
            interval=30,
            first=10
        )


# Обертка для проверки платежей, совместимая с job_queue
async def check_pending_payments_wrapper(context: CallbackContext):
    """Обертка для проверки платежей, совместимая с job_queue"""
    try:
        await check_pending_payments()
    except Exception as e:
        logger.error(f"Error in payment checking job: {e}")


async def check_pending_payments():
    """Проверяет статус pending платежей (одна итерация)"""
    try:
        pending_payments = db.get_pending_payments()

        for payment in pending_payments:
            payment_id = payment["payment_id"]
            user_id = payment["user_id"]

            try:
                payment_info = Payment.find_one(payment_id)
                status = payment_info.status

                db.update_payment_status(payment_id, status)

                if status == 'succeeded':
                    await process_successful_payment(payment_info, user_id)
                elif status == 'canceled':
                    logger.info(f"Payment {payment_id} was canceled")

            except Exception as e:
                logger.error(f"Error checking payment {payment_id}: {e}")

    except Exception as e:
        logger.error(f"Error in payment checking: {e}")

if __name__ == "__main__":
    run_bot()