"""adaptive_trend_tsmom_v1 -- fixed-parameter 6H time-series momentum / trend strategy.

Externally-motivated (AdaptiveTrend-style TSMOM), not derived by fitting our own
12-day dataset. Parameters are frozen V1 defaults, not tuned:

    L (momentum lookback)   = 24 completed 6H candles  (~6 days)
    THETA_ENTRY             = 0.03  (3.0%)
    ATR_PERIOD               = 14 completed 6H candles
    ATR_MULT                 = 2.5

This module is pure signal/sizing/ranking logic. It does not place orders, does
not touch exchange state, and does not know about execution, protection, or
account safety -- those stay in the existing, already-proven execution engine.
It exists so the strategy's arithmetic can be reasoned about and tested in
isolation, exactly as required before any execution wiring is attempted.

No look-ahead: every function here operates only on candles the caller has
already confirmed are CLOSED. This module does not fetch data and has no
notion of "now" -- `closed_candles_as_of` is the single guard a caller must
use before calling anything else here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

STRATEGY_VERSION = "adaptive_trend_tsmom_v1"
SIGNAL_TIMEFRAME = "6H"
SIGNAL_TIMEFRAME_MS = 6 * 60 * 60 * 1000

MOM_LOOKBACK = 24
MOM_THRESHOLD = 0.03
ATR_PERIOD = 14
ATR_MULT = 2.5

RISK_PCT_PER_TRADE = 0.005          # 0.50%
ABSOLUTE_MAX_RISK_PCT = 0.01        # 1.00%
MAX_LEVERAGE = 2.0
MAX_TOTAL_EXPOSURE_PCT = 1.0        # 100% of equity
MAX_OPEN_POSITIONS = 1

SYMBOL_UNIVERSE = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
_SYMBOL_TIE_BREAK = {"BTCUSDT": 0, "ETHUSDT": 1, "SOLUSDT": 2}


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(slots=True)
class Candle6h:
    """Minimal candle shape this module needs. `close_ms` is when the candle
    stopped forming -- the boundary a signal may not be evaluated before."""
    open_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float


def candle_6h_boundary(timestamp_ms: int) -> int:
    """The UTC-aligned 6H bucket start containing `timestamp_ms`.

    Boundaries are 00:00, 06:00, 12:00, 18:00 UTC -- epoch-aligned, since
    SIGNAL_TIMEFRAME_MS (6h) divides evenly into a UTC day and the Unix epoch
    is itself a UTC midnight, so integer bucketing from epoch is already
    UTC-midnight-aligned with no timezone arithmetic required.
    """
    return (timestamp_ms // SIGNAL_TIMEFRAME_MS) * SIGNAL_TIMEFRAME_MS


def closed_candles_as_of(candles: list[Candle6h], now_ms: int) -> list[Candle6h]:
    """Every candle whose close boundary is at or before `now_ms`.

    This is the ONLY function in this module that is allowed to know "now".
    Every other function must be called only with its output -- that is what
    makes "no signal before candle close" a structural guarantee rather than
    a convention callers might forget.
    """
    return [c for c in candles if c.close_ms <= now_ms]


def compute_momentum(closed: list[Candle6h], lookback: int = MOM_LOOKBACK) -> float | None:
    """MOM_t = (Close_t - Close_(t-L)) / Close_(t-L) over the last `lookback`
    completed candles. None if there is not yet enough closed history."""
    if len(closed) <= lookback:
        return None
    close_t = closed[-1].close
    close_t_minus_l = closed[-1 - lookback].close
    if close_t_minus_l == 0:
        return None
    return (close_t - close_t_minus_l) / close_t_minus_l


def classify_signal(mom: float | None, threshold: float = MOM_THRESHOLD) -> Side | None:
    if mom is None:
        return None
    if mom >= threshold:
        return Side.LONG
    if mom <= -threshold:
        return Side.SHORT
    return None


def compute_atr(closed: list[Candle6h], period: int = ATR_PERIOD) -> float | None:
    """Classic Wilder true-range average over the last `period` completed
    candles (simple mean, not the smoothed/exponential Wilder variant -- kept
    simple per the "must be manually reasoned about" requirement)."""
    if len(closed) <= period:
        return None
    window = closed[-period:]
    trs = []
    for i in range(len(window)):
        c = window[i]
        prev_close = window[i - 1].close if i > 0 else closed[-period - 1].close
        tr = max(
            c.high - c.low,
            abs(c.high - prev_close),
            abs(c.low - prev_close),
        )
        trs.append(tr)
    return sum(trs) / len(trs)


def initial_stop(entry_price: float, atr: float, side: Side, mult: float = ATR_MULT) -> float:
    if side is Side.LONG:
        return entry_price - mult * atr
    return entry_price + mult * atr


def update_trailing_stop(
    previous_stop: float, latest_close: float, atr: float, side: Side, mult: float = ATR_MULT,
) -> float:
    """Ratchet-only update. A LONG stop may only rise; a SHORT stop may only fall."""
    candidate = latest_close - mult * atr if side is Side.LONG else latest_close + mult * atr
    if side is Side.LONG:
        return max(previous_stop, candidate)
    return min(previous_stop, candidate)


@dataclass(slots=True)
class SignalCandidate:
    symbol: str
    side: Side
    signal_candle_close_ms: int
    close: float
    mom: float
    atr: float

    @property
    def atr_pct(self) -> float:
        return self.atr / self.close if self.close else 0.0

    @property
    def mom_strength(self) -> float:
        atr_pct = self.atr_pct
        if atr_pct <= 0:
            return 0.0
        return abs(self.mom) / atr_pct


def rank_candidates_ordered(candidates: list[SignalCandidate]) -> list[SignalCandidate]:
    """Full ranking, best first: highest MOM_STRENGTH wins, ties broken
    BTC > ETH > SOL. Frozen rule -- never selects on historical
    profitability. The single-winner `rank_candidates` below is this list's
    first element; callers that need to fall through to the next-best
    candidate when the top one turns out to be unexecutable (e.g. exchange
    minimum notional would push it over MAX_EFFECTIVE_RISK_PCT) use this
    instead of re-deriving the order."""
    return sorted(
        candidates,
        key=lambda c: (-c.mom_strength, _SYMBOL_TIE_BREAK.get(c.symbol, 99)),
    )


def rank_candidates(candidates: list[SignalCandidate]) -> SignalCandidate | None:
    """Highest MOM_STRENGTH wins; ties broken BTC > ETH > SOL. Frozen rule --
    never selects on historical profitability."""
    ordered = rank_candidates_ordered(candidates)
    return ordered[0] if ordered else None


@dataclass(slots=True)
class SizingResult:
    equity: float
    atr: float
    stop_distance: float
    stop_distance_pct: float
    risk_usdt: float
    raw_notional: float
    exchange_min_notional: float
    rounded_notional: float
    effective_risk_usdt: float
    effective_risk_pct: float
    required_margin: float
    projected_total_exposure_pct: float
    leverage: float
    accepted: bool
    rejection_reason: str = ""


def size_position(
    *,
    equity: float,
    entry_price: float,
    stop_price: float,
    exchange_min_notional: float,
    risk_pct: float = RISK_PCT_PER_TRADE,
    absolute_max_risk_pct: float = ABSOLUTE_MAX_RISK_PCT,
    max_leverage: float = MAX_LEVERAGE,
    max_total_exposure_pct: float = MAX_TOTAL_EXPOSURE_PCT,
) -> SizingResult:
    """Risk-to-stop sizing. Never the margin-per-slot x leverage model.

    `raw_notional = risk_usdt / stop_distance_pct` is rounded UP only as far as
    the exchange's minimum notional requires; if that rounding pushes the
    trade's *effective* risk past `absolute_max_risk_pct`, the order is
    rejected outright rather than silently accepted at a higher risk than
    configured.
    """
    if equity <= 0 or entry_price <= 0:
        return SizingResult(
            equity=equity, atr=0.0, stop_distance=0.0, stop_distance_pct=0.0,
            risk_usdt=0.0, raw_notional=0.0, exchange_min_notional=exchange_min_notional,
            rounded_notional=0.0, effective_risk_usdt=0.0, effective_risk_pct=0.0,
            required_margin=0.0, projected_total_exposure_pct=0.0, leverage=0.0,
            accepted=False, rejection_reason="invalid_equity_or_price",
        )

    stop_distance = abs(entry_price - stop_price)
    stop_distance_pct = stop_distance / entry_price
    if stop_distance_pct <= 0:
        return SizingResult(
            equity=equity, atr=0.0, stop_distance=stop_distance, stop_distance_pct=0.0,
            risk_usdt=0.0, raw_notional=0.0, exchange_min_notional=exchange_min_notional,
            rounded_notional=0.0, effective_risk_usdt=0.0, effective_risk_pct=0.0,
            required_margin=0.0, projected_total_exposure_pct=0.0, leverage=0.0,
            accepted=False, rejection_reason="zero_stop_distance",
        )

    risk_usdt = equity * risk_pct
    raw_notional = risk_usdt / stop_distance_pct

    exposure_cap_notional = equity * max_total_exposure_pct
    if exchange_min_notional > exposure_cap_notional:
        # The exchange's own floor is above what this account is allowed to
        # expose in total. Clamping to the cap would silently submit a
        # notional below the minimum -- the exchange would reject that order
        # anyway, so refuse cleanly instead of pretending sizing succeeded.
        return SizingResult(
            equity=equity, atr=0.0, stop_distance=stop_distance, stop_distance_pct=stop_distance_pct,
            risk_usdt=risk_usdt, raw_notional=raw_notional, exchange_min_notional=exchange_min_notional,
            rounded_notional=0.0, effective_risk_usdt=0.0, effective_risk_pct=0.0,
            required_margin=0.0, projected_total_exposure_pct=0.0, leverage=0.0,
            accepted=False, rejection_reason="ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER",
        )
    rounded_notional = max(raw_notional, exchange_min_notional)
    rounded_notional = min(rounded_notional, exposure_cap_notional)

    effective_risk_usdt = rounded_notional * stop_distance_pct
    effective_risk_pct = effective_risk_usdt / equity
    leverage = rounded_notional / equity
    required_margin = rounded_notional / leverage if leverage > 0 else rounded_notional
    projected_total_exposure_pct = rounded_notional / equity

    accepted = True
    rejection_reason = ""

    if effective_risk_pct > absolute_max_risk_pct:
        accepted = False
        rejection_reason = "ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER"
    elif leverage > max_leverage:
        accepted = False
        rejection_reason = "leverage_exceeds_ceiling"
    elif projected_total_exposure_pct > max_total_exposure_pct + 1e-9:
        accepted = False
        rejection_reason = "exposure_exceeds_ceiling"

    return SizingResult(
        equity=equity, atr=0.0, stop_distance=stop_distance, stop_distance_pct=stop_distance_pct,
        risk_usdt=risk_usdt, raw_notional=raw_notional, exchange_min_notional=exchange_min_notional,
        rounded_notional=rounded_notional, effective_risk_usdt=effective_risk_usdt,
        effective_risk_pct=effective_risk_pct, required_margin=required_margin,
        projected_total_exposure_pct=projected_total_exposure_pct, leverage=leverage,
        accepted=accepted, rejection_reason=rejection_reason,
    )


def fee_observability(
    *, notional: float, taker_fee_rate: float, atr_pct: float, stop_distance_pct: float,
) -> dict:
    """Log-only cost visibility. Never blocks a trade -- see module docstring:
    V1 deliberately carries no discretionary expectancy filter."""
    entry_fee = notional * taker_fee_rate
    exit_fee = notional * taker_fee_rate
    round_trip_fee = entry_fee + exit_fee
    round_trip_fee_pct = (round_trip_fee / notional) if notional else 0.0
    cost_as_pct_of_stop = (round_trip_fee_pct / stop_distance_pct) if stop_distance_pct else None
    return dict(
        estimated_entry_fee=entry_fee,
        estimated_exit_fee=exit_fee,
        estimated_round_trip_cost=round_trip_fee,
        round_trip_cost_pct=round_trip_fee_pct,
        atr_pct=atr_pct,
        stop_distance_pct=stop_distance_pct,
        cost_as_pct_of_stop_distance=cost_as_pct_of_stop,
    )


@dataclass(slots=True)
class ShadowDecisionRecord:
    """One immutable row per evaluated signal, traded or not. See spec section
    12 -- this is the forward counterfactual dataset; it never touches
    exchange or local position state."""
    timestamp: str
    symbol: str
    side: str | None
    six_h_close: float
    mom: float | None
    atr: float | None
    mom_strength: float | None
    entry_candidate: float | None
    initial_stop: float | None
    risk_pct: float | None
    notional: float | None
    decision: str
    rejection_reason: str
    strategy_version: str = STRATEGY_VERSION
    code_sha: str = ""

    def as_dict(self) -> dict:
        return dict(
            timestamp=self.timestamp, symbol=self.symbol, side=self.side,
            six_h_close=self.six_h_close, mom=self.mom, atr=self.atr,
            mom_strength=self.mom_strength, entry_candidate=self.entry_candidate,
            initial_stop=self.initial_stop, risk_pct=self.risk_pct, notional=self.notional,
            decision=self.decision, rejection_reason=self.rejection_reason,
            strategy_version=self.strategy_version, code_sha=self.code_sha,
        )
