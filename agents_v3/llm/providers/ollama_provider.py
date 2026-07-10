from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from agents_v3.llm.providers.base_provider import BaseProvider, ProviderResponse


OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
# Cold model load + a large context takes minutes on this machine.
OLLAMA_TIMEOUT_SECONDS = int(os.getenv("CGC_OLLAMA_TIMEOUT_SECONDS", "600"))
# 12288 tokens fits the 24k-char prompt budget plus response headroom while
# keeping the 14b model's KV cache small enough for 16GB RAM.
OLLAMA_NUM_CTX = int(os.getenv("CGC_OLLAMA_NUM_CTX", "12288"))


class OllamaProvider(BaseProvider):
    name = "ollama"

    def generate(self, model: str, prompt: str) -> ProviderResponse:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            # Ollama defaults to a 4096-token context; the prompt builder
            # budgets ~8k tokens of context, so request a larger window.
            "options": {"num_ctx": OLLAMA_NUM_CTX},
        }

        try:
            req = urllib.request.Request(
                OLLAMA_URL,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT_SECONDS) as response:
                data = json.loads(response.read().decode("utf-8"))

            return ProviderResponse(
                success=True,
                provider=self.name,
                model=model,
                text=data.get("response", ""),
            )

        except urllib.error.URLError as exc:
            return ProviderResponse(
                success=False,
                provider=self.name,
                model=model,
                text="",
                error=f"Ollama connection failed: {exc}",
            )
        except Exception as exc:
            return ProviderResponse(
                success=False,
                provider=self.name,
                model=model,
                text="",
                error=f"Ollama error: {exc}",
            )
