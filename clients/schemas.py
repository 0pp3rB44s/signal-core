from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    volume_quote: float | None = None


@dataclass(slots=True)
class ContractSpec:
    symbol: str
    product_type: str
    quote_coin: str
    base_coin: str
    status: str
    min_trade_num: float | None
    size_multiplier: float | None
    price_place: int | None
    volume_24h_usdt: float | None
    change_pct_24h: float | None
    raw: dict[str, Any] = field(repr=False)


@dataclass(slots=True)
class SymbolSnapshot:
    symbol: str
    granularity: str
    candles: list[Candle]
    contract_meta: dict[str, Any]


@dataclass(slots=True)
class TimeframeSnapshot:
    symbol: str
    granularity: str
    latest_close: float
    change_pct: float
    range_pct: float
    volume_ratio_20: float
    ema20: float
    ema50: float
    trend: str
    candles: list[Candle] = field(repr=False)


@dataclass(slots=True)
class MarketSnapshot:
    symbol: str
    contract: ContractSpec
    primary: TimeframeSnapshot
    confirmation: TimeframeSnapshot
    alignment: str
    score_hint: float
    notes: list[str]
    volatility_rank: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)
    origin_distance_score: float = 0.0
    impulse_freshness_score: float = 100.0
    expansion_exhaustion_score: float = 0.0


@dataclass(slots=True)
class SweepDetection:
    side: str
    swept_level: float
    sweep_extreme: float
    reclaim_level: float
    entry_hint: float
    invalidation: float
    displacement_pct: float
    bars_since_sweep: int
    volume_ratio_on_sweep: float
    local_range_size_pct: float
    reason_flags: list[str]


@dataclass(slots=True)
class StrategyCandidate:
    symbol: str
    strategy: str
    direction: str
    primary_granularity: str
    confirmation_granularity: str
    market: MarketSnapshot = field(repr=False)
    detection: SweepDetection = field(repr=False)
    notes: list[str] = field(default_factory=list)
    candidate_status: str = "candidate"


@dataclass(slots=True)
class StrategyScore:
    total: float
    breakdown: dict[str, float]
    verdict: str
    reasons: list[str]


@dataclass(slots=True)
class RiskVerdict:
    allowed: bool
    status: str
    reasons: list[str]
    account_risk_pct: float
    leverage: float
    max_open_positions: int


@dataclass(slots=True)
class TradePlan:
    symbol: str
    strategy: str
    direction: str
    verdict: str
    score: float
    entry_prices: list[float]
    stop_loss: float
    take_profits: list[float]
    risk_reward_ratio: float
    account_risk_pct: float
    leverage: float
    position_notional_usdt: float
    notes: list[str]
    reasons: list[str]
    tp_size_pcts: list[float] = field(default_factory=list)


@dataclass(slots=True)
class ExecutionReport:
    symbol: str
    direction: str
    strategy: str
    mode: str
    status: str
    message: str
    avg_entry: float
    stop_loss: float
    take_profits: list[float]
    position_notional_usdt: float
    leverage: float
    expected_entry: float = 0.0
    actual_entry: float = 0.0
    slippage_pct: float = 0.0
    fees_paid: float = 0.0
    realized_pnl: float = 0.0
    exchange_order_id: str = ""


@dataclass(slots=True)
class PositionUpdate:
    symbol: str
    status: str
    current_price: float
    unrealized_pnl_pct: float
    stop_loss: float
    break_even_active: bool
    tp1_hit: bool
    tp2_hit: bool
    tp3_hit: bool
    note: str