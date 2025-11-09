from __future__ import annotations
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def main_menu(bot_username: str, ref_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ассистент", callback_data="mode:assistant"),
         InlineKeyboardButton(text="🎨 Генерация", callback_data="mode:image")],
        [InlineKeyboardButton(text="🛠 Редактор фото", callback_data="mode:editor")],
        [InlineKeyboardButton(text="➕ Добавить людей", callback_data="mode:add_people")],
        [InlineKeyboardButton(text="👥 Реферальная ссылка", url=f"https://t.me/{bot_username}?start={ref_code}")],
        [InlineKeyboardButton(text="💳 Подписки", callback_data="subs:show")],
        [InlineKeyboardButton(text="🆕 Новый чат", callback_data="chat:new")],
        [InlineKeyboardButton(text="🗂 Мои чаты", callback_data="chat:list")],
    ])

def subscriptions_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Pro Lite — 499₽ / 10 дней", callback_data="buy:pro_lite")],
        [InlineKeyboardButton(text="Pro Plus — 1290₽ / 30 дней", callback_data="buy:pro_plus")],
        [InlineKeyboardButton(text="Pro Premium — 2990₽ / 90 дней", callback_data="buy:pro_premium")],
    ])

def admin_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Пользователи", callback_data="admin:users"),
         InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")]
    ])

def keyboards_for_modes() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ассистент", callback_data="mode:assistant"),
         InlineKeyboardButton(text="🎨 Генерация", callback_data="mode:image")],
        [InlineKeyboardButton(text="🛠 Редактор фото", callback_data="mode:editor"),
         InlineKeyboardButton(text="➕ Добавить людей", callback_data="mode:add_people")],
        [InlineKeyboardButton(text="🤳 Селфи со звёздой", callback_data="mode:celebrity_selfie")],
    ])

def top_panel(bot_username: str, ref_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Подлить подписку", callback_data="subs:show"),
         InlineKeyboardButton(text="🎛 Режим", callback_data="panel:mode")],
        [InlineKeyboardButton(text="👥 Пригласить", url=f"https://t.me/{bot_username}?start={ref_code}")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="panel:help"),
         InlineKeyboardButton(text="🛡 Админ-панель", callback_data="panel:admin")],
    ])

def plan_buy_keyboard(plan_code: str, pay_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="subs:show")]
    ])