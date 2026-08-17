"""One replay of the frozen TrendFlow V1 spec on real Bitget candles. RESEARCH ONLY.

This is a sanity check, not a search. No parameter is adjusted on the basis of what it shows.
Its only job is to reveal an economically absurd implementation — runaway entries, broken sizing,
stops that never arm, fees applied wrongly, lifecycle corruption.
"""
from __future__ import annotations
import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.trendflow_v1 import (Candle, CostModel, TrendFlowSpec, atr, cost_gate,
                                     ema, evaluate, size_position, DataQualityError)
from strategies.trendflow_lifecycle import TargetTracker, LifecycleError

REST = "https://api.bitget.com"
SPEC = TrendFlowSpec()
TAKER_BPS, MAKER_BPS = 6.0, 2.0     # authenticated 2026-08-17
EQUITY = 10_000.0


#: Bitget caps history-candles at 200 rows per request (300+ returns HTTP 400), so depth comes
#: from paging backwards on endTime rather than from a bigger limit.
PAGE = 200


def fetch(symbol: str, granularity: str, pages: int = 8) -> list[Candle]:
    step = {"1H": 3_600_000, "4H": 14_400_000}[granularity]
    seen: dict[int, Candle] = {}
    end = None
    for _ in range(pages):
        url = (f"{REST}/api/v2/mix/market/history-candles?symbol={symbol}"
               f"&productType=USDT-FUTURES&granularity={granularity}&limit={PAGE}")
        if end is not None:
            url += f"&endTime={end}"
        with urllib.request.urlopen(url, timeout=20) as r:
            rows = json.loads(r.read()).get("data") or []
        if not rows:
            break
        for row in rows:
            t = int(row[0])
            seen[t] = Candle(t, t + step, float(row[1]), float(row[2]), float(row[3]),
                             float(row[4]), float(row[5]), closed=True)
        end = min(int(row[0]) for row in rows)
    return [seen[k] for k in sorted(seen)]


def replay(symbol: str) -> list[dict]:
    c1 = fetch(symbol, "1H"); c4 = fetch(symbol, "4H")
    if len(c1) < SPEC.min_1h_candles + 50:
        return []
    trades, open_pos = [], None
    for i in range(SPEC.min_1h_candles, len(c1)):
        w1 = c1[:i + 1]
        now = w1[-1].close_time_ms
        w4 = [c for c in c4 if c.close_time_ms <= now]
        if len(w4) < SPEC.ema_slow + 1:
            continue
        bar = w1[-1]

        if open_pos is not None:
            t: TargetTracker = open_pos["tracker"]
            # Post-entry observations only: high then low (order-agnostic for stops we take the
            # conservative one), each stamped inside this bar and strictly after the fill.
            for px, off in ((bar.high, 1), (bar.low, 2)):
                ts = max(bar.close_time_ms + off, t.last_observation_ms + 1, t.fill_timestamp_ms + off)
                try:
                    t.observe(price=px, timestamp_ms=ts, atr14=open_pos["atr"])
                except LifecycleError:
                    pass
            e20 = ema([c.close for c in w1], SPEC.ema_fast)[-1]
            exit_reason = None
            if t._reached_stop(bar.low if t.direction == "LONG" else bar.high):
                exit_reason, exit_px = "STOP", t.stop_price
            elif t.trend_invalidated(closed_1h_close=bar.close, ema20_1h=e20):
                exit_reason, exit_px = "TREND_INVALIDATION", bar.close
            elif t.max_hold_exceeded(bar.close_time_ms):
                exit_reason, exit_px = "MAX_HOLD_72H", bar.close
            if exit_reason:
                sign = 1 if t.direction == "LONG" else -1
                gross = sign * (exit_px - t.entry_price) * open_pos["qty"]
                notional = t.entry_price * open_pos["qty"]
                fees = notional * (TAKER_BPS / 10_000.0) * 2
                trades.append(dict(symbol=symbol, direction=t.direction, entry=t.entry_price,
                                   exit=exit_px, qty=open_pos["qty"], gross=gross, fees=fees,
                                   net=gross - fees, reason=exit_reason,
                                   hold_h=(bar.close_time_ms - t.fill_timestamp_ms) / 3_600_000,
                                   tp1=t.tp1_hit, tp2=t.tp2_hit))
                open_pos = None
            else:
                continue

        if open_pos is not None:
            continue
        try:
            sig = evaluate(w1, w4, SPEC)
        except DataQualityError:
            continue
        if not sig.is_entry:
            continue
        a14 = sig.values["atr14_1h"]
        # Conservative opportunity proxy: the 1R move the stop defines.
        opportunity_bps = (SPEC.stop_atr_multiple * a14) / bar.close * 10_000.0
        cost = CostModel(TAKER_BPS, MAKER_BPS, spread_bps=1.0, slippage_bps=1.0)
        ok, _ = cost_gate(expected_opportunity_bps=opportunity_bps, cost=cost, spec=SPEC)
        if not ok:
            continue
        entry = bar.close
        sz = size_position(direction=sig.direction, entry_price=entry, atr14=a14,
                           equity_usdt=EQUITY, spec=SPEC, min_qty=0.0001,
                           qty_step=0.0001, min_notional=5.0)
        if sz.refused_reason:
            continue
        fill_ms = bar.close_time_ms
        open_pos = dict(qty=sz.quantity, atr=a14,
                        tracker=TargetTracker(direction=sig.direction, entry_price=entry,
                                              fill_timestamp_ms=fill_ms,
                                              initial_stop=sz.stop_price, spec=SPEC))
    return trades


def main() -> None:
    all_trades = []
    for symbol in SPEC.symbols:
        try:
            t = replay(symbol)
            all_trades += t
            print(f"  {symbol}: {len(t)} trades")
        except Exception as exc:
            print(f"  {symbol}: FETCH/REPLAY ERROR {type(exc).__name__}: {exc}")
    if not all_trades:
        print("\nNO TRADES — cannot assess"); return
    n = len(all_trades)
    gross = sum(t["gross"] for t in all_trades); fees = sum(t["fees"] for t in all_trades)
    net = gross - fees
    wins = [t for t in all_trades if t["net"] > 0]; losses = [t for t in all_trades if t["net"] <= 0]
    gp = sum(t["net"] for t in wins); gl = abs(sum(t["net"] for t in losses))
    eq, peak, dd = EQUITY, EQUITY, 0.0
    for t in sorted(all_trades, key=lambda x: x["hold_h"]):
        eq += t["net"]; peak = max(peak, eq); dd = max(dd, (peak - eq) / peak * 100)
    med = lambda xs: sorted(xs)[len(xs)//2] if xs else 0.0
    print(f"\n=== SANITY REPLAY (frozen spec, no tuning) ===")
    print(f"trades           {n}")
    print(f"gross            {gross:+.2f} USDT")
    print(f"fees             {fees:.2f} USDT")
    print(f"NET              {net:+.2f} USDT  ({net/EQUITY*100:+.2f}% on {EQUITY:.0f})")
    print(f"winrate          {len(wins)/n*100:.1f}%")
    print(f"profit factor    {gp/gl if gl else float('inf'):.2f}")
    print(f"max drawdown     {dd:.2f}%")
    print(f"median winner    {med([t['net'] for t in wins]):+.2f}")
    print(f"median loser     {med([t['net'] for t in losses]):+.2f}")
    print(f"avg hold         {sum(t['hold_h'] for t in all_trades)/n:.1f} h")
    print(f"long/short       {sum(1 for t in all_trades if t['direction']=='LONG')}/"
          f"{sum(1 for t in all_trades if t['direction']=='SHORT')}")
    for s in SPEC.symbols:
        ts=[t for t in all_trades if t["symbol"]==s]
        if ts: print(f"  {s:9s} n={len(ts):3d} net={sum(t['net'] for t in ts):+8.2f}")
    print(f"exit reasons     {({r: sum(1 for t in all_trades if t['reason']==r) for r in {t['reason'] for t in all_trades}})}")
    print(f"TP1 hit          {sum(1 for t in all_trades if t['tp1'])}/{n}")


if __name__ == "__main__":
    main()
