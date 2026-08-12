#!/usr/bin/env python3
"""Owner-authorized, Runner-local management of non-secret `.env.live` keys.

The tool never emits values from secret keys and never transports the file.
It must be executed on the authoritative Runner from its authoritative checkout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


AUTHORITATIVE_RUNNER_HOST = "MacBook-Air-van-Bryon.local"
AUTHORITATIVE_RUNNER_REPO = Path("/Users/bryonkolkman/cgc/bitget_ai_agent_phase7")
AUTHORITATIVE_ENV = AUTHORITATIVE_RUNNER_REPO / ".env.live"
BACKUP_DIR = AUTHORITATIVE_RUNNER_REPO / "backups" / "env-live"
AUTHORIZATION_ENV = "CGC_OWNER_AUTHORIZED_ENV_LIVE"

SECRET_KEYS = frozenset({
    "BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE",
    "DASHBOARD_PASSWORD", "DASHBOARD_SECRET_KEY", "TELEGRAM_BOT_TOKEN",
    "DISCORD_WEBHOOK_URL",
})
SECRET_KEY_MARKERS = ("API_KEY", "SECRET", "PASSPHRASE", "PASSWORD", "TOKEN", "WEBHOOK", "CREDENTIAL")

# Deliberately explicit: unknown keys cannot be mutated merely because they look
# harmless. Extend this list only through review when production adds a setting.
MUTABLE_NON_SECRET_KEYS = frozenset({
    "APP_ENV", "APP_MODE", "EXECUTION_ENABLED", "EXECUTION_MODE",
    "FORWARD_PAPER_ONLY", "EXECUTION_REQUIRE_CONFIRMATION",
    "EXECUTION_MARGIN_MODE", "EXECUTION_MAX_PER_CYCLE", "EXECUTION_PLAN_LIMIT",
    "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT", "EXECUTION_MIN_LIVE_NOTIONAL_USDT",
    "STRATEGY_ISOLATION_ENABLED", "ENABLED_STRATEGIES", "DISABLED_STRATEGIES",
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED", "PRODUCTION_SYMBOL_ALLOWLIST",
    "ALLOW_AUTO_WATCHLIST_REFRESH", "MAX_SYMBOLS", "DEFAULT_LEVERAGE",
    "MAX_LEVERAGE", "MAX_OPEN_POSITIONS", "MAX_CORRELATED_POSITIONS",
    "MAX_TOTAL_EXPOSURE_PCT", "MAX_CLUSTER_EXPOSURE_PCT",
    "ACCOUNT_RISK_PER_TRADE_PCT", "MAX_DAILY_LOSS_PCT", "HARD_DAILY_STOP_PCT",
    "WEEKLY_FREEZE_LOSS_PCT", "PLANNER_MAX_NOTIONAL_PCT_OF_EQUITY",
    "PLANNER_MAX_NOTIONAL_PER_TRADE_USDT", "PLANNER_MIN_LIVE_NOTIONAL_USDT",
    "EXECUTOR_ID", "HOST_ID", "DYNAMIC_GRID_ENABLED", "DYNAMIC_GRID_MODE",
    "MAKER_ENTRY_ENABLED", "MAKER_ENTRY_FALLBACK_MARKET", "MAKER_ENTRY_WAIT_SECONDS",
    "MAKER_ENTRY_POLL_SECONDS", "MAKER_ENTRY_OFFSET_BPS",
    "MICROFLOW_SCALPER_ENABLED", "MICROFLOW_SYMBOLS", "MICROFLOW_LEVERAGE",
    "MICROFLOW_MAX_SLIPPAGE_BPS", "MICROFLOW_DATA_DIR",
})

# Reviewed additive keys introduced after existing Runner env files were
# created. Only these non-secret keys may be appended by the controlled helper.
ADDITIVE_NON_SECRET_KEYS = frozenset({
    "MICROFLOW_SCALPER_ENABLED", "MICROFLOW_SYMBOLS", "MICROFLOW_LEVERAGE",
    "MICROFLOW_MAX_SLIPPAGE_BPS", "MICROFLOW_DATA_DIR",
})

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.,:/@+%-]*$")


class EnvLivePolicyError(RuntimeError):
    pass


def _is_secret_key(key: str) -> bool:
    normalized = str(key).strip().upper()
    return normalized in SECRET_KEYS or any(marker in normalized for marker in SECRET_KEY_MARKERS)


def _require_authorized_runner(*, env_path: Path, repo_path: Path, hostname: str | None = None) -> None:
    if os.environ.get(AUTHORIZATION_ENV) != "true":
        raise EnvLivePolicyError("current task authorization is absent")
    actual_host = hostname or socket.gethostname()
    if actual_host != AUTHORITATIVE_RUNNER_HOST:
        raise EnvLivePolicyError("host is not the authoritative Runner")
    if repo_path.resolve() != AUTHORITATIVE_RUNNER_REPO:
        raise EnvLivePolicyError("checkout is not the authoritative Runner repository")
    if env_path.is_symlink():
        raise EnvLivePolicyError("authoritative Runner .env.live may not be a symbolic link")
    if env_path.resolve() != AUTHORITATIVE_ENV:
        raise EnvLivePolicyError("target is not the authoritative Runner .env.live")
    if repo_path != env_path.parent:
        raise EnvLivePolicyError(".env.live must be at the authoritative repository root")


def _parse(lines: list[str]) -> tuple[dict[str, str], dict[str, int]]:
    values: dict[str, str] = {}
    indexes: dict[str, int] = {}
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if not KEY_RE.fullmatch(key):
            raise EnvLivePolicyError(f"invalid environment key at line {index + 1}")
        if key in values:
            raise EnvLivePolicyError(f"duplicate environment key: {key}")
        values[key], indexes[key] = value, index
    return values, indexes


def inspect_redacted(env_path: Path) -> dict:
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    values, _ = _parse(lines)
    return {
        "target": ".env.live",
        "credential_presence": {
            key: "PRESENT" if values.get(key, "").strip().strip("'\"") else "ABSENT"
            for key in sorted(key for key in values if _is_secret_key(key))
        },
        "non_secret": {
            key: values[key].strip().strip("'\"")
            for key in sorted(values.keys() & MUTABLE_NON_SECRET_KEYS)
        },
        "secret_values_redacted": True,
    }


def _validate_updates(updates: dict[str, str]) -> dict[str, str]:
    if not updates:
        raise EnvLivePolicyError("at least one update is required")
    normalized: dict[str, str] = {}
    for key, value in updates.items():
        key = str(key).strip().upper()
        if _is_secret_key(key):
            raise EnvLivePolicyError(f"credential mutation forbidden: {key}")
        if key not in MUTABLE_NON_SECRET_KEYS:
            raise EnvLivePolicyError(f"key is not an approved non-secret setting: {key}")
        value = str(value)
        if not SAFE_VALUE_RE.fullmatch(value):
            raise EnvLivePolicyError(f"unsafe value for {key}")
        normalized[key] = value
    return normalized


def apply_updates(env_path: Path, updates: dict[str, str], *, backup_dir: Path = BACKUP_DIR) -> dict:
    updates = _validate_updates(updates)
    original_mode = stat.S_IMODE(env_path.stat().st_mode)
    if original_mode & 0o077:
        raise EnvLivePolicyError("authoritative Runner .env.live permissions expose secrets")
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
    before, indexes = _parse(lines)
    missing = sorted(set(updates) - set(indexes))
    forbidden_missing = sorted(set(missing) - ADDITIVE_NON_SECRET_KEYS)
    if forbidden_missing:
        raise EnvLivePolicyError("refusing to add unknown/missing key(s): " + ",".join(missing))

    if backup_dir.is_symlink():
        raise EnvLivePolicyError("backup directory may not be a symbolic link")
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not backup_dir.resolve().is_relative_to(AUTHORITATIVE_RUNNER_REPO):
        raise EnvLivePolicyError("backup directory must remain inside the Runner-local ignored tree")
    os.chmod(backup_dir, 0o700)

    for key, value in updates.items():
        if key in indexes:
            newline = "\n" if lines[indexes[key]].endswith("\n") else ""
            lines[indexes[key]] = f"{key}={value}{newline}"
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}={value}\n")

    proposed, _ = _parse(lines)
    secret_keys = {key for key in before if _is_secret_key(key)}
    for key in secret_keys:
        if before.get(key) != proposed.get(key):
            raise EnvLivePolicyError(f"credential would change unexpectedly: {key}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = backup_dir / f"env.live.{stamp}.backup"
    with env_path.open("rb") as source, backup.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    os.chmod(backup, 0o600)

    fd, temporary_name = tempfile.mkstemp(prefix=".env.live.", suffix=".tmp", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, original_mode)
        os.replace(temporary_name, env_path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise

    after, _ = _parse(env_path.read_text(encoding="utf-8").splitlines(keepends=True))
    for key in secret_keys:
        if before.get(key) != after.get(key):
            raise EnvLivePolicyError(f"credential changed unexpectedly: {key}")
    return {
        "backup_created": True,
        "backup_location": "RUNNER_LOCAL_IGNORED_BACKUP",
        "changed_non_secret_keys": {
            key: {"before": before.get(key, "<ABSENT>").strip().strip("'\""), "after": after[key].strip().strip("'\"")}
            for key in sorted(updates) if before.get(key) != after[key]
        },
        "credential_values_preserved": True,
        "secret_values_redacted": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Controlled authoritative Runner .env.live management")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()
    try:
        _require_authorized_runner(env_path=AUTHORITATIVE_ENV, repo_path=AUTHORITATIVE_RUNNER_REPO)
        if args.inspect and args.set:
            raise EnvLivePolicyError("choose either --inspect or --set")
        if args.inspect:
            result = inspect_redacted(AUTHORITATIVE_ENV)
        elif args.set:
            updates = dict(item.split("=", 1) for item in args.set if "=" in item)
            if len(updates) != len(args.set):
                raise EnvLivePolicyError("updates must use KEY=VALUE")
            result = apply_updates(AUTHORITATIVE_ENV, updates)
        else:
            raise EnvLivePolicyError("choose --inspect or --set")
    except (EnvLivePolicyError, OSError, ValueError) as exc:
        print(f"CONTROLLED_ENV_LIVE=BLOCKED reason={exc}", file=sys.stderr)
        return 90
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
