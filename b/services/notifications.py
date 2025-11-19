from __future__ import annotations
from aiogram import Bot
from datetime import datetime
from config import cfg


class NotificationService:
    """Сервис для отправки уведомлений пользователям"""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_subscription_activated(
            self,
            user_id: int,
            plan_title: str,
            expires_at: datetime
    ) -> None:
        """
        Отправляет сообщение об успешной активации подписки

        Args:
            user_id: ID пользователя в Telegram
            plan_title: Название тарифного плана
            expires_at: Дата истечения подписки
        """
        try:
            # Форматируем дату в русском формате
            expires_str = expires_at.strftime("%d.%m.%Y %H:%M")

            # Текст сообщения по вашему примеру
            message_text = (
                "🚀 Ваша подписка успешно активирована!\n"
                f"Тариф: {plan_title} до {expires_str} МСК.\n"
                "Спасибо, что выбрали наш сервис!\n\n"

                "💌 Подарочные сертификаты\n"
                "Хотите сделать необычный подарок? У нас есть стильные электронные "
                "сертификаты на подписку – идеальный вариант для близких и друзей!\n\n"

                "👫 Приглашайте друзей и получайте бонусы:\n"
                "• Вам – +5 дней бесплатно за каждого приглашённого друга с оплаченной подпиской\n"
                "• Вашим друзьям – 3 дня бесплатного доступа\n\n"

                "Если у вас возникнут вопросы, мы всегда рады помочь!\n"
                "Приятного пользования! 🫶"
            )

            # Создаем клавиатуру с кнопками
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👫 Пригласить друга", callback_data="panel:referral")],
                [InlineKeyboardButton(text="❓ Помощь", callback_data="panel:help")]
            ])

            await self.bot.send_message(
                chat_id=user_id,
                text=message_text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        except Exception as e:
            # Логируем ошибку, но не прерываем выполнение
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Ошибка отправки уведомления пользователю {user_id}: {e}")

    async def send_payment_failed(self, user_id: int, reason: str) -> None:
        """
        Отправляет уведомление о неудачном платеже

        Args:
            user_id: ID пользователя в Telegram
            reason: Причина отказа
        """
        try:
            message_text = (
                "❌ Платеж не прошел\n\n"
                f"Причина: {reason}\n\n"
                "Пожалуйста, попробуйте еще раз или обратитесь в поддержку."
            )

            await self.bot.send_message(chat_id=user_id, text=message_text)

        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Ошибка отправки уведомления об ошибке платежа пользователю {user_id}: {e}")