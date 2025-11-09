from __future__ import annotations
from providers.openai_provider import OpenAIChatProvider
from services.safety import SafetyGuard

class ChatService:
    def __init__(self, provider=None):
        self.provider = provider or OpenAIChatProvider(model="gpt-4o")

    async def handle_user_message(self, message: str, bot, chat_id: int):
        """Обрабатывает текст от пользователя с потоковой генерацией."""
        sent = await bot.send_message(chat_id, "🤔 Думаю…")

        full_text = ""
        async for delta in self.provider.stream_chat([{"role": "user", "content": message}]):
            full_text += delta
            try:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=sent.message_id,
                    text=f"💬 {full_text}"
                )
            except Exception:
                pass  # при rate limit Telegram просто пропускаем шаг
        return full_text