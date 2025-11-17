from __future__ import annotations
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def subscriptions_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для выбора подписки"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Pro Lite — 499₽ / 10 дней", callback_data="buy:pro_lite")],
        [InlineKeyboardButton(text="Pro Plus — 1290₽ / 30 дней", callback_data="buy:pro_plus")],
        [InlineKeyboardButton(text="Pro Premium — 2990₽ / 90 дней", callback_data="buy:pro_premium")],
    ])

def admin_menu() -> InlineKeyboardMarkup:
    """Главное меню админ-панели"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Пользователи", callback_data="admin:users"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats")],
        [InlineKeyboardButton(text="💳 Платежи", callback_data="admin:payments"),
         InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast")],
        [InlineKeyboardButton(text="🔄 Проверить платежи", callback_data="admin:check_payments")],
        [InlineKeyboardButton(text="⬅️ Главное меню", callback_data="panel:main")]
    ])

def admin_back_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для возврата в админ-панель"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin:main")]
    ])

def keyboards_for_modes() -> InlineKeyboardMarkup:
    """Клавиатура для выбора режимов"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Ассистент", callback_data="mode:assistant"),
         InlineKeyboardButton(text="🎨 Генерация", callback_data="mode:image")],
        [InlineKeyboardButton(text="🛠 Редактор фото", callback_data="mode:editor"),
         InlineKeyboardButton(text="🤳 Селфи со звёздой", callback_data="mode:celebrity_selfie")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="panel:main")],
    ])

def top_panel(bot_username: str, ref_code: str) -> InlineKeyboardMarkup:
    """Верхняя панель управления"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Продлить подписку", callback_data="subs:show"),
         InlineKeyboardButton(text="🎛 Режим", callback_data="panel:mode")],
        [
            InlineKeyboardButton(
                text="👥 Пригласить",
                switch_inline_query=f"Переходи в https://t.me/{bot_username}?start={ref_code} — получи бонус!"
            )
        ],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="panel:help"),
         InlineKeyboardButton(text="🛡 Админ-панель", callback_data="panel:admin")],
    ])

def plan_buy_keyboard(plan_code: str, pay_url: str) -> InlineKeyboardMarkup:
    """Клавиатура для оплаты плана"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", url=pay_url)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="subs:show")]
    ])