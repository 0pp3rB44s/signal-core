"""Minimal Ollama client for local AI agents."""

from ollama import chat

MODEL = "qwen2.5-coder:14b"


def ask(prompt: str, system_prompt: str = "You are the CGC Audit AI.") -> str:
    response = chat(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
    )
    return response["message"]["content"]
