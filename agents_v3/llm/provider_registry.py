from __future__ import annotations

from agents_v3.llm.providers.base_provider import BaseProvider
from agents_v3.llm.providers.ollama_provider import OllamaProvider


class ProviderRegistry:
    def __init__(self) -> None:
        self.providers: dict[str, BaseProvider] = {
            "ollama": OllamaProvider(),
        }

    def get(self, provider_name: str) -> BaseProvider:
        if provider_name == "openai" and provider_name not in self.providers:
            # Lazy import: the openai package is optional and only needed
            # when the openai provider is actually requested.
            try:
                from agents_v3.llm.providers.openai_provider import OpenAIProvider
            except ImportError as exc:
                raise ValueError(
                    "OpenAI provider requested but the 'openai' package is not installed."
                ) from exc
            self.providers["openai"] = OpenAIProvider()

        if provider_name not in self.providers:
            raise ValueError(f"Unknown provider: {provider_name}")
        return self.providers[provider_name]
