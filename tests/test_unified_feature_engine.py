from __future__ import annotations

from dataclasses import asdict

import pytest

from analysis.detector_replay import replay_snapshot
from app.config import Settings
from backtesting.backtest_engine import BacktestEngine
from clients.schemas import Candle, ContractSpec, SymbolSnapshot
from data.market_fetcher import MarketFetcher
from market_features.engine import CandleContractError, FeatureInputs, LiveMarketContext, aggregate_candles, build_market_snapshot, closed_candle_at_offset, select_closed_candles
from scripts.validation_engine import validation_snapshot
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.momentum_breakout import MomentumBreakdownStrategy, MomentumBreakoutStrategy
from strategies.strategies.continuation import ContinuationStrategy
from strategies.strategies.low_vol_reclaim import LowVolReclaimStrategy


def candles(count=320, start=0):
    result = []
    price = 100.0
    for index in range(count):
        close = price + .08 + (index % 7) * .01
        result.append(Candle(start + index * 900_000, price, close + .2, price - .2, close, 100 + index % 13, 10_000 + index))
        price = close
    return result


def comparable(snapshot):
    return asdict(snapshot)


def test_production_backtest_replay_are_field_exact():
    source = candles()
    as_of = source[-1].timestamp_ms + 900_000
    hourly = aggregate_candles(source, "15m", "1h", as_of)
    inputs = FeatureInputs()
    production = MarketFetcher.build_snapshot_from_inputs("BTCUSDT", source, hourly, as_of_timestamp_ms=as_of, inputs=inputs)
    backtest = BacktestEngine.__new__(BacktestEngine)._build_snapshot("BTCUSDT", source, as_of_timestamp_ms=as_of)
    replay = replay_snapshot("BTCUSDT", source, as_of_timestamp_ms=as_of)
    validation = validation_snapshot("BTCUSDT", source, as_of)
    assert comparable(production) == comparable(backtest) == comparable(replay) == comparable(validation)
    assert production.primary.closed_candle_timestamp_ms == source[-1].timestamp_ms
    assert production.confirmation.closed_candle_timestamp_ms + 3_600_000 <= as_of


def test_context_features_are_exact_and_never_synthesized():
    source = candles(); as_of = source[-1].timestamp_ms + 900_000
    hourly = aggregate_candles(source, "15m", "1h", as_of)
    inputs = FeatureInputs(spread_bps=2.75, htf_context={"htf_regime_1d": "bullish", "htf_regime_4h": "mixed", "htf_regime": "bullish"})
    left = build_market_snapshot("BTCUSDT", source, hourly, as_of_timestamp_ms=as_of, inputs=inputs)
    right = MarketFetcher.build_snapshot_from_inputs("BTCUSDT", source, hourly, as_of_timestamp_ms=as_of, inputs=inputs)
    assert comparable(left) == comparable(right)
    assert "spread_bps=2.750" in left.notes
    assert "htf_regime=bullish" in left.notes


@pytest.mark.parametrize("mutation", ["gap", "duplicate", "nan"])
def test_bad_candles_fail_closed(mutation):
    source = candles(80)
    if mutation == "gap": source.pop(10)
    elif mutation == "duplicate": source[10] = source[9]
    else: source[10].close = float("nan")
    with pytest.raises(CandleContractError):
        aggregate_candles(source, "15m", "1h", source[-1].timestamp_ms + 900_000)


def test_incomplete_hour_is_not_aggregated():
    source = candles(79)
    as_of = source[-1].timestamp_ms + 900_000
    hourly = aggregate_candles(source, "15m", "1h", as_of)
    assert all(c.timestamp_ms + 3_600_000 <= as_of for c in hourly)
    assert sum(c.volume_base for c in hourly) == sum(c.volume_base for c in source[:len(hourly) * 4])


def test_all_paths_have_identical_detector_outcomes():
    source = candles(); as_of = source[-1].timestamp_ms + 900_000
    hourly = aggregate_candles(source, "15m", "1h", as_of)
    snapshots = [
        MarketFetcher.build_snapshot_from_inputs("BTCUSDT", source, hourly, as_of_timestamp_ms=as_of),
        BacktestEngine.__new__(BacktestEngine)._build_snapshot("BTCUSDT", source, as_of_timestamp_ms=as_of),
        replay_snapshot("BTCUSDT", source, as_of_timestamp_ms=as_of), validation_snapshot("BTCUSDT", source, as_of),
    ]
    settings = Settings(_env_file=None)
    factories = [lambda: MomentumBreakoutStrategy(settings), lambda: MomentumBreakdownStrategy(settings), lambda: ContinuationStrategy(), lambda: LiquiditySweepStrategy(settings), lambda: LowVolReclaimStrategy()]
    for factory in factories:
        outcomes = []
        for snapshot in snapshots:
            candidate = factory().detect(snapshot)
            outcomes.append(None if candidate is None else (candidate.strategy, candidate.direction, candidate.detection.entry_hint))
        assert outcomes.count(outcomes[0]) == len(outcomes)


def test_active_candle_never_reaches_detector_snapshot():
    source = candles(); closed_at = source[-1].timestamp_ms + 900_000
    active = Candle(closed_at, source[-1].close, source[-1].close + 50, source[-1].close - 50, source[-1].close + 25, 999999)
    with_active = source + [active]
    hourly = aggregate_candles(with_active, "15m", "1h", closed_at)
    snapshot = build_market_snapshot("BTCUSDT", with_active, hourly, as_of_timestamp_ms=closed_at)
    assert snapshot.primary.candles[-1] == source[-1]
    assert active not in snapshot.primary.candles


def _contract():
    return ContractSpec("BTCUSDT", "USDT-FUTURES", "USDT", "BTC", "normal", .001, .001, 2, 1_000_000_000, 1.0, {})


def _orderbook():
    return {"symbol": "BTCUSDT", "mid_price": 120.0, "spread_bps": 2.5, "bids": [{"price": 119.9, "size": 500}], "asks": [{"price": 120.1, "size": 500}], "bid_depth_notional": 59_950, "ask_depth_notional": 60_050, "total_depth_notional": 120_000, "depth_imbalance": .1}


def test_real_production_path_calls_shared_builder_once(monkeypatch):
    closed = candles(); as_of = closed[-1].timestamp_ms + 900_000
    active = Candle(as_of, closed[-1].close, closed[-1].close + 1, closed[-1].close - 1, closed[-1].close + .5, 250)
    source = closed + [active]; hourly = aggregate_candles(source, "15m", "1h", as_of)
    fetcher = MarketFetcher.__new__(MarketFetcher)
    fetcher.settings = type("Settings", (), {"bitget_default_granularity": "15m", "bitget_confirmation_granularity": "1h"})()
    fetcher.log = type("Log", (), {"warning": lambda *a, **k: None, "debug": lambda *a, **k: None})()
    fetcher.client = type("Client", (), {"get_orderbook": lambda self, symbol, limit: _orderbook()})()
    fetcher.fetch_contract_meta = lambda symbol: _contract()
    fetcher.fetch_snapshot = lambda symbol, granularity, as_of_timestamp_ms: SymbolSnapshot(symbol, granularity, source if granularity == "15m" else hourly, {}, as_of_timestamp_ms)
    fetcher._fetch_with_retry = lambda **kwargs: kwargs["fetch_fn"]()
    fetcher._htf_regime_for = lambda symbol: {"htf_regime_1d": "bullish", "htf_regime_4h": "mixed", "htf_regime": "lean_bullish"}
    fetcher._persist_liquidity_heatmap = lambda symbol, payload: None
    import data.market_fetcher as module
    real_builder = module.build_unified_market_snapshot; calls = []
    def counted(*args, **kwargs): calls.append(1); return real_builder(*args, **kwargs)
    monkeypatch.setattr(module, "build_unified_market_snapshot", counted)
    production = fetcher.build_market_snapshot("BTCUSDT", as_of_timestamp_ms=as_of)
    expected = build_market_snapshot("BTCUSDT", source, hourly, as_of_timestamp_ms=as_of, inputs=LiveMarketContext(orderbook=_orderbook(), htf_context={"htf_regime_1d": "bullish", "htf_regime_4h": "mixed", "htf_regime": "lean_bullish"}, contract=_contract()))
    assert calls == [1]
    assert comparable(production) == comparable(expected)
    assert production.primary.candles[-1] == closed[-1]
    assert active not in production.primary.candles
    shared_inputs = LiveMarketContext(orderbook=_orderbook(), htf_context={"htf_regime_1d": "bullish", "htf_regime_4h": "mixed", "htf_regime": "lean_bullish"}, contract=_contract())
    peers = [BacktestEngine.__new__(BacktestEngine)._build_snapshot("BTCUSDT", source, as_of_timestamp_ms=as_of, inputs=shared_inputs), replay_snapshot("BTCUSDT", source, as_of_timestamp_ms=as_of, inputs=shared_inputs), validation_snapshot("BTCUSDT", source, as_of, inputs=shared_inputs)]
    assert all(comparable(peer) == comparable(production) for peer in peers)
    settings = Settings(_env_file=None)
    for factory in (lambda: MomentumBreakoutStrategy(settings), lambda: MomentumBreakdownStrategy(settings), lambda: ContinuationStrategy(), lambda: LiquiditySweepStrategy(settings), lambda: LowVolReclaimStrategy()):
        outcomes = []
        for item in [production, *peers]:
            candidate = factory().detect(item)
            outcomes.append(None if candidate is None else (candidate.strategy, candidate.direction, candidate.detection.entry_hint))
        assert outcomes.count(outcomes[0]) == len(outcomes)
    for key in ("orderbook", "liquidity", "entry_quality", "pressure", "structure", "risk_off", "instrument", "htf"):
        assert key in production.context
    live = production.context["live"]
    assert live.orderbook_context == production.context["orderbook"]
    assert live.entry_quality == production.context["entry_quality"]
    assert live.pressure == production.context["pressure"]
    assert live.structure == production.context["structure"]
    assert live.risk_off_flags["market_risk_off"] == production.context["risk_off"]


@pytest.mark.parametrize("delta,expected_offset", [(900_000, 0), (899_999, 1)])
def test_as_of_boundary_is_identical_for_all_paths(delta, expected_offset):
    source = candles(); active_open = source[-1].timestamp_ms + 900_000
    active = Candle(active_open, source[-1].close, source[-1].close + 1, source[-1].close - 1, source[-1].close + .5, 250)
    raw = source + [active]; as_of = active_open + delta
    hourly = aggregate_candles(raw, "15m", "1h", as_of)
    snapshots = [MarketFetcher.build_snapshot_from_inputs("BTCUSDT", raw, hourly, as_of_timestamp_ms=as_of), BacktestEngine.__new__(BacktestEngine)._build_snapshot("BTCUSDT", raw, as_of_timestamp_ms=as_of), replay_snapshot("BTCUSDT", raw, as_of_timestamp_ms=as_of), validation_snapshot("BTCUSDT", raw, as_of)]
    assert all(comparable(item) == comparable(snapshots[0]) for item in snapshots)
    assert snapshots[0].primary.candles[-1] == raw[-1 - expected_offset]


def test_closed_offsets_are_exact_with_active_candle():
    source = candles(); as_of = source[-1].timestamp_ms + 900_000
    active = Candle(as_of, source[-1].close, source[-1].close + 1, source[-1].close - 1, source[-1].close, 1)
    hourly = aggregate_candles(source + [active], "15m", "1h", as_of)
    snapshot = build_market_snapshot("BTCUSDT", source + [active], hourly, as_of_timestamp_ms=as_of, inputs=FeatureInputs(spread_bps=1.0))
    assert [closed_candle_at_offset(snapshot.primary, offset) for offset in range(3)] == [source[-1], source[-2], source[-3]]


def test_missing_spread_is_data_quality_failure(monkeypatch):
    source = candles(); as_of = source[-1].timestamp_ms + 900_000; hourly = aggregate_candles(source, "15m", "1h", as_of)
    snapshot = build_market_snapshot("BTCUSDT", source, hourly, as_of_timestamp_ms=as_of)
    strategy = LowVolReclaimStrategy(); reasons = []
    monkeypatch.setattr(strategy, "_reject", lambda market, reason, **context: reasons.append(reason))
    assert strategy.detect(snapshot) is None
    assert reasons == ["DATA_QUALITY_MISSING_SPREAD"]


@pytest.mark.parametrize("timeframe,step", [("15m", 900_000), ("1h", 3_600_000)])
def test_close_boundary_for_multiple_timeframes(timeframe, step):
    source = [Candle(index * step, 100, 101, 99, 100.5, 10) for index in range(20)]
    boundary = source[-1].timestamp_ms + step
    assert select_closed_candles(source, timeframe, boundary)[-1] == source[-1]
    assert select_closed_candles(source, timeframe, boundary - 1)[-1] == source[-2]


def test_momentum_long_short_scan_exactly_offsets_zero_one_two():
    source = (ROOT := __import__("pathlib").Path(__file__).resolve().parents[1] / "strategies/momentum_breakout.py").read_text()
    assert source.count("for offset in range(1, 4):") == 2
    assert source.count("closed_candle_at_offset(market.primary, offset - 1)") == 2
