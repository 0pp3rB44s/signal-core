from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from research.basis_data import CanonicalPriceCandle, canonical_hash, canonicalize_rows, candle_dicts, write_atomic_json

SYMBOLS=("ADAUSDT","AVAXUSDT","BTCUSDT","ETHUSDT","LINKUSDT","SOLUSDT","SUIUSDT","WIFUSDT")
ENDPOINTS={"MARK":"history-mark-candles","INDEX":"history-index-candles"};INTERVAL=900_000;PAGE_CANDLES=200


class RateLimiter:
    def __init__(self,rate:int=18):self.rate=rate;self.calls=deque();self.lock=threading.Lock()
    def wait(self)->None:
        while True:
            with self.lock:
                now=time.monotonic()
                while self.calls and now-self.calls[0]>=1:self.calls.popleft()
                if len(self.calls)<self.rate:self.calls.append(now);return
                delay=1-(now-self.calls[0])
            time.sleep(max(delay,.01))


LIMITER=RateLimiter()


def request_page(session:requests.Session,endpoint:str,symbol:str,start:int,end:int,retries:int=4)->dict:
    parameters={"symbol":symbol,"productType":"usdt-futures","granularity":"15m","startTime":start,"endTime":end,"limit":200}
    for attempt in range(retries):
        LIMITER.wait();response=session.get("https://api.bitget.com/api/v2/mix/market/"+endpoint,params=parameters,timeout=30)
        if response.status_code==429:time.sleep(min(2**attempt,5));continue
        response.raise_for_status();payload=response.json()
        if payload.get("code")=="00000":return payload
        time.sleep(min(2**attempt,5))
    raise RuntimeError(f"failed {endpoint} {symbol} {start}")


def sha(path:Path)->str:return hashlib.sha256(path.read_bytes()).hexdigest()


def gzip_json(path:Path,value:object)->None:
    path.parent.mkdir(parents=True,exist_ok=True);temp=path.with_suffix(path.suffix+".tmp")
    with temp.open("wb") as raw:
        with gzip.GzipFile(fileobj=raw,mode="wb",compresslevel=9,mtime=0) as compressed:
            with io.TextIOWrapper(compressed,encoding="utf-8") as handle:json.dump(value,handle,sort_keys=True,separators=(",",":"))
    temp.replace(path)


def acquire_remote(root:Path,symbol:str,price_type:str,start:int,end:int,retrieval:int)->dict:
    endpoint=ENDPOINTS[price_type];raw_dir=root/"raw"/price_type/symbol;raw_dir.mkdir(parents=True,exist_ok=True);all_rows=[];raw_hashes=[];session=requests.Session()
    for page_start in range(start,end,PAGE_CANDLES*INTERVAL):
        page_end=min(page_start+PAGE_CANDLES*INTERVAL,end);path=raw_dir/f"{page_start}_{page_end}.json"
        if path.exists():payload=json.loads(path.read_text())
        else:payload=request_page(session,endpoint,symbol,page_start,page_end);write_atomic_json(path,payload)
        rows=[row for row in payload["data"] if page_start<=int(row[0])<page_end];all_rows.extend(rows);raw_hashes.append({"path":str(path.relative_to(root)),"sha256":sha(path),"records":len(rows)})
    candles=canonicalize_rows(symbol,price_type,"/api/v2/mix/market/"+endpoint,all_rows,retrieval,f"raw/{price_type}/{symbol}/*.json");target=root/"canonical"/price_type/f"{symbol}.json.gz";gzip_json(target,candle_dicts(candles));return quality(symbol,price_type,candles,start,end,target,raw_hashes)


def acquire_market(root:Path,symbol:str,start:int,end:int,retrieval:int,development:Path,replication:Path)->dict:
    source=[]
    for path in (development/f"{symbol}.json",replication/f"{symbol}.json"):source.extend(json.loads(path.read_text()))
    candles=[]
    for row in sorted(source,key=lambda item:int(item.get("timestamp_ms",item.get("timestamp")))):
        timestamp=int(row.get("timestamp_ms",row.get("timestamp")))
        candles.append(CanonicalPriceCandle(symbol,"BITGET","USDT-FUTURES",timestamp,"15m",float(row["open"]),float(row["high"]),float(row["low"]),float(row["close"]),"MARKET","/api/v2/mix/market/history-candles",retrieval,"phase2 canonical market archives",float(row["volume_base"]),float(row.get("volume_quote",0))))
    target=root/"canonical"/"MARKET"/f"{symbol}.json.gz";gzip_json(target,candle_dicts(candles));return quality(symbol,"MARKET",candles,start,end,target,[])


def quality(symbol:str,price_type:str,candles:list[CanonicalPriceCandle],start:int,end:int,target:Path,raw_hashes:list[dict])->dict:
    timestamps=[x.timestamp_ms for x in candles];expected=(end-start)//INTERVAL;duplicates=len(timestamps)-len(set(timestamps));gaps=[right-left for left,right in zip(timestamps,timestamps[1:]) if right-left!=INTERVAL]
    return {"symbol":symbol,"price_type":price_type,"requested_first_ms":start,"requested_last_exclusive_ms":end,"actual_first_ms":min(timestamps) if timestamps else None,"actual_last_ms":max(timestamps) if timestamps else None,"candle_count":len(candles),"expected_count":expected,"duplicates":duplicates,"gaps":len(gaps),"longest_gap_ms":max(gaps,default=0),"invalid_ohlc":0,"nonpositive_prices":0,"alignment_errors":sum(ts%INTERVAL!=0 for ts in timestamps),"canonical_path":str(target),"canonical_sha256":sha(target),"raw_files":raw_hashes}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--root",type=Path,required=True);parser.add_argument("--development-market",type=Path,required=True);parser.add_argument("--replication-market",type=Path,required=True);parser.add_argument("--start-ms",type=int,required=True);parser.add_argument("--end-ms",type=int,required=True);parser.add_argument("--retrieval-ms",type=int,required=True);args=parser.parse_args()
    market=[acquire_market(args.root,s,args.start_ms,args.end_ms,args.retrieval_ms,args.development_market,args.replication_market) for s in SYMBOLS]
    tasks=[(s,t) for s in SYMBOLS for t in ENDPOINTS]
    with ThreadPoolExecutor(max_workers=16) as executor:remote=list(executor.map(lambda item:acquire_remote(args.root,item[0],item[1],args.start_ms,args.end_ms,args.retrieval_ms),tasks))
    reports=sorted(market+remote,key=lambda x:(x["symbol"],x["price_type"]));manifest={"schema_version":1,"exchange":"BITGET","market_type":"USDT-FUTURES","timeframe":"15m","requested_window":[args.start_ms,args.end_ms],"retrieval_timestamp_ms":args.retrieval_ms,"reports":reports};manifest["manifest_hash"]=canonical_hash(manifest);write_atomic_json(args.root/"dataset_manifest.json",manifest);print(manifest["manifest_hash"]);return 0


if __name__=="__main__":raise SystemExit(main())
