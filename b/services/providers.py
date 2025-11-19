# services/providers.py
from openai import AsyncOpenAI, OpenAI

class ProviderService:
    _sync = OpenAI()
    _async = AsyncOpenAI()

    PROVIDER_MAP = {
        "openai": "openai",
        "default": "openai",
    }

    @staticmethod
    def get_provider(name):
        if not name:
            return "openai"
        return ProviderService.PROVIDER_MAP.get(name.lower(), "openai")

    # --- SYNC ---
    @staticmethod
    def responses(provider: str, **kwargs):
        provider_key = ProviderService.get_provider(provider)
        return ProviderService._sync.responses.create(provider=provider_key, **kwargs)

    @staticmethod
    def images(provider: str, **kwargs):
        provider_key = ProviderService.get_provider(provider)
        return ProviderService._sync.images.generate(provider=provider_key, **kwargs)

    # --- ASYNC ---
    @staticmethod
    async def responses_async(provider: str, **kwargs):
        provider_key = ProviderService.get_provider(provider)
        return await ProviderService._async.responses.create(provider=provider_key, **kwargs)

    @staticmethod
    async def images_async(provider: str, **kwargs):
        provider_key = ProviderService.get_provider(provider)
        return await ProviderService._async.images.generate(provider=provider_key, **kwargs)
