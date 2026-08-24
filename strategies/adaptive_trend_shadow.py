"""Shadow decision recording for adaptive_trend_tsmom_v1.

This module writes immutable JSONL rows and returns hypothetical lifecycle
state. It has NO access to any exchange client, order-submission function,
position store, or risk/exposure counter -- that is a structural guarantee,
not just a convention: nothing in this file imports `clients.*` or
`execution.*`, so a shadow decision cannot accidentally become a live one no
matter how this module is called.

A record is written for every evaluated signal, traded or not (spec section
12): real entries, ACCOUNT_FREEZE_BLOCKED candidates, and NO_SIGNAL bars are
all in-scope for the caller to log, though only actionable signals (LONG/
SHORT) produce a hypothetical lifecycle worth tracking forward.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from strategies.adaptive_trend_tsmom import STRATEGY_VERSION, Side, update_trailing_stop

DEFAULT_SHADOW_LOG_PATH = "data_store/adaptive_trend/shadow_decisions.jsonl"


@dataclass(slots=True)
class ShadowLifecycle:
    """A frozen-from-signal hypothetical position, evolved forward using the
    exact same entry/ATR-trailing rules as a real one -- so the resulting
    counterfactual is directly comparable to real forward trades, never
    mixed with them (spec section 19: shadow and real PnL stay separate)."""
    symbol: str
    side: str
    entry_price: float
    stop: float
    atr_at_entry: float
    signal_candle_close_ms: int
    status: str = "OPEN"          # OPEN | CLOSED
    exit_price: float | None = None
    exit_reason: str | None = None
    closed_at_close_ms: int | None = None

    def advance(self, *, candle_close_ms: int, close: float, high: float, low: float, atr: float) -> "ShadowLifecycle":
        if self.status != "OPEN":
            return self
        side = Side(self.side)
        stopped_out = (low <= self.stop) if side is Side.LONG else (high >= self.stop)
        if stopped_out:
            return ShadowLifecycle(
                symbol=self.symbol, side=self.side, entry_price=self.entry_price,
                stop=self.stop, atr_at_entry=self.atr_at_entry,
                signal_candle_close_ms=self.signal_candle_close_ms,
                status="CLOSED", exit_price=self.stop, exit_reason="trailing_stop",
                closed_at_close_ms=candle_close_ms,
            )
        new_stop = update_trailing_stop(self.stop, close, atr, side)
        return ShadowLifecycle(
            symbol=self.symbol, side=self.side, entry_price=self.entry_price,
            stop=new_stop, atr_at_entry=self.atr_at_entry,
            signal_candle_close_ms=self.signal_candle_close_ms,
            status="OPEN",
        )

    def hypothetical_pnl_pct(self, mark_price: float) -> float:
        side = Side(self.side)
        ref = self.exit_price if self.status == "CLOSED" else mark_price
        if self.entry_price == 0:
            return 0.0
        raw = (ref - self.entry_price) if side is Side.LONG else (self.entry_price - ref)
        return raw / self.entry_price


class ShadowDecisionLog:
    """Append-only JSONL writer. Never mutates or deletes a prior row --
    matching the project-wide "no hand-edited history" invariant."""

    def __init__(self, path: str | Path = DEFAULT_SHADOW_LOG_PATH):
        self.path = Path(path)

    def append(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True))
            fh.write("\n")

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rows.append(json.loads(line))
        return rows


def build_freeze_blocked_record(
    *, timestamp: str, symbol: str, side: str | None, six_h_close: float,
    mom: float | None, atr: float | None, mom_strength: float | None,
    entry_candidate: float | None, initial_stop: float | None,
    risk_pct: float | None, notional: float | None, code_sha: str = "",
) -> dict:
    """A frozen shadow decision record. `decision` is always
    ACCOUNT_FREEZE_BLOCKED here by construction -- this builder exists
    specifically for that path so it cannot be reused to accidentally log a
    real fill under a shadow label."""
    return dict(
        timestamp=timestamp, symbol=symbol, side=side, six_h_close=six_h_close,
        mom=mom, atr=atr, mom_strength=mom_strength, entry_candidate=entry_candidate,
        initial_stop=initial_stop, risk_pct=risk_pct, notional=notional,
        decision="ACCOUNT_FREEZE_BLOCKED", rejection_reason="weekly_freeze_active",
        strategy_version=STRATEGY_VERSION, code_sha=code_sha,
    )
