"""Deterministic, state-free selection of one execution winner.

The selector only orders already-created plans using values the existing
pipeline already computes.  It does not change strategy scoring, create order
intents, allocate a lifecycle, or perform exchange calls.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from clients.schemas import TradePlan
# The sufficient-state token comes from the module that defines it, so the two
# sides of this contract cannot drift to different spellings.
from risk.symbol_expectancy import SUFFICIENT_NEGATIVE, SUFFICIENT_OK

#: Statuses the producer reaches only once the sample clears MIN_SAMPLE. Both
#: are evidence; the difference between them is the sign of the mean, not its
#: admissibility.
_SUFFICIENT_STATUSES = frozenset({SUFFICIENT_OK, SUFFICIENT_NEGATIVE})


_FLOAT = r"([-+]?(?:\d+(?:\.\d*)?|\.\d+))"
_DIRECTIONAL_EXPECTANCY_REASON = re.compile(
    r"^\s*symbol expectancy source=[^(]+\(\s*"
    r"(?P<symbol>[A-Z0-9]+)\s+"
    r"(?P<direction>LONG|SHORT)\s*,"
    r"(?P<body>[^)]*)\)\s*$",
    flags=re.IGNORECASE,
)
_EXPECTANCY_TOKEN = re.compile(
    r"(?:^|,)\s*exp\s*=\s*(?P<value>[^,\s)]*)",
    flags=re.IGNORECASE,
)
#: Read from the same captured body as ``exp``, so a status from one evidence
#: line can never be paired with a value from another.
_STATUS_TOKEN = re.compile(
    r"(?:^|,)\s*status\s*=\s*(?P<value>[^,\s)]*)",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RankedPlan:
    plan: TradePlan
    execution_score: float
    expectancy: float
    setup_quality: float
    liquidity_spread_quality: float

    @property
    def audit_key(self) -> tuple[float, float, float, float, str, str, str, str]:
        return (
            self.execution_score,
            self.expectancy,
            self.setup_quality,
            self.liquidity_spread_quality,
            self.plan.symbol.upper(),
            self.plan.direction.upper(),
            self.plan.strategy.lower(),
            self.plan.plan_id,
        )


@dataclass(frozen=True, slots=True)
class RejectedPlan:
    symbol: str
    plan_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class PortfolioSelection:
    ranked: tuple[RankedPlan, ...]
    rejected: tuple[RejectedPlan, ...]

    @property
    def winner(self) -> TradePlan | None:
        return self.ranked[0].plan if self.ranked else None

    @property
    def winner_metrics(self) -> RankedPlan | None:
        return self.ranked[0] if self.ranked else None


def _metric(plan: TradePlan, marker: str, default: float) -> float:
    text = " | ".join(
        str(value)
        for value in [
            *(getattr(plan, "notes", None) or []),
            *(getattr(plan, "reasons", None) or []),
        ]
    )
    match = re.search(re.escape(marker) + _FLOAT, text, flags=re.IGNORECASE)
    if not match:
        return default
    try:
        value = float(match.group(1))
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def _expectancy(plan: TradePlan) -> float:
    """Return the plan's directional symbol expectancy, if it is admissible.

    Strategy-level reasons can also contain ``exp=``.  They must never become
    the portfolio symbol tiebreaker when the directional value is unavailable.
    Missing, malformed, non-finite, or ambiguous directional evidence is
    therefore neutral and fails closed to ``0.0``.

    Insufficient evidence now fails closed the same way. ``symbol_expectancy``
    computes a sample size, decides below ``MIN_SAMPLE`` that the sample cannot
    gate anything, and records that as ``status``. It then prints the mean
    regardless, on a line its own docstring calls "evidence, not a decision".
    This function used to read the mean and ignore the verdict beside it.

    In the frozen Release-A window every one of the 64 selected trades carried
    ``INSUFFICIENT_LIVE_DATA``, with a median sample of two closes and a maximum
    of nine; 53 still carried a number. Ranking by the average of two trades is
    ranking by the last trade on that pair. Requiring ``SUFFICIENT_OK`` restores
    the threshold the producer already computes.

    ``SUFFICIENT_NEGATIVE`` is admitted, and that is deliberate. It looks like a
    kill-switch verdict, but ``symbol_expectancy.evaluate`` only blocks on it
    while the evidence is ``FRESH`` or ``AGING``; beyond fourteen days it
    returns ``(False, None)`` so a pause can be re-earned rather than becoming
    permanent. ``RiskManager`` then finds no hard reason -- the provenance line
    is in ``SOFT_PREFIXES`` and never blocks on its own -- and the candidate
    proceeds. A stale, sufficiently-sampled, losing pair therefore reaches this
    function legitimately, carrying a negative mean over at least ten closes.
    Neutralising that would discard real evidence; ranking it below a better
    pair is exactly what the sort key is for.
    """
    scoped: list[str] = []
    for value in [
        *(getattr(plan, "notes", None) or []),
        *(getattr(plan, "reasons", None) or []),
    ]:
        match = _DIRECTIONAL_EXPECTANCY_REASON.fullmatch(str(value))
        if not match:
            continue
        if match.group("symbol").upper() != str(plan.symbol or "").upper():
            continue
        if match.group("direction").upper() != str(plan.direction or "").upper():
            continue
        scoped.append(match.group("body"))

    if len(scoped) != 1:
        return 0.0

    statuses = list(_STATUS_TOKEN.finditer(scoped[0]))
    if len(statuses) != 1:
        return 0.0
    if statuses[0].group("value").strip().upper() not in _SUFFICIENT_STATUSES:
        return 0.0

    tokens = list(_EXPECTANCY_TOKEN.finditer(scoped[0]))
    if len(tokens) != 1:
        return 0.0
    try:
        value = float(tokens[0].group("value"))
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _setup_quality(plan: TradePlan) -> float:
    return _metric(plan, "planner_entry_quality=", float(plan.score))


def _liquidity_spread_quality(plan: TradePlan) -> float:
    spread = _metric(plan, "spread_bps_for_edge=", math.inf)
    if not math.isfinite(spread):
        spread = _metric(plan, "spread_bps=", math.inf)
    return -spread if math.isfinite(spread) else -math.inf


def _invalid_reason(plan: TradePlan, allowed_symbols: frozenset[str] | None) -> str:
    symbol = str(plan.symbol or "").upper()
    direction = str(plan.direction or "").upper()
    if str(plan.verdict or "").upper() != "EXECUTABLE":
        return "not_executable"
    if not symbol:
        return "symbol_missing"
    if allowed_symbols is not None and symbol not in allowed_symbols:
        return "symbol_not_in_canonical_allowlist"
    if direction not in {"LONG", "SHORT"}:
        return "direction_invalid"
    if not plan.plan_id or not plan.candidate_id:
        return "identity_missing"
    if not math.isfinite(float(plan.score)):
        return "score_invalid"
    if not plan.entry_prices or any(float(value) <= 0 for value in plan.entry_prices):
        return "planned_entry_invalid"
    if float(plan.stop_loss or 0) <= 0:
        return "stop_loss_invalid"
    authorized_stop_only = bool(
        plan.strategy == "funding_crowding_continuation_24h"
        and plan.protection_mode == "STOP_ONLY_TIME_EXIT"
        and plan.frozen_spec_sha256 == "cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13"
        and plan.pilot_authorized
        and plan.scheduled_exit_at_ms > 0
        and not plan.take_profits
    )
    if not authorized_stop_only and (
        not plan.take_profits or any(float(value) <= 0 for value in plan.take_profits)
    ):
        return "take_profit_invalid"
    return ""


def select_execution_winner(
    plans: Iterable[TradePlan],
    *,
    allowed_symbols: Iterable[str] | None = None,
    execution_scores: Mapping[str, float] | None = None,
) -> PortfolioSelection:
    """Rank valid plans and return exactly one winner through ``winner``.

    Sort order, highest first:
      1. existing execution-aware score;
      2. existing directional expectancy;
      3. existing planner setup quality;
      4. best (lowest) existing spread;
      5. alphabetic symbol, then stable identity fields.
    """
    allowed = (
        frozenset(str(symbol).upper() for symbol in allowed_symbols)
        if allowed_symbols is not None
        else None
    )
    score_map = {
        str(symbol).upper(): float(value)
        for symbol, value in (execution_scores or {}).items()
        if math.isfinite(float(value))
    }
    ranked: list[RankedPlan] = []
    rejected: list[RejectedPlan] = []

    for plan in plans:
        reason = _invalid_reason(plan, allowed)
        if reason:
            rejected.append(RejectedPlan(str(plan.symbol or "").upper(), plan.plan_id, reason))
            continue
        execution_score = score_map.get(plan.symbol.upper(), float(plan.score))
        ranked.append(
            RankedPlan(
                plan=plan,
                execution_score=round(execution_score, 8),
                expectancy=round(_expectancy(plan), 8),
                setup_quality=round(_setup_quality(plan), 8),
                liquidity_spread_quality=round(_liquidity_spread_quality(plan), 8),
            )
        )

    ranked.sort(
        key=lambda row: (
            -row.execution_score,
            -row.expectancy,
            -row.setup_quality,
            -row.liquidity_spread_quality,
            row.plan.symbol.upper(),
            row.plan.direction.upper(),
            row.plan.strategy.lower(),
            row.plan.plan_id,
        )
    )
    return PortfolioSelection(tuple(ranked), tuple(rejected))


__all__ = [
    "PortfolioSelection",
    "RankedPlan",
    "RejectedPlan",
    "select_execution_winner",
]
