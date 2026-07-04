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
    }
    response = requests.post(OLLAMA_URL, json=data, timeout=180)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload.get("response"), str):
        return payload.get("response")
    else:
        return json.dumps(payload, ensure_ascii=False)