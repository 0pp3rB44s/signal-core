

"""Central access layer for the Learning Engine."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_PATH = Path(__file__).resolve().parents[2]
REPORTS_PATH = BASE_PATH / "agents_v2" / "reports"
LEARNING_PATH = REPORTS_PATH / "learning.json"
PATTERNS_PATH = REPORTS_PATH / "patterns.json"


class LearningService:
    def __init__(self) -> None:
        self.learning = self._load_json(LEARNING_PATH)
        self.patterns = self._load_json(PATTERNS_PATH)

    @staticmethod
    def _load_json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def reload(self) -> None:
        self.learning = self._load_json(LEARNING_PATH)
        self.patterns = self._load_json(PATTERNS_PATH)

    def get_learning(self) -> dict[str, Any]:
        return self.learning

    def get_patterns(self) -> dict[str, Any]:
        return self.patterns

    def get_summary(self) -> dict[str, Any]:
        return {
            "metadata": self.learning.get("metadata", {}),
            "best_strategy": self.patterns.get("best_strategy"),
            "worst_strategy": self.patterns.get("worst_strategy"),
            "best_symbol": self.patterns.get("best_symbol"),
            "worst_symbol": self.patterns.get("worst_symbol"),
            "diagnosis": self.patterns.get("diagnosis", {}),
        }


learning_service = LearningService()