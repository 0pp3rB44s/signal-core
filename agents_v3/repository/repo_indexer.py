from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


IGNORE_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache",
    "node_modules", ".claude", "backups"
}


@dataclass
class RepoIndex:
    root: str
    python_file_count: int
    test_file_count: int
    python_files: list[str]
    test_files: list[str]
    production_files: list[str]
    critical_files: list[str]
    risk_files: list[str]


def should_ignore(path: Path) -> bool:
    return any(part in IGNORE_DIRS for part in path.parts)


def build_repo_index(root: str = ".") -> RepoIndex:
    base = Path(root).resolve()

    python_files = sorted(
        str(p.relative_to(base))
        for p in base.rglob("*.py")
        if not should_ignore(p.relative_to(base))
    )

    test_files = [
        f for f in python_files
        if f.startswith("tests/") or "/tests/" in f or Path(f).name.startswith("test_")
    ]

    production_files = [
        f for f in python_files
        if f not in test_files
    ]

    critical_keywords = (
        "position_manager",
        "trade_planner",
        "adaptive_tp",
        "risk_manager",
        "execution_service",
    )

    critical_files = [
        f for f in production_files
        if any(k in Path(f).name for k in critical_keywords)
    ]

    risk_files = [
        f for f in production_files
        if Path(f).name in {"config.py", "settings.py"} or "risk" in f.lower()
    ]

    return RepoIndex(
        root=str(base),
        python_file_count=len(python_files),
        test_file_count=len(test_files),
        python_files=python_files,
        test_files=test_files,
        production_files=production_files,
        critical_files=critical_files,
        risk_files=risk_files,
    )
