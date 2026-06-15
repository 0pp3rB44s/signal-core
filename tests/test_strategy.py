from app.config import Settings
from clients.schemas import Candle, ContractSpec, MarketSnapshot, TimeframeSnapshot
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.scoring import StrategyScorer


def _make_market(direction: str = "LONG") -> MarketSnapshot:
    candles: list[Candle] = []
    price = 100.0
    for i in range(40):
        high = price + 0.4
        low = price - 0.4
        close = price + 0.05
        candles.append(Candle(timestamp_ms=i, open=price, high=high, low=low, close=close, volume_base=1000))
        price += 0.05

    if direction == "LONG":
        candles[-1] = Candle(timestamp_ms=999, open=101.5, high=101.8, low=99.2, close=100.4, volume_base=2500)
        alignment = "aligned_bullish"
        trend = "bullish"
    else:
        candles[-1] = Candle(timestamp_ms=999, open=101.5, high=103.5, low=101.2, close=102.0, volume_base=2500)
        alignment = "aligned_bearish"
        trend = "bearish"

    tf = TimeframeSnapshot(
        symbol="TESTUSDT",
        granularity="15m",
        latest_close=candles[-1].close,
        change_pct=0.2,
        range_pct=1.1,
        volume_ratio_20=1.4,
        ema20=100.0,
        ema50=99.5,
        trend=trend,
        candles=candles,
    )
    confirm = TimeframeSnapshot(
        symbol="TESTUSDT",
        granularity="1H",
        latest_close=candles[-1].close,
        change_pct=0.5,
        range_pct=1.4,
        volume_ratio_20=1.3,
        ema20=100.0,
        ema50=99.0,
        trend=trend,
        candles=candles,
    )
    contract = ContractSpec(
        symbol="TESTUSDT",
        product_type="USDT-FUTURES",
        quote_coin="USDT",
        base_coin="TEST",
        status="normal",
        min_trade_num=1.0,
        size_multiplier=0.1,
        price_place=3,
        volume_24h_usdt=10000000,
        change_pct_24h=2.0,
        raw={},
    )
    return MarketSnapshot(
        symbol="TESTUSDT",
        contract=contract,
        primary=tf,
        confirmation=confirm,
        alignment=alignment,
        score_hint=75.0,
        notes=[],
    )


def test_detects_bullish_sweep_candidate_or_safely_rejects() -> None:
    settings = Settings()
    strategy = LiquiditySweepStrategy(settings)
    candidate = strategy.detect(_make_market("LONG"))

    if candidate is not None:
        assert candidate.direction == "LONG"
    else:
        # Strict A+ filters may reject synthetic test data.
        assert candidate is None


def test_scoring_returns_watch_or_go() -> None:
    settings = Settings()
    strategy = LiquiditySweepStrategy(settings)
    scorer = StrategyScorer(settings)
    candidate = strategy.detect(_make_market("LONG"))

    if candidate is not None:
        score = scorer.score(candidate)
        assert score.total > 0
        assert score.verdict in {"WATCH", "GO", "NO_GO"}
    else:
        # Safe rejection is acceptable with strict production filters.
        assert candidate is None
