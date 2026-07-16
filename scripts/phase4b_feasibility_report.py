from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.derivatives_data import canonical_hash, validate_primary_hypotheses, write_atomic_json

SYMBOLS=("ADAUSDT","AVAXUSDT","BTCUSDT","ETHUSDT","LINKUSDT","SOLUSDT","SUIUSDT","WIFUSDT")
BLOCK={"status":"BLOCKED_DATA_FOUNDATION","reason":"No continuous historical Bitget open-interest dataset is accessible from an approved source; outcome analysis was not opened."}


def hypotheses() -> list[dict]:
    common={"minimum_sample":500,"contradiction_rule":"development/replication sign reversal or magnitude ratio below 0.25","analysis_family":"PRIMARY_PREREGISTERED"}
    values=[
      ("P1","extreme_positive_funding_mean_reversion","funding_percentile=TOP_10","SHORT",16,"positive","crowded long carry may mean-revert"),
      ("P2","extreme_negative_funding_mean_reversion","funding_percentile=BOTTOM_10","LONG",16,"positive","crowded short carry may mean-revert"),
      ("P3","price_up_oi_up_continuation","price_1h>0 and oi_change_1h>0","LONG",8,"positive","price and positioning expand together"),
      ("P4","price_down_oi_up_continuation","price_1h<0 and oi_change_1h>0","SHORT",8,"positive","down move and positioning expand together"),
      ("P5","price_up_oi_down_exhaustion","price_1h>0 and oi_change_1h<0","SHORT",8,"positive","price rise with contracting positioning may exhaust"),
      ("P6","price_down_oi_down_exhaustion","price_1h<0 and oi_change_1h<0","LONG",8,"positive","price fall with contracting positioning may exhaust"),
      ("P7","funding_extreme_oi_acceleration_reversal","absolute_funding=TOP_10 and oi_acceleration=TOP_10","AGAINST_FUNDING_SIGN",16,"positive","extreme carry with accelerating positioning proxies crowding"),
      ("P8","funding_normalization_falling_oi_unwind","funding_abs_change<0 and oi_change_4h<0","WITH_PRIOR_PRICE_DIRECTION",8,"positive","normalizing carry and contracting positioning may extend unwind"),
    ]
    result=[{"id":i,"family":family,"feature":feature,"direction":direction,"horizon":horizon,"expected_sign":sign,"rationale":rationale,**common} for i,family,feature,direction,horizon,sign,rationale in values]
    validate_primary_hypotheses(result);return result


def main() -> int:
    parser=argparse.ArgumentParser();parser.add_argument("--funding-manifest",type=Path,required=True);parser.add_argument("--output",type=Path,required=True);args=parser.parse_args()
    if args.output.exists():raise SystemExit("refusing to overwrite Phase 4B report")
    funding=json.loads(args.funding_manifest.read_text());quality=funding["reports"]
    feasibility={"audit_timestamp_utc":"2026-07-16T10:45:00Z","requested_window":[1721001600000,1784073600000],"sources":[
      {"dataset":"funding","provider":"Bitget","endpoint":"/api/v2/mix/market/history-fund-rate","authentication":"public","pagination":"pageNo 1..100, pageSize max 100","rate_limit":"20 requests/second/IP","timestamp_semantics":"realised settlement timestamp","available_window":[1776441600000,1784044800000],"symbols":list(SYMBOLS),"verdict":"PARTIALLY AVAILABLE","limitation":"endpoint returned 270 8h records per symbol (540 4h WIF), only 2026-04-17 through requested end"},
      {"dataset":"open_interest","provider":"Bitget","endpoint":"/api/v2/mix/market/open-interest","authentication":"public","granularity":"current snapshot only","unit":"base coin size","symbols":list(SYMBOLS),"verdict":"UNAVAILABLE FROM BITGET","limitation":"no historical timestamp, range or pagination parameters"},
      {"dataset":"funding_and_open_interest","provider":"Tardis.dev","endpoint":"bitget-futures derivative_ticker","authentication":"API key/subscription required except first day of each month","available_window":[1731024000000,1784073600000],"symbols":list(SYMBOLS),"exchange_specific":True,"methodology":"captured Bitget public ticker WebSocket; funding, OI, mark and index fields","verdict":"REQUIRES EXTERNAL SOURCE","limitation":"starts 2024-11-08, omits first 116 requested days; continuous download requires paid access"}],"implementation_gate":"CLOSED: no approved continuous OI source is locally accessible"}
    oi_manifest={"schema_version":1,"dataset":"open_interest","status":"UNAVAILABLE","bitget_endpoint_is_snapshot_only":True,"external_candidate":"Tardis.dev bitget-futures derivative_ticker","external_available_since_ms":1731024000000,"continuous_access":"REQUIRES_SUBSCRIPTION","records":0,"fabricated_records":0}
    synchronization={"requested_window":[1721001600000,1784073600000],"largest_common_funding_window":[1776441600000,1784044800000],"largest_common_oi_window":None,"largest_accessible_synchronized_ohlcv_funding_oi_window":None,"potential_external_common_window":[1731024000000,1784073600000],"potential_split":"must be frozen only after paid-source acquisition and quality validation",**BLOCK}
    feature_dictionary={"status":"FROZEN_BEFORE_OUTCOMES","funding_level":["realised_rate","absolute_rate","sign","development_percentile","rolling_zscore","consecutive_sign"],"funding_change":["previous_settlement_change","acceleration","cumulative_8h","cumulative_24h","cumulative_72h"],"oi_level":["development_percentile","rolling_zscore","relative_to_rolling_median","notional_usdt"],"oi_change":["15m","1h","4h","8h","24h","log_change","change_relative_to_volume"],"interactions":["price_x_oi_state","funding_x_oi_state","crowding_proxy","unwind_proxy"],"interpretation_warning":"No state is labelled liquidation or new long/short without direct data."}
    args.output.mkdir(parents=True)
    artifacts={"source_feasibility_audit.json":feasibility,"funding_manifest.json":funding,"open_interest_manifest.json":oi_manifest,"data_quality.json":{"funding":quality,"open_interest":oi_manifest},"synchronization_report.json":synchronization,"feature_dictionary.json":feature_dictionary,"primary_hypothesis_registry.json":hypotheses()}
    for name in ("development_bin_boundaries.json","single_factor_results.json","funding_oi_interaction_results.json","funding_event_study.json","replication_results.json","stability_results.json","multiple_testing_correction.json","economic_screen.json","shortlist.json"):
        artifacts[name]={**BLOCK,"results":[]}
    manifest={"phase":"4B","source_commit":"878356a471fdb228e815638ce4f61e9103b79bf7","phase4a_artifact_hash":"b3759b5ab034616b7b7e192caa5a593552633d56ed77dea90edd4d0172302286","outcome_analysis_opened":False,"strategy_created":False,"trade_pnl_calculated":False,"artifacts":sorted(artifacts)};artifacts["analysis_manifest.json"]=manifest
    for name,value in artifacts.items():write_atomic_json(args.output/name,value)
    digest=canonical_hash({name:canonical_hash(value) for name,value in sorted(artifacts.items())});write_atomic_json(args.output/"artifact_hash.json",{"sha256":digest});print(digest);return 0


if __name__=="__main__":raise SystemExit(main())
