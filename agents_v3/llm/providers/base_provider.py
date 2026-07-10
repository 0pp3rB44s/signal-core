from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ProviderResponse:
    success: bool
    text: str
    provider: str
    model: str
    error: str | None = None


class BaseProvider:
    name: str = "base"

    def generate(self, model: str, prompt: str) -> ProviderResponse:
        raise NotImplementedError
