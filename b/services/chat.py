from __future__ import annotations
from providers.openai_provider import OpenAIChatProvider

class ChatService:
    """
    Сервис обработки сообщений Telegram → GPT → Telegram (streaming).
    """

    def __init__(self, provider=None):
        self.provider = provider or OpenAIChatProvider(model="gpt-4o")

    async def handle_user_message(self, message: str, bot, chat_id: int):
        """
        Асинхронный потоковый ответ GPT с защитой от:
        - Telegram "message is not modified"
        - частого обновления
        """

        sent = await bot.send_message(chat_id, "🤔 Думаю…")

        full_text = ""
        last_sent_text = ""  # ← Храним предыдущую версию текста

        async for delta in self.provider.stream_chat(
            [{"role": "user", "content": message}]
        ):
            full_text += delta

            # Если текст не изменился — Telegram выдаст ошибку
            if full_text == last_sent_text:
                continue

            last_sent_text = full_text

            # Пробуем обновить сообщение
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent.message_id,
                    text=f"💬 {full_text}"
                )
            except Exception:
                # Игнорируем MessageNotModified и другие мелкие ошибки
                pass

        # Финальное обновление (тоже с проверкой)
        if full_text != last_sent_text:
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent.message_id,
                    text=f"💬 {full_text}"
                )
            except Exception:
                pass
