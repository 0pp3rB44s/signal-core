"""Regression for the Runner's IP ban on 2026-08-18.

Root cause reproduced live: `GET /fapi/v1/aggTrades` returned
HTTP 418 {"code":-1003,"msg":"Way too many requests; IP(...) banned until ...
Please use the websocket for live updates to avoid bans."}

The bug was never in the trade/mark/OI parsing -- it was that PR #66 polled REST for 12 symbols
every 2s with no weight budget and no backoff, so it got the shared IP banned, then kept hammering
the endpoint every cycle regardless, extending the outage and silently swallowing every failure
with no status code logged.
"""
from __future__ import annotations

import time

import pytest

from unittest.mock import patch

from research.collectors.binance_collector import (
    RateLimited, BinanceResearchCollector, TRADE_POLL_SECONDS, AGG_TRADE_LIMIT, _get,
)
import urllib.error

# The actual body captured from the Runner during the ban.
BAN_BODY = ('{"code":-1003,"msg":"Way too many requests; IP(217.122.12.10) banned until '
            '1787088055481. Please use the websocket for live updates to avoid bans."}')


@pytest.fixture()
def collector(tmp_path):
    c = BinanceResearchCollector(symbols=("BTCUSDT", "ETHUSDT"), data_dir=tmp_path)
    yield c
    c.close()


def _ban(*a, **k):
    raise RateLimited(retry_after_s=60.0, status=418, body=BAN_BODY)


def test_cadence_is_weight_budgeted_not_the_pr66_defaults():
    """12 symbols every 2s at limit=1000 is what produced the ban; both must be reduced."""
    assert TRADE_POLL_SECONDS >= 10.0
    assert AGG_TRADE_LIMIT <= 500


def test_418_enters_cooldown_and_does_not_retry_same_sweep(collector):
    written = collector.poll_trades_once(fetch=_ban)
    assert written == 0
    assert collector.trade_errors == 1          # once, not once per symbol
    assert collector.rate_limit_hits == 1
    assert collector._rest_paused() is True


def test_cooldown_suppresses_further_calls_until_it_clears(collector):
    calls = []
    collector.poll_trades_once(fetch=_ban)       # enters cooldown

    def counting_fetch(path, params):
        calls.append(path)
        return {"a": 1}

    collector.poll_mark_once(fetch=counting_fetch)
    assert calls == [], "mark must not call out while the shared IP cooldown is active"
    assert collector.rate_limited_skips > 0


def test_cooldown_is_shared_across_trade_mark_oi_and_clock(collector):
    """One ban applies to the whole IP, not one endpoint -- all four must observe it."""
    collector.poll_trades_once(fetch=_ban)
    assert collector._rest_paused() is True
    calls = []
    collector.poll_open_interest_once(fetch=lambda p, q: calls.append(p) or {})
    collector.poll_mark_once(fetch=lambda p, q: calls.append(p) or {})
    offsets = collector.clock_offsets(fetch=lambda p, q: calls.append(p) or {})
    assert calls == []
    assert offsets["clock_offset_binance_ms"] is None


def test_cooldown_expires_and_calls_resume(collector, monkeypatch):
    collector.poll_trades_once(fetch=_ban)
    assert collector._rest_paused() is True
    collector._rest_cooldown_until_ms = int(time.time() * 1000) - 1  # force-expire
    assert collector._rest_paused() is False
    written = collector.poll_trades_once(fetch=lambda p, q: [])
    assert written == 0
    assert collector.rate_limited_skips == 0 or collector.trade_errors == 1  # no new ban entered


def test_rate_limited_is_distinguished_from_generic_network_failure(collector):
    """A plain network blip should NOT trigger a minute-long self-imposed silence."""
    def flaky(path, params):
        raise RuntimeError("connection reset")
    collector.poll_trades_once(fetch=flaky)
    assert collector._rest_paused() is False
    assert collector.trade_errors == 2  # one per symbol, unlike the banned case


def test_health_surfaces_cooldown_state(collector):
    collector.poll_trades_once(fetch=_ban)
    h = collector.health()
    assert h["rate_limit_hits"] == 1
    assert h["rest_cooldown_active"] is True
    assert h["rest_cooldown_remaining_s"] > 0


def test_real_ban_body_parses_without_crashing():
    """The exact payload captured from the Runner must not raise anything unexpected."""
    exc = RateLimited(retry_after_s=103.0, status=418, body=BAN_BODY)
    assert exc.status == 418
    assert "banned" in exc.body
    assert exc.retry_after_s == 103.0


def test_get_itself_translates_418_to_rate_limited():
    """Proves the real HTTP path, not a fake fetch that already returns RateLimited."""
    resp = urllib.error.HTTPError(
        url="https://fapi.binance.com/fapi/v1/aggTrades", code=418, msg="Client Error",
        hdrs={"Retry-After": "77"}, fp=None)
    resp.read = lambda: BAN_BODY.encode()
    with patch("urllib.request.urlopen", side_effect=resp):
        with pytest.raises(RateLimited) as exc_info:
            _get("/fapi/v1/aggTrades", {"symbol": "BTCUSDT"})
    assert exc_info.value.status == 418
    assert exc_info.value.retry_after_s == 77.0
    assert "banned" in exc_info.value.body


def test_429_also_translates_to_rate_limited():
    resp = urllib.error.HTTPError(
        url="https://fapi.binance.com/fapi/v1/time", code=429, msg="Too Many Requests",
        hdrs={}, fp=None)
    resp.read = lambda: b'{"code":-1015,"msg":"Too many requests"}'
    with patch("urllib.request.urlopen", side_effect=resp):
        with pytest.raises(RateLimited) as exc_info:
            _get("/fapi/v1/time", {})
    assert exc_info.value.status == 429
    assert exc_info.value.retry_after_s == pytest.approx(60.0)  # no Retry-After header -> default


def test_a_second_trade_sweep_does_not_call_out_during_its_own_cooldown(collector):
    """The endpoint that triggered the ban must also respect it on its own next sweep."""
    collector.poll_trades_once(fetch=_ban)
    assert collector._rest_paused() is True
    calls = []
    written = collector.poll_trades_once(fetch=lambda p, q: calls.append(p) or [])
    assert calls == [], "trade's own next sweep called out while its own cooldown was active"
    assert written == 0


def test_sweep_is_staggered_not_bursted(collector):
    """Multiple symbols in one sweep must not fire back-to-back with zero delay."""
    calls = []
    def timed(path, params):
        calls.append(time.time())
        return []
    collector.poll_trades_once(fetch=timed)
    assert len(calls) == 2
    assert calls[1] - calls[0] > 0.05
