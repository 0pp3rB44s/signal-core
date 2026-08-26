"""AdaptiveTrend v1 — the only entry-enabled strategy, and the one the
dashboard could not previously see at all.

Before this panel, `KNOWN_STRATEGIES` listed five strategies from two
generations ago and the freshness registry tracked two MicroFlow status files
but not `data_store/adaptive_trend/shadow_decisions.jsonl`. An operator could
not answer "what is the strategy seeing right now" from any page.

Everything here is read from what the strategy itself writes. Nothing is
recomputed from candles: if the shadow log has not recorded a value, this panel
says UNKNOWN rather than deriving a number the engine never saw.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from dashboard_v3.core import sources
from dashboard_v3.core.status import Status

SHADOW_LOG = "data_store/adaptive_trend/shadow_decisions.jsonl"
SCAN_STATE = "state/adaptive_trend_scan_state.json"

STRATEGY_ID = "adaptive_trend_tsmom_v1"
TIMEFRAME_HOURS = 6
SYMBOL_UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

#: Frozen spec values, mirrored for display only. They are never used to
#: recompute a signal — showing the threshold next to the engine's own `mom`
#: is what makes "distance from threshold" meaningful.
MOM_LOOKBACK = 24
MOM_THRESHOLD = 0.03
ATR_PERIOD = 14
ATR_MULT = 2.5
MAX_OPEN_POSITIONS = 1

#: A shadow row older than this means the 6H scan is not running: one candle
#: plus a generous grace period for scan scheduling.
SHADOW_STALE_SECONDS = (TIMEFRAME_HOURS + 2) * 3600


def next_boundary(now: datetime | None = None) -> datetime:
    """Next 6H candle close, UTC. Boundaries are 00/06/12/18."""
    now = now or datetime.now(timezone.utc)
    floor = now.replace(minute=0, second=0, microsecond=0, hour=(now.hour // TIMEFRAME_HOURS) * TIMEFRAME_HOURS)
    return floor + timedelta(hours=TIMEFRAME_HOURS)


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class SymbolView:
    symbol: str
    side: str | None
    six_h_close: float | None
    mom: float | None
    mom_strength: float | None
    atr: float | None
    entry_candidate: float | None
    initial_stop: float | None
    notional: float | None
    risk_pct: float | None
    decision: str | None
    rejection_reason: str | None
    timestamp: datetime | None
    code_sha: str | None

    @property
    def actionable(self) -> bool | None:
        """Whether |mom| clears the frozen threshold. UNKNOWN when mom is absent."""
        if self.mom is None:
            return None
        return abs(self.mom) >= MOM_THRESHOLD

    @property
    def distance_from_threshold(self) -> float | None:
        if self.mom is None:
            return None
        return abs(self.mom) - MOM_THRESHOLD


def _empty_view(symbol: str) -> SymbolView:
    """A universe symbol with no shadow row at all. Built by keyword so adding a
    field to SymbolView cannot silently shift positional Nones."""
    return SymbolView(
        symbol=symbol, side=None, six_h_close=None, mom=None, mom_strength=None,
        atr=None, entry_candidate=None, initial_stop=None, notional=None,
        risk_pct=None, decision=None, rejection_reason=None, timestamp=None,
        code_sha=None,
    )


def _view(row: dict[str, Any]) -> SymbolView:
    return SymbolView(
        symbol=str(row.get("symbol") or "UNKNOWN"),
        side=row.get("side"),
        six_h_close=_f(row.get("six_h_close")),
        mom=_f(row.get("mom")),
        mom_strength=_f(row.get("mom_strength")),
        atr=_f(row.get("atr")),
        entry_candidate=_f(row.get("entry_candidate")),
        initial_stop=_f(row.get("initial_stop")),
        notional=_f(row.get("notional")),
        risk_pct=_f(row.get("risk_pct")),
        decision=row.get("decision"),
        rejection_reason=row.get("rejection_reason"),
        timestamp=_ts(row.get("timestamp")),
        code_sha=row.get("code_sha") or None,
    )


def build(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    loaded = sources.load_jsonl_tail(SHADOW_LOG)
    rows = loaded.value if isinstance(loaded.value, list) else []
    scan = sources.load_json(SCAN_STATE)

    views = [_view(r) for r in rows if isinstance(r, dict)]
    latest: dict[str, SymbolView] = {}
    for view in views:
        current = latest.get(view.symbol)
        if current is None or (view.timestamp and current.timestamp and view.timestamp > current.timestamp):
            latest[view.symbol] = view

    # Universe order, and symbols with no row at all are shown as UNKNOWN rather
    # than omitted — "never evaluated" and "evaluated, no signal" differ.
    per_symbol = [latest.get(sym) or _empty_view(sym) for sym in SYMBOL_UNIVERSE]

    newest = max((v.timestamp for v in views if v.timestamp), default=None)
    age = (now - newest).total_seconds() if newest else None

    if not rows:
        signal_status = Status.UNKNOWN
    elif age is not None and age > SHADOW_STALE_SECONDS:
        signal_status = Status.STALE
    else:
        signal_status = Status.HEALTHY

    decisions: dict[str, int] = {}
    for view in views:
        key = str(view.decision or "UNKNOWN")
        decisions[key] = decisions.get(key, 0) + 1

    actionable = [v for v in per_symbol if v.actionable]
    blocked = [v for v in views if v.decision and v.decision != "EXECUTED"]

    return {
        "strategy_id": STRATEGY_ID,
        "timeframe": f"{TIMEFRAME_HOURS}H",
        "universe": list(SYMBOL_UNIVERSE),
        "spec": {
            "mom_lookback": MOM_LOOKBACK, "mom_threshold": MOM_THRESHOLD,
            "atr_period": ATR_PERIOD, "atr_mult": ATR_MULT,
            "max_open_positions": MAX_OPEN_POSITIONS,
            "exit_model": "trailing-stop only (no fixed take-profit)",
        },
        "per_symbol": per_symbol,
        "signal_status": signal_status,
        "last_signal_at": newest,
        "last_signal_age_seconds": age,
        "next_boundary": next_boundary(now),
        "seconds_to_boundary": (next_boundary(now) - now).total_seconds(),
        "shadow_rows": len(rows),
        "decision_counts": decisions,
        "actionable_count": len(actionable),
        "blocked_count": len(blocked),
        "provenance": {"shadow": loaded.provenance, "scan_state": scan.provenance},
    }
