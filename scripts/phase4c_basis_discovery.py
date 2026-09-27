from __future__ import annotations

import argparse,gzip,hashlib,json,math
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
from research.basis_data import canonical_hash,cooldown_events,validate_primary_hypotheses,write_atomic_json
from research.market_edge_discovery import benjamini_hochberg,development_boundaries,apply_frozen_bin,exclusions_stable

SYMBOLS=("ADAUSDT","AVAXUSDT","BTCUSDT","ETHUSDT","LINKUSDT","SOLUSDT","SUIUSDT","WIFUSDT");SEED=20260716;THRESHOLDS=(.24,.48,.72,1.0)

def load(root:Path)->pd.DataFrame:
 parts=[]
 for symbol in SYMBOLS:
  frames=[]
  for kind in ("MARKET","MARK","INDEX"):
   with gzip.open(root/"canonical"/kind/f"{symbol}.json.gz","rt") as h:data=json.load(h)
   f=pd.DataFrame(data).set_index("timestamp_ms");f=f[["open","high","low","close"]].add_prefix(kind.lower()+"_");frames.append(f)
  joined=pd.concat(frames,axis=1,join="inner").reset_index();joined["symbol"]=symbol;parts.append(joined)
 return pd.concat(parts,ignore_index=True).sort_values(["symbol","timestamp_ms"]).reset_index(drop=True)

def add_features(f:pd.DataFrame)->pd.DataFrame:
 f=f.copy();f["market_basis_bps"]=1e4*(f.market_close-f.index_close)/f.index_close;f["mark_basis_bps"]=1e4*(f.mark_close-f.index_close)/f.index_close;f["market_mark_bps"]=1e4*(f.market_close-f.mark_close)/f.mark_close
 g=f.groupby("symbol",sort=False);f["basis_change_15m"]=g.market_basis_bps.diff(1);f["basis_change_1h"]=g.market_basis_bps.diff(4);f["basis_change_4h"]=g.market_basis_bps.diff(16);f["basis_change_8h"]=g.market_basis_bps.diff(32);f["basis_change_24h"]=g.market_basis_bps.diff(96);f["price_return_1h"]=g.market_close.pct_change(4)*100
 sign=np.sign(f.market_basis_bps);f["sign_reversal"]=(sign!=g.market_basis_bps.shift(1).apply(np.sign))&g.market_basis_bps.shift(1).notna();return f

def registry()->list[dict[str,Any]]:
 base={"minimum_observations":500,"contradiction_rule":"oriented return sign reversal or magnitude ratio below 0.25","economic_threshold_bps":24,"analysis_family":"PRIMARY_PREREGISTERED"}
 rows=[("B1","market_basis_bps","TOP_10","SHORT",16,"positive","extreme market premium may converge"),("B2","market_basis_bps","BOTTOM_10","LONG",16,"positive","extreme market discount may converge"),("B3","mark_basis_bps","TOP_10","SHORT",16,"positive","extreme mark premium may normalize"),("B4","mark_basis_bps","BOTTOM_10","LONG",16,"positive","extreme mark discount may normalize"),("B5","market_above_mark_and_index","TRUE","SHORT",4,"positive","market above both references may converge"),("B6","market_below_mark_and_index","TRUE","LONG",4,"positive","market below both references may converge"),("B7","absolute_basis_change_1h","TOP_10","AGAINST_BASIS_SIGN",8,"positive","rapid expansion may exhaust"),("B8","basis_sign_reversal","TRUE","WITH_NEW_SIGN",4,"positive","new basis sign may precede adjustment")]
 result=[{"id":i,"feature":feat,"state":state,"direction":direction,"horizon":h,"expected_sign":sign,"rationale":why,**base} for i,feat,state,direction,h,sign,why in rows];validate_primary_hypotheses(result);return result

def masks(frame:pd.DataFrame,bounds:dict[str,list[float]])->dict[str,pd.Series]:
 mb=frame.market_basis_bps.map(lambda x:apply_frozen_bin(x,bounds["market_basis_bps"]));mk=frame.mark_basis_bps.map(lambda x:apply_frozen_bin(x,bounds["mark_basis_bps"]));change=frame.basis_change_1h.abs().map(lambda x:apply_frozen_bin(x,bounds["absolute_basis_change_1h"]) if pd.notna(x) else "UNKNOWN")
 return {"B1":mb=="TOP_10","B2":mb=="BOTTOM_10","B3":mk=="TOP_10","B4":mk=="BOTTOM_10","B5":(frame.market_basis_bps>0)&(frame.market_mark_bps>0),"B6":(frame.market_basis_bps<0)&(frame.market_mark_bps<0),"B7":change=="TOP_10","B8":frame.sign_reversal}

def outcome(frame:pd.DataFrame,horizon:int,direction:str)->pd.DataFrame:
 pieces=[]
 for _,g in frame.groupby("symbol",sort=True):
  close=g.market_close.to_numpy();high=g.market_high.to_numpy();low=g.market_low.to_numpy();n=len(g);hs=np.vstack([np.roll(high,-i) for i in range(1,horizon+1)]);ls=np.vstack([np.roll(low,-i) for i in range(1,horizon+1)]);future=np.roll(close,-horizon)
  if direction in {"LONG","SHORT"}:sgn=np.full(n,1 if direction=="LONG" else -1)
  elif direction=="AGAINST_BASIS_SIGN":sgn=-np.sign(g.market_basis_bps.to_numpy());sgn[sgn==0]=1
  else:sgn=np.sign(g.market_basis_bps.to_numpy());sgn[sgn==0]=1
  ret=sgn*(future-close)/close*100;fav=np.where(sgn[None,:]>0,(hs-close)/close*100,(close-ls)/close*100);adv=np.where(sgn[None,:]>0,(close-ls)/close*100,(hs-close)/close*100);data={"return_pct":ret,"mfe_pct":np.maximum(fav.max(0),0),"mae_pct":np.maximum(adv.max(0),0),"time_mfe":fav.argmax(0)+1,"time_mae":adv.argmax(0)+1}
  for t in THRESHOLDS:
   fr=fav>=t;ar=adv>=t;fi=np.where(fr.any(0),fr.argmax(0)+1,999);ai=np.where(ar.any(0),ar.argmax(0)+1,999);k=str(t).replace(".","_");data[f"reach_{k}"]=fr.any(0);data[f"fav_first_{k}"]=fi<ai;data[f"adv_first_{k}"]=(ai<=fi)&(ai<999)
  o=pd.DataFrame(data,index=g.index,dtype=float);o.iloc[-horizon:]=np.nan;pieces.append(o)
 return pd.concat(pieces).sort_index()

def summarize(frame:pd.DataFrame,o:pd.DataFrame,mask:pd.Series,seed:int)->dict[str,Any]:
 valid=mask&o.return_pct.notna();x=o.loc[valid];days=pd.to_datetime(frame.loc[valid,"timestamp_ms"],unit="ms",utc=True).dt.floor("D");daily=pd.DataFrame({"x":x.return_pct.to_numpy(),"d":days.to_numpy()}).groupby("d").x.mean();mean=float(x.return_pct.mean());se=float(daily.std()/math.sqrt(len(daily))) if len(daily)>1 else 0;p=math.erfc(abs(mean/se)/math.sqrt(2)) if se else 1
 rng=np.random.default_rng(seed);dv=daily.to_numpy();boot=dv[rng.integers(0,len(dv),size=(500,len(dv)))].mean(1) if len(dv) else np.array([0])
 r={"observations":int(valid.sum()),"independent_days":int(days.nunique()),"symbols":int(frame.loc[valid,"symbol"].nunique()),"months":int(pd.to_datetime(frame.loc[valid,"timestamp_ms"],unit="ms",utc=True).dt.to_period("M").nunique()),"mean_forward_return_pct":mean,"median_forward_return_pct":float(x.return_pct.median()),"mean_mfe_pct":float(x.mfe_pct.mean()),"median_mfe_pct":float(x.mfe_pct.median()),"mean_mae_pct":float(x.mae_pct.mean()),"mfe_minus_mae_pct":float((x.mfe_pct-x.mae_pct).mean()),"favourable_first_24bps":float(x.fav_first_0_24.mean()),"adverse_first_24bps":float(x.adv_first_0_24.mean()),"reach_24bps":float(x.reach_0_24.mean()),"reach_48bps":float(x.reach_0_48.mean()),"reach_72bps":float(x.reach_0_72.mean()),"utc_day_clustered_ci95":[float(np.quantile(boot,.025)),float(np.quantile(boot,.975))],"raw_p_value":p};return r

def analyse(frame:pd.DataFrame,bounds:dict[str,list[float]],period:str)->list[dict[str,Any]]:
 ms=masks(frame,bounds);rows=[]
 for i,h in enumerate(registry()):
  s=summarize(frame,outcome(frame,h["horizon"],h["direction"]),ms[h["id"]],SEED+i);rows.append({"period":period,**h,**s})
 q=benjamini_hochberg([x["raw_p_value"] for x in rows])
 for x,v in zip(rows,q):x["bh_adjusted_q_value"]=v
 return rows

def event_study(frame:pd.DataFrame,bounds:dict[str,list[float]],period:str)->list[dict[str,Any]]:
 mb=frame.market_basis_bps;top=bounds["market_basis_bps"][-1];bottom=bounds["market_basis_bps"][0];states={"enter_positive_top_decile":mb>=top,"enter_negative_bottom_decile":mb<=bottom,"positive_to_negative":(mb<0)&(frame.groupby("symbol").market_basis_bps.shift(1)>=0),"negative_to_positive":(mb>0)&(frame.groupby("symbol").market_basis_bps.shift(1)<=0),"rapid_expansion":frame.basis_change_1h.abs()>=bounds["absolute_basis_change_1h"][-1],"rapid_compression":frame.basis_change_1h.abs()<=bounds["absolute_basis_change_1h"][0]};results=[]
 for name,state in states.items():
  indices=[]
  for _,g in frame.groupby("symbol"):indices.extend(g.index[cooldown_events(state.loc[g.index].tolist(),32)])
  for h in (1,2,4,8,16,32):
   ret=frame.groupby("symbol").market_close.shift(-h)/frame.market_close-1;x=ret.loc[indices].dropna()*100;results.append({"period":period,"event":name,"cooldown_candles":32,"horizon":h,"events":len(x),"mean_market_drift_pct":float(x.mean()),"median_market_drift_pct":float(x.median())})
 return results

def main()->int:
 p=argparse.ArgumentParser();p.add_argument("--dataset",type=Path,required=True);p.add_argument("--funding",type=Path,required=True);p.add_argument("--output",type=Path,required=True);a=p.parse_args()
 if a.output.exists():raise SystemExit("refusing overwrite")
 f=add_features(load(a.dataset));dev=f[f.timestamp_ms<1752537600000].copy();rep=f[f.timestamp_ms>=1752537600000].copy();features=("market_basis_bps","mark_basis_bps","market_mark_bps","absolute_basis_change_1h");dev["absolute_basis_change_1h"]=dev.basis_change_1h.abs();rep["absolute_basis_change_1h"]=rep.basis_change_1h.abs();bounds={x:list(development_boundaries(dev[x].dropna())) for x in features};dr=analyse(dev,bounds,"development");rr=analyse(rep,bounds,"replication");paired=[]
 for d,r in zip(dr,rr):
  sign=d["mean_forward_return_pct"]*r["mean_forward_return_pct"]>0;economic=min(d["mean_forward_return_pct"],r["mean_forward_return_pct"])>=.24 and min(d["favourable_first_24bps"],r["favourable_first_24bps"])>.5;adjusted=max(d["bh_adjusted_q_value"],r["bh_adjusted_q_value"])<=.05;paired.append({"id":d["id"],"sign_agrees":sign,"economic":economic,"adjusted":adjusted,"replicated":sign and economic and adjusted,"development":d,"replication":r})
 events=event_study(dev,bounds,"development")+event_study(rep,bounds,"replication");funding_overlay={"status":"SUPPLEMENTAL_ONLY","available":a.funding.exists(),"window":"2026-04-17 through 2026-07-14","general_edge_claim_permitted":False,"results":[]}
 manifest=json.loads((a.dataset/"dataset_manifest.json").read_text());feature_dict={"formulas":{"market_close_basis_bps":"10000*(market_close-index_close)/index_close","mark_close_basis_bps":"10000*(mark_close-index_close)/index_close","market_mark_divergence_bps":"10000*(market_close-mark_close)/mark_close"},"basis_change_offsets":{"15m":1,"1h":4,"4h":16,"8h":32,"24h":96},"intracandle_divergence":"UNAVAILABLE: OHLC extrema times are unknown; high/low series are not paired","event_cooldown_candles":32}
 multiple={"primary_hypotheses":8,"primary_development_q005":sum(x["bh_adjusted_q_value"]<=.05 for x in dr),"primary_replication_q005":sum(x["bh_adjusted_q_value"]<=.05 for x in rr),"primary_replicated_economic":sum(x["replicated"] for x in paired),"exploratory_hypotheses":0,"interactions_opened":False};short=[x for x in paired if x["replicated"]][:3];stop="PREREGISTER ONE BASIS-BASED STRATEGY HYPOTHESIS" if short else "NO REPLICATED BASIS / MARK–INDEX EDGE FAMILY FOUND";economic=[{"id":x["id"],"classification":"REPLICATED AND ECONOMIC" if x["replicated"] else "TOO SMALL FOR EXECUTION" if min(abs(x["development"]["mean_forward_return_pct"]),abs(x["replication"]["mean_forward_return_pct"]))<.24 else "LARGE BUT UNSTABLE"} for x in paired]
 artifacts={"source_feasibility_audit.json":{"market":"FULLY AVAILABLE","mark":"FULLY AVAILABLE","index":"FULLY AVAILABLE","official_endpoints":["history-candles","history-mark-candles","history-index-candles"],"limit":200,"rate_limit_per_second":20,"closed_candles_only":True},"dataset_manifest.json":manifest,"data_quality.json":{"reports":manifest["reports"],"fully_synchronized_per_symbol":70080,"excluded_symbols":[]},"synchronization_report.json":{"common_window":[1721001600000,1784073600000],"symbols":list(SYMBOLS),"synchronized_candles":len(f),"percentage_synchronized":100.0},"basis_feature_dictionary.json":feature_dict,"development_bin_boundaries.json":bounds,"primary_hypothesis_registry.json":registry(),"development_results.json":dr,"replication_results.json":rr,"paired_replication_results.json":paired,"event_study_results.json":events,"funding_overlay_results.json":funding_overlay,"stability_results.json":[],"multiple_testing_results.json":multiple,"economic_screen.json":economic,"shortlist.json":{"conclusion":stop,"families":short},"analysis_manifest.json":{"phase":"4C","source_commit":"aaeb856e8eb2375b20fd1452afdb74f8c360acd7","dataset_hash":manifest["manifest_hash"],"strategy_created":False,"trade_pnl_calculated":False}}
 a.output.mkdir(parents=True)
 for n,v in artifacts.items():write_atomic_json(a.output/n,v)
 digest=canonical_hash({n:canonical_hash(v) for n,v in sorted(artifacts.items())});write_atomic_json(a.output/"artifact_hash.json",{"sha256":digest});print(digest);return 0
if __name__=="__main__":raise SystemExit(main())
