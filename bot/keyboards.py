"""
Модуль для создания клавиатур бота
"""

import emoji
from datetime import datetime
from telegram import ReplyKeyboardMarkup, KeyboardButton
import database
import config
from subscription import SubscriptionType
import logging

logger = logging.getLogger(__name__)

# Константы для эмодзи и текстов
_EMOJI = {
    "green_circle": ":green_circle:",
    "red_circle": ":red_circle:",
    "money_bag": ":money_bag:",
    "red_heart": ":red_heart:",
    "woman_and_man_holding_hands": ":woman_and_man_holding_hands:",
    "heart_hands": ":heart_hands:",
    "smiling_face_with_sunglasses": ":smiling_face_with_sunglasses:",
    "back_arrow": ":right_arrow_curving_left:"
}

_SUBSCRIPTION_NAMES = {
    SubscriptionType.PRO_LITE: "Подписка Pro Lite",
    SubscriptionType.PRO_PLUS: "Подписка Pro Plus",
    SubscriptionType.PRO_PREMIUM: "Подписка Pro Premium"
}

class BotKeyboards:
    """Класс для создания клавиатур бота"""

    @staticmethod
    async def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
        """
        Создает главную клавиатуру с информацией о подписке и основными кнопками

        Args:
            user_id: ID пользователя

        Returns:
            ReplyKeyboardMarkup: Клавиатура главного меню
        """
        db_instance = database.Database()
        subscription_info = db_instance.get_user_subscription_info(user_id)

        keyboard = [[KeyboardButton(BotKeyboards._get_subscription_status_text(subscription_info))]]

        # Основные кнопки
        keyboard.extend([
            [
                KeyboardButton(emoji.emojize(f"Продлить подписку {_EMOJI['money_bag']}")),
                KeyboardButton(emoji.emojize(f"Выбрать режим {_EMOJI['red_heart']}"))
            ],
            [
                KeyboardButton(emoji.emojize(f"Пригласить {_EMOJI['woman_and_man_holding_hands']}")),
                KeyboardButton(emoji.emojize(f"Помощь {_EMOJI['heart_hands']}"))
            ]
        ])

        # Кнопка админ-панели для администраторов
        if str(user_id) in config.roles.get('admin', []):
            keyboard.append([KeyboardButton(emoji.emojize(f"Админ-панель {_EMOJI['smiling_face_with_sunglasses']}"))])

        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    @staticmethod
    def _get_subscription_status_text(subscription_info: dict) -> str:
        """Формирует текст статуса подписки"""
        if not subscription_info["is_active"]:
            return BotKeyboards._get_inactive_subscription_text(subscription_info)
        return BotKeyboards._get_active_subscription_text(subscription_info)

    @staticmethod
    def _get_active_subscription_text(subscription_info: dict) -> str:
        """Формирует текст для активной подписки"""
        expires_at = subscription_info["expires_at"]
        dateto = expires_at.strftime('%d.%m.%Y %H:%M')

        if expires_at > datetime(2100, 1, 1):
            return emoji.emojize(f"{_EMOJI['green_circle']} Подписка активна навсегда")

        sub_name = _SUBSCRIPTION_NAMES.get(
            subscription_info["type"],
            "Тестовая подписка"
        )
        return emoji.emojize(f"{_EMOJI['green_circle']} {sub_name} активна до: {dateto} МСК")

    @staticmethod
    def _get_inactive_subscription_text(subscription_info: dict) -> str:
        """Формирует текст для неактивной подписки"""
        expires_at = subscription_info.get("expires_at")
        if expires_at:
            dateto = expires_at.strftime('%d.%m.%Y %H:%M')
            return emoji.emojize(f"{_EMOJI['red_circle']} Подписка закончилась: {dateto} МСК")
        return emoji.emojize(f"{_EMOJI['red_circle']} Подписка не активна")

    @staticmethod
    def get_back_keyboard() -> ReplyKeyboardMarkup:
        """
        Создает клавиатуру с кнопкой "Назад"

        Returns:
            ReplyKeyboardMarkup: Клавиатура с кнопкой назад
        """
        keyboard = [
            [KeyboardButton(emoji.emojize(f"Назад {_EMOJI['back_arrow']}"))]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    @staticmethod
    def get_back_to_admin_keyboard() -> ReplyKeyboardMarkup:
        """
        Создает клавиатуру для возврата в админ-панель.

        Returns:
            ReplyKeyboardMarkup: Клавиатура с кнопкой возврата в админ-панель
        """
        keyboard = [
            [KeyboardButton(emoji.emojize("Назад в админ-панель"))],
            [KeyboardButton(emoji.emojize("Главное меню"))]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    @staticmethod
    def get_admin_keyboard() -> ReplyKeyboardMarkup:
        """
        Создает клавиатуру админ-панели

        Returns:
            ReplyKeyboardMarkup: Клавиатура админ-панели
        """
        keyboard = [
            [KeyboardButton(emoji.emojize("Вывести пользователей"))],
            [KeyboardButton(emoji.emojize("Редактировать пользователя"))],
            [KeyboardButton(emoji.emojize("Данные пользователя"))],
            [KeyboardButton(emoji.emojize("Отправить рассылку"))],
            [KeyboardButton(emoji.emojize("Главное меню"))]
        ]
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)