from __future__ import annotations

class SafetyDecision:
    def __init__(self, allowed: bool, reason: str | None = None):
        self.allowed = allowed
        self.reason = reason

class SafetyGuard:
    """Простая проверка контента. Здесь можно внедрить свои правила и/или модерацию провайдеров."""
    BLOCKED_CELEBRITY_SELFIE = False  # пример флага — для OpenAI лучше блокировать deepfake с публичными фигурами

    @classmethod
    def check_celebrity_selfie(cls, celebrity_name: str) -> SafetyDecision:
        if cls.BLOCKED_CELEBRITY_SELFIE:
            return SafetyDecision(False, "Создание реалистичных изображений с участием знаменитостей ограничено политиками. Выберите другой режим.")
        return SafetyDecision(True)

    @classmethod
    def check_text_length(cls, text: str, max_len: int) -> SafetyDecision:
        if len(text) > max_len:
            return SafetyDecision(False, f"Превышена длина текста ({len(text)} > {max_len}). Укоротите запрос.")
        return SafetyDecision(True)
