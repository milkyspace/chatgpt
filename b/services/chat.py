from __future__ import annotations
from typing import AsyncGenerator, Dict, Any, List, Optional
from providers.aitunnel_provider import AITunnelChatProvider


class ChatService:
    """
    Сервис обработки сообщений через AITUNNEL с поддержкой потоковой передачи.
    """

    def __init__(self, provider: AITunnelChatProvider = None):
        """
        Инициализация сервиса чата.

        Args:
            provider: Провайдер чата (по умолчанию AITunnelChatProvider)
        """
        self.provider = provider or AITunnelChatProvider()

    async def handle_user_message(
            self,
            message: str,
            bot,
            chat_id: int,
            system_prompt: str = "Ты полезный ассистент."
    ) -> None:
        """
        Обработка пользовательского сообщения с потоковой передачей ответа.

        Args:
            message: Сообщение пользователя
            bot: Экземпляр бота
            chat_id: ID чата
            system_prompt: Системный промпт
        """
        # Отправляем начальное сообщение
        sent_message = await bot.send_message(chat_id, "🤔 Думаю…")

        full_text = ""
        last_sent_text = ""

        # Подготавливаем сообщения для модели
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message}
        ]

        # Получаем потоковый ответ
        async for delta in self.provider.stream_chat(messages):
            full_text += delta

            # Обновляем сообщение только если текст изменился
            if full_text != last_sent_text:
                last_sent_text = full_text

                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=sent_message.message_id,
                        text=f"💬 {full_text}"
                    )
                except Exception:
                    # Игнорируем ошибки редактирования (MessageNotModified и др.)
                    pass

        # Финальное обновление для гарантии актуальности текста
        if full_text != last_sent_text:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent_message.message_id,
                    text=f"💬 {full_text}"
                )
            except Exception:
                pass

    async def chat_with_tools(
            self,
            messages: List[Dict[str, Any]],
            tools: List[Dict],
            max_tokens: int = 800
    ) -> Dict[str, Any]:
        """
        Чат с поддержкой вызова инструментов.

        Args:
            messages: История сообщений
            tools: Список инструментов
            max_tokens: Максимальное количество токенов

        Returns:
            Результат выполнения с контентом и вызовами инструментов
        """
        return await self.provider.chat_with_tools(
            messages=messages,
            tools=tools,
            max_tokens=max_tokens
        )