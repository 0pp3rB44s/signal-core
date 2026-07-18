from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


PRICE_TYPES={"MARKET","MARK","INDEX"};TIMEFRAME_MS=900_000


@dataclass(frozen=True)
class CanonicalPriceCandle:
    symbol:str;exchange:str;market_type:str;timestamp_ms:int;timeframe:str
    open:float;high:float;low:float;close:float;price_type:str;source_endpoint:str
    retrieval_timestamp_ms:int;raw_source_reference:str
    volume_base:float|None=None;volume_quote:float|None=None


@dataclass(frozen=True)
class BasisFeatures:
    market_close_basis_bps:float;mark_close_basis_bps:float;market_mark_divergence_bps:float
    maximum_market_index_premium_bps:float|None;minimum_market_index_premium_bps:float|None
    maximum_market_mark_divergence_bps:float|None;minimum_market_mark_divergence_bps:float|None;basis_range_bps:float|None


def canonical_hash(value:Any)->str:return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()


def canonicalize_rows(symbol:str,price_type:str,endpoint:str,rows:Sequence[Sequence[str]],retrieval_ms:int,raw_reference:str)->list[CanonicalPriceCandle]:
    price_type=price_type.upper()
    if price_type not in PRICE_TYPES:raise ValueError("invalid price type")
    seen=set();result=[]
    for row in sorted(rows,key=lambda item:int(item[0])):
        timestamp=int(row[0]);values=[float(value) for value in row[1:5]]
        if timestamp in seen:raise ValueError("duplicate candle timestamp")
        seen.add(timestamp);o,h,l,c=values
        if timestamp%TIMEFRAME_MS or not all(math.isfinite(x) and x>0 for x in values) or h<max(o,c) or l>min(o,c):raise ValueError("invalid candle")
        volume_base=float(row[5]) if price_type=="MARKET" and len(row)>5 else None;volume_quote=float(row[6]) if price_type=="MARKET" and len(row)>6 else None
        result.append(CanonicalPriceCandle(symbol.upper(),"BITGET","USDT-FUTURES",timestamp,"15m",o,h,l,c,price_type,endpoint,retrieval_ms,raw_reference,volume_base,volume_quote))
    return result


def synchronize_exact(market:Sequence[CanonicalPriceCandle],mark:Sequence[CanonicalPriceCandle],index:Sequence[CanonicalPriceCandle])->tuple[list[tuple[CanonicalPriceCandle,CanonicalPriceCandle,CanonicalPriceCandle]],dict[str,int]]:
    maps=[{item.timestamp_ms:item for item in values} for values in (market,mark,index)];all_ts=set().union(*[set(m) for m in maps]);common=set(maps[0])&set(maps[1])&set(maps[2])
    rows=[(maps[0][ts],maps[1][ts],maps[2][ts]) for ts in sorted(common)]
    return rows,{"market_available":len(maps[0]),"mark_available":len(maps[1]),"index_available":len(maps[2]),"fully_synchronized":len(common),"incomplete_timestamps":len(all_ts-common)}


def basis_features(market:CanonicalPriceCandle,mark:CanonicalPriceCandle,index:CanonicalPriceCandle)->BasisFeatures:
    if len({market.timestamp_ms,mark.timestamp_ms,index.timestamp_ms})!=1:raise ValueError("asynchronous candles")
    b=lambda left,right:10_000*(left-right)/right
    # OHLC extrema inside the same 15m bucket have unknown event times. Pairing
    # market.high with index.high would invent simultaneity, so intrabar fields
    # remain unavailable without tick-level synchronized data.
    return BasisFeatures(b(market.close,index.close),b(mark.close,index.close),b(market.close,mark.close),None,None,None,None,None)


def basis_changes(values:Sequence[float],index:int)->dict[str,float|None]:
    result={}
    for name,offset in (("15m",1),("1h",4),("4h",16),("8h",32),("24h",96)):
        result[name]=values[index]-values[index-offset] if index>=offset else None
    return result


def cooldown_events(states:Sequence[bool],cooldown:int)->list[int]:
    result=[];last=-cooldown-1
    for i,state in enumerate(states):
        entered=state and (i==0 or not states[i-1])
        if entered and i-last>cooldown:result.append(i);last=i
    return result


def validate_primary_hypotheses(values:Sequence[dict[str,Any]])->None:
    required={"id","feature","state","direction","horizon","expected_sign","rationale","minimum_observations","contradiction_rule","economic_threshold_bps","analysis_family"}
    if len(values)>8:raise ValueError("more than eight primary hypotheses")
    if any(set(item)<required or item["analysis_family"]!="PRIMARY_PREREGISTERED" for item in values):raise ValueError("invalid primary hypothesis")


def split_families(values:Sequence[dict[str,Any]])->tuple[list[dict[str,Any]],list[dict[str,Any]]]:
    primary=[x for x in values if x.get("analysis_family")=="PRIMARY_PREREGISTERED"];exploratory=[x for x in values if x.get("analysis_family")=="SECONDARY_EXPLORATORY"]
    if len(primary)+len(exploratory)!=len(values):raise ValueError("unknown analysis family")
    return primary,exploratory


def write_atomic_json(path:Path,value:Any)->None:
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix(path.suffix+".tmp");temp.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n");temp.replace(path)


def candle_dicts(values:Iterable[CanonicalPriceCandle])->list[dict[str,Any]]:return [asdict(value) for value in values]
