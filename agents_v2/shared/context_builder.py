"""Build a reusable context package for AI agents."""

from pathlib import Path

from agents_v2.shared.file_loader import load_existing_files


def _load_csv_head_and_tail(paths: list[Path], tail_rows: int = 40) -> dict[str, str]:
    """CSV context as header + most recent rows.

    Dumping whole datasets blew the prompt past the local model's context
    window (the model then answered with refusals); the header plus a recent
    tail is what the audit actually needs.
    """
    loaded: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        header, rows = lines[0], lines[1:]
        loaded[path.name] = "\n".join([header] + rows[-tail_rows:])
    return loaded

LOG_FILES = [
    Path("logs/runtime.log"),
    Path("logs/agent.log"),
    Path("logs/night_runner.log"),
    Path("logs/morning_runner.log"),
]

ROADMAP_FILES = [
    Path("ROADMAP.md"),
    Path("CGC_MASTER_JOURNAL_V5.md"),
    Path("state/live_trade_journal.json"),
]

CODE_FILES = [
    Path("planning/trade_planner.py"),
    Path("risk/risk_manager.py"),
    Path("execution/position_manager.py"),
]

DATASET_FILES = [
    Path("logs/trade_dataset_v2.csv"),
    Path("logs/trade_dataset.csv"),
]

SETTINGS_FILES = [
    Path("app/config.py"),
    Path("config/runtime.yaml"),
    Path("config/settings.yaml"),
]


def build_context() -> dict[str, dict[str, str]]:
    """Collect all available context for AI agents."""
    return {
        "logs": load_existing_files(LOG_FILES, max_chars=4000),
        "roadmap": load_existing_files(ROADMAP_FILES, max_chars=3000),
        "code": load_existing_files(CODE_FILES, max_chars=3500),
        "dataset": _load_csv_head_and_tail(DATASET_FILES, tail_rows=40),
        "settings": load_existing_files(SETTINGS_FILES, max_chars=3000),
    }
