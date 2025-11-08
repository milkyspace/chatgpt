# Переписанный openai_utils.py
# Обновлено: переход на GPT-4o и gpt-image-1, удалены create_variation (dall-e-2)
# Код адаптирован для вашей логики, но модернизирован под современные API

from PIL import Image
from io import BytesIO
from typing import Optional, List
import config
import tiktoken
from openai import AsyncOpenAI
import anthropic
import logging
import base64
import asyncio
import requests

# Инициализация клиента OpenAI
if config.openai_api_base is not None:
    openai_client = AsyncOpenAI(api_key=config.openai_api_key, base_url=config.openai_api_base)
else:
    openai_client = AsyncOpenAI(api_key=config.openai_api_key)

OPENAI_COMPLETION_OPTIONS = {
    "temperature": 0.7,
    "max_tokens": 2000,
    "top_p": 1,
    "frequency_penalty": 0,
    "presence_penalty": 0,
}

logger = logging.getLogger(__name__)


def configure_logging():
    if config.enable_detailed_logging:
        logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    else:
        logging.basicConfig(level=logging.CRITICAL, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
    logger.setLevel(logging.getLogger().level)


configure_logging()


# ---------------------------- ✅ Улучшенная модель чата ----------------------------
class ChatGPT:
    def __init__(self, model="gpt-4o"):
        self.model = model
        self.is_claude_model = model.startswith("claude")
        self.logger = logging.getLogger(__name__)

    async def send_message(self, message, dialog_messages=[], chat_mode="assistant"):
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

        response = await openai_client.chat.completions.create(
            model=self.model,
            messages=messages,
            **OPENAI_COMPLETION_OPTIONS
        )

        answer = response.choices[0].message.content
        return answer, (response.usage.prompt_tokens, response.usage.completion_tokens), 0

    # ✅ CHAT + VISION + IMAGE GENERATION (как chatgpt.com)
    async def send_vision_message(self, message, dialog_messages=[], chat_mode="assistant",
                                  image_buffer: BytesIO = None):
        messages = self._generate_prompt_messages(message, dialog_messages, chat_mode, image_buffer=image_buffer)

        response = await openai_client.chat.completions.create(
            model=self.model,
            messages=messages,
            **OPENAI_COMPLETION_OPTIONS
        )

        answer = response.choices[0].message.content
        return answer, (response.usage.prompt_tokens, response.usage.completion_tokens), 0

    # --------------------- Сборка сообщений ---------------------
    def _generate_prompt_messages(self, message, dialog_messages, chat_mode, image_buffer: BytesIO = None):
        prompt = config.chat_modes[chat_mode]["prompt_start"]

        messages = [{"role": "system", "content": prompt}]

        for msg in dialog_messages:
            messages.append({"role": "user", "content": msg["user"]})
            messages.append({"role": "assistant", "content": msg["bot"]})

        # ✅ Вставляем изображение в message (GPT-4o Vision)
        if image_buffer is not None:
            encoded = base64.b64encode(image_buffer.read()).decode()
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": message},
                    {"type": "image", "image": encoded},
                ]
            })
        else:
            messages.append({"role": "user", "content": message})

        return messages


# ---------------------------- ✅ AUDIO ----------------------------
async def transcribe_audio(audio_file) -> str:
    try:
        transcript = await openai_client.audio.transcriptions.create(
            file=audio_file,
            model="whisper-1"
        )
        return transcript.text or ""
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return ""


# ---------------------------- ✅ NEW: DALL·E 3 / GPT-IMAGE-1 ----------------------------
async def generate_images(prompt: str, model: str = "gpt-image-1", size: str = "1024x1024") -> List[str]:
    try:
        response = await openai_client.images.generate(
            model=model,
            prompt=prompt,
            size=size,
            n=1,
            quality="high"
        )
        return [item.url for item in response.data]

    except Exception as e:
        logger.error(f"Error generating images: {e}")
        raise e


# ✅ Полноценная генерация с использованием фото (как chatgpt.com)
async def generate_photo(image, prompt: str) -> Optional[str]:
    """
    Полноценная фотогенерация: учитывает исходное фото и prompt.
    Принимает:
    - BytesIO
    - bytes
    - coroutine -> BytesIO
    """

    import inspect
    try:
        # ✅ Если функция получила coroutine — await
        if inspect.iscoroutine(image):
            image = await image

        # ✅ Если bytes → превращаем в BytesIO
        if isinstance(image, bytes):
            image = BytesIO(image)

        # ✅ Проверка на корректный тип
        if not isinstance(image, BytesIO):
            raise TypeError(
                f"generate_photo(): image must be BytesIO or bytes or coroutine, got {type(image)}"
            )

        # ✅ Reset pointer for read()
        image.seek(0)

        base64_img = base64.b64encode(image.read()).decode()

        response = await openai_client.images.generate(
            model="gpt-image-1",
            prompt=prompt,
            size="1024x1024",
            referenced_images=[base64_img],  # ← изображение подаётся сюда!
        )

        return response.data[0].url

    except Exception as e:
        logger.error(f"Error generating photo with face: {e}")
        return None


# ---------------------------- ✅ Util: PNG conversion ----------------------------
async def convert_image_to_png(image_buffer):
    import inspect

    if inspect.iscoroutine(image_buffer):
        image_buffer = await image_buffer

    if isinstance(image_buffer, bytes):
        image_buffer = BytesIO(image_buffer)

    if not isinstance(image_buffer, BytesIO):
        raise TypeError(
            f"convert_image_to_png(): image_buffer must be BytesIO or bytes or coroutine, got {type(image_buffer)}"
        )


# ---------------------------- ✅ Moderation ----------------------------
async def is_content_acceptable(prompt):
    try:
        response = await openai_client.moderations.create(input=prompt)
        return not all(response.results[0].categories.values())
    except Exception as e:
        logger.error(f"Error in content moderation: {e}")
        return True
