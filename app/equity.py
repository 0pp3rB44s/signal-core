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
from datetime import datetime, timezone
from pathlib import Path

BASE_PATH = Path(__file__).resolve().parents[1]
EQUITY_SNAPSHOT_PATH = BASE_PATH / "state" / "account_equity.json"

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
