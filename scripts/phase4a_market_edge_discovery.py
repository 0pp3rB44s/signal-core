from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.market_edge_discovery import (
    HORIZONS, MIN_BIN_OBSERVATIONS, THRESHOLDS_PCT, apply_frozen_bin,
    assert_descriptive_artifact, benjamini_hochberg, development_boundaries,
    effect_size, exclusions_stable, normal_two_sided_p, validate_interactions,
)

DEVELOPMENT_START=1721001600000;DEVELOPMENT_END=1752537600000
REPLICATION_START=DEVELOPMENT_END;REPLICATION_END=1784073600000
SYMBOLS=("ADAUSDT","AVAXUSDT","BTCUSDT","ETHUSDT","LINKUSDT","SOLUSDT","SUIUSDT","WIFUSDT")
SEED=20260716;BOOTSTRAPS=200
CONTINUOUS=(
    "ema20_slope_atr","distance_ema20_atr","return_1","return_2","return_4","return_8","rsi14","roc8",
    "atr_pct","realized_vol20","range_pct","bb_width","compression_duration","volume_ratio","volume_percentile",
    "movement_per_volume","range_position20","range_position48","range_position96","distance_recent_high_atr",
    "distance_recent_low_atr","distance_vwap_atr","distance_session_high_atr","distance_session_low_atr",
    "distance_prev_day_high_atr","distance_prev_day_low_atr","body_ratio","upper_wick_ratio","lower_wick_ratio",
    "close_location","btc_return_4","btc_atr_pct","broad_fraction_up","broad_dispersion","relative_return_btc","volatility_relative_btc",
)
CATEGORICAL=(
    "trend15","trend1h","structure","consecutive_direction","body_direction","volatility_transition",
    "high_volume_expansion","low_volume_drift","inside_bar","outside_bar","engulfing_body","range_expansion",
    "utc_hour","session","overlap","weekday","month","broad_direction",
)
PAIR_CANDIDATES=(
    # Registered after the exploratory single-factor pass and frozen before the
    # official pairwise pass.  These are causal state combinations, not an
    # exhaustive interaction search.
    ("broad_dispersion","utc_hour"),("broad_dispersion","atr_pct"),
    ("broad_dispersion","btc_atr_pct"),("utc_hour","atr_pct"),
    ("btc_atr_pct","atr_pct"),
)


def canonical_hash(value:Any)->str:return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode()).hexdigest()
def write_json(path:Path,value:Any)->None:assert_descriptive_artifact(value);path.write_text(json.dumps(value,indent=2,sort_keys=True)+"\n")


def load(path:Path,start:int,end:int)->dict[str,pd.DataFrame]:
    result={}
    for symbol in SYMBOLS:
        payload=json.loads((path/f"{symbol}.json").read_text());rows=payload.get("candles",payload) if isinstance(payload,dict) else payload
        frame=pd.DataFrame(rows);frame["timestamp_ms"]=frame.get("timestamp_ms",frame.get("timestamp")).astype("int64")
        for column in ("open","high","low","close","volume_base"):frame[column]=frame[column].astype(float)
        frame=frame.sort_values("timestamp_ms").drop_duplicates("timestamp_ms")
        if len(frame)!=35040 or int(frame.timestamp_ms.iloc[0])!=start or int(frame.timestamp_ms.iloc[-1])+900000!=end or not np.all(np.diff(frame.timestamp_ms)==900000):raise ValueError(f"invalid frozen dataset {symbol}")
        frame["symbol"]=symbol;result[symbol]=frame.reset_index(drop=True)
    return result


def ema(series:pd.Series,period:int)->pd.Series:return series.ewm(span=period,adjust=False).mean()
def percentile_rank(series:pd.Series,window:int=480)->pd.Series:return series.rolling(window,min_periods=window).apply(lambda x: pd.Series(x).rank(pct=True).iloc[-1],raw=False)


def features(frame:pd.DataFrame)->pd.DataFrame:
    f=frame.copy();c=f.close;prev=c.shift(1);tr=pd.concat([f.high-f.low,(f.high-prev).abs(),(f.low-prev).abs()],axis=1).max(axis=1);atr=tr.rolling(14).mean()
    e20=ema(c,20);e50=ema(c,50);f["trend15"]=np.where(e20>e50,"UP","DOWN");f["ema20_slope_atr"]=(e20-e20.shift(4))/atr;f["distance_ema20_atr"]=(c-e20)/atr
    for horizon in (1,2,4,8):f[f"return_{horizon}"]=c.pct_change(horizon)*100
    f["roc8"]=f.return_8;delta=c.diff();gain=delta.clip(lower=0).rolling(14).mean();loss=(-delta.clip(upper=0)).rolling(14).mean();f["rsi14"]=100-100/(1+gain/loss.replace(0,np.nan))
    direction=np.sign(delta);runs=[];last=0;count=0
    for value in direction.fillna(0):
        if value and value==last:count+=1
        elif value:count=1;last=value
        else:count=0;last=0
        runs.append(count*last)
    f["consecutive_direction"]=pd.cut(runs,[-99,-4,-2,0,2,4,99],labels=["DOWN_5P","DOWN_3_4","DOWN_1_2","UP_1_2","UP_3_4","UP_5P"],include_lowest=True).astype(str)
    higher=(f.high>f.high.shift(4))&(f.low>f.low.shift(4));lower=(f.high<f.high.shift(4))&(f.low<f.low.shift(4));f["structure"]=np.where(higher,"HH_HL",np.where(lower,"LH_LL","MIXED"))
    f["atr_pct"]=atr/c*100;f["realized_vol20"]=f.return_1.rolling(20).std();f["range_pct"]=(f.high-f.low)/c*100
    mean20=c.rolling(20).mean();std20=c.rolling(20).std(ddof=0);f["bb_width"]=4*std20/mean20*100
    atr_rank=percentile_rank(f.atr_pct);rv_rank=percentile_rank(f.realized_vol20);range_rank=percentile_rank(f.range_pct);bb_rank=percentile_rank(f.bb_width)
    compressed=(atr_rank<=.25)&(bb_rank<=.25);groups=(~compressed).cumsum();f["compression_duration"]=compressed.groupby(groups).cumsum()
    f["volatility_transition"]=np.where(compressed.shift(1,fill_value=False)&(range_rank>.75),"LOW_TO_EXPANDING","OTHER")
    volume_mean=f.volume_base.rolling(20).mean();f["volume_ratio"]=f.volume_base/volume_mean;f["volume_percentile"]=percentile_rank(f.volume_base);f["movement_per_volume"]=f.return_1.abs()/f.volume_ratio.replace(0,np.nan)
    f["high_volume_expansion"]=np.where((f.volume_ratio>=1.5)&(range_rank>.75),"YES","NO");f["low_volume_drift"]=np.where((f.volume_ratio<.75)&(f.return_4.abs()>.15),"YES","NO")
    for window in (20,48,96):
        high=f.high.shift(1).rolling(window).max();low=f.low.shift(1).rolling(window).min();f[f"range_position{window}"]=(c-low)/(high-low)
        if window==20:f["distance_recent_high_atr"]=(high-c)/atr;f["distance_recent_low_atr"]=(c-low)/atr
    typical=(f.high+f.low+c)/3;vwap=(typical*f.volume_base).rolling(96).sum()/f.volume_base.rolling(96).sum();f["distance_vwap_atr"]=(c-vwap)/atr
    dt=pd.to_datetime(f.timestamp_ms,unit="ms",utc=True);f["utc_hour"]=dt.dt.hour.astype(str);f["weekday"]=dt.dt.day_name();f["month"]=dt.dt.strftime("%m")
    f["session"]=np.where(dt.dt.hour<8,"ASIA",np.where(dt.dt.hour<16,"EUROPE","US"));f["overlap"]=np.where(dt.dt.hour.isin([7,8]),"ASIA_EUROPE",np.where(dt.dt.hour.isin([13,14,15,16]),"EUROPE_US","NONE"))
    day=dt.dt.floor("D");session_key=day.astype(str)+f.session;session_high=f.high.groupby(session_key).cummax();session_low=f.low.groupby(session_key).cummin();f["distance_session_high_atr"]=(session_high-c)/atr;f["distance_session_low_atr"]=(c-session_low)/atr
    day_high=f.high.groupby(day).max();day_low=f.low.groupby(day).min();previous_high=day.map(day_high.shift(1));previous_low=day.map(day_low.shift(1));f["distance_prev_day_high_atr"]=(previous_high.to_numpy()-c)/atr;f["distance_prev_day_low_atr"]=(c-previous_low.to_numpy())/atr
    candle_range=(f.high-f.low).replace(0,np.nan);f["body_ratio"]=(c-f.open).abs()/candle_range;f["upper_wick_ratio"]=(f.high-np.maximum(f.open,c))/candle_range;f["lower_wick_ratio"]=(np.minimum(f.open,c)-f.low)/candle_range;f["close_location"]=(c-f.low)/candle_range
    f["body_direction"]=np.where(c>f.open,"UP",np.where(c<f.open,"DOWN","FLAT"));f["inside_bar"]=np.where((f.high<f.high.shift(1))&(f.low>f.low.shift(1)),"YES","NO");f["outside_bar"]=np.where((f.high>f.high.shift(1))&(f.low<f.low.shift(1)),"YES","NO")
    f["engulfing_body"]=np.where((np.minimum(f.open,c)<=np.minimum(f.open.shift(1),c.shift(1)))&(np.maximum(f.open,c)>=np.maximum(f.open.shift(1),c.shift(1))),"YES","NO");f["range_expansion"]=np.where(f.range_pct>f.range_pct.shift(1)*1.5,"YES","NO")
    htf_close=c.where((f.timestamp_ms%3600000)==2700000).dropna();h20=ema(htf_close,20);h50=ema(htf_close,50);rel=pd.Series(np.where(h20>h50,"UP","DOWN"),index=h20.index);f["trend1h"]=rel.reindex(f.index).ffill()
    f["atr_value"]=atr;return f


def add_cross_context(frames:dict[str,pd.DataFrame])->pd.DataFrame:
    all_frame=pd.concat([features(frame) for frame in frames.values()],ignore_index=True)
    returns=all_frame.pivot(index="timestamp_ms",columns="symbol",values="return_4");vol=all_frame.pivot(index="timestamp_ms",columns="symbol",values="atr_pct")
    fraction=(returns>0).mean(axis=1);dispersion=returns.std(axis=1);btc_return=returns["BTCUSDT"];btc_vol=vol["BTCUSDT"]
    all_frame["broad_fraction_up"]=all_frame.timestamp_ms.map(fraction);all_frame["broad_dispersion"]=all_frame.timestamp_ms.map(dispersion);all_frame["btc_return_4"]=all_frame.timestamp_ms.map(btc_return);all_frame["btc_atr_pct"]=all_frame.timestamp_ms.map(btc_vol)
    all_frame["relative_return_btc"]=all_frame.return_4-all_frame.btc_return_4;all_frame["volatility_relative_btc"]=all_frame.atr_pct/all_frame.btc_atr_pct.replace(0,np.nan);all_frame["broad_direction"]=np.where(all_frame.broad_fraction_up>=.625,"BROAD_UP",np.where(all_frame.broad_fraction_up<=.375,"BROAD_DOWN","MIXED"))
    return all_frame


def outcomes(frame:pd.DataFrame)->dict[tuple[str,int],pd.DataFrame]:
    result={};close=frame.close.to_numpy();high=frame.high.to_numpy();low=frame.low.to_numpy();length=len(frame)
    for horizon in HORIZONS:
        high_stack=np.vstack([np.roll(high,-offset) for offset in range(1,horizon+1)]);low_stack=np.vstack([np.roll(low,-offset) for offset in range(1,horizon+1)]);future_close=np.roll(close,-horizon)
        invalid=np.arange(length)>=length-horizon
        for orientation in ("LONG","SHORT"):
            sign=1 if orientation=="LONG" else -1;close_return=sign*(future_close-close)/close*100;mfe=(high_stack.max(axis=0)-close)/close*100 if orientation=="LONG" else (close-low_stack.min(axis=0))/close*100;mae=(close-low_stack.min(axis=0))/close*100 if orientation=="LONG" else (high_stack.max(axis=0)-close)/close*100
            time_mfe=(high_stack.argmax(axis=0)+1) if orientation=="LONG" else (low_stack.argmin(axis=0)+1);time_mae=(low_stack.argmin(axis=0)+1) if orientation=="LONG" else (high_stack.argmax(axis=0)+1)
            data={"close_return_pct":close_return,"mfe_pct":np.maximum(mfe,0),"mae_pct":np.maximum(mae,0),"time_to_mfe":time_mfe,"time_to_mae":time_mae}
            favourable_paths=(high_stack-close)/close*100 if orientation=="LONG" else (close-low_stack)/close*100;adverse_paths=(close-low_stack)/close*100 if orientation=="LONG" else (high_stack-close)/close*100
            for threshold in THRESHOLDS_PCT:
                f=favourable_paths>=threshold;a=adverse_paths>=threshold;fi=np.where(f.any(axis=0),f.argmax(axis=0)+1,999);ai=np.where(a.any(axis=0),a.argmax(axis=0)+1,999);key=str(threshold).replace(".","_");data[f"positive_{key}"]=f.any(axis=0);data[f"negative_{key}"]=a.any(axis=0);data[f"favourable_first_{key}"]=(fi<ai);data[f"adverse_first_{key}"]=(ai<=fi)&(ai<999)
            out=pd.DataFrame(data).astype(float);out.loc[invalid,:]=np.nan;result[(orientation,horizon)]=out
    return result


def bootstrap_ci(values:np.ndarray,days:pd.Series,seed:int)->list[float]:
    valid=np.isfinite(values);grouped=pd.DataFrame({"value":values[valid],"day":days.to_numpy()[valid]}).groupby("day").value.agg(["sum","count"])
    if grouped.empty:return [0.0,0.0]
    rng=np.random.default_rng(seed);indices=rng.integers(0,len(grouped),size=(BOOTSTRAPS,len(grouped)));sums=grouped["sum"].to_numpy()[indices].sum(axis=1);counts=grouped["count"].to_numpy()[indices].sum(axis=1);sampled=sums/counts
    return [float(np.quantile(sampled,.025)),float(np.quantile(sampled,.975))]


def summarize(values:pd.DataFrame,days:pd.Series,symbols:pd.Series,baseline:dict[str,float],seed:int)->dict[str,Any]:
    returns=values.close_return_pct.to_numpy(dtype=float);count=int(np.isfinite(returns).sum());mean_return=float(np.nanmean(returns));daily=pd.DataFrame({"value":returns,"day":days.to_numpy()}).dropna().groupby("day").value.mean();se=float(daily.std(ddof=1)/math.sqrt(len(daily))) if len(daily)>1 else 0
    result={"observations":count,"independent_days":int(days.nunique()),"symbols":int(symbols.nunique()),"mean_forward_return_pct":mean_return,"median_forward_return_pct":float(np.nanmedian(returns)),"mean_mfe_pct":float(np.nanmean(values.mfe_pct)),"median_mfe_pct":float(np.nanmedian(values.mfe_pct)),"mean_mae_pct":float(np.nanmean(values.mae_pct)),"mfe_minus_mae_pct":float(np.nanmean(values.mfe_pct-values.mae_pct)),"mean_time_to_mfe":float(np.nanmean(values.time_to_mfe)),"mean_time_to_mae":float(np.nanmean(values.time_to_mae)),"bootstrap_mean_ci95":bootstrap_ci(returns,days,seed),"raw_p_value":normal_two_sided_p(mean_return,se),"effect_size_vs_unconditional":effect_size(mean_return,baseline["mean"],baseline["std"])}
    for threshold in THRESHOLDS_PCT:
        key=str(threshold).replace(".","_");result[f"positive_reach_{key}"]=float(values[f"positive_{key}"].mean());result[f"negative_reach_{key}"]=float(values[f"negative_{key}"].mean());result[f"favourable_first_{key}"]=float(values[f"favourable_first_{key}"].mean());result[f"adverse_first_{key}"]=float(values[f"adverse_first_{key}"].mean())
    return result


def bins(frame:pd.DataFrame,boundaries:dict[str,list[float]])->pd.DataFrame:
    result=pd.DataFrame(index=frame.index)
    for feature in CONTINUOUS:result[feature]=frame[feature].map(lambda value:apply_frozen_bin(float(value),boundaries[feature]) if pd.notna(value) else "UNKNOWN")
    for feature in CATEGORICAL:result[feature]=frame[feature].fillna("UNKNOWN").astype(str)
    return result


def analyse(frame:pd.DataFrame,binned:pd.DataFrame,period:str,feature_names:tuple[str,...]|None=None)->tuple[list[dict[str,Any]],dict[str,Any]]:
    by_symbol={symbol:outcomes(group.reset_index(drop=True)) for symbol,group in frame.groupby("symbol",sort=True)};rows=[];baselines={}
    day=pd.to_datetime(frame.timestamp_ms,unit="ms",utc=True).dt.floor("D")
    for orientation in ("LONG","SHORT"):
      for horizon in HORIZONS:
        pieces=[]
        for symbol,indices in frame.groupby("symbol",sort=True).groups.items():piece=by_symbol[symbol][(orientation,horizon)].copy();piece.index=indices;pieces.append(piece)
        outcome=pd.concat(pieces).sort_index();valid=outcome.close_return_pct.notna();base_values=outcome.loc[valid,"close_return_pct"];base={"mean":float(base_values.mean()),"median":float(base_values.median()),"std":float(base_values.std()),"mean_mfe_pct":float(outcome.loc[valid,"mfe_pct"].mean()),"mean_mae_pct":float(outcome.loc[valid,"mae_pct"].mean()),"observations":int(valid.sum())};baselines[f"{orientation}_{horizon}"]=base
        for feature in feature_names or CONTINUOUS+CATEGORICAL:
          labels=binned[feature]
          for label in sorted(set(labels)-{"UNKNOWN"}):
            mask=valid&(labels==label);count=int(mask.sum())
            if count<MIN_BIN_OBSERVATIONS:continue
            summary=summarize(outcome.loc[mask],day.loc[mask],frame.loc[mask,"symbol"],base,SEED+len(rows));rows.append({"period":period,"feature":feature,"bin":label,"orientation":orientation,"horizon":horizon,**summary})
    adjusted=benjamini_hochberg([row["raw_p_value"] for row in rows])
    for row,q in zip(rows,adjusted):row["bh_adjusted_p_value"]=q
    return rows,baselines


def stability(effect:dict[str,Any],frame:pd.DataFrame,binned:pd.DataFrame)->dict[str,Any]:
    feature=effect["feature"];label=effect["bin"];orientation=effect["orientation"];horizon=effect["horizon"];sign=1 if orientation=="LONG" else -1
    working=frame.copy();working["forward"]=sign*(working.groupby("symbol").close.shift(-horizon)-working.close)/working.close*100;selected=working[(binned[feature]==label)].dropna(subset=["forward"]).copy();full=float(selected.forward.mean());dt=pd.to_datetime(selected.timestamp_ms,unit="ms",utc=True);selected["month_key"]=dt.dt.strftime("%Y-%m");selected["session_key"]=np.where(dt.dt.hour<8,"ASIA",np.where(dt.dt.hour<16,"EUROPE","US"))
    symbol_means=selected.groupby("symbol").forward.agg(["count","mean"]);breadth=int(((symbol_means["count"]>=50)&(symbol_means["mean"]*full>0)).sum())
    strongest_symbol=selected.groupby("symbol").forward.mean().abs().idxmax();strongest_month=selected.groupby("month_key").forward.mean().abs().idxmax()
    exclusions=[float(selected[selected.symbol!="BTCUSDT"].forward.mean()),float(selected[selected.symbol!="WIFUSDT"].forward.mean()),float(selected[selected.symbol!=strongest_symbol].forward.mean()),float(selected[selected.month_key!=strongest_month].forward.mean())]
    session_means=selected.groupby("session_key").forward.mean().to_dict();return {"feature":feature,"bin":label,"orientation":orientation,"horizon":horizon,"full_mean":full,"symbol_breadth":breadth,"symbol_means":symbol_means.reset_index().to_dict("records"),"session_means":session_means,"excluded_means":{"without_btc":exclusions[0],"without_wif":exclusions[1],"without_strongest_symbol":exclusions[2],"without_strongest_month":exclusions[3]},"stable_exclusions":exclusions_stable(full,exclusions),"strongest_symbol":strongest_symbol,"strongest_month":strongest_month}


def main()->int:
    parser=argparse.ArgumentParser();parser.add_argument("--development",type=Path,required=True);parser.add_argument("--replication",type=Path,required=True);parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    if args.output.exists():raise SystemExit("refusing to overwrite Phase 4A output")
    development=add_cross_context(load(args.development,DEVELOPMENT_START,DEVELOPMENT_END));boundaries={feature:list(development_boundaries(development[feature].dropna())) for feature in CONTINUOUS};development_bins=bins(development,boundaries)
    replication=add_cross_context(load(args.replication,REPLICATION_START,REPLICATION_END));replication_bins=bins(replication,boundaries)
    development_results,development_baseline=analyse(development,development_bins,"development");replication_results,replication_baseline=analyse(replication,replication_bins,"replication")
    by_dev={(r["feature"],r["bin"],r["orientation"],r["horizon"]):r for r in development_results};by_rep={(r["feature"],r["bin"],r["orientation"],r["horizon"]):r for r in replication_results};replicated_statistical=[];replicated=[];rejected=[]
    for key,left in by_dev.items():
        right=by_rep.get(key)
        if not right:continue
        signs=left["mean_forward_return_pct"]*right["mean_forward_return_pct"]>0;adjusted=left["bh_adjusted_p_value"]<=.05 and right["bh_adjusted_p_value"]<=.05
        directional_economic=(abs(left["mean_forward_return_pct"])>=.24 and abs(right["mean_forward_return_pct"])>=.24 and left["favourable_first_0_25"]>left["adverse_first_0_25"] and right["favourable_first_0_25"]>right["adverse_first_0_25"])
        item={"feature":key[0],"bin":key[1],"orientation":key[2],"horizon":key[3],"development":left,"replication":right,"sign_agrees":signs,"economic_screen":directional_economic,"adjusted_evidence":adjusted}
        if signs and adjusted:replicated_statistical.append(item)
        (replicated if signs and directional_economic and adjusted else rejected).append(item)
    replicated_statistical=sorted(replicated_statistical,key=lambda x:abs(x["replication"]["mean_forward_return_pct"]),reverse=True);replicated=sorted(replicated,key=lambda x:abs(x["replication"]["mean_forward_return_pct"]),reverse=True)
    stability_results=[stability(item,replication,replication_bins) for item in replicated_statistical[:100]];stable_keys={(r["feature"],r["bin"],r["orientation"],r["horizon"]) for r in stability_results if r["symbol_breadth"]>=3 and r["stable_exclusions"]}
    stable_effects=[item for item in replicated if (item["feature"],item["bin"],item["orientation"],item["horizon"]) in stable_keys]
    replicated_features={item["feature"] for item in replicated_statistical};interactions=[pair for pair in PAIR_CANDIDATES if pair[0] in replicated_features and pair[1] in replicated_features][:10];validate_interactions(interactions)
    pair_dev=[];pair_rep=[]
    for first,second in interactions:
      for label,frame,binned,target in (("development",development,development_bins,pair_dev),("replication",replication,replication_bins,pair_rep)):
        combined=binned[first]+"__"+binned[second];temp=binned.copy();temp[first+"__"+second]=combined
        subset_features=(first+"__"+second,)
        # Pair outcomes use the same frozen aggregator on one categorical feature.
        results,_=analyse(frame,temp,label,subset_features)
        for row in results:row["interaction"]=[first,second]
        target.extend(results)
    for rows in (pair_dev,pair_rep):
        adjusted=benjamini_hochberg([row["raw_p_value"] for row in rows])
        for row,q in zip(rows,adjusted):row["bh_adjusted_p_value"]=q
    pair_dev_by_key={(tuple(r["interaction"]),r["bin"],r["orientation"],r["horizon"]):r for r in pair_dev};pair_evaluation=[]
    for right in pair_rep:
        key=(tuple(right["interaction"]),right["bin"],right["orientation"],right["horizon"]);left=pair_dev_by_key.get(key)
        if not left:continue
        sign_agrees=left["mean_forward_return_pct"]*right["mean_forward_return_pct"]>0;adjusted=max(left["bh_adjusted_p_value"],right["bh_adjusted_p_value"])<=.05
        economic=(left["mean_forward_return_pct"]>=.24 and right["mean_forward_return_pct"]>=.24 and left["median_forward_return_pct"]>0 and right["median_forward_return_pct"]>0 and min(left["median_mfe_pct"],right["median_mfe_pct"])>=.24 and min(left["bootstrap_mean_ci95"])>0 and min(right["bootstrap_mean_ci95"])>0)
        item={"interaction":list(key[0]),"bin":key[1],"orientation":key[2],"horizon":key[3],"development":left,"replication":right,"sign_agrees":sign_agrees,"adjusted_evidence":adjusted,"economic_screen":economic,"stable":False}
        if economic and adjusted:
            first,second=key[0];combined_name=first+"__"+second;development_pair_bins=development_bins.copy();replication_pair_bins=replication_bins.copy();development_pair_bins[combined_name]=development_bins[first]+"__"+development_bins[second];replication_pair_bins[combined_name]=replication_bins[first]+"__"+replication_bins[second]
            effect={"feature":combined_name,"bin":key[1],"orientation":key[2],"horizon":key[3]};dev_stability=stability(effect,development,development_pair_bins);rep_stability=stability(effect,replication,replication_pair_bins)
            def robust(result:dict[str,Any])->bool:
                agreeing_sessions=sum(value*result["full_mean"]>0 for value in result["session_means"].values())
                return result["symbol_breadth"]>=3 and result["stable_exclusions"] and agreeing_sessions>=2
            magnitude_ratio=min(abs(left["mean_forward_return_pct"]),abs(right["mean_forward_return_pct"]))/max(abs(left["mean_forward_return_pct"]),abs(right["mean_forward_return_pct"]))
            item.update({"development_stability":dev_stability,"replication_stability":rep_stability,"magnitude_ratio":magnitude_ratio,"stable":robust(dev_stability) and robust(rep_stability) and magnitude_ratio>=.25})
        pair_evaluation.append(item)
    screen=[]
    for item in replicated_statistical:
        med=min(item["development"]["median_mfe_pct"],item["replication"]["median_mfe_pct"]);stable=(item["feature"],item["bin"],item["orientation"],item["horizon"]) in stable_keys
        classification="TOO SMALL FOR COSTS" if med<.24 else "LARGE BUT UNSTABLE" if not item["economic_screen"] else "MARGINALLY ECONOMIC" if med<.36 else "ECONOMICALLY PLAUSIBLE" if stable else "LARGE BUT UNSTABLE"
        screen.append({"feature":item["feature"],"bin":item["bin"],"orientation":item["orientation"],"horizon":item["horizon"],"minimum_year_median_mfe_pct":med,"minimum_year_absolute_mean_return_pct":min(abs(item["development"]["mean_forward_return_pct"]),abs(item["replication"]["mean_forward_return_pct"])),"development_favourable_first_0_25":item["development"]["favourable_first_0_25"],"development_adverse_first_0_25":item["development"]["adverse_first_0_25"],"replication_favourable_first_0_25":item["replication"]["favourable_first_0_25"],"replication_adverse_first_0_25":item["replication"]["adverse_first_0_25"],"round_trip_cost_pct":.24,"classification":classification,"stable":stable})
    family_map={"relative_strength_continuation":{"relative_return_btc","broad_direction","btc_return_4"},"cross_market_dispersion":{"broad_dispersion","broad_fraction_up","volatility_relative_btc"},"volatility_transition":{"volatility_transition","atr_pct","bb_width","range_expansion"},"range_location_exhaustion":{"range_position20","range_position48","range_position96","rsi14"},"trend_momentum":{"trend1h","trend15","return_4","return_8","structure"},"session_state":{"session","overlap","utc_hour"}}
    shortlist=[]
    for family,factors in family_map.items():
        evidence=[item for item in stable_effects if item["feature"] in factors]
        if evidence:shortlist.append({"family":family,"replicated_effects":len(evidence),"top_effects":[{"feature":x["feature"],"bin":x["bin"],"orientation":x["orientation"],"horizon":x["horizon"],"development_mean_pct":x["development"]["mean_forward_return_pct"],"replication_mean_pct":x["replication"]["mean_forward_return_pct"]} for x in evidence[:5]]})
    stable_pairs=[item for item in pair_evaluation if item["stable"]]
    if stable_pairs:
        shortlist.append({"family":"high_dispersion_high_volatility_directional_drift","replicated_effects":len(stable_pairs),"top_effects":[{"interaction":x["interaction"],"bin":x["bin"],"orientation":x["orientation"],"horizon":x["horizon"],"development_mean_pct":x["development"]["mean_forward_return_pct"],"replication_mean_pct":x["replication"]["mean_forward_return_pct"]} for x in sorted(stable_pairs,key=lambda x:abs(x["replication"]["mean_forward_return_pct"]),reverse=True)[:5]]})
    shortlist=sorted(shortlist,key=lambda x:x["replicated_effects"],reverse=True)[:3]
    feature_dictionary={"continuous":list(CONTINUOUS),"categorical":list(CATEGORICAL),"definitions":"See transparent formulas in scripts/phase4a_market_edge_discovery.py; candle volume is participation only, never orderbook liquidity.","outcomes":{"horizons":list(HORIZONS),"thresholds_pct":list(THRESHOLDS_PCT),"orientations":["LONG","SHORT"]}}
    multiple={"method":"Benjamini-Hochberg FDR independently within each year and analysis family","development_hypotheses":len(development_results),"replication_hypotheses":len(replication_results),"development_adjusted_005":sum(r["bh_adjusted_p_value"]<=.05 for r in development_results),"replication_adjusted_005":sum(r["bh_adjusted_p_value"]<=.05 for r in replication_results),"replicated_statistical":len(replicated_statistical),"replicated_economic_adjusted":len(replicated),"stable_replicated_economic":len(stable_effects),"pair_development_hypotheses":len(pair_dev),"pair_replication_hypotheses":len(pair_rep),"stable_replicated_economic_pairs":len(stable_pairs)}
    manifest={"phase":"4A","source_commit":"cad0693d24ee245245d479a5d23a066d32ed50f4","development":[DEVELOPMENT_START,DEVELOPMENT_END],"replication":[REPLICATION_START,REPLICATION_END],"symbols":list(SYMBOLS),"source_timeframe":"15m","cost_reference_pct":.24,"seed":SEED,"bootstraps":BOOTSTRAPS,"bootstrap_unit":"UTC day blocks","strategy_execution":False,"strategy_performance_loaded":False}
    args.output.mkdir(parents=True);write_json(args.output/"feature_dictionary.json",feature_dictionary);write_json(args.output/"development_bin_boundaries.json",boundaries);write_json(args.output/"unconditional_forward_baselines.json",{"development":development_baseline,"replication":replication_baseline});write_json(args.output/"single_factor_development.json",development_results);write_json(args.output/"single_factor_replication.json",replication_results);write_json(args.output/"stability_results.json",stability_results);write_json(args.output/"pairwise_interaction_registry.json",[{"first":a,"second":b} for a,b in interactions]);write_json(args.output/"pairwise_development.json",pair_dev);write_json(args.output/"pairwise_replication.json",pair_rep);write_json(args.output/"pairwise_evaluation.json",pair_evaluation);write_json(args.output/"economic_movement_screen.json",screen);write_json(args.output/"multiple_testing_adjustment.json",multiple);write_json(args.output/"shortlist_evidence.json",shortlist);write_json(args.output/"rejected_effects.json",rejected);write_json(args.output/"analysis_manifest.json",manifest)
    digest=hashlib.sha256(b"".join(path.read_bytes() for path in sorted(args.output.iterdir()) if path.name!="artifact_hash.txt")).hexdigest();(args.output/"artifact_hash.txt").write_text(digest+"\n");print(digest);return 0


if __name__=="__main__":raise SystemExit(main())
