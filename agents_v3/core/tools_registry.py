from __future__ import annotations

import re
import subprocess
from pathlib import Path

from agents_v3.tools.test_runner import run_tests
from agents_v3.tools.trade_analyzer import analyze_trades, format_report


MAX_TOOL_OUTPUT_CHARS = 3500
SEARCH_DIRS = ("app", "clients", "data", "execution", "market_data", "planning", "risk", "strategies", "telemetry", "tests", "agents_v3", "docs")


def _blocked_path(path: str) -> str | None:
    # Resolve first so tricks like "configs/../.env" are judged by the
    # real target, then check the final filename.
    resolved = (Path.cwd() / path.strip()).resolve()
    if not str(resolved).startswith(str(Path.cwd().resolve())):
        return "Path escapes the repository."
    name = resolved.name
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        return "Access to .env files is forbidden."
    return None


def _truncate(text: str) -> str:
    if len(text) <= MAX_TOOL_OUTPUT_CHARS:
        return text
    return text[:MAX_TOOL_OUTPUT_CHARS] + "\n[output truncated]"


def tool_read_file(path: str = "", start_line: int = 1, max_lines: int = 120) -> str:
    if not path:
        return "ERROR: path argument is required."
    blocked = _blocked_path(path)
    if blocked:
        return f"ERROR: {blocked}"
    file = Path(path)
    if not file.exists() or not file.is_file():
        return f"ERROR: file not found: {path}"
    lines = file.read_text(errors="replace").splitlines()
    start = max(1, int(start_line))
    end = min(len(lines), start + max(1, int(max_lines)) - 1)
    body = "\n".join(f"{i}: {lines[i - 1]}" for i in range(start, end + 1))
    return _truncate(f"{path} lines {start}-{end} of {len(lines)}:\n{body}")


def tool_search_code(pattern: str = "", directory: str = "") -> str:
    if not pattern:
        return "ERROR: pattern argument is required."
    try:
        re.compile(pattern)
    except re.error:
        pattern = re.escape(pattern)
    dirs = [directory] if directory else list(SEARCH_DIRS)
    for d in dirs:
        blocked = _blocked_path(d)
        if blocked:
            return f"ERROR: {blocked}"
    existing = [d for d in dirs if Path(d).exists()]
    if not existing:
        return f"ERROR: directory not found: {directory}"
    completed = subprocess.run(
        ["grep", "-rn", "-E", pattern, "--include=*.py", "--include=*.md", *existing],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = completed.stdout.strip() or "(no matches)"
    return _truncate(output)


def tool_list_files(directory: str = ".") -> str:
    blocked = _blocked_path(directory)
    if blocked:
        return f"ERROR: {blocked}"
    base = Path(directory)
    if not base.exists():
        return f"ERROR: directory not found: {directory}"
    entries = sorted(
        p.name + ("/" if p.is_dir() else "")
        for p in base.iterdir()
        if not p.name.startswith(".") and p.name != "__pycache__"
    )
    return _truncate("\n".join(entries) or "(empty)")


def tool_trade_stats(days: int = 14) -> str:
    analysis = analyze_trades(days=int(days))
    return _truncate(format_report(analysis))


def tool_run_tests(targets: str = "") -> str:
    target_list = [t for t in targets.split() if t] or None
    result = run_tests(target_list)
    status = "PASSED" if result.success else "FAILED"
    return _truncate(f"{status} (rc={result.return_code})\n{result.output[-2500:]}")


def tool_git_diff() -> str:
    completed = subprocess.run(
        ["git", "diff", "--stat"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return _truncate(completed.stdout.strip() or "(no changes)")


TOOLS = {
    "read_file": {
        "fn": tool_read_file,
        "description": "Read part of a repository file. Args: path (str), start_line (int, default 1), max_lines (int, default 120).",
    },
    "search_code": {
        "fn": tool_search_code,
        "description": "Search the repository with a regex. Args: pattern (str), directory (str, optional).",
    },
    "list_files": {
        "fn": tool_list_files,
        "description": "List files in a directory. Args: directory (str, default '.').",
    },
    "trade_stats": {
        "fn": tool_trade_stats,
        "description": "Get live trading performance stats (pnl, winrate, fees, per strategy/direction/duration). Args: days (int, default 14).",
    },
    "run_tests": {
        "fn": tool_run_tests,
        "description": "Run pytest. Args: targets (str, space-separated test paths, empty = full suite).",
    },
    "git_diff": {
        "fn": tool_git_diff,
        "description": "Show current uncommitted changes (stat). No args.",
    },
}


def describe_tools() -> str:
    return "\n".join(f"- {name}: {spec['description']}" for name, spec in TOOLS.items())


def execute_tool(name: str, args: dict) -> str:
    spec = TOOLS.get(name)
    if spec is None:
        return f"ERROR: unknown tool '{name}'. Available: {', '.join(TOOLS)}"
    try:
        return spec["fn"](**{k: v for k, v in (args or {}).items()})
    except TypeError as exc:
        return f"ERROR: bad arguments for {name}: {exc}"
    except Exception as exc:
        return f"ERROR: tool {name} failed: {exc}"
