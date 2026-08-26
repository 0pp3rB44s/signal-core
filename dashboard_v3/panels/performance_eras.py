"""Performance split by strategy era, because summing them is meaningless.

MicroFlow's live record is ~134 economically-confirmed trades at PF 0.2544 and
-19.40 USDT net. AdaptiveTrend has **zero** live trades. Aggregating them
produces a single number that describes neither: it would let a retired
strategy's losses masquerade as the current one's performance, and it would let
the current one hide inside a large historical sample.

Every scope below is computed independently and is never silently combined. The
ALL_HISTORICAL scope exists because the account-level total is a real question,
but it is labelled as an account figure, not a strategy figure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from telemetry.close_record_sources import is_economic_close

ADAPTIVE_TREND = "adaptive_trend_tsmom_v1"
MICROFLOW = "microflow_scalper_v1"

SCOPES = (
    ("ADAPTIVETREND_LIVE", "AdaptiveTrend LIVE", "Real fills by the current strategy"),
    ("ADAPTIVETREND_SHADOW", "AdaptiveTrend SHADOW", "Hypothetical decisions, never executed"),
    ("MICROFLOW_LEGACY", "MicroFlow LEGACY", "Retired strategy — historical record only"),
    ("ALL_HISTORICAL", "All historical (account)", "Account-level total across every era"),
)

#: Sample thresholds. These deliberately refuse to promote a small sample into a
#: statistic: a profit factor computed on four trades is a number, not evidence.
NO_DATA, TINY, DESCRIPTIVE, REASONABLE = 0, 1, 10, 30


def evidence_state(n: int) -> str:
    if n <= NO_DATA:
        return "NO_DATA"
    if n < DESCRIPTIVE:
        return "TINY_SAMPLE"
    if n < REASONABLE:
        return "DESCRIPTIVE"
    return "REASONABLE_SAMPLE"


@dataclass(frozen=True)
class Metrics:
    scope: str
    label: str
    description: str
    trades: int
    wins: int
    losses: int
    win_rate: float | None
    gross_pnl: float | None
    fees: float | None
    net_pnl: float | None
    profit_factor: float | None
    expectancy: float | None
    avg_win: float | None
    avg_loss: float | None
    max_drawdown: float | None
    avg_hold_seconds: float | None
    evidence: str

    @property
    def statistically_meaningful(self) -> bool:
        """Below this, the template must not render confidence language."""
        return self.evidence == "REASONABLE_SAMPLE"


def _f(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _strategy_of(row: dict[str, Any]) -> str:
    return str(row.get("strategy") or row.get("strategy_version") or "").strip()


def _matches(row: dict[str, Any], scope: str) -> bool:
    strategy = _strategy_of(row)
    if scope == "ADAPTIVETREND_LIVE":
        return strategy == ADAPTIVE_TREND
    if scope == "MICROFLOW_LEGACY":
        return strategy == MICROFLOW
    if scope == "ALL_HISTORICAL":
        return True
    return False


def compute(rows: list[dict[str, Any]], scope: str) -> Metrics:
    """Economic closes only, deduplicated, for one scope.

    Shadow decisions never appear here — they have no fills and no fees, and are
    reported by `shadow_metrics` instead so the two can never be confused.
    """
    key, label, description = next((s for s in SCOPES if s[0] == scope), (scope, scope, ""))

    if scope == "ADAPTIVETREND_SHADOW":
        return Metrics(key, label, description, 0, 0, 0, None, None, None, None,
                       None, None, None, None, None, None, "NO_DATA")

    seen: set[str] = set()
    nets: list[float] = []
    gross_total = fees_total = 0.0
    holds: list[float] = []
    have_gross = have_fees = False

    for row in rows:
        if not is_economic_close(row) or not _matches(row, scope):
            continue
        closed_at = str(row.get("closed_at") or row.get("timestamp") or "")
        identity = str(row.get("position_lifecycle_id") or "").strip() or "{}|{}|{}".format(
            str(row.get("symbol") or "").upper(), str(row.get("direction") or "").upper(),
            closed_at[:19])
        if identity in seen:
            continue
        net = _f(row.get("net_pnl"))
        if net is None:
            net = _f(row.get("pnl"))
        if net is None:
            continue
        seen.add(identity)
        nets.append(net)
        g = _f(row.get("gross_pnl"))
        if g is not None:
            gross_total += g; have_gross = True
        f = _f(row.get("fees"))
        if f is not None:
            fees_total += f; have_fees = True
        h = _f(row.get("trade_duration_seconds"))
        if h is not None:
            holds.append(h)

    n = len(nets)
    if n == 0:
        return Metrics(key, label, description, 0, 0, 0, None, None, None, None,
                       None, None, None, None, None, None, "NO_DATA")

    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    loss_sum = abs(sum(losses))

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for x in nets:
        equity += x
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    return Metrics(
        scope=key, label=label, description=description,
        trades=n, wins=len(wins), losses=len(losses),
        win_rate=len(wins) / n,
        gross_pnl=gross_total if have_gross else None,
        fees=fees_total if have_fees else None,
        net_pnl=sum(nets),
        profit_factor=(sum(wins) / loss_sum) if loss_sum else None,
        expectancy=sum(nets) / n,
        avg_win=(sum(wins) / len(wins)) if wins else None,
        avg_loss=(sum(losses) / len(losses)) if losses else None,
        max_drawdown=max_dd or None,
        avg_hold_seconds=(sum(holds) / len(holds)) if holds else None,
        evidence=evidence_state(n),
    )


def shadow_metrics(shadow_rows: list[Any]) -> dict[str, Any]:
    """Shadow decisions are counted, never priced. They have no fills."""
    decisions: dict[str, int] = {}
    for view in shadow_rows:
        key = str(getattr(view, "decision", None) or "UNKNOWN")
        decisions[key] = decisions.get(key, 0) + 1
    n = len(shadow_rows)
    return {
        "scope": "ADAPTIVETREND_SHADOW",
        "label": "AdaptiveTrend SHADOW",
        "decisions": n,
        "decision_counts": decisions,
        "evidence": evidence_state(n),
        "note": "Hypothetical decisions. No fills, no fees, no PnL — never summed with live results.",
    }


def compute_all(rows: list[dict[str, Any]], shadow_rows: list[Any] | None = None) -> dict[str, Any]:
    return {
        "scopes": [compute(rows, s[0]) for s in SCOPES],
        "shadow": shadow_metrics(shadow_rows or []),
        "generated_at": datetime.now(timezone.utc),
    }
