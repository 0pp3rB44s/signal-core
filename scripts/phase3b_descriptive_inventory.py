from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

from clients.schemas import Candle
from research.preregistration_protocol import (
    DEVELOPMENT_END_MS, DEVELOPMENT_START_MS, VALIDATION_END_MS, VALIDATION_START_MS,
    assert_descriptive_source, assert_no_performance_fields, canonical_hash, validate_split,
)


def load_candles(path: Path) -> dict[str, list[Candle]]:
    result: dict[str, list[Candle]] = {}
    for source in sorted(path.glob("*.json")):
        payload = json.loads(source.read_text())
        rows = payload.get("candles", payload) if isinstance(payload, dict) else payload
        result[source.stem.upper()] = [
            Candle(
                timestamp_ms=int(row.get("timestamp_ms", row.get("timestamp"))),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume_base=float(row["volume_base"]),
                volume_quote=float(row["volume_quote"]) if row.get("volume_quote") is not None else None,
            )
            for row in rows
        ]
    return result


def ema(values: list[float], period: int) -> list[float]:
    alpha=2/(period+1);result=[];current=values[0]
    for value in values:
        current=alpha*value+(1-alpha)*current;result.append(current)
    return result


def rolling_mean(values: list[float], period: int) -> list[float | None]:
    result=[None]*len(values);total=0.0
    for i,value in enumerate(values):
        total+=value
        if i>=period:total-=values[i-period]
        if i>=period-1:result[i]=total/period
    return result


def quantile(values: list[float], probability: float) -> float:
    ordered=sorted(values);position=(len(ordered)-1)*probability;lo=math.floor(position);hi=math.ceil(position)
    return ordered[lo] if lo==hi else ordered[lo]*(hi-position)+ordered[hi]*(position-lo)


def features(candles) -> dict[str, Any]:
    closes=[c.close for c in candles];volumes=[c.volume_base for c in candles]
    e20=ema(closes,20);e50=ema(closes,50);vol20=rolling_mean(volumes,20)
    tr=[]
    for i,c in enumerate(candles):
        previous=closes[i-1] if i else c.open;tr.append(max(c.high-c.low,abs(c.high-previous),abs(c.low-previous)))
    atr14=rolling_mean(tr,14);mean20=rolling_mean(closes,20)
    bandwidth=[None]*len(candles);rsi=[None]*len(candles);gains=[0.0];losses=[0.0]
    for i in range(1,len(candles)):
        delta=closes[i]-closes[i-1];gains.append(max(0,delta));losses.append(max(0,-delta))
    gain14=rolling_mean(gains,14);loss14=rolling_mean(losses,14)
    for i in range(19,len(candles)):
        window=closes[i-19:i+1];mu=mean20[i];sd=math.sqrt(sum((x-mu)**2 for x in window)/20);bandwidth[i]=4*sd/mu if mu else None
    for i in range(13,len(candles)):
        g=gain14[i] or 0;l=loss14[i] or 0;rsi[i]=100 if l==0 else 100-100/(1+g/l)
    return {"ema20":e20,"ema50":e50,"atr14":atr14,"volume20":vol20,"bb_width":bandwidth,"rsi14":rsi}


def outcome(candles,index:int,direction:int,atr:float) -> dict[str,float]:
    entry=candles[index].close;result={}
    for horizon in (1,2,4,8,16):
        path=candles[index+1:index+1+horizon]
        if len(path)<horizon:continue
        close_move=direction*(path[-1].close-entry)/atr
        favourable=max([0.0]+[direction*(c.high-entry) if direction==1 else direction*(c.low-entry) for c in path])/atr
        adverse=max([0.0]+[-direction*(c.low-entry) if direction==1 else -direction*(c.high-entry) for c in path])/atr
        result[f"close_{horizon}_atr"]=close_move;result[f"mfe_{horizon}_atr"]=favourable;result[f"mae_{horizon}_atr"]=adverse
    return result


def aggregate(events:list[dict[str,Any]]) -> dict[str,Any]:
    if not events:return {"events":0}
    numeric=sorted({key for row in events for key,value in row.items() if isinstance(value,(int,float)) and key not in {"timestamp_ms"}})
    summary={"events":len(events)}
    for key in numeric:
        values=[float(row[key]) for row in events if key in row];summary[key]={"mean":mean(values),"median":median(values),"q25":quantile(values,.25),"q75":quantile(values,.75)}
    for key in ("direction","symbol","session","session_overlap","hour","htf_regime","htf_supports_reversal"):
        counts=defaultdict(int)
        for row in events:counts[str(row.get(key,"UNKNOWN"))]+=1
        summary[f"by_{key}"]=dict(sorted(counts.items()))
        strata={}
        for value in counts:
            rows=[row for row in events if str(row.get(key,"UNKNOWN"))==value]
            strata[value]={"events":len(rows)}
            for metric in ("close_4_atr","close_8_atr","close_16_atr","mfe_8_atr","mae_8_atr"):
                values=[float(row[metric]) for row in rows if metric in row]
                if values:strata[value][metric+"_mean"]=mean(values);strata[value][metric+"_median"]=median(values)
        summary[f"strata_{key}"]=strata
    return summary


def observe(symbol:str,candles) -> dict[str,list[dict[str,Any]]]:
    f=features(candles);families=defaultdict(list);width_history=[];atr_pct_history=[]
    for i in range(140,len(candles)-17):
        c=candles[i];atr=f["atr14"][i]
        if not atr or not f["bb_width"][i]:continue
        prior20=candles[i-20:i];prior8=candles[i-8:i];high20=max(x.high for x in prior20);low20=min(x.low for x in prior20);high8=max(x.high for x in prior8);low8=min(x.low for x in prior8)
        width20=(high20-low20)/c.close;atr_pct=atr/c.close
        width_history.append(width20);atr_pct_history.append(atr_pct)
        if len(width_history)<120:continue
        direction=1 if c.close>high20 else -1 if c.close<low20 else 0
        hour=(c.timestamp_ms//3_600_000)%24;session="ASIA" if hour<8 else "EUROPE" if hour<16 else "US";overlap="ASIA_EUROPE_TRANSITION" if hour in {7,8} else "EUROPE_US_OVERLAP" if hour in {13,14,15,16} else "NONE"
        htf="bullish" if f["ema20"][i]>f["ema50"][i] else "bearish"
        base={"symbol":symbol,"timestamp_ms":c.timestamp_ms,"direction":"LONG" if direction==1 else "SHORT" if direction==-1 else "NONE","session":session,"session_overlap":overlap,"hour":str(hour),"htf_regime":htf,"atr_pct":atr_pct,"bb_width":f["bb_width"][i],"range20_pct":width20,"volume_ratio":c.volume_base/f["volume20"][i] if f["volume20"][i] else 0,"body_fraction":abs(c.close-c.open)/(c.high-c.low) if c.high>c.low else 0}
        compressed=width20<=quantile(width_history[-120:],.25) and atr_pct<=quantile(atr_pct_history[-120:],.25) and f["bb_width"][i]<=quantile([x for x in f["bb_width"][max(19,i-119):i+1] if x is not None],.25)
        if direction and compressed:
            path=candles[i+1:i+5];false_break=any((x.close<=high20 if direction==1 else x.close>=low20) for x in path)
            retest=any((x.low<=high20 if direction==1 else x.high>=low20) for x in path)
            families["compression_breakout"].append({**base,"false_break":int(false_break),"retest":int(retest),"htf_aligned":int((direction==1)==(htf=="bullish")),**outcome(candles,i,direction,atr)})
        trend_direction=1 if f["ema20"][i]>f["ema50"][i] else -1
        touched=(c.low<=f["ema20"][i] and c.close>f["ema20"][i]) if trend_direction==1 else (c.high>=f["ema20"][i] and c.close<f["ema20"][i])
        if touched:
            depth=abs((c.low if trend_direction==1 else c.high)-f["ema20"][i])/atr
            families["trend_pullback_continuation"].append({**base,"direction":"LONG" if trend_direction==1 else "SHORT","pullback_depth_atr":depth,"distance_ema20_atr":abs(c.close-f["ema20"][i])/atr,**outcome(candles,i,trend_direction,atr)})
        previous=candles[i-1];old=candles[i-21:i-1];old_high=max(x.high for x in old);old_low=min(x.low for x in old)
        failed_direction=1 if previous.close>old_high and c.close<=old_high else -1 if previous.close<old_low and c.close>=old_low else 0
        if failed_direction:
            reversal=-failed_direction
            escape_atr=abs(previous.close-(old_high if failed_direction==1 else old_low))/atr;reentry_atr=abs(c.close-(old_high if failed_direction==1 else old_low))/atr
            event={**base,"direction":"LONG" if reversal==1 else "SHORT","escape_atr":escape_atr,"reentry_atr":reentry_atr,"htf_supports_reversal":int((reversal==1)==(htf=="bullish")),**outcome(candles,i,reversal,atr)}
            families["failed_breakout_reversal"].append(event)
            reversal_body=(c.close>c.open) if reversal==1 else (c.close<c.open)
            invalidation=previous.low if reversal==1 else previous.high
            stop_distance=abs(c.close-invalidation);tp1_distance_bps=1.2*stop_distance/c.close*10_000
            if escape_atr>=.10 and reentry_atr>=.15 and event["body_fraction"]>=.50 and reversal_body and stop_distance/atr<=2.0 and tp1_distance_bps>=72:
                target_hit=event.get("mfe_8_atr",0)>=1.2*stop_distance/atr
                families["selected_hypothesis_descriptive_diagnostic"].append({**event,"stop_distance_atr":stop_distance/atr,"tp1_distance_bps":tp1_distance_bps,"tp1_reached_within_8":int(target_hit)})
        consolidation=(high8-low8)/atr<=2.0;expansion=(c.high-c.low)/atr>=1.5;escape8=1 if c.close>high8 else -1 if c.close<low8 else 0
        if consolidation and expansion and escape8:
            families["volatility_expansion_after_consolidation"].append({**base,"direction":"LONG" if escape8==1 else "SHORT","consolidation_range_atr":(high8-low8)/atr,"expansion_range_atr":(c.high-c.low)/atr,**outcome(candles,i,escape8,atr)})
        distance=(c.close-f["ema20"][i])/atr;exhausted=(distance>=2 and f["rsi14"][i]>=70) or (distance<=-2 and f["rsi14"][i]<=30)
        reclaim=1 if exhausted and distance<0 and c.close>previous.high else -1 if exhausted and distance>0 and c.close<previous.low else 0
        if reclaim:
            families["extreme_mean_reversion_reclaim"].append({**base,"direction":"LONG" if reclaim==1 else "SHORT","distance_mean_atr":abs(distance),"rsi14":f["rsi14"][i],**outcome(candles,i,reclaim,atr)})
        if direction:
            path=candles[i+1:i+17];continued=direction*(path[-1].close-c.close)>0 if len(path)==16 else False
            families["breakout_quality"].append({**base,"range_escape_atr":abs(c.close-(high20 if direction==1 else low20))/atr,"continued_16":int(continued),"retest":int(any((x.low<=high20 if direction==1 else x.high>=low20) for x in path[:4])),**outcome(candles,i,direction,atr)})
        if abs((c.close-f["ema20"][i])/atr)>=2:
            revert_direction=-1 if c.close>f["ema20"][i] else 1
            families["mean_reversion_behavior"].append({**base,"direction":"LONG" if revert_direction==1 else "SHORT","distance_mean_atr":abs(c.close-f["ema20"][i])/atr,"rsi14":f["rsi14"][i],**outcome(candles,i,revert_direction,atr)})
        if hour in {0,7,8,12,13,15,16}:
            families["session_transition"].append({**base,"direction":"NONE","range_atr":(c.high-c.low)/atr,"next4_absolute_close_atr":abs(candles[i+4].close-c.close)/atr})
    return families


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument("--development",type=Path,required=True);parser.add_argument("--validation",type=Path,required=True);parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    validate_split();assert_descriptive_source(args.development);assert_descriptive_source(args.validation)
    if args.output.exists():raise SystemExit("refusing to overwrite descriptive inventory")
    result={"contract":{"analysis":"DESCRIPTIVE_PRICE_BEHAVIOR_ONLY","strategy_execution_invoked":False,"performance_fields_loaded":False,"development":{"start_ms":DEVELOPMENT_START_MS,"end_ms_exclusive":DEVELOPMENT_END_MS},"locked_validation":{"start_ms":VALIDATION_START_MS,"end_ms_exclusive":VALIDATION_END_MS},"validation_use":"descriptive stability only; no profitability, threshold selection or ranking"},"periods":{}}
    for label,path,start,end in (("development",args.development,DEVELOPMENT_START_MS,DEVELOPMENT_END_MS),("locked_validation_descriptive",args.validation,VALIDATION_START_MS,VALIDATION_END_MS)):
        data=load_candles(path);families=defaultdict(list)
        for symbol,candles in data.items():
            if candles[0].timestamp_ms!=start or candles[-1].timestamp_ms+900_000!=end:raise ValueError(f"{label} boundary mismatch for {symbol}")
            for family,events in observe(symbol,candles).items():families[family].extend(events)
        result["periods"][label]={family:aggregate(events) for family,events in sorted(families.items())}
    assert_no_performance_fields(result);args.output.mkdir(parents=True);(args.output/"descriptive_evidence.json").write_text(json.dumps(result,indent=2,sort_keys=True)+"\n");digest=canonical_hash(result);(args.output/"evidence_hash.txt").write_text(digest+"\n");print(digest);return 0


if __name__=="__main__":raise SystemExit(main())
