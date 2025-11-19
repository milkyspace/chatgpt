from __future__ import annotations
from typing import AsyncGenerator, Dict, Any, List, Optional
from providers.aitunnel_provider import AITunnelChatProvider


class ChatService:
    def __init__(self, provider=None):
        self.provider = provider or AITunnelChatProvider()

    async def handle_user_message(
        self,
        message: str,
        bot,
        chat_id: int,
        system_prompt: str = "Ты полезный ассистент."
    ) -> None:

        # начальное сообщение
        sent_message = await bot.send_message(chat_id, "🤔 Думаю…")

        buffer_text = ""
        last_sent = ""
        last_edit_time = 0

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": message},
        ]

        async for delta in self.provider.stream_chat(messages):
            buffer_text += delta
            now = asyncio.get_event_loop().time()

            # обновляем не чаще 1 раза в 0.3 сек
            if now - last_edit_time >= 0.3:
                last_edit_time = now

                if buffer_text != last_sent:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=sent_message.message_id,
                            text=f"💬 {buffer_text}"
                        )
                        last_sent = buffer_text
                    except Exception:
                        # можно добавить логирование
                        pass

        # финальное обновление
        if buffer_text != last_sent:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent_message.message_id,
                    text=f"💬 {buffer_text}"
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