"""ONE frozen replay of ExpansionFlow V1. No tuning after results. RESEARCH ONLY.

Book confirmation is a *veto* stage, and historical book snapshots exist only for 2026-08-12..17.
The replay therefore runs the structural pipeline over the full candle history and reports the
book stage as NOT EVALUABLE, which makes the trade count an UPPER BOUND: the live strategy would
take this set minus whatever the book vetoed. That is disclosed, not silently assumed away.
"""
from __future__ import annotations
import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.expansionflow_v1 import (Candle, ExpansionSpec, ExpansionDataError, Fees,
                                         atr_series, available_move_bps, breakout, direction,
                                         economic_gate, opportunity, retest, size_position,
                                         structural_stop)
from strategies.expansionflow_lifecycle import (ExpansionPosition, ExpansionLifecycleError,
                                                MakerOrder)

REST="https://api.bitget.com"; SPEC=ExpansionSpec(); PAGE=200
FEES=Fees(maker_bps=2.0, taker_bps=6.0, slippage_bps=1.0)   # authenticated 2026-08-17
EQUITY=10_000.0
#: Conservative post-only fill model: an entry at the breakout level fills only if a later bar
#: trades through it. No optimism about queue position.
def fetch(symbol, gran, pages=10):
    step={"1H":3_600_000,"15m":900_000}[gran]
    g={"1H":"1H","15m":"15m"}[gran]
    seen={}; end=None
    for _ in range(pages):
        url=(f"{REST}/api/v2/mix/market/history-candles?symbol={symbol}"
             f"&productType=USDT-FUTURES&granularity={g}&limit={PAGE}")
        if end is not None: url+=f"&endTime={end}"
        with urllib.request.urlopen(url,timeout=25) as r:
            rows=json.loads(r.read()).get("data") or []
        if not rows: break
        for row in rows:
            t=int(row[0])
            seen[t]=Candle(t,t+step,float(row[1]),float(row[2]),float(row[3]),float(row[4]),float(row[5]),True)
        end=min(int(row[0]) for row in rows)
    return [seen[k] for k in sorted(seen)]

STATS={k:0 for k in ("bars","opportunity","direction","breakout","retest","economic",
                     "orders","filled","unfilled","book_not_evaluable")}

def replay(symbol):
    c1=fetch(symbol,"1H"); c15=fetch(symbol,"15m")
    if len(c1)<SPEC.min_1h_candles+5 or len(c15)<SPEC.min_15m_candles+20: return []
    h1_by_time=sorted(c1,key=lambda c:c.close_time_ms)
    trades=[]; pos=None; pending=None
    for i in range(SPEC.min_15m_candles,len(c15)):
        w15=c15[:i+1]; bar=w15[-1]; now=bar.close_time_ms
        STATS["bars"]+=1
        h1=[c for c in h1_by_time if c.close_time_ms<=now]
        if len(h1)<SPEC.min_1h_candles: continue

        if pos is not None:
            try: reason=pos.observe(high=bar.high,low=bar.low,timestamp_ms=now)
            except ExpansionLifecycleError: reason=None
            if reason is None: reason=pos.check_max_hold(now,mark_price=bar.close)
            if reason:
                sign=1 if pos.side=="LONG" else -1
                gross=sign*(pos.exit_price-pos.fill_price)*pos.quantity
                notional_in=pos.fill_price*pos.quantity
                notional_out=abs(pos.exit_price)*pos.quantity
                fees=notional_in*FEES.maker_bps/1e4 + notional_out*FEES.taker_bps/1e4
                slip=notional_out*FEES.slippage_bps/1e4
                trades.append(dict(symbol=symbol,side=pos.side,gross=gross,fees=fees,slip=slip,
                                   net=gross-fees-slip,reason=reason,r=pos.realized_r(),
                                   hold_h=(now-pos.fill_ts)/3.6e6,entry_ms=pos.fill_ts,maker=True))
                pos=None
            else: continue

        # pending post-only order
        if pending is not None:
            o,level,stop,side,qty,atr15=pending
            if o.status=="WORKING":
                touched = bar.low<=o.desired_price if side=="LONG" else bar.high>=o.desired_price
                if touched:
                    o.fill(price=o.desired_price,quantity=qty,timestamp_ms=now)
                    STATS["filled"]+=1
                    try:
                        pos=ExpansionPosition(symbol=symbol,side=side,setup_ts=o.created_ms,
                                              fill_ts=now,fill_price=o.desired_price,quantity=qty,
                                              initial_stop=stop,spec=SPEC)
                    except ExpansionLifecycleError: pos=None
                    pending=None; continue
                if o.expired(now):
                    if o.reprices<SPEC.max_reprices:
                        o.reprice(new_price=o.desired_price,now_ms=now,setup_still_valid=True)
                    else:
                        o.cancel("TTL"); STATS["unfilled"]+=1; pending=None
                if pending is not None and o.status!="WORKING":
                    STATS["unfilled"]+=1; pending=None
            else:
                pending=None
            if pending is not None or pos is not None: continue

        try:
            ok,_,_=opportunity(h1,SPEC,FEES)
        except ExpansionDataError: continue
        if not ok: continue
        STATS["opportunity"]+=1
        side,_,_=direction(h1,SPEC)
        if side is None: continue
        STATS["direction"]+=1
        try:
            level,bv,_=breakout(w15,side,SPEC)
        except ExpansionDataError: continue
        if level is None: continue
        STATS["breakout"]+=1
        atr15=bv["atr15"]
        # look forward for a retest within the allowed window (bars after the breakout bar)
        fwd=c15[i+1:i+1+SPEC.retest_max_bars]
        rb,_=retest(fwd,side,level,atr15,SPEC)
        if rb is None: continue
        STATS["retest"]+=1
        since=[b for b in fwd[:fwd.index(rb)]]
        stop=structural_stop(side,rb,since,atr15,SPEC)
        entry=level
        if (side=="LONG" and stop>=entry) or (side=="SHORT" and stop<=entry): continue
        risk_bps=abs(entry-stop)/entry*1e4
        atr1h=atr_series(h1,SPEC.atr_period)[-1]
        move=available_move_bps(side,entry,h1,atr1h,SPEC)
        okg,_,_=economic_gate(risk_bps=risk_bps,move_bps=move,
                              friction_bps=FEES.maker_first_round_trip_bps(),spec=SPEC)
        if not okg: continue
        STATS["economic"]+=1
        STATS["book_not_evaluable"]+=1     # book snapshots do not cover this history
        sz=size_position(entry=entry,stop=stop,equity_usdt=EQUITY,spec=SPEC,
                         min_qty=0.0001,qty_step=0.0001,min_notional=5.0)
        if sz.refused_reason: continue
        o=MakerOrder(symbol=symbol,side=side,desired_price=entry,quantity=sz.quantity,
                     created_ms=rb.close_time_ms,spec=SPEC,atr15=atr15)
        STATS["orders"]+=1
        pending=(o,level,stop,side,sz.quantity,atr15)
    return trades

def main():
    allt=[]
    for s in SPEC.symbols:
        try:
            t=replay(s); allt+=t; print(f"  {s}: {len(t)} trades")
        except Exception as e:
            print(f"  {s}: ERROR {type(e).__name__}: {e}")
    print(f"\nfunnel: {STATS}")
    if not allt:
        print("\nNO TRADES — INSUFFICIENT_SAMPLE"); return
    n=len(allt)
    gross=sum(t["gross"] for t in allt); fees=sum(t["fees"] for t in allt); slip=sum(t["slip"] for t in allt)
    net=gross-fees-slip
    wins=[t for t in allt if t["net"]>0]; losses=[t for t in allt if t["net"]<=0]
    gp=sum(t["net"] for t in wins); gl=abs(sum(t["net"] for t in losses))
    eq=peak=EQUITY; dd=0.0
    for t in sorted(allt,key=lambda x:x["entry_ms"]):
        eq+=t["net"]; peak=max(peak,eq); dd=max(dd,(peak-eq)/peak*100)
    srt=sorted(allt,key=lambda t:-t["net"]); med=lambda xs: sorted(xs)[len(xs)//2] if xs else 0.0
    holds=sorted(t["hold_h"] for t in allt); rs=[t["r"] for t in allt]
    print(f"\n=== EXPANSIONFLOW V1 — FROZEN REPLAY ===")
    print(f"trades {n}   maker fill rate {STATS['filled']}/{STATS['orders']} = "
          f"{STATS['filled']/STATS['orders']*100 if STATS['orders'] else 0:.1f}%")
    print(f"gross {gross:+.2f}  fees {fees:.2f}  slippage {slip:.2f}  funding 0.00 (no settlement crossed in model)")
    print(f"NET {net:+.2f}  ({net/EQUITY*100:+.2f}%)")
    print(f"expectancy/trade {net/n:+.3f}   expectancy R {sum(rs)/n:+.3f}")
    print(f"PF {gp/gl if gl else float('inf'):.3f}   winrate {len(wins)/n*100:.1f}%")
    print(f"avg win R {sum(t['r'] for t in wins)/len(wins) if wins else 0:+.2f}   "
          f"avg loss R {sum(t['r'] for t in losses)/len(losses) if losses else 0:+.2f}")
    print(f"max DD {dd:.2f}%   avg/median hold {sum(holds)/n:.1f}/{med(holds):.1f} h")
    print(f"exits {({r:sum(1 for t in allt if t['reason']==r) for r in {t['reason'] for t in allt}})}")
    print(f"LONG {sum(t['net'] for t in allt if t['side']=='LONG'):+.2f} ({sum(1 for t in allt if t['side']=='LONG')})  "
          f"SHORT {sum(t['net'] for t in allt if t['side']=='SHORT'):+.2f} ({sum(1 for t in allt if t['side']=='SHORT')})")
    for s in SPEC.symbols:
        ts=[t for t in allt if t["symbol"]==s]
        if ts: print(f"  {s:9s} n={len(ts):3d} net={sum(t['net'] for t in ts):+8.2f}")
    print(f"without best {net-srt[0]['net']:+.2f}   without top3 {net-sum(t['net'] for t in srt[:3]):+.2f}")

if __name__=="__main__": main()
