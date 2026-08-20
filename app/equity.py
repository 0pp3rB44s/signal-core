"""Live account equity resolution with fail-closed fallback.

The bot historically sized every trade against the static ACCOUNT_EQUITY_USDT
from .env. When the real balance drifts, risk-per-trade and the daily/weekly
kill-switch thresholds silently drift with it. The runner now snapshots the
real Bitget equity to a state file every cycle; every consumer (risk manager,
planner, execution caps) resolves equity through this module.

Fail-closed rule: if the snapshot is missing, stale or implausible, fall back
to the smaller of (configured, last snapshot) so sizing errs small.
"""

from __future__ import annotations

import json
import math
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

BASE_PATH = Path(__file__).resolve().parents[1]
EQUITY_SNAPSHOT_PATH = BASE_PATH / "state" / "account_equity.json"
PORTFOLIO_EQUITY_GUARD_PATH = BASE_PATH / "state" / "portfolio_equity_guard.json"
_EQUITY_GUARD_LOCK = threading.Lock()

SNAPSHOT_MAX_AGE_SECONDS = 15 * 60
# Snapshots outside configured*[1/PLAUSIBLE_RATIO, PLAUSIBLE_RATIO] are treated
# as parsing junk, not as a real balance change.
PLAUSIBLE_RATIO = 25.0


def write_equity_snapshot(equity: float, source: str = "bitget_accounts") -> None:
    try:
        equity = float(equity)
        if not math.isfinite(equity) or equity <= 0:
            return
        EQUITY_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        EQUITY_SNAPSHOT_PATH.write_text(json.dumps({
            "equity": round(equity, 4),
            "source": source,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }))
    except Exception:
        # Snapshot writing must never break the trading loop.
        pass


def resolve_account_equity(settings) -> tuple[float, str]:
    """Return (equity, source). source is 'live', 'stale_min' or 'configured'."""
    configured = float(getattr(settings, "account_equity_usdt", 0.0) or 0.0)

    try:
        payload = json.loads(EQUITY_SNAPSHOT_PATH.read_text())
        snapshot = float(payload.get("equity") or 0.0)
        updated_at = datetime.fromisoformat(str(payload.get("updated_at")))
        age = (datetime.now(timezone.utc) - updated_at).total_seconds()
    except Exception:
        return configured, "configured"

    plausible = (
        math.isfinite(snapshot)
        and snapshot > 0
        and (configured <= 0 or (configured / PLAUSIBLE_RATIO) <= snapshot <= configured * PLAUSIBLE_RATIO)
    )
    if not plausible:
        return configured, "configured"

    if age <= SNAPSHOT_MAX_AGE_SECONDS:
        return snapshot, "live"

    # Stale snapshot: err small.
    if configured > 0:
        return min(snapshot, configured), "stale_min"
    return snapshot, "stale_min"


def portfolio_equity_drawdown_gate(
    settings,
    *,
    observed_equity: float | None = None,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Persist and enforce the UTC-day equity high-water breaker.

    ``HARD_DAILY_STOP_PCT`` is the already documented account-level stop.  The
    realized-PnL gate can miss open loss, funding, or an incomplete close row;
    this independent breaker measures authenticated/current total equity.  A
    malformed existing state is UNKNOWN and therefore blocks new entries.
    """
    try:
        limit_pct = float(getattr(settings, "hard_daily_stop_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False, "portfolio equity breaker blocked: invalid hard daily stop policy"
    if not math.isfinite(limit_pct) or limit_pct <= 0:
        return False, "portfolio equity breaker blocked: hard daily stop policy unavailable"

    if observed_equity is None:
        equity, source = resolve_account_equity(settings)
    else:
        try:
            equity = float(observed_equity)
        except (TypeError, ValueError):
            equity = 0.0
        source = "authenticated"
    if not math.isfinite(equity) or equity <= 0:
        return False, "portfolio equity breaker blocked: current equity unavailable"

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    utc_day = current_time.astimezone(timezone.utc).date().isoformat()

    with _EQUITY_GUARD_LOCK:
        state: dict = {}
        if PORTFOLIO_EQUITY_GUARD_PATH.exists():
            try:
                payload = json.loads(PORTFOLIO_EQUITY_GUARD_PATH.read_text())
                if not isinstance(payload, dict):
                    raise ValueError("state is not an object")
                state = payload
            except Exception as exc:
                return False, f"portfolio equity breaker blocked: state unreadable ({type(exc).__name__})"

        if state.get("utc_day") == utc_day:
            try:
                prior_high = float(state["high_water_equity"])
            except (KeyError, TypeError, ValueError):
                return False, "portfolio equity breaker blocked: high-water state malformed"
            if not math.isfinite(prior_high) or prior_high <= 0:
                return False, "portfolio equity breaker blocked: high-water state invalid"
            high_water = max(prior_high, equity)
        else:
            high_water = equity

        drawdown_pct = max(0.0, (high_water - equity) / high_water * 100.0)
        next_state = {
            "schema_version": "portfolio_equity_guard_v1",
            "utc_day": utc_day,
            "high_water_equity": round(high_water, 8),
            "last_equity": round(equity, 8),
            "last_source": source,
            "drawdown_pct": round(drawdown_pct, 8),
            "hard_daily_stop_pct": limit_pct,
            "updated_at": current_time.astimezone(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            PORTFOLIO_EQUITY_GUARD_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary = PORTFOLIO_EQUITY_GUARD_PATH.with_name(
                f".{PORTFOLIO_EQUITY_GUARD_PATH.name}.{os.getpid()}.tmp"
            )
            temporary.write_text(json.dumps(next_state, sort_keys=True))
            os.replace(temporary, PORTFOLIO_EQUITY_GUARD_PATH)
        except Exception as exc:
            return False, f"portfolio equity breaker blocked: state persistence failed ({type(exc).__name__})"

    if drawdown_pct >= limit_pct:
        return False, (
            "portfolio equity breaker active: "
            f"equity={equity:.8f}, high_water={high_water:.8f}, "
            f"drawdown_pct={drawdown_pct:.4f}%, hard_daily_stop_pct={limit_pct:.4f}%"
        )
    return True, (
        "portfolio equity breaker ok: "
        f"drawdown_pct={drawdown_pct:.4f}% < hard_daily_stop_pct={limit_pct:.4f}% "
        f"source={source}"
    )
