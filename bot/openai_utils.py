from PIL import Image
from io import BytesIO
from typing import Optional, List
import config
import imghdr
import tiktoken
import openai
from openai import AsyncOpenAI
import anthropic
import logging
import base64
import asyncio
import requests

# setup openai
openai_api_key = config.openai_api_key
anthropic_api_key = config.anthropic_api_key

# Инициализация клиента для нового API
if config.openai_api_base is not None:
    openai_client = AsyncOpenAI(api_key=openai_api_key, base_url=config.openai_api_base)
else:
    openai_client = AsyncOpenAI(api_key=openai_api_key)

OPENAI_COMPLETION_OPTIONS = {
    "temperature": 0.7,
    "max_tokens": 1000,
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


def validate_payload(payload):
    for message in payload.get("messages", []):
        if not isinstance(message.get("content"), str):
            logger.error("Invalid message content: Not a string")
            raise ValueError("Message content must be a string")


class ChatGPT:
    def __init__(self, model="gpt-4-1106-preview"):
        assert model in {"text-davinci-003", "gpt-3.5-turbo-16k", "gpt-3.5-turbo", "gpt-4", "gpt-4-1106-preview",
                         "gpt-4-vision-preview", "gpt-4-turbo-2024-04-09", "gpt-4o", "claude-3-opus-20240229",
                         "claude-3-sonnet-20240229", "claude-3-haiku-20240307"}, f"Unknown model: {model}"
        self.model = model
        self.is_claude_model = model.startswith("claude")
        self.logger = logging.getLogger(__name__)

    async def send_message(self, message, dialog_messages=[], chat_mode="assistant"):
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if self.is_claude_model:
                    prompt = self._generate_claude_prompt(message, dialog_messages, chat_mode)
                    self.logger.debug(f"Claude prompt: {prompt}")

                    if not prompt.strip():
                        raise ValueError("Generated prompt is empty")

                    client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)
                    response = await client.completions.create(
                        model=self.model,
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=1000,
                        temperature=0.7
                    )
                    self.logger.debug(f"Claude API response: {response}")

                    answer = ""
                    for text_block in response.content:
                        self.logger.debug(f"TextBlock: {text_block}")
                        answer += text_block.text

                    if not answer.strip():
                        self.logger.error("Received empty response from Claude API.")
                        raise ValueError("Received empty response from Claude API.")

                    n_input_tokens, n_output_tokens = self._count_tokens_from_messages([], answer, model=self.model)
                else:
                    # ИСПРАВЛЕНО: используем новый API для OpenAI моделей
                    if self.model in {"gpt-3.5-turbo-16k", "gpt-3.5-turbo", "gpt-4", "gpt-4-1106-preview",
                                      "gpt-4-vision-preview", "gpt-4-turbo-2024-04-09", "gpt-4o"}:
                        messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

                        validate_payload({
                            "model": self.model,
                            "messages": messages,
                            **OPENAI_COMPLETION_OPTIONS
                        })

                        # НОВЫЙ API
                        response = await openai_client.chat.completions.create(
                            model=self.model,
                            messages=messages,
                            **OPENAI_COMPLETION_OPTIONS
                        )
                        answer = response.choices[0].message.content
                        n_input_tokens, n_output_tokens = response.usage.prompt_tokens, response.usage.completion_tokens

                    elif self.model == "text-davinci-003":
                        # Для старых моделей используем старый API через requests
                        prompt = self._generate_prompt(message, dialog_messages, chat_mode)
                        answer, n_input_tokens, n_output_tokens = await self._send_legacy_completion(prompt)
                    else:
                        raise ValueError(f"Unknown model: {self.model}")

                answer = self._postprocess_answer(answer)
            except Exception as e:  # Убрана специфичная проверка на InvalidRequestError
                if len(dialog_messages) == 0:
                    raise ValueError(
                        "Dialog messages is reduced to zero, but still has too many tokens to make completion") from e

                # forget first message in dialog_messages
                dialog_messages = dialog_messages[1:]

        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)

        return answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    async def _send_legacy_completion(self, prompt):
        """Отправка запросов для устаревших моделей через старый API"""
        try:
            # Используем requests для старых моделей
            headers = {
                "Authorization": f"Bearer {config.openai_api_key}",
                "Content-Type": "application/json"
            }

            data = {
                "model": "text-davinci-003",
                "prompt": prompt,
                **OPENAI_COMPLETION_OPTIONS
            }

            if config.openai_api_base:
                url = f"{config.openai_api_base}/completions"
            else:
                url = "https://api.openai.com/v1/completions"

            response = requests.post(url, headers=headers, json=data, timeout=60)
            response.raise_for_status()

            result = response.json()
            answer = result['choices'][0]['text']
            n_input_tokens = result['usage']['prompt_tokens']
            n_output_tokens = result['usage']['completion_tokens']

            return answer, n_input_tokens, n_output_tokens

        except Exception as e:
            logger.error(f"Error in legacy completion: {e}")
            raise e

    async def send_message_stream(self, message, dialog_messages=[], chat_mode="assistant"):
        if chat_mode not in config.chat_modes.keys():
            raise ValueError(f"Chat mode {chat_mode} is not supported")

        n_dialog_messages_before = len(dialog_messages)
        answer = None
        n_input_tokens, n_output_tokens, n_first_dialog_messages_removed = 0, 0, 0

        while answer is None:
            try:
                if self.is_claude_model:
                    prompt = self._generate_claude_prompt(message, dialog_messages, chat_mode)

                    if not prompt.strip():
                        raise ValueError("Generated prompt is empty")

                    client = anthropic.AsyncAnthropic(api_key=config.anthropic_api_key)

                    async with client.messages.stream(
                            model=self.model,
                            messages=[{"role": "user", "content": prompt}],
                            max_tokens=1000,
                            temperature=0.0
                    ) as stream:
                        async for event in stream.text_stream:
                            if event:
                                if isinstance(event, str):
                                    if answer is None:
                                        answer = event
                                    else:
                                        answer += event
                                    n_input_tokens, n_output_tokens = self._count_tokens_from_messages([], answer,
                                                                                                       model=self.model)
                                    yield "not_finished", answer, (
                                    n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                    if not answer.strip():
                        raise ValueError("Received empty response from Claude API.")

                else:
                    if self.model in {"gpt-3.5-turbo-16k", "gpt-3.5-turbo", "gpt-4", "gpt-4-1106-preview",
                                      "gpt-4-turbo-2024-04-09", "gpt-4o"}:

                        messages = self._generate_prompt_messages(message, dialog_messages, chat_mode)

                        # НОВЫЙ API для streaming
                        response = await openai_client.chat.completions.create(
                            model=self.model,
                            messages=messages,
                            stream=True,
                            **OPENAI_COMPLETION_OPTIONS
                        )

                        answer = ""
                        async for chunk in response:
                            if chunk.choices[0].delta.content is not None:
                                answer += chunk.choices[0].delta.content
                                n_input_tokens, n_output_tokens = self._count_tokens_from_messages(messages, answer,
                                                                                                   model=self.model)
                                n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
                                yield "not_finished", answer, (
                                n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                        yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                    elif self.model == "text-davinci-003":
                        prompt = self._generate_prompt(message, dialog_messages, chat_mode)
                        # Для streaming старых моделей используем синхронный подход
                        answer = ""
                        # Упрощенная реализация без streaming для старых моделей
                        answer, n_input_tokens, n_output_tokens = await self._send_legacy_completion(prompt)
                        yield "finished", answer, (n_input_tokens, n_output_tokens), 0

                answer = self._postprocess_answer(answer)

            except Exception as e:
                if len(dialog_messages) == 0:
                    raise e

                dialog_messages = dialog_messages[1:]
                n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)

        yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    async def send_vision_message(
            self,
            message,
            dialog_messages=[],
            chat_mode="assistant",
            image_buffer: BytesIO = None,
    ):
        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if self.model == "gpt-4-vision-preview":
                    messages = self._generate_prompt_messages(
                        message, dialog_messages, chat_mode, image_buffer
                    )
                    # НОВЫЙ API для vision
                    response = await openai_client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        **OPENAI_COMPLETION_OPTIONS
                    )
                    answer = response.choices[0].message.content
                    n_input_tokens, n_output_tokens = response.usage.prompt_tokens, response.usage.completion_tokens
                else:
                    raise ValueError(f"Unsupported model: {self.model}")

                answer = self._postprocess_answer(answer)
            except Exception as e:
                if len(dialog_messages) == 0:
                    raise ValueError(
                        "Dialog messages is reduced to zero, but still has too many tokens to make completion"
                    ) from e

                dialog_messages = dialog_messages[1:]

        n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)

        return (
            answer,
            (n_input_tokens, n_output_tokens),
            n_first_dialog_messages_removed,
        )

    async def send_vision_message_stream(
            self,
            message,
            dialog_messages=[],
            chat_mode="assistant",
            image_buffer: BytesIO = None,
    ):
        n_dialog_messages_before = len(dialog_messages)
        answer = None
        while answer is None:
            try:
                if self.model == "gpt-4-vision-preview":
                    messages = self._generate_prompt_messages(
                        message, dialog_messages, chat_mode, image_buffer
                    )

                    # НОВЫЙ API для vision streaming
                    response = await openai_client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        stream=True,
                        **OPENAI_COMPLETION_OPTIONS,
                    )

                    answer = ""
                    async for chunk in response:
                        if chunk.choices[0].delta.content is not None:
                            answer += chunk.choices[0].delta.content
                            n_input_tokens, n_output_tokens = self._count_tokens_from_messages(messages, answer,
                                                                                               model=self.model)
                            n_first_dialog_messages_removed = n_dialog_messages_before - len(dialog_messages)
                            yield "not_finished", answer, (
                            n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

                answer = self._postprocess_answer(answer)

            except Exception as e:
                if len(dialog_messages) == 0:
                    raise e
                dialog_messages = dialog_messages[1:]

        yield "finished", answer, (n_input_tokens, n_output_tokens), n_first_dialog_messages_removed

    # Остальные методы класса (_generate_prompt, _encode_image, и т.д.) остаются без изменений
    def _generate_prompt(self, message, dialog_messages, chat_mode):
        prompt = config.chat_modes[chat_mode]["prompt_start"]
        prompt += "\n\n"

        if len(dialog_messages) > 0:
            prompt += "Chat:\n"
            for dialog_message in dialog_messages:
                prompt += f"User: {dialog_message['user']}\n"
                prompt += f"Assistant: {dialog_message['bot']}\n"

        prompt += f"User: {message}\n"
        prompt += "Assistant: "

        return prompt

    def _encode_image(self, image_buffer: BytesIO) -> bytes:
        return base64.b64encode(image_buffer.read()).decode("utf-8")

    def _generate_prompt_messages(self, message, dialog_messages, chat_mode, image_buffer: BytesIO = None):
        prompt = config.chat_modes[chat_mode]["prompt_start"]
        messages = [{"role": "system", "content": prompt}]

        for dialog_message in dialog_messages:
            messages.append({"role": "user", "content": dialog_message["user"]})
            messages.append({"role": "assistant", "content": dialog_message["bot"]})

        if image_buffer is not None:
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": message,
                        },
                        {
                            "type": "image",
                            "image": self._encode_image(image_buffer),
                        }
                    ]
                }
            )
        else:
            messages.append({"role": "user", "content": message})

        return messages

    def _generate_claude_prompt(self, message, dialog_messages, chat_mode, image_buffer: BytesIO = None):
        prompt = config.chat_modes[chat_mode]["prompt_start"]
        combined_prompt = prompt

        for dialog_message in dialog_messages:
            combined_prompt += f"\n\nHuman: {dialog_message['user']}\n\nAssistant: {dialog_message['bot']}"

        if image_buffer is not None:
            encoded_image = self._encode_image(image_buffer)
            combined_prompt += f"\n\nHuman: {message}\n\nAssistant: [IMAGE: {encoded_image}]"
        else:
            combined_prompt += f"\n\nHuman: {message}"

        combined_prompt += "\n\nAssistant:"
        return combined_prompt

    def _postprocess_answer(self, answer):
        self.logger.debug(f"Pre-processed answer: {answer}")
        answer = answer.strip()
        self.logger.debug(f"Post-processed answer: {answer}")
        return answer

    def _count_tokens_from_messages(self, messages, answer, model="gpt-4-1106-preview"):
        if model.startswith("claude"):
            encoding = tiktoken.encoding_for_model("gpt-4o")
        else:
            encoding = tiktoken.encoding_for_model(model)

        tokens_per_message = 3
        tokens_per_name = 1

        if model.startswith("gpt-3"):
            tokens_per_message = 4
            tokens_per_name = -1
        elif model.startswith("gpt-4"):
            tokens_per_message = 3
            tokens_per_name = 1
        elif model.startswith("claude"):
            tokens_per_message = 3
            tokens_per_name = 1
        else:
            raise ValueError(f"Unknown model: {model}")

        n_input_tokens = 0
        for message in messages:
            n_input_tokens += tokens_per_message
            if isinstance(message["content"], list):
                for sub_message in message["content"]:
                    if "type" in sub_message:
                        if sub_message["type"] == "text":
                            n_input_tokens += len(encoding.encode(sub_message["text"]))
                        elif sub_message["type"] == "image_url":
                            pass
            else:
                if "type" in message:
                    if message["type"] == "text":
                        n_input_tokens += len(encoding.encode(message["text"]))
                    elif message["type"] == "image_url":
                        pass

        n_input_tokens += 2
        n_output_tokens = 1 + len(encoding.encode(answer))

        return n_input_tokens, n_output_tokens

    def _count_tokens_from_prompt(self, prompt, answer, model="text-davinci-003"):
        if model.startswith("claude"):
            encoding = tiktoken.encoding_for_model("gpt-4o")
        else:
            encoding = tiktoken.encoding_for_model(model)

        n_input_tokens = len(encoding.encode(prompt)) + 1
        n_output_tokens = len(encoding.encode(answer))

        return n_input_tokens, n_output_tokens


async def transcribe_audio(audio_file) -> str:
    try:
        # НОВЫЙ API для транскрипции
        transcript = await openai_client.audio.transcriptions.create(
            file=audio_file,
            model="whisper-1"
        )
        return transcript.text or ""
    except Exception as e:
        logger.error(f"Error transcribing audio: {e}")
        return ""


async def generate_images(prompt, model="dall-e-2", n_images=4, size="1024x1024", quality="standard"):
    """Generate images using OpenAI's specified model."""
    if model == "dalle-2":
        model = "dall-e-2"
        quality = "standard"

    if model == "dalle-3":
        model = "dall-e-3"
        n_images = 1

    try:
        # НОВЫЙ API для генерации изображений
        response = await openai_client.images.generate(
            model=model,
            prompt=prompt,
            n=n_images,
            size=size,
            quality=quality
        )

        image_urls = [item.url for item in response.data]
        return image_urls

    except Exception as e:
        logger.error(f"Error generating images: {e}")
        raise e


async def is_content_acceptable(prompt):
    try:
        # НОВЫЙ API для модерации
        response = await openai_client.moderations.create(input=prompt)
        return not all(response.results[0].categories.values())
    except Exception as e:
        logger.error(f"Error in content moderation: {e}")
        return True


async def edit_image(image: BytesIO, prompt: str, size: str = "1024x1024",
                     model: str = "dall-e-2") -> Optional[str]:
    """
    Редактирует изображение с помощью DALL-E с использованием маски.
    """
    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            if model == "dall-e-3":
                logger.warning("DALL-E 3 doesn't support image editing yet, using DALL-E 2")
                model = "dall-e-2"

            # Конвертируем изображение в PNG с RGBA форматом
            png_buffer = await _convert_image_to_png(image)

            # Создаем маску для редактирования
            mask_buffer = await _create_edit_mask(png_buffer)

            logger.info(f"Attempt {attempt + 1}/{max_retries}: Sending image edit request to OpenAI")

            # Подготавливаем файлы для загрузки
            png_buffer.seek(0)
            mask_buffer.seek(0)

            # НОВЫЙ API для редактирования изображений
            response = await openai_client.images.edit(
                model=model,
                image=png_buffer,
                mask=mask_buffer,
                prompt=prompt,
                size=size,
                n=1
            )

            logger.info("Image editing successful")
            return response.data[0].url

        except Exception as e:
            logger.error(f"Error in photo editing (attempt {attempt + 1}): {e}")

            if attempt < max_retries - 1:
                logger.info(f"Retrying in {retry_delay} seconds...")
                await asyncio.sleep(retry_delay)
                retry_delay *= 2
            else:
                logger.error(f"All attempts failed: {e}")
                return None

    return None


async def _convert_image_to_png(image_buffer: BytesIO) -> BytesIO:
    """Конвертирует изображение в PNG формат с RGBA каналами."""
    try:
        image_buffer.seek(0)
        image = Image.open(image_buffer)

        logger.info(f"Original image: size={image.size}, mode={image.mode}")

        if image.mode != 'RGBA':
            image = image.convert('RGBA')

        max_size = (1024, 1024)
        if image.size[0] > max_size[0] or image.size[1] > max_size[1]:
            image.thumbnail(max_size, Image.Resampling.LANCZOS)

        png_buffer = BytesIO()
        image.save(png_buffer, format='PNG', optimize=True)
        png_buffer.seek(0)

        logger.info(f"Image converted to PNG: {image.size}, size: {len(png_buffer.getvalue())} bytes")
        return png_buffer

    except Exception as e:
        logger.error(f"Error converting image to PNG: {e}")
        raise ValueError(f"Не удалось обработать изображение: {str(e)}")


async def _create_edit_mask(original_image_buffer: BytesIO) -> BytesIO:
    """Создает маску для редактирования изображения."""
    try:
        original_image_buffer.seek(0)
        image = Image.open(original_image_buffer)

        mask = Image.new('RGBA', image.size, (0, 0, 0, 0))
        mask_buffer = BytesIO()
        mask.save(mask_buffer, format='PNG', optimize=True)
        mask_buffer.seek(0)

        logger.info(f"Created edit mask: size={mask.size}")
        return mask_buffer

    except Exception as e:
        logger.error(f"Error creating edit mask: {e}")
        simple_mask = Image.new('RGBA', (1024, 1024), (0, 0, 0, 0))
        mask_buffer = BytesIO()
        simple_mask.save(mask_buffer, format='PNG')
        mask_buffer.seek(0)
        return mask_buffer


async def create_image_variation(image: BytesIO, size: str = "1024x1024",
                                 n: int = 1, model: str = "dall-e-2") -> List[str]:
    """
    Создает вариации изображения.
    """
    try:
        # НОВЫЙ API для вариаций изображений
        response = await openai_client.images.create_variation(
            image=image,
            size=size,
            n=n,
            model=model
        )

        return [item.url for item in response.data]

    except Exception as e:
        logger.error(f"Error creating image variations: {e}")
        raise e