"""ONE frozen replay of TrendFlow V1.1. RESEARCH ONLY. No search, no grid, no post-hoc tuning."""
from __future__ import annotations
import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.trendflow_v1 import Candle, CostModel, DataQualityError, cost_gate, ema, size_position
from strategies.trendflow_v1_1 import TrendFlowV11Spec, classify_4h, evaluate, trend_invalidated_4h
from strategies.trendflow_lifecycle import TrendRunnerTracker, LifecycleError

REST = "https://api.bitget.com"; SPEC = TrendFlowV11Spec()
TAKER_BPS, MAKER_BPS, EQUITY = 6.0, 2.0, 10_000.0
PAGE = 200


def fetch(symbol, granularity, pages=8):
    step = {"1H": 3_600_000, "4H": 14_400_000}[granularity]
    seen, end = {}, None
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
                             float(row[4]), float(row[5]), True)
        end = min(int(row[0]) for row in rows)
    return [seen[k] for k in sorted(seen)]


def replay(symbol):
    c1, c4 = fetch(symbol, "1H"), fetch(symbol, "4H")
    if len(c1) < SPEC.min_1h_candles + 50 or len(c4) < SPEC.min_4h_candles + 5:
        return [], 0
    trades, pos, reentries = [], None, 0
    #: Re-entry needs a NEW closed 4H candle after any exit — this is the churn control.
    blocked_until_4h_open = None
    for i in range(SPEC.min_1h_candles, len(c1)):
        w1 = c1[:i + 1]; bar = w1[-1]; now = bar.close_time_ms
        w4 = [c for c in c4 if c.close_time_ms <= now]
        if len(w4) < SPEC.min_4h_candles:
            continue
        last4 = w4[-1]

        if pos is not None:
            t: TrendRunnerTracker = pos["tracker"]
            for px, off in ((bar.high, 1), (bar.low, 2)):
                ts = max(now + off, t.last_observation_ms + 1, t.fill_timestamp_ms + off)
                try:
                    t.observe(price=px, timestamp_ms=ts, atr14=pos["atr"])
                except LifecycleError:
                    pass
            reason = exit_px = None
            if t.reached_stop(bar.low if t.direction == "LONG" else bar.high):
                reason, exit_px = ("TRAIL" if t.tp1_hit else "STOP"), t.stop_price
            elif last4.close_time_ms > pos["entry_4h_close_ms"]:
                e20_4h = ema([c.close for c in w4], SPEC.ema_fast)[-1]
                if trend_invalidated_4h(t.direction, closed_4h_close=last4.close, ema20_4h=e20_4h):
                    reason, exit_px = "TREND_INVALIDATION_4H", bar.close
            if reason is None and t.max_hold_exceeded(now):
                reason, exit_px = "MAX_HOLD_72H", bar.close
            if reason:
                sign = 1 if t.direction == "LONG" else -1
                qty = pos["qty"]
                notional_in = t.entry_price * qty
                # TP1 closed 25% at +1R; the rest exits here.
                partial = 0.0
                exits = 1
                if t.tp1_hit:
                    partial = sign * (t.tp1_price() - t.entry_price) * qty * 0.25
                    exits = 2
                runner_qty = qty * (0.75 if t.tp1_hit else 1.0)
                gross = partial + sign * (exit_px - t.entry_price) * runner_qty
                turnover = notional_in + (t.tp1_price() * qty * 0.25 if t.tp1_hit else 0.0) \
                           + exit_px * runner_qty
                entry_fee = notional_in * TAKER_BPS / 10_000.0
                exit_fee = (turnover - notional_in) * TAKER_BPS / 10_000.0
                trades.append(dict(symbol=symbol, direction=t.direction, gross=gross,
                                   entry_fee=entry_fee, exit_fee=exit_fee,
                                   fees=entry_fee + exit_fee, net=gross - entry_fee - exit_fee,
                                   reason=reason, exits=exits, turnover=turnover,
                                   hold_h=(now - t.fill_timestamp_ms) / 3_600_000,
                                   tp1=t.tp1_hit, entry_ms=t.fill_timestamp_ms))
                pos = None
                blocked_until_4h_open = last4.open_time_ms
            else:
                continue

        if pos is not None:
            continue
        if blocked_until_4h_open is not None and last4.open_time_ms <= blocked_until_4h_open:
            continue
        if blocked_until_4h_open is not None:
            reentries += 1
            blocked_until_4h_open = None
        try:
            st = evaluate(w1, w4, SPEC)
        except DataQualityError:
            continue
        if not st.is_entry:
            continue
        a14 = st.values["atr14_1h"]
        opp = (SPEC.stop_atr_multiple * a14) / bar.close * 10_000.0
        if not cost_gate(expected_opportunity_bps=opp,
                         cost=CostModel(TAKER_BPS, MAKER_BPS, 1.0, 1.0), spec=SPEC)[0]:
            continue
        sz = size_position(direction=st.direction, entry_price=bar.close, atr14=a14,
                           equity_usdt=EQUITY, spec=SPEC, min_qty=0.0001,
                           qty_step=0.0001, min_notional=5.0)
        if sz.refused_reason:
            continue
        pos = dict(qty=sz.quantity, atr=a14, entry_4h_close_ms=last4.close_time_ms,
                   tracker=TrendRunnerTracker(direction=st.direction, entry_price=bar.close,
                                              fill_timestamp_ms=now, initial_stop=sz.stop_price,
                                              spec=SPEC))
    return trades, reentries


def main():
    allt, reent = [], 0
    for s in SPEC.symbols:
        try:
            t, r = replay(s); allt += t; reent += r
            print(f"  {s}: {len(t)} trades, {r} re-entries")
        except Exception as e:
            print(f"  {s}: ERROR {type(e).__name__}: {e}")
    if not allt:
        print("\nNO TRADES"); return
    n = len(allt)
    gross = sum(t["gross"] for t in allt); fees = sum(t["fees"] for t in allt); net = gross - fees
    ef = sum(t["entry_fee"] for t in allt); xf = sum(t["exit_fee"] for t in allt)
    wins = [t for t in allt if t["net"] > 0]; losses = [t for t in allt if t["net"] <= 0]
    gp = sum(t["net"] for t in wins); gl = abs(sum(t["net"] for t in losses))
    eq = peak = EQUITY; dd = 0.0
    for t in sorted(allt, key=lambda x: x["entry_ms"]):
        eq += t["net"]; peak = max(peak, eq); dd = max(dd, (peak - eq) / peak * 100)
    srt = sorted(allt, key=lambda t: -t["net"])
    med = lambda xs: sorted(xs)[len(xs)//2] if xs else 0.0
    holds = sorted(t["hold_h"] for t in allt)
    print(f"\n=== TRENDFLOW V1.1 FROZEN REPLAY ===")
    print(f"trades              {n}")
    print(f"gross               {gross:+.2f}")
    print(f"fees                {fees:.2f}   (entry {ef:.2f} / exit {xf:.2f})")
    print(f"NET                 {net:+.2f}   ({net/EQUITY*100:+.2f}%)")
    print(f"winrate             {len(wins)/n*100:.1f}%")
    print(f"profit factor       {gp/gl if gl else float('inf'):.2f}")
    print(f"max drawdown        {dd:.2f}%")
    print(f"avg hold            {sum(holds)/n:.1f} h    median hold {med(holds):.1f} h")
    print(f"TP1 rate            {sum(1 for t in allt if t['tp1'])/n*100:.1f}%")
    reasons = {r: sum(1 for t in allt if t["reason"] == r) for r in {t["reason"] for t in allt}}
    print(f"exits               {reasons}")
    print(f"re-entries          {reent}")
    print(f"turnover            {sum(t['turnover'] for t in allt):.0f}")
    print(f"fees/|gross|        {fees/abs(gross) if gross else float('inf'):.2f}")
    print(f"median winner/loser {med([t['net'] for t in wins]):+.2f} / {med([t['net'] for t in losses]):+.2f}")
    print(f"top5 winners        {sum(t['net'] for t in srt[:5]):+.2f}")
    print(f"top5 losers         {sum(t['net'] for t in srt[-5:]):+.2f}")
    print(f"NET without best    {net - srt[0]['net']:+.2f}")
    print(f"NET without top3    {net - sum(t['net'] for t in srt[:3]):+.2f}")
    print(f"long/short          {sum(1 for t in allt if t['direction']=='LONG')}/"
          f"{sum(1 for t in allt if t['direction']=='SHORT')}")
    for s in SPEC.symbols:
        ts = [t for t in allt if t["symbol"] == s]
        if ts: print(f"  {s:9s} n={len(ts):3d} net={sum(t['net'] for t in ts):+8.2f}")
    print("\n--- V1 -> V1.1 design checks ---")
    print(f"trade count   153 -> {n}          ({(1-n/153)*100:+.0f}% churn)")
    print(f"avg hold      6.6h -> {sum(holds)/n:.1f}h   ({(sum(holds)/n)/6.6:.1f}x)")
    print(f"fees          420.67 -> {fees:.2f}  ({(1-fees/420.67)*100:+.0f}%)")


if __name__ == "__main__":
    main()
