from __future__ import annotations

from pathlib import Path


def test_removed_event_logger_has_no_runtime_reference():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "telemetry" / "event_logger.py").exists()

    references: list[str] = []
    for source in root.rglob("*.py"):
        if any(part in {".venv", ".git", "__pycache__"} for part in source.parts):
            continue
        if source == Path(__file__):
            continue
        text = source.read_text(encoding="utf-8", errors="ignore")
        if "telemetry.event_logger" in text or "telemetry/event_logger.py" in text:
            references.append(str(source.relative_to(root)))

    assert references == []
