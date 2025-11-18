from __future__ import annotations

import asyncio
import random

from datetime import datetime, timezone

from aiogram import Router, F
from aiogram.types import BufferedInputFile
from aiogram.filters import Command, CommandStart
from aiogram.types import Message as TgMessage, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import select
from sqlalchemy import update
from aiogram.types import CallbackQuery, User, Chat

from config import cfg
from db import AsyncSessionMaker
from keyboards import plan_buy_keyboard
from keyboards import top_panel, keyboards_for_modes
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
from services.subscriptions import ensure_user, get_limits
from services.usage import can_spend_request, spend_request, can_spend_image, spend_image
from services.subscriptions import has_active_subscription
from utils import store_message, get_history, trim_messages

router = Router()

# Пулы фоновых задач
chat_pool = AsyncWorkerPool(cfg.workers_chat)
img_pool = AsyncWorkerPool(cfg.workers_images)


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


async def _render_status_line(session, user_id: int) -> str:
    sub = await session.scalar(select(UserSubscription).where(UserSubscription.user_id == user_id))
    usage = await session.scalar(select(Usage).where(Usage.user_id == user_id))
    now = datetime.now(timezone.utc)  # Исправлено: всегда используем UTC

    expires_at = None
    if sub and sub.expires_at:
        # Приводим дату к UTC для корректного сравнения
        expires_at = sub.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        else:
            expires_at = expires_at.astimezone(timezone.utc)

    if not sub or not expires_at or expires_at <= now:
        status = "🔴 Неактивна"
        expires_str = "—"
        plan_name = "Пробный период истёк" if (sub and sub.is_trial) else "Нет"
        limits = "Запросы: 0 / Изображения: 0"
    else:
        plan_code = sub.plan_code or "trial"
        plan_conf = cfg.plans.get(plan_code)
        status = "🟢 Активна"
        # Форматируем дату для отображения
        expires_str = expires_at.astimezone().strftime("%d.%m.%Y %H:%M")
        if sub.is_trial:
            plan_name = "Trial"
            max_req, max_img, _ = cfg.trial_max_requests, cfg.trial_max_images, 4000
        else:
            plan_name = plan_conf.title if plan_conf else plan_code
            max_req = plan_conf.max_requests
            max_img = plan_conf.max_image_generations
        ur = usage.used_requests if usage else 0
        ui = usage.used_images if usage else 0
        limits = f"Запросы: {('∞' if max_req is None else f'{ur}/{max_req}')}, " \
                 f"Изобр.: {('∞' if max_img is None else f'{ui}/{max_img}')}"

    text = f"<b>Подписка:</b> {status}\n" \
           f"<b>Тариф:</b> {plan_name}\n"
    if expires_str:
        text += f"<b>Действует до:</b> {expires_str}\n"
        text += f"<b>Лимиты:</b> {limits}"

    return text


@router.message(CommandStart())
async def start(m: TgMessage):
    ref_code = None
    if m.text and " " in m.text:
        ref_code = m.text.split(" ", 1)[1].strip()

    async with AsyncSessionMaker() as session:
        user = await ensure_user(session, m.from_user.id, m.from_user.username,
                                 m.from_user.first_name, m.from_user.last_name, ref_code)
        status = await _render_status_line(session, m.from_user.id)

    me = await m.bot.get_me()  # ← вот здесь получаем имя бота
    await m.answer(
        status,
        reply_markup=top_panel(me.username, user.referral_code)  # ← передаём его сюда
    )


@router.message(Command("mode"))
async def cmd_mode(m: TgMessage):
    await m.answer("Выберите режим:", reply_markup=keyboards_for_modes())


@router.message(Command("subscription"))
async def cmd_subscription(m: TgMessage):
    await show_subscription_panel(m)


@router.message(Command("help"))
async def cmd_help(m: TgMessage):
    text = (
        "ℹ️ <b>Помощь</b>\n\n"
        "Команды:\n"
        "• /start — главное меню\n"
        "• /mode — выбор режима\n"
        "• /subscription — информация о подписке\n"
        "• /new — новый чат\n\n"
        "Просто отправьте текст, и бот ответит вам 🤖"
    )
    await m.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="panel:main")]
    ]))


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
        "• <b>Другу</b> – 7 дней бесплатного доступа\n\n"
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
    await cq.message.edit_reply_markup(reply_markup=keyboards_for_modes())
    await cq.answer("Выберите режим")


@router.callback_query(F.data == "panel:help")
async def panel_help(cq: CallbackQuery):
    text = (
        "ℹ️ <b>Помощь</b>\n\n"
        "Доступные команды:\n"
        "• /start — главное меню\n"
        "• /new — новый чат\n"
        "• /mode — выбрать режим\n"
        "• /subscription — информация о подписке\n"
        "• Просто отправьте текст — и получите ответ\n\n"
        "Поддержка: @your_support_username"
    )
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:main")]
    ]))
    await cq.answer()


@router.callback_query(F.data.startswith("mode:"))
async def switch_mode(cq: CallbackQuery):
    mode = cq.data.split(":", 1)[1]
    if mode not in cfg.modes:
        await cq.answer("Неизвестный режим")
        return

    # Проверяем доступ к режиму
    async with AsyncSessionMaker() as session:
        has_access = await has_active_subscription(session, cq.from_user.id)

        if not has_access:
            # Показываем окно с предложением подписки
            text = (
                f"🚫 <b>Доступ ограничен</b>\n\n"
                f"💎 <b>Оформите подписку</b> чтобы получить доступ ко всем функциям:"
            )
            await cq.message.edit_text(text)
            await cq.answer()

            await show_subs(cq, False)

            return

    async with AsyncSessionMaker() as session:
        # создаем новую сессию чата в выбранном режиме
        res = await session.execute(select(ChatSession).where(
            ChatSession.user_id == cq.from_user.id, ChatSession.is_active == True))
        active = res.scalars().first()
        if active:
            active.is_active = False
        session.add(ChatSession(user_id=cq.from_user.id, title=f"{mode.capitalize()} чат", mode=mode, is_active=True))
        await session.commit()
    await cq.message.answer(f"Режим переключен: {mode}")
    await cq.answer()


def format_plan_info(code: str) -> str:
    plan = cfg.plans[code]
    limits = []
    limits.append("Запросы: ∞" if plan.max_requests is None else f"Запросы: до {plan.max_requests}")
    limits.append(
        "Генерации: ∞" if plan.max_image_generations is None else f"Генерации: до {plan.max_image_generations}")
    limits.append(f"Длина запроса: до {plan.max_text_len} символов")
    return (f"<b>{plan.title}</b>\n"
            f"Стоимость: <b>{plan.price_rub} ₽</b> / {plan.duration_days} дней\n"
            f"{' • '.join(limits)}")


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
    """Принимаем фото. Работаем в выбранном режиме: editor/add_people/celebrity_selfie."""
    file_id = m.photo[-1].file_id
    mode = "editor"
    async with AsyncSessionMaker() as session:
        # узнаем активную сессию и лимиты
        res = await session.execute(
            select(ChatSession).where(ChatSession.user_id == m.from_user.id, ChatSession.is_active == True))
        chat_sess = res.scalars().first()
        if chat_sess:
            mode = chat_sess.mode
        max_req, max_img, max_text_len = await get_limits(session, m.from_user.id)
        if not await can_spend_image(session, m.from_user.id):
            await m.answer("Лимит изображений исчерпан. Оформите подписку или подождите продления.")
            return

    # подгружаем bytes фото
    photo_file = await m.bot.get_file(file_id)
    photo_bytes = await m.bot.download_file(photo_file.file_path)

    img_service = ImageService()

    # Задача в фоне
    async def job():
        new_img: bytes | None = None
        error: str | None = None
        if mode == "editor":
            instruction = m.caption or "Слегка улучшить качество и цвет."
            new_img, error = await img_service.edit(photo_bytes.read(), instruction)
        elif mode == "add_people":
            desc = m.caption or "Добавить двух людей на задний план, естественная композиция."
            new_img, error = await img_service.add_people(photo_bytes.read(), desc)
        elif mode == "celebrity_selfie":
            celeb = (m.caption or "Известная личность").strip()
            new_img, error = await img_service.celebrity_selfie(photo_bytes.read(), celeb)
        else:
            new_img, error = await img_service.edit(photo_bytes.read(), "Улучшить изображение.")

        if error:
            await m.answer(f"❗️ {error}")
            return

        if new_img:
            await m.answer_photo(new_img, caption=f"Готово! Режим: {mode}")
            async with AsyncSessionMaker() as session:
                await spend_image(session, m.from_user.id)

    await img_pool.submit(job)


@router.message(F.text & ~F.via_bot)
async def on_text(m: TgMessage):
    """
    Обрабатывает текстовые запросы в зависимости от текущего режима чата.

    Режимы:
    - assistant: потоковый чат с GPT
    - image: генерация изображения по тексту
    - editor: правка изображения на основе текстовой инструкции (без фото)
    - add_people: генерация изображения с добавлением людей
    - celebrity_selfie: создание селфи с селебой
    """

    # Игнорируем команды
    if m.text and m.text.startswith("/"):
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

        # Получаем лимиты
        max_req, max_img, max_text_len = await get_limits(session, user_id)

        # Проверка лимитов только для режимов-изображений
        is_image_mode = mode in ("image", "editor", "add_people", "celebrity_selfie")
        if is_image_mode:
            if not await can_spend_image(session, user_id):
                await m.answer("❗ Лимит изображений исчерпан. Оформите подписку или дождитесь продления.")
                return

    # Режим assistant → GPT-текст
    if mode == "assistant":
        chat_service = ChatService()
        await chat_service.handle_user_message(text, m.bot, m.chat.id)
        async with AsyncSessionMaker() as session:
            await spend_request(session, user_id)
        return

    # ---- Изображения ----
    img_service = ImageService()

    # image — генерация изображения по тексту
    if mode == "image":

        done_event = asyncio.Event()

        # начальное сообщение
        progress_msg = await m.answer(
            "🎨 Генерирую изображение…\n"
            "▰▱▱▱▱▱▱▱▱  0%"
        )

        async def progress_updater():
            total_blocks = 9
            progress = 0  # проценты 0–100

            while not done_event.is_set():
                await asyncio.sleep(0.3)

                # медленный прогресс — 1–2% за шаг
                progress = min(progress + random.randint(1, 2), 80)

                filled = progress * total_blocks // 100
                bar = "▰" * filled + "▱" * (total_blocks - filled)

                try:
                    await progress_msg.edit_text(
                        f"🎨 Генерирую изображение…\n{bar}  {progress}%"
                    )
                except Exception:
                    pass

            # закончили — ставим 100%
            try:
                bar = "▰" * total_blocks
                await progress_msg.edit_text(
                    f"📸 Готово!\n{bar}  100%"
                )
            except Exception:
                pass

        async def generate_job():
            img, err = await img_service.generate(text)
            done_event.set()  # останов прогресса

            if err:
                await progress_msg.edit_text(f"❗ Ошибка: {err}")
                return

            file = BufferedInputFile(img, filename="generated.png")
            await m.answer_photo(file, caption="Готово!")

            async with AsyncSessionMaker() as session:
                await spend_image(session, user_id)

        asyncio.create_task(progress_updater())
        await img_pool.submit(generate_job)

        return

    # editor — создаём изображение с текстовой инструкцией
    if mode == "editor":

        done_event = asyncio.Event()

        # показываем прогресс
        progress_msg = await m.answer(
            "🛠 Редактирую изображение…\n"
            "▰▱▱▱▱▱▱▱▱  0%"
        )

        async def progress_updater():
            total_blocks = 9
            progress = 0

            while not done_event.is_set():
                await asyncio.sleep(0.3)

                progress = min(progress + random.randint(1, 2), 80)

                filled = progress * total_blocks // 100
                bar = "▰" * filled + "▱" * (total_blocks - filled)

                try:
                    await progress_msg.edit_text(
                        f"🛠 Редактирую изображение…\n{bar}  {progress}%"
                    )
                except:
                    pass

            # финальный рывок
            try:
                bar = "▰" * total_blocks
                await progress_msg.edit_text(
                    f"📸 Готово!\n{bar}  100%"
                )
            except:
                pass

        async def edit_job():
            # ВНИМАНИЕ — ТЕПЕРЬ ИСПОЛЬЗУЕМ ПРАВИЛЬНО
            img_bytes = photo_bytes.read()

            instruction = m.caption or "Слегка улучшить качество и цвет."

            new_img, err = await img_service.edit(img_bytes, instruction)
            done_event.set()

            if err:
                await progress_msg.edit_text(f"❗ Ошибка: {err}")
                return

            # Telegram-файл
            file = BufferedInputFile(new_img, filename="edit.png")

            await m.answer_photo(file, caption="Готово! Режим: editor")

            async with AsyncSessionMaker() as session:
                await spend_image(session, m.from_user.id)

        asyncio.create_task(progress_updater())
        await img_pool.submit(edit_job)

        return

    # add_people — текстовое описание → картинка
    if mode == "add_people":
        async def job():
            img, err = await img_service.add_people(b"", text)
            if err:
                await m.answer(f"❗ {err}")
                return

            file = BufferedInputFile(img, filename="result.png")
            await m.answer_photo(file, caption="Готово!")

            async with AsyncSessionMaker() as session:
                await spend_image(session, user_id)

        await img_pool.submit(job)
        return

    # celebrity_selfie — селфи с селебой по тексту
    if mode == "celebrity_selfie":
        async def job():
            celeb = text.strip()
            img, err = await img_service.celebrity_selfie(b"", celeb)
            if err:
                await m.answer(f"❗ {err}")
                return

            file = BufferedInputFile(img, filename="result.png")
            await m.answer_photo(file, caption="Готово!")

            async with AsyncSessionMaker() as session:
                await spend_image(session, user_id)

        await img_pool.submit(job)
        return

    # fallback
    await m.answer(f"⚙️ Режим '{mode}' пока недоступен для текстовых запросов.")


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