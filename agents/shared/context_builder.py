"""Build a reusable context package for AI agents."""

from pathlib import Path

from agents.shared.file_loader import load_existing_files

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
        # The trade dataset must include its CSV header for parsing.
        # Use a much larger limit so the header is preserved instead of
        # truncating into the middle of the file.
        "dataset": load_existing_files(DATASET_FILES, max_chars=250000),
        "settings": load_existing_files(SETTINGS_FILES, max_chars=3000),
    }