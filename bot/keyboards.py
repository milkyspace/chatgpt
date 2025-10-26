"""
Модуль для создания клавиатур бота
"""

import emoji
from datetime import datetime
from telegram import ReplyKeyboardMarkup, KeyboardButton
import database
import config
from subscription import SubscriptionType


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

        # Получаем информацию о подписке
        subscription_info = db_instance.get_user_subscription_info(user_id)

        # Создаем клавиатуру
        keyboard = []

        # Кнопка с информацией о подписке
        if subscription_info["is_active"]:
            expires_at = subscription_info["expires_at"]
            dateto = expires_at.strftime('%d.%m.%Y %H:%M')

            if expires_at > datetime(2100, 1, 1):
                status_text = emoji.emojize(":green_circle: Подписка активна навсегда")
            else:
                subName = 'Тестовая подписка'
                if subscription_info["type"] == SubscriptionType.PRO_LITE:
                    subName = 'Подписка Pro Lite'
                elif subscription_info["type"] == SubscriptionType.PRO_PLUS:
                    subName = 'Подписка Pro Plus'
                elif subscription_info["type"] == SubscriptionType.PRO_PREMIUM:
                    subName = 'Подписка Pro Premium'
                status_text = emoji.emojize(f":green_circle: {subName} активна до: {dateto} МСК")
        else:
            # Если подписка была, но истекла
            if "expires_at" in subscription_info and subscription_info["expires_at"]:
                dateto = subscription_info["expires_at"].strftime('%d.%m.%Y %H:%M')
                status_text = emoji.emojize(f":red_circle: Подписка закончилась: {dateto} МСК")
            else:
                status_text = emoji.emojize(":red_circle: Подписка не активна")

        keyboard.append([KeyboardButton(status_text)])

        # Основные кнопки
        keyboard.extend([
            [
                KeyboardButton(emoji.emojize("Продлить подписку :money_bag:")),
                KeyboardButton(emoji.emojize("Поддержать проект :red_heart:"))
            ],
            [
                KeyboardButton(emoji.emojize("Пригласить :woman_and_man_holding_hands:")),
                KeyboardButton(emoji.emojize("Помощь :heart_hands:"))
            ]
        ])

        # Кнопка админ-панели для администраторов
        if user_id in config.roles.get('admin', []):
            keyboard.append([KeyboardButton(emoji.emojize("Админ-панель :smiling_face_with_sunglasses:"))])

        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    @staticmethod
    def get_admin_keyboard() -> ReplyKeyboardMarkup:
        """
        Создает клавиатуру админ-панели

        Returns:
            ReplyKeyboardMarkup: Клавиатура админ-панели
        """
        keyboard = [
            [KeyboardButton(emoji.emojize("Серверы :desktop_computer:"))],
            [KeyboardButton(emoji.emojize("Вывести пользователей :bust_in_silhouette:"))],
            [KeyboardButton(emoji.emojize("Активировать пользователя вручную :man:"))],
            [KeyboardButton(emoji.emojize("Редактировать пользователя по id"))],
            [KeyboardButton(emoji.emojize("Отправить аналитику :bar_chart:"))],
            [KeyboardButton(emoji.emojize("Отправить рассылку :pencil:"))],
            [KeyboardButton(emoji.emojize("Главное меню :right_arrow_curving_left:"))]
        ]

        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    @staticmethod
    def get_back_keyboard() -> ReplyKeyboardMarkup:
        """
        Создает клавиатуру с кнопкой "Назад"

        Returns:
            ReplyKeyboardMarkup: Клавиатура с кнопкой назад
        """
        keyboard = [
            [KeyboardButton(emoji.emojize("Назад :right_arrow_curving_left:"))]
        ]

        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)