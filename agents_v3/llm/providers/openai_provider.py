from __future__ import annotations

import os

from openai import OpenAI

from agents_v3.llm.providers.base_provider import BaseProvider, ProviderResponse


class OpenAIProvider(BaseProvider):
    name = "openai"

    def generate(self, model: str, prompt: str) -> ProviderResponse:
        api_key = os.getenv("OPENAI_API_KEY")

        if not api_key:
            return ProviderResponse(
                success=False,
                provider=self.name,
                model=model,
                text="",
                error="OPENAI_API_KEY missing. Set it in your shell or .env loader.",
            )

        try:
            client = OpenAI(api_key=api_key)

            response = client.responses.create(
                model=model,
                input=prompt,
            )

            return ProviderResponse(
                success=True,
                provider=self.name,
                model=model,
                text=response.output_text,
            )

        except Exception as exc:
            return ProviderResponse(
                success=False,
                provider=self.name,
                model=model,
                text="",
                error=f"OpenAI error: {exc}",
            )
