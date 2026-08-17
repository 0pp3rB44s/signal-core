"""ONE frozen replay of Simple Trend Reversal V0. No tuning after results. RESEARCH ONLY."""
from __future__ import annotations
import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategies.simple_trend_reversal_v0 import Candle, V0Spec, V0DataError, evaluate, size_position
from strategies.simple_trend_reversal_lifecycle import (V0Position, V0Portfolio, V0SignalLedger,
                                                        V0LifecycleError)

REST="https://api.bitget.com"; SPEC=V0Spec(); PAGE=200
TAKER_BPS=6.0; SLIP_BPS=1.0; EQUITY=10_000.0
SYMBOLS=("BTCUSDT","ETHUSDT","SOLUSDT")

def fetch(symbol, pages=10):
    step=3_600_000; seen={}; end=None
    for _ in range(pages):
        url=(f"{REST}/api/v2/mix/market/history-candles?symbol={symbol}"
             f"&productType=USDT-FUTURES&granularity=1H&limit={PAGE}")
        if end is not None: url+=f"&endTime={end}"
        with urllib.request.urlopen(url,timeout=20) as r:
            rows=json.loads(r.read()).get("data") or []
        if not rows: break
        for row in rows:
            t=int(row[0])
            seen[t]=Candle(t,t+step,float(row[1]),float(row[2]),float(row[3]),float(row[4]),float(row[5]),True)
        end=min(int(row[0]) for row in rows)
    return [seen[k] for k in sorted(seen)]

def replay(symbol):
    c=fetch(symbol)
    if len(c)<SPEC.min_candles+20: return [],0,0
    trades=[]; pos=None; ledger=V0SignalLedger(); blocked=0
    for i in range(SPEC.min_candles,len(c)):
        w=c[:i+1]; bar=w[-1]; now=bar.close_time_ms
        if pos is not None:
            try:
                reason=pos.observe(high=bar.high,low=bar.low,timestamp_ms=now)
            except V0LifecycleError:
                reason=None
            if reason is None:
                reason=pos.check_max_hold(now,mark_price=bar.close)
            if reason:
                sign=1 if pos.side=="LONG" else -1
                gross=sign*(pos.exit_price-pos.fill_price)*pos.position_size
                notional=pos.fill_price*pos.position_size
                fees=(notional+abs(pos.exit_price)*pos.position_size)*TAKER_BPS/10_000.0
                slip=(notional+abs(pos.exit_price)*pos.position_size)*SLIP_BPS/10_000.0
                trades.append(dict(symbol=symbol,side=pos.side,gross=gross,fees=fees,slip=slip,
                                   net=gross-fees-slip,reason=reason,r=pos.realized_r(),
                                   hold_h=(now-pos.fill_ts)/3_600_000,entry_ms=pos.fill_ts,
                                   mfe=pos.mfe_bps,mae=pos.mae_bps))
                pos=None
            else:
                continue
        try: sig=evaluate(w,SPEC)
        except V0DataError: continue
        if not sig.is_entry: continue
        kw=dict(strategy_id=SPEC.strategy_id,symbol=symbol,side=sig.direction,
                signal_candle_ts=sig.signal_candle_ts)
        if ledger.already_taken(**kw): blocked+=1; continue
        # Conservative: entry slips against us by SLIP_BPS.
        s=1 if sig.direction=="LONG" else -1
        fill=sig.signal_price*(1+s*SLIP_BPS/10_000.0)
        stop=sig.stop_price
        if (sig.direction=="LONG" and stop>=fill) or (sig.direction=="SHORT" and stop<=fill):
            continue
        sz=size_position(entry=fill,stop=stop,equity_usdt=EQUITY,spec=SPEC,
                         min_qty=0.0001,qty_step=0.0001,min_notional=5.0)
        if sz.refused_reason: continue
        ledger.record(**kw)
        r=abs(fill-stop)
        tp=fill+s*SPEC.reward_multiple*r
        pos=V0Position(symbol=symbol,side=sig.direction,signal_candle_ts=sig.signal_candle_ts,
                       signal_price=sig.signal_price,fill_ts=now,fill_price=fill,
                       initial_stop=stop,take_profit=tp,position_size=sz.quantity,
                       risk_usdt=sz.risk_usdt,spec=SPEC)
    return trades,blocked,len(c)

def main():
    allt=[]; dup=0; bars=0
    for s in SYMBOLS:
        try:
            t,b,n=replay(s); allt+=t; dup+=b; bars=max(bars,n)
            print(f"  {s}: {len(t)} trades over {n} 1H candles")
        except Exception as e:
            print(f"  {s}: ERROR {type(e).__name__}: {e}")
    if not allt: print("\nNO TRADES"); return
    n=len(allt)
    gross=sum(t["gross"] for t in allt); fees=sum(t["fees"] for t in allt)
    slip=sum(t["slip"] for t in allt); net=gross-fees-slip
    wins=[t for t in allt if t["net"]>0]; losses=[t for t in allt if t["net"]<=0]
    gp=sum(t["net"] for t in wins); gl=abs(sum(t["net"] for t in losses))
    eq=peak=EQUITY; dd=0.0
    for t in sorted(allt,key=lambda x:x["entry_ms"]):
        eq+=t["net"]; peak=max(peak,eq); dd=max(dd,(peak-eq)/peak*100)
    med=lambda xs: sorted(xs)[len(xs)//2] if xs else 0.0
    holds=sorted(t["hold_h"] for t in allt); srt=sorted(allt,key=lambda t:-t["net"])
    chrono=sorted(allt,key=lambda x:x["entry_ms"]); half=len(chrono)//2
    rs=[t["r"] for t in allt]
    print(f"\n=== SIMPLE TREND REVERSAL V0 — FROZEN REPLAY ===")
    print(f"symbols/candles     {len(SYMBOLS)} x ~{bars} 1H")
    print(f"trades              {n}   longs {sum(1 for t in allt if t['side']=='LONG')} / shorts {sum(1 for t in allt if t['side']=='SHORT')}")
    print(f"duplicate blocked   {dup}")
    print(f"gross               {gross:+.2f}")
    print(f"fees                {fees:.2f}")
    print(f"slippage            {slip:.2f}")
    print(f"NET                 {net:+.2f}   ({net/EQUITY*100:+.2f}%)")
    print(f"expectancy/trade    {net/n:+.3f} USDT")
    print(f"expectancy R        {sum(rs)/n:+.3f} R")
    print(f"winrate             {len(wins)/n*100:.1f}%")
    print(f"avg win R           {sum(t['r'] for t in wins)/len(wins) if wins else 0:+.2f}")
    print(f"avg loss R          {sum(t['r'] for t in losses)/len(losses) if losses else 0:+.2f}")
    print(f"profit factor       {gp/gl if gl else float('inf'):.3f}")
    print(f"max drawdown        {dd:.2f}%")
    print(f"avg / median hold   {sum(holds)/n:.1f} h / {med(holds):.1f} h")
    reasons={r:sum(1 for t in allt if t['reason']==r) for r in {t['reason'] for t in allt}}
    print(f"exits               {reasons}")
    print(f"without best        {net-srt[0]['net']:+.2f}")
    print(f"without top3        {net-sum(t['net'] for t in srt[:3]):+.2f}")
    print(f"EARLY / LATE half   {sum(t['net'] for t in chrono[:half]):+.2f} / {sum(t['net'] for t in chrono[half:]):+.2f}")
    for s in SYMBOLS:
        ts=[t for t in allt if t["symbol"]==s]
        if ts: print(f"  {s:9s} n={len(ts):3d} net={sum(t['net'] for t in ts):+8.2f} PF={(sum(x['net'] for x in ts if x['net']>0)/abs(sum(x['net'] for x in ts if x['net']<=0)) if any(x['net']<=0 for x in ts) else float('inf')):.2f}")

if __name__=="__main__": main()
