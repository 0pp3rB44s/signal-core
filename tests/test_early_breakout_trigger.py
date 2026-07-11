from types import SimpleNamespace

from clients.schemas import Candle, MarketSnapshot, TimeframeSnapshot, ContractSpec
from strategies.early_breakout_trigger import EarlyBreakoutTrigger


def _settings(**overrides):
    base = dict(
        early_trigger_1m_enabled=True,
        early_trigger_1m_granularity="1m",
        early_trigger_1m_lookback=20,
        early_trigger_1m_min_volume_ratio=2.0,
        early_trigger_1m_min_body_pct=0.5,
        early_trigger_1m_max_displacement_pct=0.5,
        early_trigger_1m_structural_stop_lookback_15m=4,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _candle(close, *, o=None, high=None, low=None, vol=1.0, ts=0):
    o = close if o is None else o
    high = max(o, close) if high is None else high
    low = min(o, close) if low is None else low
    return Candle(timestamp_ms=ts, open=o, high=high, low=low, close=close, volume_base=vol, volume_quote=None)


def _base_1m(n=25, price=100.0, vol=1.0):
    # Flat range around `price`, tight bodies, baseline volume.
    return [_candle(price, o=price, high=price + 0.05, low=price - 0.05, vol=vol, ts=i) for i in range(n)]


def _tf(trend, candles, granularity, volume_ratio_20=1.0):
    return TimeframeSnapshot(
        symbol="TESTUSDT",
        granularity=granularity,
        latest_close=candles[-1].close,
        change_pct=0.0,
        range_pct=1.0,
        volume_ratio_20=volume_ratio_20,
        ema20=candles[-1].close,
        ema50=candles[-1].close,
        trend=trend,
        candles=candles,
    )


def _snapshot(candles_1m, *, alignment="aligned_bullish", primary_trend="bullish", confirmation_trend="bullish"):
    c15 = [_candle(100.0, o=99.9, high=100.2, low=99.5, vol=5.0, ts=i) for i in range(30)]
    primary = _tf(primary_trend, c15, "15m", volume_ratio_20=1.0)
    confirmation = _tf(confirmation_trend, c15, "1H")
    contract = ContractSpec(
        symbol="TESTUSDT", product_type="USDT-FUTURES", quote_coin="USDT", base_coin="TEST",
        status="normal", min_trade_num=0.1, size_multiplier=1.0, price_place=2,
        volume_24h_usdt=1e6, change_pct_24h=1.0, raw={},
    )
    return MarketSnapshot(
        symbol="TESTUSDT", contract=contract, primary=primary, confirmation=confirmation,
        alignment=alignment, score_hint=60.0, notes=[], volatility_rank=20.0,
        context={"candles_1m": candles_1m},
    )


def _breakout_1m():
    # 24 flat candles at 100, then a strong volume+body breakout candle above 100.05.
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(100.30, o=100.05, high=100.32, low=100.04, vol=4.0, ts=99))
    return candles


def test_detects_genuine_long_breakout():
    trig = EarlyBreakoutTrigger(_settings())
    candidate = trig.detect(_snapshot(_breakout_1m()))
    assert candidate is not None
    assert candidate.strategy == "momentum_breakout"
    assert candidate.direction == "LONG"
    assert any("entry_trigger=1m_early" in n for n in candidate.notes)
    # structural stop must sit below entry
    assert candidate.detection.invalidation < candidate.detection.entry_hint


def test_flag_off_returns_none():
    trig = EarlyBreakoutTrigger(_settings(early_trigger_1m_enabled=False))
    assert trig.detect(_snapshot(_breakout_1m())) is None


def test_weak_volume_rejected():
    trig = EarlyBreakoutTrigger(_settings())
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(100.30, o=100.05, high=100.32, low=100.04, vol=1.1, ts=99))  # vol_ratio ~1.1 < 2.0
    assert trig.detect(_snapshot(candles)) is None


def test_wick_breakout_rejected():
    # Price wicks above the range but closes back inside with a tiny body.
    trig = EarlyBreakoutTrigger(_settings())
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(100.02, o=100.00, high=100.40, low=99.98, vol=4.0, ts=99))  # close 100.02 < resistance 100.05
    assert trig.detect(_snapshot(candles)) is None


def test_small_body_rejected():
    trig = EarlyBreakoutTrigger(_settings())
    candles = _base_1m(24, price=100.0, vol=1.0)
    # closes above range but body is a small fraction of a large range (indecision)
    candles.append(_candle(100.30, o=100.28, high=101.30, low=100.05, vol=4.0, ts=99))
    assert trig.detect(_snapshot(candles)) is None


def test_counter_trend_breakout_rejected():
    trig = EarlyBreakoutTrigger(_settings())
    snap = _snapshot(_breakout_1m(), alignment="aligned_bearish", primary_trend="bearish", confirmation_trend="bearish")
    assert trig.detect(snap) is None


def test_insufficient_candles_returns_none():
    trig = EarlyBreakoutTrigger(_settings())
    assert trig.detect(_snapshot(_base_1m(5))) is None


def test_no_1m_candles_returns_none():
    trig = EarlyBreakoutTrigger(_settings())
    assert trig.detect(_snapshot(None)) is None


def test_detects_short_breakdown():
    trig = EarlyBreakoutTrigger(_settings())
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(99.70, o=99.95, high=99.96, low=99.68, vol=4.0, ts=99))  # closes below support 99.95
    snap = _snapshot(candles, alignment="aligned_bearish", primary_trend="bearish", confirmation_trend="bearish")
    candidate = trig.detect(snap)
    assert candidate is not None
    assert candidate.strategy == "momentum_breakdown"
    assert candidate.direction == "SHORT"
    assert candidate.detection.invalidation > candidate.detection.entry_hint


def test_extended_breakout_rejected():
    # Already ran far past the level (displacement > max) -> not fresh, skip.
    trig = EarlyBreakoutTrigger(_settings(early_trigger_1m_max_displacement_pct=0.5))
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(102.0, o=100.05, high=102.1, low=100.04, vol=4.0, ts=99))  # +1.95% past resistance
    assert trig.detect(_snapshot(candles)) is None
