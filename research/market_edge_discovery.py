from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from clients.schemas import Candle

HORIZONS = (1, 2, 4, 8, 16, 32)
THRESHOLDS_PCT = (0.25, 0.50, 0.75, 1.00)
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)
BIN_LABELS = ("BOTTOM_10", "P10_25", "P25_50", "P50_75", "P75_90", "TOP_10")
MIN_BIN_OBSERVATIONS = 500
MAX_INTERACTIONS = 10
FORBIDDEN_PERFORMANCE_FIELDS = {"pnl", "net_pnl", "fees", "profit_factor", "trade", "equity"}


@dataclass(frozen=True)
class ForwardOutcome:
    orientation: str
    horizon: int
    close_return_pct: float
    mfe_pct: float
    mae_pct: float
    mfe_minus_mae_pct: float
    time_to_mfe: int
    time_to_mae: int
    positive_reached: tuple[bool, ...]
    negative_reached: tuple[bool, ...]
    favourable_first: tuple[bool, ...]
    adverse_first: tuple[bool, ...]


def forward_outcome(candles: Sequence[Candle], index: int, horizon: int, orientation: str) -> ForwardOutcome:
    if horizon not in HORIZONS or index < 0 or index + horizon >= len(candles):
        raise ValueError("forward horizon is unavailable")
    orientation = orientation.upper()
    if orientation not in {"LONG", "SHORT"}:
        raise ValueError("orientation must be LONG or SHORT")
    entry = float(candles[index].close)
    path = candles[index + 1:index + horizon + 1]
    sign = 1.0 if orientation == "LONG" else -1.0
    close_return = sign * (float(path[-1].close) - entry) / entry * 100
    favourable = [sign * ((float(candle.high) if orientation == "LONG" else float(candle.low)) - entry) / entry * 100 for candle in path]
    adverse = [-sign * ((float(candle.low) if orientation == "LONG" else float(candle.high)) - entry) / entry * 100 for candle in path]
    mfe = max([0.0] + favourable); mae = max([0.0] + adverse)
    time_mfe = favourable.index(max(favourable)) + 1 if favourable and max(favourable) > 0 else 0
    time_mae = adverse.index(max(adverse)) + 1 if adverse and max(adverse) > 0 else 0
    positive=[];negative=[];fav_first=[];adv_first=[]
    for threshold in THRESHOLDS_PCT:
        f_index=next((i for i,value in enumerate(favourable,1) if value>=threshold),None)
        a_index=next((i for i,value in enumerate(adverse,1) if value>=threshold),None)
        positive.append(f_index is not None);negative.append(a_index is not None)
        fav_first.append(f_index is not None and (a_index is None or f_index<a_index))
        adv_first.append(a_index is not None and (f_index is None or a_index<=f_index))
    return ForwardOutcome(orientation,horizon,close_return,mfe,mae,mfe-mae,time_mfe,time_mae,tuple(positive),tuple(negative),tuple(fav_first),tuple(adv_first))


def development_boundaries(values: Iterable[float]) -> tuple[float, ...]:
    ordered=sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:raise ValueError("development values required")
    def quantile(probability: float) -> float:
        position=(len(ordered)-1)*probability;low=math.floor(position);high=math.ceil(position)
        return ordered[low] if low==high else ordered[low]*(high-position)+ordered[high]*(position-low)
    return tuple(quantile(probability) for probability in QUANTILES)


def apply_frozen_bin(value: float, boundaries: Sequence[float]) -> str:
    if len(boundaries)!=5 or not math.isfinite(float(value)):return "UNKNOWN"
    for boundary,label in zip(boundaries,BIN_LABELS):
        if value<=boundary:return label
    return BIN_LABELS[-1]


def benjamini_hochberg(p_values: Sequence[float]) -> list[float]:
    count=len(p_values);ordered=sorted(enumerate(p_values),key=lambda item:item[1]);adjusted=[1.0]*count;running=1.0
    for rank in range(count,0,-1):
        index,p_value=ordered[rank-1];running=min(running,float(p_value)*count/rank);adjusted[index]=min(1.0,running)
    return adjusted


def normal_two_sided_p(mean: float, standard_error: float) -> float:
    if standard_error<=0:return 0.0 if mean else 1.0
    return math.erfc(abs(mean/standard_error)/math.sqrt(2.0))


def effect_size(mean_value: float, baseline_mean: float, baseline_std: float) -> float:
    return (mean_value-baseline_mean)/baseline_std if baseline_std>0 else 0.0


def enforce_sample_size(count: int, minimum: int = MIN_BIN_OBSERVATIONS) -> None:
    if count<minimum:raise ValueError(f"insufficient observations: {count} < {minimum}")


def validate_interactions(interactions: Sequence[tuple[str,str]]) -> None:
    if len(interactions)>MAX_INTERACTIONS:raise ValueError("more than ten pairwise interactions")
    if any(len(pair)!=2 or pair[0]==pair[1] for pair in interactions):raise ValueError("interactions must contain two distinct factors")


def assert_descriptive_artifact(value: Any) -> None:
    if isinstance(value,dict):
        forbidden={str(key).lower() for key in value}&FORBIDDEN_PERFORMANCE_FIELDS
        if forbidden:raise ValueError(f"strategy performance fields forbidden: {sorted(forbidden)}")
        for item in value.values():assert_descriptive_artifact(item)
    elif isinstance(value,(list,tuple)):
        for item in value:assert_descriptive_artifact(item)


def exclusions_stable(full_mean: float, excluded_means: Sequence[float]) -> bool:
    if full_mean==0:return False
    return all(value*full_mean>0 and abs(value)>=abs(full_mean)*.25 for value in excluded_means)
