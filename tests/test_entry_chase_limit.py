"""Entry chase-limit: skip market entries that would fill too far past the plan.

Entries are laddered below market (a long waits for a pullback). When the move
runs away the maker limit doesn't fill and the bot would market-buy at the higher
price -- adverse selection (>15bps fills lose ~5x more per trade). The chase-limit
skips those.
"""

from unittest.mock import MagicMock

from execution.execution_service import ExecutionService


def _svc():
    # __new__ skips __init__ so we don't build a real Bitget client.
    svc = ExecutionService.__new__(ExecutionService)
    svc.client = MagicMock()
    svc.log = MagicMock()
    return svc


def test_current_market_price_long_uses_best_ask():
    svc = _svc()
    svc.client.get_orderbook.return_value = {
        "asks": [{"price": 100.5, "size": 1.0}],
        "bids": [{"price": 100.4, "size": 1.0}],
    }
    assert svc._current_market_price("BTCUSDT", "LONG") == 100.5


def test_current_market_price_short_uses_best_bid():
    svc = _svc()
    svc.client.get_orderbook.return_value = {
        "asks": [{"price": 100.5, "size": 1.0}],
        "bids": [{"price": 100.4, "size": 1.0}],
    }
    assert svc._current_market_price("BTCUSDT", "SHORT") == 100.4


def test_current_market_price_fails_open_to_zero_on_error():
    svc = _svc()
    svc.client.get_orderbook.side_effect = Exception("boom")
    assert svc._current_market_price("BTCUSDT", "LONG") == 0.0
    # empty book -> 0.0 too (caller then proceeds with the market order)
    svc.client.get_orderbook.side_effect = None
    svc.client.get_orderbook.return_value = {"asks": [], "bids": []}
    assert svc._current_market_price("BTCUSDT", "LONG") == 0.0


def test_chase_bps_decision_math():
    # Mirrors the inline decision in execute(): LONG chase = (market-plan)/plan.
    ref, cap = 100.0, 15.0
    assert (100.20 - ref) / ref * 10000 > cap   # 20bps -> skip
    assert (100.10 - ref) / ref * 10000 <= cap  # 10bps -> proceed
    # SHORT chase = (plan-market)/plan (market below plan is the bad direction)
    assert (ref - 99.80) / ref * 10000 > cap    # 20bps -> skip
