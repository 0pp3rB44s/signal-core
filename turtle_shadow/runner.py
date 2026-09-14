"""One scan cycle for the turtle_long_strong_breakout_v1 prospective shadow.

PROSPECTIVE_FREEZE_2026-09-14: read-only market data + local JSONL ledgers
only. This module never places, amends, or cancels an exchange order --
there is no code path to any order-transport client method anywhere below,
only BitgetRestClient.get_candles() (public market data) and
BitgetPrecisionMixin.validate_entry_size()/_min_notional() (read-only
contract metadata, mixed into BitgetRestClient already). Intended to be
invoked periodically (see scripts/start_turtle_shadow.sh); each invocation
is a complete, idempotent scan: fetch fresh candles, advance open positions,
evaluate new signals, persist state.

TURTLE_EDGE_PROVEN=NO. STRATEGY_FROZEN=YES for the duration of the
prospective epoch -- nothing in this file may change a threshold, lookback,
or gate based on observed shadow results.

REALISTIC ledger semantics: a signal's real-account executability is decided
ONCE, at signal time, against the real ~$27.44 NAV and live exchange
minimums -- and, per the strategy's own author (Agent C), is expected to be
False for essentially every signal. A non-executable signal gets its
REALISTIC row immediately (SKIPPED, no fabricated fill) and is not tracked
further on the realistic side. An (currently unreachable, but structurally
supported for when/if the real NAV grows) executable signal is tracked
through to its real exit before its REALISTIC row is written.

NORMALIZED ledger semantics: every otherwise-valid signal (strategy gates
passed) is tracked through to its causal exit and recorded with its
hypothetical result_R, regardless of real-account executability -- this is
the only ledger meant to accumulate prospective edge evidence.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict

from app.config import get_settings
from clients.bitget_rest import BitgetRestClient

from turtle_shadow.strategy import (
    Candle, TurtleSignal, sma, wilder_atr, wilder_adx, detect_turtle_signal, simulate_trade,
    SMA_TREND_PERIOD, ATR_PERIOD, ADX_PERIOD,
)
from turtle_shadow.sizing import compute_risk_sized_position, ENABLED_SYMBOLS
from turtle_shadow.ledger import TurtleShadowLedgers, RealisticLedgerRow, NormalizedLedgerRow

STATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state", "turtle_shadow")
STATE_PATH = os.path.join(STATE_DIR, "turtle_shadow_state.json")
CANDLE_LIMIT = 320  # >= SMA200 warmup + a comfortable margin
GRANULARITY = "1D"
DAY_MS = 24 * 3600 * 1000

logger = logging.getLogger("turtle_shadow")


def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"open_signals": {}}
    with open(STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, STATE_PATH)


def _fetch_completed_candles(client: BitgetRestClient, settings, symbol: str) -> list[Candle]:
    """Signal-detection candle feed: Binance public 1D klines, the same
    source Agent C's validated research and this module's own historical
    reproduction test (test_reproduces_agent_c_xlmusdt_boundary_trade) were
    checked against. Bitget's own /mix/market/candles endpoint caps out at
    ~90 daily bars with no pagination support in the shared client wrapper
    (clients/bitget_market_client.py), well short of the 200+ needed for
    SMA200 warmup -- and modifying that shared wrapper to add pagination
    would be exactly the kind of unrelated production-code change this
    mission forbids. Bitget is still used, live and read-only, for
    everything account-specific: contract metadata / minimum-notional
    sizing (see turtle_shadow/sizing.py), which is what actually needs to
    reflect the real exchange this account trades on."""
    import json as _json
    from urllib.request import urlopen, Request

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - (CANDLE_LIMIT + 5) * DAY_MS
    out: list[Candle] = []
    cursor = start_ms
    while cursor < end_ms:
        url = (f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1d"
               f"&startTime={cursor}&endTime={end_ms}&limit=1500")
        req = Request(url, headers={"User-Agent": "turtle-shadow/1.0"})
        with urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read())
        if not data:
            break
        for r in data:
            out.append(Candle(timestamp_ms=int(r[0]), open=float(r[1]), high=float(r[2]),
                               low=float(r[3]), close=float(r[4])))
        last_open = int(data[-1][0])
        if last_open <= cursor:
            break
        cursor = last_open + DAY_MS
        time.sleep(0.15)

    out.sort(key=lambda c: c.timestamp_ms)
    now_ms = int(time.time() * 1000)
    # causal guard: never treat a still-forming candle as completed
    return [c for c in out if c.timestamp_ms + DAY_MS <= now_ms]


def run_one_scan() -> dict:
    settings = get_settings()
    client = BitgetRestClient(settings)  # also the BitgetPrecisionMixin instance -- read-only here
    ledgers = TurtleShadowLedgers(STATE_DIR)
    state = _load_state()
    open_signals: dict = state.setdefault("open_signals", {})

    summary = {"symbols_scanned": 0, "new_signals": 0, "trades_closed": 0, "realistic_skipped_at_signal": 0, "errors": []}

    for symbol in ENABLED_SYMBOLS:
        try:
            candles = _fetch_completed_candles(client, settings, symbol)
        except Exception as exc:
            logger.warning("TURTLE_CANDLE_FETCH_FAILED | %s | error=%s", symbol, exc)
            summary["errors"].append(f"{symbol}: fetch_failed: {exc}")
            continue

        if len(candles) < SMA_TREND_PERIOD + 5:
            continue
        summary["symbols_scanned"] += 1

        entry = open_signals.get(symbol)
        if entry is not None:
            sig = TurtleSignal(**entry["signal"])
            trade = simulate_trade(candles, sig)
            if trade is not None:
                ledgers.append_normalized(NormalizedLedgerRow(
                    trade_id=f"turtle_{symbol}_{sig.signal_timestamp_ms}",
                    symbol=symbol, signal_timestamp_ms=sig.signal_timestamp_ms,
                    breakout_dist_atr=sig.breakout_dist_atr, entry_price=sig.entry_price,
                    initial_stop=sig.initial_stop, exit_timestamp_ms=trade.exit_timestamp_ms,
                    exit_price=trade.exit_price, exit_reason=trade.exit_reason, result_r=trade.result_r,
                ))
                if entry.get("realistic_pending"):
                    sizing = entry["sizing"]
                    realized_pnl = sizing["risk_sized_notional"] * trade.result_r
                    ledgers.append_realistic(RealisticLedgerRow(
                        trade_id=f"turtle_{symbol}_{sig.signal_timestamp_ms}",
                        symbol=symbol, signal_timestamp_ms=sig.signal_timestamp_ms,
                        entry_price=sig.entry_price, initial_stop=sig.initial_stop,
                        risk_usdt=sizing["risk_usdt"], risk_sized_notional=sizing["risk_sized_notional"],
                        exchange_min_notional_or_min_order=sizing["exchange_min_notional_or_min_order"],
                        executed=True, skip_reason=None,
                        exit_timestamp_ms=trade.exit_timestamp_ms, exit_price=trade.exit_price,
                        exit_reason=trade.exit_reason, realized_pnl_usdt=realized_pnl, result_r=trade.result_r,
                    ))
                del open_signals[symbol]
                summary["trades_closed"] += 1
            continue

        i = len(candles) - 1
        closes = [c.close for c in candles]
        sma200 = sma(closes, SMA_TREND_PERIOD)
        atr20 = wilder_atr(candles, ATR_PERIOD)
        adx14 = wilder_adx(candles, ADX_PERIOD)
        sig = detect_turtle_signal(candles, i, sma200, atr20, adx14)
        if sig is None:
            continue

        summary["new_signals"] += 1
        logger.info("TURTLE_SIGNAL_DETECTED | %s | breakout_dist_atr=%.4f | entry=%.8f | stop=%.8f",
                     symbol, sig.breakout_dist_atr, sig.entry_price, sig.initial_stop)

        sizing = compute_risk_sized_position(symbol, sig.entry_price, sig.initial_stop, precision_mixin=client)
        sizing_dict = {
            "risk_usdt": sizing.risk_usdt, "risk_sized_notional": sizing.risk_sized_notional,
            "exchange_min_notional_or_min_order": sizing.exchange_min_notional_or_min_order,
        }

        if not sizing.exchange_executable:
            ledgers.append_realistic(RealisticLedgerRow(
                trade_id=f"turtle_{symbol}_{sig.signal_timestamp_ms}",
                symbol=symbol, signal_timestamp_ms=sig.signal_timestamp_ms,
                entry_price=sig.entry_price, initial_stop=sig.initial_stop,
                risk_usdt=sizing.risk_usdt, risk_sized_notional=sizing.risk_sized_notional,
                exchange_min_notional_or_min_order=sizing.exchange_min_notional_or_min_order,
                executed=False, skip_reason=sizing.skip_reason,
            ))
            summary["realistic_skipped_at_signal"] += 1
            open_signals[symbol] = {"signal": asdict(sig), "realistic_pending": False, "sizing": sizing_dict}
        else:
            open_signals[symbol] = {"signal": asdict(sig), "realistic_pending": True, "sizing": sizing_dict}

    _save_state(state)
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    result = run_one_scan()
    logger.info("TURTLE_SCAN_COMPLETE | %s", json.dumps(result, default=str))
