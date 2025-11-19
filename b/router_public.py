from __future__ import annotations

import asyncio
import random
import logging

from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import BufferedInputFile
from aiogram.filters import Command, CommandStart
from aiogram.types import Message as TgMessage, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from sqlalchemy import update
from aiogram.types import CallbackQuery, User

from config import cfg
from db import AsyncSessionMaker
from keyboards import top_panel, keyboards_for_modes, help_main_menu, plan_buy_keyboard, help_back_kb
from models import (
    User,
    ChatSession,
    UserSubscription,
    Usage,
    Payment,
)
from payments.yoomoney import YooMoneyProvider
from queue_bg import AsyncWorkerPool
from services.chat import ChatService
from services.images import ImageService
from services.subscriptions import ensure_user
from services.usage import spend_request, can_spend_image, spend_image
from aiogram.fsm.state import default_state
from aiogram.filters import StateFilter

router = Router()

# Пулы фоновых задач
chat_pool = AsyncWorkerPool(cfg.workers_chat)
img_pool = AsyncWorkerPool(cfg.workers_images)

logger = logging.getLogger(__name__)


@router.startup()
async def _startup(bot):
    """Запуск фоновых пулов при старте бота."""
    await chat_pool.start()
    await img_pool.start()


@router.shutdown()
async def _shutdown(bot):
    """Остановка фоновых пулов при завершении работы."""
    await chat_pool.stop()
    await img_pool.stop()


async def animate_panel_change(message, new_text: str, new_markup=None):
    """
    Плавное обновление текста без скачков.
    Используем ZWJ и мини-переход, который Telegram
    отрисовывает как мягкое изменение.
    """
    try:
        # Шаг 1: добавляем невидимый символ для запуска "перерисовки"
        zwj_text = new_text + "\u2063"  # Zero-width joiner
        await message.edit_text(zwj_text, reply_markup=new_markup)
        await asyncio.sleep(0.03)

        # Шаг 2: финальный текст (ничего не скачет)
        await message.edit_text(new_text, reply_markup=new_markup)

    except Exception:
        await message.edit_text(new_text, reply_markup=new_markup)


def build_progress_bar(used: int, max_val: int | None, segments: int = 8) -> str:
    """
    Адаптивный прогресс-бар:
    - 20 сегментов
    - цветовая индикация (красный/желтый/зелёный)
    - поддержка безлимита

    Вернёт строку вида:
    🟩🟩🟩🟨🟨🟥⬛⬛⬛⬛ ...
    """

    # Безлимит
    if max_val is None:
        return "🟩" * segments

    # Защита от деления
    max_val = max_val or 1

    pct = min(100, int((used / max_val) * 100))
    filled = pct * segments // 100

    # Цветовая схема
    if pct <= 30:
        color = "🟥"
    elif pct <= 70:
        color = "🟨"
    else:
        color = "🟩"

    bar = color * filled + "⬜️" * (segments - filled)
    return bar


async def _render_status_line(session, user_id: int) -> str:
    """
    Улучшенный статус подписки:
    - цветовой статус (зел/жел/кр)
    - тариф
    - оставшиеся дни
    - лимиты + прогресс бары (20 сег)
    - личный ID
    """

    # --- Загружаем ---
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))
    user = await session.scalar(select(User).where(User.id == user_id))

    now = datetime.now(timezone.utc)

    used_req = usage.used_requests if usage else 0
    used_img = usage.used_images if usage else 0

    # Значения по умолчанию
    status_icon = "🔴"
    status_text = "Неактивна"
    expires_str = "—"
    days_left_str = "—"
    plan_name = "Нет"
    max_req = 0
    max_img = 0

    if sub:
        expires_at = sub.expires_at
        if expires_at:
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            expires_at = expires_at.astimezone(timezone.utc)

        # Активна?
        if expires_at and expires_at > now:
            # Days left
            days_left = (expires_at - now).days
            days_left_str = str(days_left)
            expires_str = expires_at.astimezone().strftime("%d.%m.%Y %H:%M")

            # Цветовая индикация
            if days_left <= 3:
                status_icon = "🟡"
                status_text = "Скоро заканчивается"
            else:
                status_icon = "🟢"
                status_text = "Активна"

            # Тариф
            if sub.is_trial:
                plan_name = "Trial"
                max_req = cfg.trial_max_requests
                max_img = cfg.trial_max_images
            else:
                plan = cfg.plans.get(sub.plan_code)
                plan_name = plan.title if plan else sub.plan_code
                max_req = plan.max_requests
                max_img = plan.max_image_generations
        else:
            status_icon = "🔴"
            status_text = "Истекла"

    # --- Прогресс-бары ---
    req_bar = build_progress_bar(used_req, max_req)
    img_bar = build_progress_bar(used_img, max_img)

    # Значения для лимитов
    def fmt(v):
        return "∞" if v is None else v

    limits_text = (
        f"Запросы: {used_req}/{fmt(max_req)}\n"
        f"{req_bar}\n\n"
        f"Изображения: {used_img}/{fmt(max_img)}\n"
        f"{img_bar}"
    )

    # --- Финальный текст ---
    return (
        "📊 <b>Подписка</b>\n"
        f"<b>Статус:</b> {status_icon} {status_text}\n"
        f"<b>Тариф:</b> {plan_name}\n"
        f"<b>Действует до:</b> {expires_str}\n"
        f"<b>Осталось дней:</b> {days_left_str}\n"
        "\n"
        "📈 <b>Лимиты</b>\n"
        f"{limits_text}\n"
        "\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>"
    )


@router.message(CommandStart())
async def start(m: TgMessage):
    ref_code = None
    if m.text and " " in m.text:
        ref_code = m.text.split(" ", 1)[1].strip()

    async with AsyncSessionMaker() as session:
        user = await ensure_user(session, m.from_user.id, m.from_user.username,
                                 m.from_user.first_name, m.from_user.last_name, ref_code)

        status_panel = await _render_status_line(session, m.from_user.id)

    me = await m.bot.get_me()

    await m.answer(
        status_panel,
        reply_markup=top_panel(me.username, user.referral_code)
    )


@router.message(Command("mode"))
async def cmd_mode(m: TgMessage):
    await m.answer("Выберите режим:", reply_markup=keyboards_for_modes())


@router.message(Command("subscription"))
async def cmd_subscription(m: TgMessage):
    await show_subscription_panel(m)


@router.message(Command("help"))
async def cmd_help(m: TgMessage):
    fake_cq = CallbackQuery(
        id="manual",
        from_user=m.from_user,
        chat_instance="manual",
        message=m,
        data="panel:help"
    )
    await panel_help(fake_cq)


@router.message(Command("new"))
async def cmd_new_chat(m: TgMessage):
    """Создание нового чата"""
    async with AsyncSessionMaker() as session:
        # Деактивируем все активные чаты
        await session.execute(update(ChatSession).where(
            ChatSession.user_id == m.from_user.id,
            ChatSession.is_active == True
        ).values(is_active=False))

        # Создаем новый чат
        new_session = ChatSession(
            user_id=m.from_user.id,
            title="Новый чат",
            mode="assistant",
            is_active=True
        )
        session.add(new_session)
        await session.commit()

    await m.answer("✅ Создан новый чат. Теперь можно отправлять сообщения.")


@router.message(F.text.contains("Подписка"))
async def reply_subscription_status(m: TgMessage):
    async with AsyncSessionMaker() as session:
        # получаем статус
        sub = await session.scalar(
            select(UserSubscription).where(UserSubscription.user_id == m.from_user.id)
        )
        now = datetime.now(timezone.utc)

        # неактивна → сразу открываем меню подписок
        if not sub or not sub.expires_at or sub.expires_at <= now:
            await show_subs(m, is_edit=False)  # выводим меню подписок
            return

        # активна → показываем панель как при /start
        status = await _render_status_line(session, m.from_user.id)
        user = await session.scalar(select(User).where(User.id == m.from_user.id))

    me = await m.bot.get_me()
    await m.answer(
        status,
        reply_markup=top_panel(me.username, user.referral_code)
    )


@router.callback_query(F.data == "panel:referral")
async def panel_referral(cq: CallbackQuery):
    """Показывает информацию о реферальной программе"""
    async with AsyncSessionMaker() as session:
        user_row = await session.scalar(
            select(User).where(User.id == cq.from_user.id)
        )

    if not user_row:
        await cq.answer("Ошибка: пользователь не найден")
        return

    me = await cq.bot.get_me()
    referral_url = f"https://t.me/{me.username}?start={user_row.referral_code}"

    text = (
        "👫 <b>Приглашайте друзей и получайте бонусы!</b>\n\n"
        f"Ваша реферальная ссылка:\n<code>{referral_url}</code>\n\n"
        "За каждого друга, который оплатит подписку:\n"
        "• <b>Вам</b> – +5 дней к подписке\n"
        "• <b>Другу</b> – 3 дня бесплатного доступа\n\n"
        "Просто поделитесь ссылкой с друзьями!"
    )

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться ссылкой",
                              switch_inline_query=f"Присоединяйся! {referral_url}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:main")]
    ])

    await cq.message.edit_text(text, reply_markup=keyboard)
    await cq.answer()


@router.message(Command("admin"))
async def cmd_admin(m: TgMessage):
    if m.from_user.id not in cfg.admin_ids:
        await m.answer("🚫 У вас нет доступа к админ-панели.")
        return

    await m.answer(
        "🛡 <b>Админ-панель</b>\n\n"
        "1️⃣ Управление пользователями\n"
        "2️⃣ Рассылки\n"
        "3️⃣ Проверка платежей\n\n"
        "⚙️ Доступ только для администраторов.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="panel:main")]
        ])
    )


@router.callback_query(F.data == "panel:mode")
async def panel_mode(cq: CallbackQuery):
    user_id = cq.from_user.id

    # получаем активный режим
    async with AsyncSessionMaker() as session:
        chat_session = await session.scalar(
            select(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.is_active == True
            )
        )
        active_mode = chat_session.mode if chat_session else "assistant"

    text = (
        "🎛 <b>Режимы работы</b>\n\n"

        "💬 <b>Ассистент</b>\n"
        "Обычный диалог с GPT: ответы, помощь, код, идеи.\n\n"

        "🎨 <b>Генерация</b>\n"
        "Создание изображений по тексту. Идеи, арты, фото.\n\n"

        "🛠 <b>Редактор фото</b>\n"
        "Улучшение, ретушь, изменение объектов на фото.\n\n"

        "🤳 <b>Селфи со звездой</b>\n"
        "Добавление знаменитости на ваш снимок.\n"
    )

    await cq.message.edit_text(
        text,
        reply_markup=keyboards_for_modes(active_mode=active_mode)
    )
    await cq.answer()


@router.callback_query(F.data == "panel:help")
async def panel_help(cq: CallbackQuery):
    text = (
        "ℹ️ <b>Помощь и быстрый старт</b>\n\n"

        "💬 <b>Ассистент</b>\n"
        "Общение с GPT: ответы, идеи, помощь, код.\n\n"

        "🎨 <b>Генерация изображений</b>\n"
        "Создание картинок по вашему описанию.\n\n"

        "🛠 <b>Редактор фото</b>\n"
        "Улучшение качества, изменение объектов.\n\n"

        "🤳 <b>Селфи со звездой</b>\n"
        "Добавление знаменитостей на ваши фото.\n\n"

        "🆘 <b>Поддержка:</b> " + cfg.support_username + "\n\n"
        "👇 Выберите действие в меню ниже."
    )

    await cq.message.edit_text(text, reply_markup=help_main_menu())
    await cq.answer()


@router.callback_query(F.data.startswith("mode:"))
async def switch_mode(cq: CallbackQuery):
    mode = cq.data.split(":", 1)[1]

    async with AsyncSessionMaker() as session:
        # Деактивируем старый активный режим
        chat_session = await session.scalar(
            select(ChatSession).where(
                ChatSession.user_id == cq.from_user.id,
                ChatSession.is_active == True
            )
        )
        if chat_session:
            chat_session.mode = mode
            await session.commit()

    DESCRIPTIONS = {
        "assistant": (
            "💬 <b>Ассистент</b>\n"
            "GPT-чат для любых задач: вопросы, идеи, код, советы.\n\n"
            "<b>Как пользоваться:</b>\n"
            "Просто напишите сообщение — получите ответ."
        ),
        "image": (
            "🎨 <b>Генерация изображений</b>\n"
            "Создаёт картинки по вашему тексту.\n\n"
            "<b>Как пользоваться:</b>\n"
            "Напишите, что должно быть на изображении.\n"
            "Пример: <i>«кот в космосе»</i>"
        ),
        "editor": (
            "🛠 <b>Редактор фото</b>\n"
            "Улучшение, ретушь, изменение содержимого фото.\n\n"
            "<b>Как пользоваться:</b>\n"
            "Отправьте фото + инструкцию.\n"
            "Пример: <i>«сделай ярче», «удали лишние объекты»</i>"
        ),
        "celebrity_selfie": (
            "🤳 <b>Селфи со звездой</b>\n"
            "Магическое добавление знаменитостей на ваше фото.\n\n"
            "<b>Как пользоваться:</b>\n"
            "Отправьте своё фото + имя звезды.\n"
            "Пример: <i>«Скарлетт Йоханссон»</i>"
        ),
    }

    new_text = DESCRIPTIONS.get(mode, "Режим переключён.")
    markup = keyboards_for_modes(active_mode=mode)

    await animate_panel_change(cq.message, new_text, markup)
    await cq.answer("Режим переключён")


def format_plan_info(code: str) -> str:
    plan = cfg.plans[code]

    BENEFITS = {
        "pro_lite": [
            "Быстрые ответы",
            "Редактирование & генерация фото",
            "Для ежедневных задач"
        ],
        "pro_plus": [
            "Высокая скорость",
            "Повышенные лимиты",
            "Качественная генерация"
        ],
        "pro_premium": [
            "Максимальные лимиты",
            "Приоритетная обработка",
            "Для работы, бизнеса и творчества"
        ]
    }

    req_limit = "∞" if plan.max_requests is None else plan.max_requests
    img_limit = "∞" if plan.max_image_generations is None else plan.max_image_generations
    text_limit = f"{plan.max_text_len} символов"

    benefits = " / ".join(BENEFITS.get(code, []))

    return (
        f"<b>{plan.title}</b> — <b>{plan.price_rub} ₽</b> / {plan.duration_days} дн.\n\n"
        f"✨ <b>Преимущества:</b>\n"
        f"{benefits}\n\n"
        f"📦 <b>Включено:</b>\n"
        f"• Запросы: <b>{req_limit}</b>\n"
        f"• Изобр.: <b>{img_limit}</b>\n"
        f"• Сообщения: <b>{text_limit}</b>"
    )


@router.callback_query(F.data == "subs:show")
async def show_subs(cq: CallbackQuery, is_edit: bool = True):
    text = (
        "💳 <b>Доступные подписки</b>\n\n"
        f"{format_plan_info('pro_lite')}\n\n"
        f"{format_plan_info('pro_plus')}\n\n"
        f"{format_plan_info('pro_premium')}\n\n"
        "Выберите нужный тариф для оплаты."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить Pro Lite", callback_data="buy:pro_lite")],
        [InlineKeyboardButton(text="Купить Pro Plus", callback_data="buy:pro_plus")],
        [InlineKeyboardButton(text="Купить Pro Premium", callback_data="buy:pro_premium")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="panel:main")],
    ])
    if is_edit:
        await cq.message.edit_text(text=text, reply_markup=kb)
        await cq.answer()
    else:
        await cq.message.answer(text=text, reply_markup=kb)


@router.callback_query(F.data.startswith("buy:"))
async def buy(cq: CallbackQuery):
    plan = cq.data.split(":", 1)[1]
    plan_conf = cfg.plans[plan]
    provider = YooMoneyProvider() if cfg.payment_provider == "yoomoney" else None
    description = f"Оплата плана {plan_conf.title}"

    # создаем платёж и получаем URL и ID платежа
    pay_url, payment_id = await provider.create_invoice(cq.from_user.id, plan, plan_conf.price_rub, description)

    # Сохраняем информацию о платеже в базу данных
    async with AsyncSessionMaker() as session:
        payment = Payment(
            user_id=cq.from_user.id,
            provider=cfg.payment_provider,
            provider_payment_id=payment_id,
            plan_code=plan,
            amount_rub=plan_conf.price_rub,
            status="pending"
        )
        session.add(payment)
        await session.commit()

    # красивый текст + красивая кнопка
    text = (
        f"🧾 <b>Счёт на оплату</b>\n\n"
        f"<b>Тариф:</b> {plan_conf.title}\n"
        f"<b>Стоимость:</b> {plan_conf.price_rub} ₽ за {plan_conf.duration_days} дней\n"
        f"<b>Что входит:</b>\n"
        f"• Запросы: {'∞' if plan_conf.max_requests is None else plan_conf.max_requests}\n"
        f"• Генерации изображений: {'∞' if plan_conf.max_image_generations is None else plan_conf.max_image_generations}\n"
        f"• Длина запроса: до {plan_conf.max_text_len} символов\n\n"
        f"Нажмите кнопку ниже, чтобы перейти к оплате 👇"
    )
    await cq.message.answer(text, reply_markup=plan_buy_keyboard(plan, pay_url))
    await cq.answer()


@router.message(F.photo)
async def on_photo(m: TgMessage):
    """
    Обработка изображений через AITUNNEL.

    Режимы:
    - editor: редактирование по инструкции
    - analyze: анализ изображения
    - add_people: добавление людей
    - celebrity_selfie: селфи со знаменитостью
    """

    from services.auth import is_user_blocked
    if await is_user_blocked(m.from_user.id):
        await m.answer("🚫 Вы не можете отправлять изображения. Поддержка: @support")
        return

    # Загружаем файл из Telegram
    file_id = m.photo[-1].file_id
    file = await m.bot.get_file(file_id)
    photo_bytes = await m.bot.download_file(file.file_path)
    img_bytes = photo_bytes.getvalue()

    img_service = ImageService()

    # Флаг ошибки — чтобы понять, что писать в конце прогресса
    error_happened = False
    done_event = asyncio.Event()

    # Стартовое сообщение с прогресс-баром
    progress_msg = await m.answer(
        "🛠 Обрабатываю изображение…\n"
        "▰▱▱▱▱▱▱▱▱  0%"
    )

    async def progress_updater() -> None:
        """
        Фоновое обновление прогресс-бара.
        Останавливается, когда done_event.set().
        """
        total_blocks = 9
        progress = 0

        while not done_event.is_set():
            await asyncio.sleep(0.3)
            progress = min(progress + random.randint(1, 2), 85)
            filled = progress * total_blocks // 100
            bar = "▰" * filled + "▱" * (total_blocks - filled)

            try:
                await progress_msg.edit_text(
                    f"🛠 Обрабатываю изображение…\n{bar}  {progress}%"
                )
            except Exception:
                # Игнорируем любые ошибки Telegram при редактировании
                pass

        # После завершения обработки — финальное состояние
        try:
            if error_happened:
                # При ошибке показываем, что обработка остановлена
                await progress_msg.edit_text("⛔ Обработка остановлена из-за ошибки.")
            else:
                # При успехе — 100%
                bar = "▰" * total_blocks
                await progress_msg.edit_text(f"📸 Готово!\n{bar}  100%")
        except Exception:
            pass

    async def job() -> None:
        """
        Основная задача обработки изображения:
        выбирает режим, вызывает нужный метод сервиса
        и отправляет результат/ошибку пользователю.
        """
        nonlocal error_happened

        try:
            # Получаем активный режим пользователя
            async with AsyncSessionMaker() as session:
                chat_session = await session.scalar(
                    select(ChatSession).where(
                        ChatSession.user_id == m.from_user.id,
                        ChatSession.is_active == True,
                    )
                )
                mode = chat_session.mode if chat_session else "editor"

            instruction = (m.caption or "").strip()

            # -----------------------------
            #  РЕЖИМ: celebrity_selfie
            # -----------------------------
            if mode == "celebrity_selfie":
                # В этом режиме подпись = имя знаменитости
                celebrity_name = instruction

                if not celebrity_name:
                    # Не редактируем progress_msg — выводим ОТДЕЛЬНОЕ сообщение
                    error_happened = True
                    await m.answer("❗ Укажите имя знаменитости в подписи к фото.")
                    return

                new_img, err = await img_service.celebrity_selfie(
                    image_bytes=img_bytes,
                    celebrity_name=celebrity_name,
                )

                if err:
                    error_happened = True
                    logger.error(f"Ошибка celebrity_selfie: {err}")
                    await m.answer(f"❗ {err}")
                    return

                await m.answer_photo(
                    BufferedInputFile(new_img, filename="celebrity_selfie.png"),
                    caption=f"Готово! ⭐ Ваше селфи с {celebrity_name}",
                )

                # Списание изображения
                async with AsyncSessionMaker() as session:
                    await spend_image(session, m.from_user.id)

                return

            # -----------------------------
            #  РЕЖИМ: editor (редактор)
            # -----------------------------
            if mode == "editor":
                # Если явной инструкции нет — просто улучшить
                instruction_for_edit = instruction or "Улучшить изображение."
                new_img, err = await img_service.edit(
                    image_bytes=img_bytes,
                    instruction=instruction_for_edit,
                )

                if err:
                    error_happened = True
                    logger.error(f"Ошибка editor: {err}")
                    await m.answer(f"❗ {err}")
                    return

                await m.answer_photo(
                    BufferedInputFile(new_img, filename="edited.png"),
                    caption="Готово! 🎨",
                )

                async with AsyncSessionMaker() as session:
                    await spend_image(session, m.from_user.id)

                return

            # -----------------------------
            #  РЕЖИМ: analyze
            # -----------------------------
            if mode == "analyze":
                question = instruction or "Опиши, что находится на изображении."
                answer, err = await img_service.analyze(
                    image_bytes=img_bytes,
                    question=question,
                )

                if err:
                    error_happened = True
                    logger.error(f"Ошибка analyze: {err}")
                    await m.answer(f"❗ {err}")
                    return

                await m.answer(f"📊 Анализ изображения:\n{answer}")
                return

            # -----------------------------
            #  РЕЖИМ: add_people
            # -----------------------------
            if mode == "add_people":
                if not instruction:
                    error_happened = True
                    await m.answer(
                        "❗ В подписи опишите, каких людей нужно добавить (например: "
                        "'добавь двоих друзей справа, в casual-одежде')."
                    )
                    return

                new_img, err = await img_service.add_people(
                    image_bytes=img_bytes,
                    description=instruction,
                )

                if err:
                    error_happened = True
                    logger.error(f"Ошибка add_people: {err}")
                    await m.answer(f"❗ {err}")
                    return

                await m.answer_photo(
                    BufferedInputFile(new_img, filename="add_people.png"),
                    caption="Готово! 👥",
                )

                async with AsyncSessionMaker() as session:
                    await spend_image(session, m.from_user.id)

                return

            # -----------------------------
            #  НЕИЗВЕСТНЫЙ / НЕПОДДЕРЖИВАЕМЫЙ РЕЖИМ
            # -----------------------------
            error_happened = True
            await m.answer(
                f"⚙️ Для режима '{mode}' пока нет обработки изображений. "
                f"Переключитесь в /mode на editor / analyze / celebrity_selfie / add_people."
            )

        except Exception as e:
            # Логируем критическую ошибку и показываем отдельным сообщением
            error_happened = True
            logger.error(f"Критическая ошибка обработки изображения: {e}")
            await m.answer(f"❗ Произошла ошибка при обработке изображения: {str(e)}")

        finally:
            # В любом случае завершаем прогресс
            done_event.set()

    # Стартуем задачи: прогресс и саму обработку
    asyncio.create_task(progress_updater())
    await img_pool.submit(job)


@router.message(StateFilter(default_state), F.text & ~F.via_bot)
async def on_text(m: TgMessage):
    """
    Обработка текстовых запросов через AITUNNEL.

    Режимы:
    - assistant: потоковый чат
    - image: генерация изображения
    - editor: инструкции для редактирования
    - celebrity_selfie: селфи со знаменитостью
    """

    # Игнорируем команды
    if m.text and m.text.startswith("/"):
        return

    from services.auth import is_user_blocked
    if await is_user_blocked(m.from_user.id):
        await m.answer("🚫 Ваш доступ ограничен. Свяжитесь с поддержкой.")
        return

    user_id = m.from_user.id
    text: str = m.text.strip()

    # Получаем активный режим
    async with AsyncSessionMaker() as session:
        chat_session = await session.scalar(
            select(ChatSession).where(
                ChatSession.user_id == user_id,
                ChatSession.is_active == True
            )
        )
        mode = chat_session.mode if chat_session else "assistant"

        # Проверяем лимиты для режимов изображений
        is_image_mode = mode in ("image", "editor", "celebrity_selfie")
        if is_image_mode and not await can_spend_image(session, user_id):
            await m.answer("❗ Лимит изображений исчерпан. Оформите подписку или дождитесь продления.")
            return

    # Режим assistant - чат с GPT
    if mode == "assistant":
        chat_service = ChatService()
        await chat_service.handle_user_message(text, m.bot, m.chat.id)
        async with AsyncSessionMaker() as session:
            await spend_request(session, user_id)
        return

    # Режим image - генерация изображения
    if mode == "image":
        img_service = ImageService()
        done_event = asyncio.Event()

        progress_msg = await m.answer("🎨 Генерирую изображение…\n▰▱▱▱▱▱▱▱▱  0%")

        async def progress_updater():
            """Обновление прогресс-бара для генерации"""
            total_blocks = 9
            progress = 0

            while not done_event.is_set():
                await asyncio.sleep(0.3)
                progress = min(progress + random.randint(1, 2), 85)
                bar = "▰" * (progress * total_blocks // 100)
                bar += "▱" * (total_blocks - len(bar))

                try:
                    await progress_msg.edit_text(f"🎨 Генерирую изображение…\n{bar}  {progress}%")
                except Exception:
                    pass

            # Финальное обновление
            try:
                bar = "▰" * total_blocks
                await progress_msg.edit_text(f"📸 Готово!\n{bar}  100%")
            except Exception:
                pass

        async def generate_job():
            """Задача генерации изображения"""
            img, err = await img_service.generate(text)
            done_event.set()

            if err:
                logger.error(f"❗ {err}")
                await progress_msg.edit_text(f"❗ {err}")
                return

            # Отправляем результат
            file = BufferedInputFile(img, filename="generated.png")
            await m.answer_photo(file, caption="Готово! 🎨")

            # Списание использования
            async with AsyncSessionMaker() as session:
                await spend_image(session, user_id)

        asyncio.create_task(progress_updater())
        await img_pool.submit(generate_job)
        return

    # Другие режимы требуют загрузки изображения
    await m.answer(f"⚙️ Для режима '{mode}' необходимо загрузить изображение. Отправьте фото с текстовой инструкцией.")


@router.callback_query(F.data == "chat:new")
async def new_chat(cq: CallbackQuery):
    async with AsyncSessionMaker() as session:
        # деактивируем все и создаем новый assistant
        await session.execute(update(ChatSession).where(
            ChatSession.user_id == cq.from_user.id, ChatSession.is_active == True
        ).values(is_active=False))
        session.add(ChatSession(user_id=cq.from_user.id, title="Новый чат", mode="assistant", is_active=True))
        await session.commit()
    await cq.message.answer("Создан новый чат. Пишите сообщение.")
    await cq.answer()


@router.callback_query(F.data == "chat:list")
async def chat_list(cq: CallbackQuery):
    PAGE_SIZE = 10
    page = 1
    if cq.message and cq.message.reply_markup:
        # можно реализовать пагинацию через callback_data вида chat:list:2
        pass
    async with AsyncSessionMaker() as session:
        rows = (await session.execute(
            select(ChatSession).where(ChatSession.user_id == cq.from_user.id).order_by(ChatSession.id.desc()).limit(100)
        )).scalars().all()

    if not rows:
        await cq.message.answer("У вас пока нет сохранённых чатов.")
        await cq.answer()
        return

    lines = []
    for s in rows[:PAGE_SIZE]:
        mark = "🟢" if s.is_active else "⚪️"
        lines.append(f"{mark} <b>{s.title}</b> — {s.mode} (#{s.id})")
    text = "📁 <b>Ваши чаты</b>\n" + "\n".join(lines)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Активировать первый", callback_data=f"chat:activate:{rows[0].id}")],
        [InlineKeyboardButton(text="Создать новый", callback_data="chat:new")]
    ])
    await cq.message.answer(text, reply_markup=kb)
    await cq.answer()


@router.callback_query(F.data.startswith("chat:activate:"))
async def chat_activate(cq: CallbackQuery):
    sess_id = int(cq.data.split(":")[-1])
    async with AsyncSessionMaker() as session:
        await session.execute(update(ChatSession).where(
            ChatSession.user_id == cq.from_user.id, ChatSession.is_active == True
        ).values(is_active=False))
        await session.execute(update(ChatSession).where(
            ChatSession.id == sess_id, ChatSession.user_id == cq.from_user.id
        ).values(is_active=True))
        await session.commit()
    await cq.message.answer(f"✔️ Активирован чат #{sess_id}")
    await cq.answer()


async def show_subscription_panel(m: TgMessage):
    async with AsyncSessionMaker() as session:
        status = await _render_status_line(session, m.from_user.id)
        user_row = (await session.execute(select(User).where(User.id == m.from_user.id))).scalars().first()
    me = await m.bot.get_me()
    await m.answer(status, reply_markup=top_panel(me.username, user_row.referral_code))


@router.callback_query(F.data == "panel:main")
async def panel_main(cq: CallbackQuery):
    async with AsyncSessionMaker() as session:
        status = await _render_status_line(session, cq.from_user.id)
        user_row = (await session.execute(
            select(User).where(User.id == cq.from_user.id)
        )).scalars().first()
    me = await cq.bot.get_me()
    await cq.message.edit_text(status, reply_markup=top_panel(me.username, user_row.referral_code))
    await cq.answer()


@router.callback_query(F.data == "help:main")
async def help_back_to_main(cq: CallbackQuery):
    await cq.message.edit_text(
        "ℹ️ <b>Помощь и обучение</b>\n\nВыберите раздел:",
        reply_markup=help_main_menu()
    )
    await cq.answer()


@router.callback_query(F.data == "help:features")
async def help_features(cq: CallbackQuery):
    await cq.message.edit_text(
        (
            "💬 <b>Возможности бота</b>\n\n"
            "• Ассистент — ответы, идеи, код, обучение.\n"
            "• Генерация изображений — арты, фото, сцены.\n"
            "• Редактор фото — улучшение, ретушь, замена объектов.\n"
            "• Селфи со звездами — добавляет знаменитостей на фото.\n"
            "• Анализ изображений — понимание содержания снимков."
        ),
        reply_markup=help_back_kb()
    )
    await cq.answer()


@router.callback_query(F.data == "help:limits")
async def help_limits(cq: CallbackQuery):
    await cq.message.edit_text(
        (
            "❓ <b>FAQ по лимитам</b>\n\n"
            "<b>Зачем лимиты?</b>\n"
            "Чтобы бот работал стабильно и быстро.\n\n"
            "<b>Что считается запросом?</b>\n"
            "Любой текст, на который бот отвечает.\n\n"
            "<b>Что считается генерацией изображения?</b>\n"
            "Создание или редактирование фото.\n\n"
            "<b>Когда обновляются лимиты?</b>\n"
            "При активации подписки или начале нового периода.\n"
        ),
        reply_markup=help_back_kb()
    )
    await cq.answer()


@router.callback_query(F.data == "help:guide")
async def help_guide(cq: CallbackQuery):
    await cq.message.edit_text(
        (
            "🧠 <b>Как правильно формулировать запросы</b>\n\n"
            "1) Будьте конкретны.\n"
            "2) Указывайте стиль или формат.\n"
            "3) Формулируйте цель.\n"
            "4) Используйте структуру.\n"
            "5) Приводите примеры.\n\n"
            "Пример:\n"
            "<i>«Напиши пост в стиле Apple: 3 пункта + призыв»</i>"
        ),
        reply_markup=help_back_kb()
    )
    await cq.answer()


@router.callback_query(F.data == "help:examples")
async def help_examples(cq: CallbackQuery):
    await cq.message.edit_text(
        (
            "🔥 <b>Примеры лучших запросов</b>\n\n"
            "<b>Тексты:</b>\n"
            "• «Напиши продающий текст о картошке в стиле Apple»\n"
            "• «Сделай пост для Telegram с 5 пунктами»\n\n"
            "<b>Код:</b>\n"
            "• «Объясни этот Python-код простыми словами»\n"
            "• «Оптимизируй SQL-запрос»\n\n"
            "<b>Изображения:</b>\n"
            "• «Кот-астронавт в стиле пиксель-арт»\n"
            "• «Логотип буквы D в минимализме»\n\n"
            "<b>Редактор:</b>\n"
            "• «Осветли лицо, убери шум»\n"
            "• «Добавь солнце на задний план»\n\n"
            "<b>Селфи со звездой:</b>\n"
            "• «Ди Каприо»\n"
        ),
        reply_markup=help_back_kb()
    )
    await cq.answer()


@router.callback_query(F.data == "help:support")
async def help_support(cq: CallbackQuery):
    await cq.message.edit_text(
        (
            f"🆘 <b>Поддержка</b>\n\n"
            f"Если что-то не работает или есть вопросы — мы рядом.\n\n"
            f"<b>Свяжитесь с нами:</b> {cfg.support_username}\n"
        ),
        reply_markup=help_back_kb()
    )
    await cq.answer()