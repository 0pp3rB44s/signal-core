from __future__ import annotations

import os
from dataclasses import dataclass


# Patch-producing work goes to the strongest local model; read-only
# analysis uses the faster one. Both are overridable via environment.
# NOTE: qwen2.5-coder:32b (19GB) does not fit in this machine's 16GB RAM
# and times out from swapping; set CGC_STRONG_MODEL=qwen2.5-coder:32b
# only on hardware with 24GB+.
FAST_CODE_MODEL = os.getenv("CGC_FAST_MODEL", "qwen2.5-coder:14b")
STRONG_CODE_MODEL = os.getenv("CGC_STRONG_MODEL", "qwen2.5-coder:14b")

PATCH_MODES = {"propose", "do", "patch", "auto"}


@dataclass
class ModelDecision:
    provider: str
    model: str
    reason: str


def choose_model(mode: str, task: str, risk_level: str) -> ModelDecision:
    if mode in PATCH_MODES or risk_level == "HIGH":
        return ModelDecision(
            provider="ollama",
            model=STRONG_CODE_MODEL,
            reason=f"Strong local model for patch-producing or high-risk work (mode={mode}, risk={risk_level}).",
        )
    return ModelDecision(
        provider="ollama",
        model=FAST_CODE_MODEL,
        reason=f"Fast local model for read-only analysis (mode={mode}, risk={risk_level}).",
    )
