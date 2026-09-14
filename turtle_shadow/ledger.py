"""Two explicitly separate shadow ledgers for the Turtle prospective epoch.

REALISTIC ACCOUNT LEDGER: what would actually have executed against the real
~$27.44 NAV and real Bitget exchange constraints. A signal that cannot clear
the exchange minimum is recorded as SKIPPED, never as a fabricated fill.

NORMALIZED RESEARCH LEDGER: hypothetical R-multiple outcome for every
otherwise-valid signal (strategy gates passed), regardless of real-account
executability. This exists ONLY to accumulate prospective evidence about
whether the strategy has edge. It is NEVER representable as executable PnL,
and every row is tagged EXECUTABLE=NO to make that impossible to launder
downstream by dropping the tag.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RealisticLedgerRow:
    trade_id: str
    symbol: str
    signal_timestamp_ms: int
    entry_price: float
    initial_stop: float
    risk_usdt: float
    risk_sized_notional: float
    exchange_min_notional_or_min_order: float | None
    executed: bool
    skip_reason: str | None
    exit_timestamp_ms: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    realized_pnl_usdt: float | None = None
    result_r: float | None = None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ledger"] = "REALISTIC_ACCOUNT"
        return d


@dataclass(frozen=True, slots=True)
class NormalizedLedgerRow:
    trade_id: str
    symbol: str
    signal_timestamp_ms: int
    breakout_dist_atr: float
    entry_price: float
    initial_stop: float
    exit_timestamp_ms: int | None
    exit_price: float | None
    exit_reason: str | None
    result_r: float | None

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ledger"] = "NORMALIZED_RESEARCH"
        d["EXECUTABLE"] = "NO"  # explicit, redundant tag: never treat this row as real PnL
        return d


class TurtleShadowLedgers:
    """Append-only JSONL writer for both ledgers. Separate files, separate
    schemas, never merged -- the whole point is that a downstream reader
    cannot accidentally aggregate normalized R as if it were realized USDT."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self.realistic_path = os.path.join(state_dir, "realistic_account_ledger.jsonl")
        self.normalized_path = os.path.join(state_dir, "normalized_research_ledger.jsonl")

    def append_realistic(self, row: RealisticLedgerRow) -> None:
        with open(self.realistic_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row.as_dict(), default=str) + "\n")

    def append_normalized(self, row: NormalizedLedgerRow) -> None:
        with open(self.normalized_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row.as_dict(), default=str) + "\n")

    def read_realistic(self) -> list[dict[str, Any]]:
        return self._read(self.realistic_path)

    def read_normalized(self) -> list[dict[str, Any]]:
        return self._read(self.normalized_path)

    @staticmethod
    def _read(path: str) -> list[dict[str, Any]]:
        if not os.path.exists(path):
            return []
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out
