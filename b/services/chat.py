from __future__ import annotations
from providers.openai_provider import OpenAIChatProvider

class ChatService:
    """
    Сервис обработки сообщений Telegram → GPT → Telegram (streaming).
    """

    def __init__(self, provider=None):
        # Можно менять модель через DI
        self.provider = provider or OpenAIChatProvider(model="gpt-4o")

    async def handle_user_message(self, message: str, bot, chat_id: int):
        """
        Асинхронный потоковый ответ GPT.
        Сначала отправляем "Думаю...", затем обновляем сообщение по мере получения токенов.
        """
        sent = await bot.send_message(chat_id, "🤔 Думаю…")
        full_text = ""

        async for delta in self.provider.stream_chat(
            [{"role": "user", "content": message}]
        ):
            full_text += delta

            # пробуем обновить сообщение
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent.message_id,
                    text=f"💬 {full_text}"
                )
            except Exception:
                # иногда Telegram может бросать FloodLimit или MessageNotModified — это нормально
                pass

        # финальное обновление текста
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=sent.message_id,
            text=f"💬 {full_text}"
        )
