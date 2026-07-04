"""Shared helpers for loading files used by AI agents."""

from pathlib import Path
from typing import Iterable


def load_text_file(path: Path, max_chars: int = 4000) -> str:
    """Load the tail of a text file if it exists."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")[-max_chars:]


def load_existing_files(paths: Iterable[Path], max_chars: int = 4000) -> dict[str, str]:
    """Return a mapping of filename -> contents for all existing files."""
    loaded: dict[str, str] = {}
    for path in paths:
        text = load_text_file(path, max_chars=max_chars)
        if text:
            loaded[path.name] = text
    return loaded
