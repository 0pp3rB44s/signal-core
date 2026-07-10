from __future__ import annotations

from dataclasses import dataclass

from agents_v3.llm.provider_registry import ProviderRegistry


@dataclass
class LLMResponse:
    success: bool
    text: str
    provider: str
    model: str
    error: str | None = None


def ask_model(provider: str, model: str, prompt: str) -> LLMResponse:
    try:
        registry = ProviderRegistry()
        selected_provider = registry.get(provider)
        response = selected_provider.generate(model=model, prompt=prompt)

        return LLMResponse(
            success=response.success,
            text=response.text,
            provider=response.provider,
            model=response.model,
            error=response.error,
        )

    except Exception as exc:
        return LLMResponse(
            success=False,
            text="",
            provider=provider,
            model=model,
            error=f"LLM client error: {exc}",
        )
