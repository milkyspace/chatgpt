import logging
import asyncio

from datetime import datetime
from typing import Optional, Dict, Any
from telegram import (Update, User, InlineKeyboardButton, InlineKeyboardMarkup,)
from telegram.ext import (CallbackContext)
from telegram.constants import ParseMode

import config
from ..handlers.base_handler import BaseHandler
from ..keyboards import BotKeyboards
from ..database import database

# Глобальные переменные
db = database.Database()
bot_instance = None
user_semaphores: Dict[int, asyncio.Semaphore] = {}
user_tasks: Dict[int, asyncio.Task] = {}

# Настройка логирования
logger = logging.getLogger(__name__)

class AdminHandlers(BaseHandler):
    """Класс для обработки админ-панели."""

    async def admin_panel_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду админ-панели."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к админ-панели.")
            return

        await self._show_admin_panel(update, context)

    def _is_admin(self, user_id: int) -> bool:
        """Проверяет, является ли пользователь администратором."""
        return str(user_id) in config.roles.get('admin', [])

    async def _show_admin_panel(self, update: Update, context: CallbackContext) -> None:
        """Показывает админ-панель."""
        text = "🛠️ <b>Админ-панель</b>\n\nВыберите действие:"
        reply_markup = BotKeyboards.get_admin_keyboard()

        if update.message:
            await update.message.reply_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        else:
            await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode=ParseMode.HTML)

    async def show_users_handle(self, update: Update, context: CallbackContext) -> None:
        """Показывает список пользователей."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        users = self.db.get_users_and_roles()

        if not users:
            await update.message.reply_text("📝 Пользователей не найдено.")
            return

        text = "👥 <b>Список пользователей:</b>\n\n"
        for i, user in enumerate(users[:50], 1):
            username = user.get('username', 'Нет username')
            first_name = user.get('first_name', 'Нет имени')
            role = user.get('role', 'Не указана')
            last_interaction = user.get('last_interaction', 'Неизвестно')

            if isinstance(last_interaction, datetime):
                last_interaction = last_interaction.strftime("%d.%m.%Y %H:%M")

            text += f"{i}. ID: {user['_id']}\n"
            text += f"   👤: {first_name} (@{username})\n"
            text += f"   🏷️: {role}\n"
            text += f"   ⏰: {last_interaction}\n\n"

        if len(users) > 50:
            text += f"\n... и еще {len(users) - 50} пользователей"

        reply_markup = BotKeyboards.get_back_to_admin_keyboard()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def edit_user_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает запрос на редактирование пользователя."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        text = (
            "✏️ <b>Редактирование пользователя</b>\n\n"
            "Для редактирования пользователя отправьте команду в формате:\n"
            "<code>/edit_user USER_ID ROLE</code>\n\n"
            "Пример:\n"
            "<code>/edit_user 123456789 admin</code>\n\n"
            "Доступные роли: admin, beta_tester, friend, regular_user, trial_user"
        )

        reply_markup = BotKeyboards.get_back_to_admin_keyboard()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def broadcast_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает запрос на рассылку."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        text = (
            "📢 <b>Рассылка сообщений</b>\n\n"
            "Для отправки рассылки отправьте команду в формате:\n"
            "<code>/broadcast ТЕКСТ_СООБЩЕНИЯ</code>\n\n"
            "Пример:\n"
            "<code>/broadcast Всем привет! Это тестовая рассылка.</code>"
        )

        reply_markup = BotKeyboards.get_back_to_admin_keyboard()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def handle_main_menu_back(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает возврат в главное меню из админ-панели."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        reply_markup = await BotKeyboards.get_main_keyboard(user_id)
        await update.message.reply_text(
            "Возврат в главное меню...",
            reply_markup=reply_markup,
            parse_mode=ParseMode.HTML
        )

    async def handle_admin_panel_back(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает возврат в админ-панель."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к админ-панели.")
            return

        await self._show_admin_panel(update, context)

    async def edit_user_command(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /edit_user."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        if not context.args or len(context.args) < 2:
            await update.message.reply_text(
                "❌ Неправильный формат команды.\n"
                "Используйте: /edit_user USER_ID ROLE\n"
                "Пример: /edit_user 123456789 admin"
            )
            return

        try:
            target_user_id = int(context.args[0])
            new_role = context.args[1]

            if not self.db.check_if_user_exists(target_user_id):
                await update.message.reply_text(f"❌ Пользователь с ID {target_user_id} не найден.")
                return

            valid_roles = ['admin', 'beta_tester', 'friend', 'regular_user', 'trial_user']
            if new_role not in valid_roles:
                await update.message.reply_text(
                    f"❌ Неверная роль. Допустимые роли: {', '.join(valid_roles)}"
                )
                return

            self.db.set_user_attribute(target_user_id, "role", new_role)

            await update.message.reply_text(
                f"✅ Роль пользователя {target_user_id} успешно изменена на '{new_role}'"
            )

        except ValueError:
            await update.message.reply_text("❌ ID пользователя должен быть числом.")
        except Exception as e:
            logger.error(f"Error editing user: {e}")
            await update.message.reply_text("❌ Произошла ошибка при изменении роли пользователя.")

    async def broadcast_command(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /broadcast."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        if not context.args:
            await update.message.reply_text(
                "❌ Неправильный формат команды.\n"
                "Используйте: /broadcast ТЕКСТ_СООБЩЕНИЯ\n"
                "Пример: /broadcast Всем привет! Это тестовая рассылка."
            )
            return

        message_text = ' '.join(context.args)

        confirmation_text = (
            f"📢 <b>Подтверждение рассылки</b>\n\n"
            f"Текст сообщения:\n{message_text}\n\n"
            f"Отправить это сообщение всем пользователям?"
        )

        keyboard = [
            [
                InlineKeyboardButton("✅ Да, отправить", callback_data=f"confirm_broadcast|{message_text}"),
                InlineKeyboardButton("❌ Отмена", callback_data="cancel_broadcast")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(confirmation_text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def broadcast_confirmation_handler(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает подтверждение рассылки."""
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        if not self._is_admin(user_id):
            await query.edit_message_text("❌ У вас нет доступа к этой команде.")
            return

        data = query.data

        if data == "cancel_broadcast":
            await query.edit_message_text("❌ Рассылка отменена.")
            return

        if data.startswith("confirm_broadcast|"):
            message_text = data.split("|", 1)[1]

            await query.edit_message_text("🔄 Начинаю рассылку...")

            all_user_ids = self.db.get_all_user_ids()
            success_count = 0
            fail_count = 0

            for target_user_id in all_user_ids:
                try:
                    user_data = self.db.get_user_by_id(target_user_id)
                    if user_data and 'chat_id' in user_data:
                        await context.bot.send_message(
                            chat_id=user_data['chat_id'],
                            text=f"📢 <b>Рассылка от администратора:</b>\n\n{message_text}",
                            parse_mode=ParseMode.HTML
                        )
                        success_count += 1
                    else:
                        fail_count += 1
                except Exception as e:
                    logger.error(f"Error sending broadcast to {target_user_id}: {e}")
                    fail_count += 1

                await asyncio.sleep(0.1)

            result_text = (
                f"✅ <b>Рассылка завершена</b>\n\n"
                f"✅ Успешно: {success_count}\n"
                f"❌ Не удалось: {fail_count}\n"
                f"📊 Всего: {len(all_user_ids)}"
            )

            await query.edit_message_text(result_text, parse_mode=ParseMode.HTML)

    async def get_user_data_handle(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает запрос на получение данных пользователя."""
        user_id = await self.ensure_user_initialized(update, context, update.message.from_user)

        if not self._is_admin(user_id):
            await update.message.reply_text("❌ У вас нет доступа к этой команде.")
            return

        text = (
            "👤 <b>Получение данных пользователя</b>\n\n"
            "Для получения данных отправьте команду в формате:\n"
            "<code>/user_data USER_ID</code>\n\n"
            "Пример:\n"
            "<code>/user_data 123456789</code>\n\n"
            "Или отправьте username пользователя:\n"
            "<code>/user_data @username</code>"
        )

        reply_markup = BotKeyboards.get_back_to_admin_keyboard()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

    async def get_user_data_command(self, update: Update, context: CallbackContext) -> None:
        """Обрабатывает команду /user_data."""
        try:
            user = self._get_user_from_update(update)
            user_id = await self.ensure_user_initialized(update, context, user)

            if not self._is_admin(user_id):
                await self._send_reply(update, "❌ У вас нет доступа к этой команде.")
                return

            if not context.args:
                error_text = (
                    "❌ Неправильный формат команды.\n"
                    "Используйте: /user_data USER_ID\n"
                    "Пример: /user_data 123456789"
                )
                await self._send_reply(update, error_text)
                return

            user_identifier = context.args[0]
            target_user = self._find_user_by_identifier(user_identifier)

            if not target_user:
                await self._send_reply(update, f"❌ Пользователь '{user_identifier}' не найден.")
                return

            user_info = await self._format_user_details(target_user)
            await self._send_reply(update, user_info)

        except Exception as e:
            logger.error(f"Error getting user data: {e}")
            error_text = "❌ Произошла ошибка при получении данных пользователя."
            await self._send_reply(update, error_text)

    def _get_user_from_update(self, update: Update) -> Optional[User]:
        """Получает пользователя из update."""
        if update.message:
            return update.message.from_user
        elif update.callback_query:
            return update.callback_query.from_user
        return None

    def _find_user_by_identifier(self, user_identifier: str) -> Optional[Dict[str, Any]]:
        """Находит пользователя по ID или username."""
        if user_identifier.startswith('@'):
            username = user_identifier[1:]
            return self.db.find_user_by_username(username)
        else:
            try:
                target_user_id = int(user_identifier)
                return self.db.get_user_by_id(target_user_id)
            except ValueError:
                return None

    async def _send_reply(self, update: Update, text: str, parse_mode: str = ParseMode.HTML) -> None:
        """Вспомогательный метод для отправки ответа."""
        try:
            if update.message:
                await update.message.reply_text(text, parse_mode=parse_mode)
            elif update.callback_query:
                await update.callback_query.message.reply_text(text, parse_mode=parse_mode)
        except Exception as e:
            logger.error(f"Error sending reply: {e}")

    async def _format_user_details(self, user_data: Dict[str, Any]) -> str:
        """Форматирует подробную информацию о пользователе."""
        user_id = user_data['_id']

        text = f"👤 <b>Данные пользователя</b>\n\n"
        text += f"<b>ID:</b> <code>{user_id}</code>\n"
        text += f"<b>Username:</b> @{user_data.get('username', 'не указан')}\n"
        text += f"<b>Имя:</b> {user_data.get('first_name', 'не указано')}\n"
        text += f"<b>Фамилия:</b> {user_data.get('last_name', 'не указана')}\n"
        text += f"<b>Chat ID:</b> <code>{user_data.get('chat_id', 'не указан')}</code>\n"
        text += f"<b>Роль:</b> {user_data.get('role', 'не указана')}\n\n"

        # Информация о подписке
        subscription_info = self.db.get_user_subscription_info(user_id)
        if subscription_info["is_active"]:
            expires_at = subscription_info["expires_at"].strftime("%d.%m.%Y %H:%M")
            text += f"<b>Подписка:</b> {subscription_info['type']}\n"
            text += f"<b>Действует до:</b> {expires_at}\n"
            text += f"<b>Запросов использовано:</b> {subscription_info['requests_used']}\n"
            text += f"<b>Изображений использовано:</b> {subscription_info['images_used']}\n\n"
        else:
            text += "<b>Подписка:</b> не активна\n\n"

        # Статистика использования
        text += "<b>Статистика использования:</b>\n"

        n_used_tokens = user_data.get('n_used_tokens', {})
        if n_used_tokens:
            for model, tokens in n_used_tokens.items():
                input_tokens = tokens.get('n_input_tokens', 0)
                output_tokens = tokens.get('n_output_tokens', 0)
                text += f"  {model}: {input_tokens} ввод / {output_tokens} вывод\n"
        else:
            text += "  Токены: не использовались\n"

        n_generated_images = user_data.get('n_generated_images', 0)
        text += f"  Сгенерировано изображений: {n_generated_images}\n"

        n_transcribed_seconds = user_data.get('n_transcribed_seconds', 0)
        text += f"  Расшифровано аудио: {n_transcribed_seconds} сек.\n\n"

        # Финансовая информация
        financials = self.db.get_user_financials(user_id)
        text += "<b>Финансовая информация:</b>\n"
        text += f"  Баланс RUB: {user_data.get('rub_balance', 0)}₽\n"
        text += f"  Баланс EUR: {user_data.get('euro_balance', 0)}€\n"
        text += f"  Всего пополнено: {financials.get('total_topup', 0)}₽\n"
        text += f"  Всего потрачено: {user_data.get('total_spent', 0)}₽\n"
        text += f"  Пожертвовано: {financials.get('total_donated', 0)}₽\n\n"

        # Информация о активности
        first_seen = user_data.get('first_seen', 'неизвестно')
        last_interaction = user_data.get('last_interaction', 'неизвестно')

        if isinstance(first_seen, datetime):
            first_seen = first_seen.strftime("%d.%m.%Y %H:%M")
        if isinstance(last_interaction, datetime):
            last_interaction = last_interaction.strftime("%d.%m.%Y %H:%M")

        text += f"<b>Первое посещение:</b> {first_seen}\n"
        text += f"<b>Последняя активность:</b> {last_interaction}\n"

        current_model = user_data.get('current_model', 'не указана')
        current_chat_mode = user_data.get('current_chat_mode', 'не указан')
        text += f"<b>Текущая модель:</b> {current_model}\n"
        text += f"<b>Режим чата:</b> {current_chat_mode}\n"

        return text