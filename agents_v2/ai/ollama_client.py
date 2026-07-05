"""Minimal Ollama client for CGC Audit Engine V2."""

import requests
import json

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen2.5-coder:14b"

def ask(prompt: str, model: str = DEFAULT_MODEL) -> str:
    """Send a prompt to Ollama and return the response string."""
    data = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {
            # Ollama's default context window (often 4096 tokens) silently
            # truncates the audit prompt, which made the model refuse. The
            # full audit context is ~10-15K tokens.
            "num_ctx": 16384,
            "temperature": 0.2,
        },
    }
    response = requests.post(OLLAMA_URL, json=data, timeout=300)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload.get("response"), str):
        return payload.get("response")
    else:
        return json.dumps(payload, ensure_ascii=False)