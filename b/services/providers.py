from openai import OpenAI

client = OpenAI()

class ProviderService:
    """
    Унифицированный провайдер для всех API: chat, image, vision, audio.
    """

    PROVIDER_MAP = {
        "openai": "openai",
        "oai": "openai",
        "default": "openai",
        # "google": "google",
        # "anthropic": "anthropic",
        # "mistral": "mistral",
    }

    @staticmethod
    def get_provider(name: str | None):
        if not name:
            return "openai"
        return ProviderService.PROVIDER_MAP.get(name.lower(), "openai")

    @staticmethod
    def responses(provider: str, **kwargs):
        provider_key = ProviderService.get_provider(provider)
        return client.responses.create(provider=provider_key, **kwargs)

    @staticmethod
    def images(provider: str, **kwargs):
        provider_key = ProviderService.get_provider(provider)
        return client.images.generate(provider=provider_key, **kwargs)
