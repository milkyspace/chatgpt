from __future__ import annotations
from providers.openai_provider import OpenAIChatProvider
from services.safety import SafetyGuard

class ChatService:
    """Фасад для чатов. Можно подменять провайдер через DI."""
    def __init__(self, provider: OpenAIChatProvider | None = None):
        self.provider = provider or OpenAIChatProvider(model="gpt-4o")

    async def reply(self, user_id: int, messages: list[dict[str, str]], max_text_len: int) -> str:
        # safety: проверяем длину последнего пользователя
        for m in reversed(messages):
            if m.get("role") == "user":
                dec = SafetyGuard.check_text_length(m.get("content", ""), max_text_len)
                if not dec.allowed:
                    return dec.reason or "Запрос слишком длинный."
                break
        # Простейший респонс
        return await self.provider.chat(messages, max_tokens=800)

    async def stream_reply(self, bot, chat_id: int, messages: list[dict[str, str]], max_text_len: int):
        """Потоково отвечает пользователю — редактирует сообщение по мере генерации."""
        for m in reversed(messages):
            if m.get("role") == "user":
                dec = SafetyGuard.check_text_length(m.get("content", ""), max_text_len)
                if not dec.allowed:
                    await bot.send_message(chat_id, dec.reason)
                    return

        msg = await bot.send_message(chat_id, "🌀 Думаю...")
        text = ""

        async for chunk in self.provider.stream_chat(messages):
            text += chunk
            if len(text) % 100 == 0:  # обновляем каждые ~100 символов
                try:
                    await bot.edit_message_text(text, chat_id, msg.message_id)
                except Exception:
                    pass

        try:
            await bot.edit_message_text(text or "⚠️ Ошибка генерации.", chat_id, msg.message_id)
        except Exception:
            await bot.send_message(chat_id, text or "⚠️ Ошибка генерации.")
        return text
