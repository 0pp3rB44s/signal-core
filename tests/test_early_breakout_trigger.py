from types import SimpleNamespace

from clients.schemas import Candle, MarketSnapshot, TimeframeSnapshot, ContractSpec
from strategies.early_breakout_trigger import EarlyBreakoutTrigger, candles_from_cache_rows


def _settings(**overrides):
    base = dict(
        early_trigger_1m_enabled=True,
        early_trigger_5m_confirm_enabled=True,
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
    return [_candle(price, o=price, high=price + 0.05, low=price - 0.05, vol=vol, ts=i) for i in range(n)]


def _confirm_5m(direction="LONG", price=100.0):
    # 21 rising 5m candles closing up, so EMA sits below the last close (LONG).
    candles = []
    for i in range(21):
        p = price - (21 - i) * 0.01 if direction == "LONG" else price + (21 - i) * 0.01
        if direction == "LONG":
            candles.append(_candle(p + 0.02, o=p, high=p + 0.03, low=p - 0.01, vol=2.0, ts=i))
        else:
            candles.append(_candle(p - 0.02, o=p, high=p + 0.01, low=p - 0.03, vol=2.0, ts=i))
    return candles


def _tf(trend, candles, granularity, volume_ratio_20=1.0):
    return TimeframeSnapshot(
        symbol="TESTUSDT", granularity=granularity, latest_close=candles[-1].close,
        change_pct=0.0, range_pct=1.0, volume_ratio_20=volume_ratio_20,
        ema20=candles[-1].close, ema50=candles[-1].close, trend=trend, candles=candles,
    )


def _snapshot(candles_1m, candles_5m=None, *, alignment="aligned_bullish", primary_trend="bullish", confirmation_trend="bullish"):
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
        context={"candles_1m": candles_1m, "candles_5m": candles_5m},
    )


def _breakout_1m():
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(100.30, o=100.05, high=100.32, low=100.04, vol=4.0, ts=99))
    return candles


def test_detects_genuine_long_breakout_with_5m_confirm():
    trig = EarlyBreakoutTrigger(_settings())
    candidate = trig.detect(_snapshot(_breakout_1m(), _confirm_5m("LONG")))
    assert candidate is not None
    assert candidate.strategy == "momentum_breakout"
    assert candidate.direction == "LONG"
    assert any("entry_trigger=1m_early" in n for n in candidate.notes)
    assert any("early_trigger_probe=true" in n for n in candidate.notes)
    assert any("5m_confirmed=true" in n for n in candidate.notes)
    assert candidate.detection.invalidation < candidate.detection.entry_hint


def test_5m_disagreement_blocks():
    # 1m breaks out long, but the last 5m candle is red / below its EMA.
    trig = EarlyBreakoutTrigger(_settings())
    bad_5m = _confirm_5m("LONG")
    bad_5m[-1] = _candle(99.5, o=100.2, high=100.25, low=99.4, vol=2.0, ts=99)  # red 5m close below EMA
    assert trig.detect(_snapshot(_breakout_1m(), bad_5m)) is None


def test_5m_confirm_fail_open_when_missing():
    # No 5m data -> confirmation is skipped (fail-open), trigger still fires.
    trig = EarlyBreakoutTrigger(_settings())
    assert trig.detect(_snapshot(_breakout_1m(), None)) is not None


def test_5m_confirm_disabled_via_flag():
    trig = EarlyBreakoutTrigger(_settings(early_trigger_5m_confirm_enabled=False))
    bad_5m = _confirm_5m("LONG")
    bad_5m[-1] = _candle(99.5, o=100.2, high=100.25, low=99.4, vol=2.0, ts=99)
    assert trig.detect(_snapshot(_breakout_1m(), bad_5m)) is not None


def test_flag_off_returns_none():
    trig = EarlyBreakoutTrigger(_settings(early_trigger_1m_enabled=False))
    assert trig.detect(_snapshot(_breakout_1m(), _confirm_5m("LONG"))) is None


def test_weak_volume_rejected():
    trig = EarlyBreakoutTrigger(_settings())
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(100.30, o=100.05, high=100.32, low=100.04, vol=1.1, ts=99))
    assert trig.detect(_snapshot(candles, _confirm_5m("LONG"))) is None


def test_wick_breakout_rejected():
    trig = EarlyBreakoutTrigger(_settings())
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(100.02, o=100.00, high=100.40, low=99.98, vol=4.0, ts=99))
    assert trig.detect(_snapshot(candles, _confirm_5m("LONG"))) is None


def test_counter_trend_breakout_rejected():
    trig = EarlyBreakoutTrigger(_settings())
    snap = _snapshot(_breakout_1m(), _confirm_5m("LONG"), alignment="aligned_bearish", primary_trend="bearish", confirmation_trend="bearish")
    assert trig.detect(snap) is None


def test_insufficient_candles_returns_none():
    trig = EarlyBreakoutTrigger(_settings())
    assert trig.detect(_snapshot(_base_1m(5), _confirm_5m("LONG"))) is None


def test_detects_short_breakdown():
    trig = EarlyBreakoutTrigger(_settings())
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(99.70, o=99.95, high=99.96, low=99.68, vol=4.0, ts=99))
    snap = _snapshot(candles, _confirm_5m("SHORT"), alignment="aligned_bearish", primary_trend="bearish", confirmation_trend="bearish")
    candidate = trig.detect(snap)
    assert candidate is not None
    assert candidate.strategy == "momentum_breakdown"
    assert candidate.direction == "SHORT"
    assert candidate.detection.invalidation > candidate.detection.entry_hint


def test_extended_breakout_rejected():
    trig = EarlyBreakoutTrigger(_settings(early_trigger_1m_max_displacement_pct=0.5))
    candles = _base_1m(24, price=100.0, vol=1.0)
    candles.append(_candle(102.0, o=100.05, high=102.1, low=100.04, vol=4.0, ts=99))
    assert trig.detect(_snapshot(candles, _confirm_5m("LONG"))) is None


def test_cache_rows_to_candles_sorts_and_parses():
    rows = [
        {"timestamp": 3000, "open": 3, "high": 4, "low": 2, "close": 3.5, "volume": 10},
        {"timestamp": 1000, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 5},
        {"timestamp": 2000, "open": 2, "high": 3, "low": 1.5, "close": 2.5, "volume": 7},
        {"bad": "row"},  # skipped
    ]
    candles = candles_from_cache_rows(rows)
    assert [c.timestamp_ms for c in candles] == [1000, 2000, 3000]  # sorted ascending
    assert candles[-1].close == 3.5
    assert candles[0].volume_base == 5.0


def test_cache_rows_empty_returns_empty():
    assert candles_from_cache_rows(None) == []
    assert candles_from_cache_rows([]) == []
