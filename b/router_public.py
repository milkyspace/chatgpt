from __future__ import annotations
from aiogram import Router, F
from aiogram.types import Message as TgMessage, CallbackQuery, InputMediaPhoto
from aiogram.filters import CommandStart, Command
from sqlalchemy.ext.asyncio import AsyncSession
from db import AsyncSessionMaker
from config import cfg
from models import ChatSession
from sqlalchemy import select, insert, update
from services.subscriptions import ensure_user, get_limits
from services.usage import can_spend_request, spend_request, can_spend_image, spend_image
from services.chat import ChatService
from services.images import ImageService
from keyboards import main_menu, subscriptions_keyboard
from utils import store_message, get_history, trim_messages
from queue_bg import AsyncWorkerPool
from services.chat import ChatService

router = Router()

# Пулы фоновых задач
chat_pool = AsyncWorkerPool(cfg.workers_chat)
img_pool = AsyncWorkerPool(cfg.workers_images)

@router.startup()
async def _startup(_):
    await chat_pool.start()
    await img_pool.start()

@router.shutdown()
async def _shutdown(_):
    await chat_pool.stop()
    await img_pool.stop()

@router.message(CommandStart())
async def start(m: TgMessage):
    ref_code = m.text.split(" ", 1)[1] if (m.text and " " in m.text) else None
    async with AsyncSessionMaker() as session:
        user = await ensure_user(session, m.from_user.id, m.from_user.username, m.from_user.first_name, m.from_user.last_name, ref_code)
    await m.answer(
        "Привет! Я AI-бот с доступом к ChatGPT и генерации изображений.\nВыберите режим или напишите сообщение.",
        reply_markup=main_menu(ref_code=user.referral_code)
    )

@router.callback_query(F.data.startswith("mode:"))
async def switch_mode(cq: CallbackQuery):
    mode = cq.data.split(":", 1)[1]
    if mode not in cfg.modes:
        await cq.answer("Неизвестный режим")
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

@router.callback_query(F.data == "subs:show")
async def show_subs(cq: CallbackQuery):
    await cq.message.answer("Выберите подписку:", reply_markup=subscriptions_keyboard())
    await cq.answer()

@router.callback_query(F.data.startswith("buy:"))
async def buy(cq: CallbackQuery):
    plan = cq.data.split(":", 1)[1]
    plan_conf = cfg.plans[plan]
    description = f"Оплата плана {plan_conf.title}"
    # создаем ссылку на оплату
    provider_url: str
    if cfg.payment_provider == "yoomoney":
        from payments.yoomoney import YooMoneyProvider
        provider = YooMoneyProvider()
    else:
        from payments.mock import MockPaymentProvider
        provider = MockPaymentProvider()

    provider_url = await provider.create_invoice(cq.from_user.id, plan, plan_conf.price_rub, description)
    await cq.message.answer(f"Ссылка на оплату: {provider_url}\nПосле оплаты подписка активируется автоматически.")
    await cq.answer()

@router.message(F.photo)
async def on_photo(m: TgMessage):
    """Принимаем фото. Работаем в выбранном режиме: editor/add_people/celebrity_selfie."""
    file_id = m.photo[-1].file_id
    mode = "editor"
    async with AsyncSessionMaker() as session:
        # узнаем активную сессию и лимиты
        res = await session.execute(select(ChatSession).where(ChatSession.user_id == m.from_user.id, ChatSession.is_active == True))
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
    text = m.text.strip()
    async with AsyncSessionMaker() as session:
        max_req, max_img, max_text_len = await get_limits(session, m.from_user.id)
        if not await can_spend_request(session, m.from_user.id):
            await m.answer("Лимит текстовых запросов исчерпан.")
            return
        res = await session.execute(select(ChatSession).where(ChatSession.user_id == m.from_user.id, ChatSession.is_active == True))
        chat_sess = res.scalars().first()
        if not chat_sess:
            chat_sess = ChatSession(user_id=m.from_user.id, title="Chat", mode="assistant", is_active=True)
            session.add(chat_sess)
            await session.commit()
        await store_message(session, chat_sess.id, "user", text)
        history = await get_history(session, chat_sess.id, limit=30)
        history = trim_messages(tokens_est=0, messages=history, max_len=max_text_len)

    chat_service = ChatService()

    async def job():
        # Используем streaming
        reply_text = await chat_service.stream_reply(m.bot, m.chat.id, history, max_text_len)
        async with AsyncSessionMaker() as session:
            await spend_request(session, m.from_user.id)
            await store_message(session, chat_sess.id, "assistant", reply_text)

    await chat_pool.submit(job)

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
