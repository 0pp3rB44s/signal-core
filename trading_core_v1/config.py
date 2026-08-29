"""Loads config/trading_core_v1.yaml (parameters) and, only if a value is
actually needed, reuses the existing `.env.live` file for secrets -- via the
same file this repo already treats as the sole source of credentials. This
module never creates a second secrets file and never prints secret values.

Trading Core V1's own YAML config carries PARAMETERS ONLY (symbols, timeframe,
strategy params, risk %, fees, slippage, initial equity, execution mode) --
never credentials.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "trading_core_v1.yaml"
ENV_LIVE_PATH = REPO_ROOT / ".env.live"


def load_env_live(env_path: Path = ENV_LIVE_PATH) -> dict[str, str]:
    """Parse `.env.live` KEY=VALUE lines into a dict, WITHOUT printing or
    logging any value. Returns {} if the file does not exist (this module must
    keep working -- e.g. for a pure historical backtest -- even with no
    .env.live present, since no secret is required to fetch public candle
    data). Mirrors the parsing semantics of scripts/lib/env_guard.sh's
    `guard_load_env` (simple KEY=VALUE, `#` comments, blank lines skipped)
    without shelling out or mutating this process's real environment unless
    the caller explicitly asks via `apply_to_environ=True`.
    """
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def apply_env_live_to_environ(env_path: Path = ENV_LIVE_PATH) -> None:
    """Populate os.environ from .env.live for keys not already set. Used only
    for optional lifecycle features (e.g. Telegram) that read directly from
    the environment; never required for the backtester to run."""
    for key, value in load_env_live(env_path).items():
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class TradingCoreV1Config:
    raw: dict[str, Any]

    @property
    def symbol(self) -> str:
        return self.raw["data"]["symbol"]

    @property
    def timeframe(self) -> str:
        return self.raw["data"]["timeframe"]

    @property
    def product_type(self) -> str:
        return self.raw["data"].get("product_type", "USDT-FUTURES")

    @property
    def start(self) -> str:
        return self.raw["data"]["start"]

    @property
    def end(self) -> str:
        return self.raw["data"]["end"]

    @property
    def cache_dir(self) -> Path:
        return REPO_ROOT / self.raw["data"].get("cache_dir", "data_store/trading_core_v1/candles_cache")

    @property
    def risk_pct(self) -> float:
        return float(self.raw["risk"]["risk_pct"])

    @property
    def initial_equity(self) -> float:
        return float(self.raw["risk"]["initial_equity"])

    @property
    def min_notional(self) -> float:
        return float(self.raw["risk"]["min_notional"])

    @property
    def compounding(self) -> bool:
        return bool(self.raw["risk"].get("compounding", False))

    @property
    def maker_fee_bps(self) -> float:
        return float(self.raw["costs"]["maker_fee_bps"])

    @property
    def taker_fee_bps(self) -> float:
        return float(self.raw["costs"]["taker_fee_bps"])

    @property
    def slippage_bps(self) -> float:
        return float(self.raw["costs"]["slippage_bps"])

    @property
    def take_profit_r_multiple(self) -> float:
        return float(self.raw["strategy"].get("take_profit_r_multiple", 1.5))

    @property
    def strategy_params(self) -> dict[str, Any]:
        return self.raw["strategy"].get("params", {})

    @property
    def execution_mode(self) -> str:
        return self.raw.get("execution", {}).get("mode", "BACKTEST")

    @property
    def split(self) -> dict[str, float]:
        s = self.raw.get("split", {"train": 1.0, "validation": 0.0, "test": 0.0})
        return {"train": float(s.get("train", 1.0)), "validation": float(s.get("validation", 0.0)), "test": float(s.get("test", 0.0))}

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.raw.get("telegram", {}).get("enabled", False))


def _coerce_scalar(text: str) -> Any:
    t = text.strip()
    if len(t) >= 2 and ((t[0] == t[-1] == '"') or (t[0] == t[-1] == "'")):
        return t[1:-1]
    low = t.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~", ""):
        return None
    try:
        if any(c in t for c in (".", "e", "E")) and t.replace(".", "", 1).replace("-", "", 1).replace("e", "", 1).replace("E", "", 1).replace("+", "", 1).isdigit():
            return float(t)
        return int(t)
    except ValueError:
        return t


def _parse_minimal_yaml(text: str) -> dict[str, Any]:
    """Tiny indentation-based parser for the flat/nested-scalar subset of YAML
    this project's own config file uses (no lists, no anchors, no multi-line
    strings). Avoids adding a PyYAML dependency to the production venv for one
    small, fully-owned config file. `#` starts a comment; blank lines skipped.
    """
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, _, value = line.strip().partition(":")
        key = key.strip()
        value = value.strip()

        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if value == "":
            new_dict: dict[str, Any] = {}
            parent[key] = new_dict
            stack.append((indent, new_dict))
        else:
            parent[key] = _coerce_scalar(value)
    return root


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> TradingCoreV1Config:
    raw = _parse_minimal_yaml(Path(path).read_text())
    return TradingCoreV1Config(raw=raw)
