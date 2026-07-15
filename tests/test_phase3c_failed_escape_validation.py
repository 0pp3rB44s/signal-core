from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from backtesting.execution_contract import BacktestExecutionConfig, BacktestExecutionContract
from clients.schemas import Candle
from research.failed_range_escape_reversal_v1 import (
    FINAL_R, MAX_HOLD_CANDLES, MIN_BODY_FRACTION, MIN_ESCAPE_ATR,
    MIN_REENTRY_ATR, RANGE_LOOKBACK, STRATEGY, TP1_R, atr14,
    closed_hourly_context, detect_at,
)
from research.preregistration_protocol import LOCKED_ACCEPTANCE_CRITERIA
from scripts.phase3c_failed_escape_validation import (
    execute_candidates, finalize_locked_eighth_candle, predicted_market_entry,
    verify_contract,
)

ROOT = Path(__file__).resolve().parents[1]
PREREG = ROOT / "research/preregistrations/failed_range_escape_reversal_v1.json"
EXECUTION_HASH = "fd93cd8900ea4e36c68bda878bde895484ce32ee62e479b7f623245f8b04f0eb"


def series(direction: str = "SHORT") -> list[Candle]:
    candles = [Candle(i * 900_000, 100, 100.2, 99.8, 100, 100 + i, None) for i in range(220)]
    if direction == "SHORT":
        candles[204] = Candle(204 * 900_000, 100.1, 100.7, 100, 100.5, 500, None)
        candles[205] = Candle(205 * 900_000, 100.4, 100.45, 99.7, 99.9, 600, None)
        candles[206] = Candle(206 * 900_000, 99.9, 100.1, 99.7, 99.9, 300, None)
    else:
        candles[204] = Candle(204 * 900_000, 99.9, 100, 99.3, 99.5, 500, None)
        candles[205] = Candle(205 * 900_000, 99.6, 100.3, 99.55, 100.1, 600, None)
        candles[206] = Candle(206 * 900_000, 100.1, 100.3, 99.9, 100.1, 300, None)
    return candles


def candidate(direction: str = "SHORT"):
    decision = detect_at("BTCUSDT", series(direction), 205)
    assert decision.status == "CANDIDATE"
    return decision.candidate


def test_prior_range_excludes_escape_and_reentry() -> None:
    item = candidate()
    assert item.prior_range_high == 100.2 and item.escape_close == 100.5 and item.reentry_close == 99.9


def test_prior_range_is_exactly_twenty_candles() -> None:
    assert RANGE_LOOKBACK == 20
    candles = series(); candles[183] = replace(candles[183], high=150)
    assert detect_at("BTCUSDT", candles, 205).candidate.prior_range_high == 100.2


def test_atr14_uses_no_future_candle() -> None:
    candles = series(); before = atr14(candles, 205)
    candles[206] = Candle(206 * 900_000, 100, 200, 1, 100, 1, None)
    assert atr14(candles, 205) == before


def test_atr14_is_sma_true_range_ending_at_reentry() -> None:
    candles = series()
    expected = sum(max(c.high-c.low, abs(c.high-(candles[i-1].close if i else c.open)), abs(c.low-(candles[i-1].close if i else c.open))) for i,c in enumerate(candles[192:206], start=192))/14
    assert atr14(candles,205) == pytest.approx(expected)


def test_upside_escape_threshold() -> None:
    item=candidate("SHORT"); assert item.escape_close >= item.escape_threshold and item.escape_distance_atr >= MIN_ESCAPE_ATR


def test_downside_escape_threshold() -> None:
    item=candidate("LONG"); assert item.escape_close <= item.escape_threshold and item.escape_distance_atr >= MIN_ESCAPE_ATR


def test_reentry_must_be_immediate() -> None:
    candles=series(); candles.insert(205,Candle(205*900_000,100,100.2,99.8,100,1,None))
    candles=[replace(c,timestamp_ms=i*900_000) for i,c in enumerate(candles)]
    assert detect_at("BTCUSDT",candles,206).status == "NO_ESCAPE"


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_reentry_depth(direction: str) -> None:
    item=candidate(direction); assert item.reentry_distance_atr >= MIN_REENTRY_ATR


def test_body_range_ratio() -> None:
    item=candidate(); assert item.body_fraction >= MIN_BODY_FRACTION


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_reversal_body_direction(direction: str) -> None:
    item=candidate(direction); assert item.direction == direction


def test_wrong_reversal_body_is_rejected() -> None:
    candles=series(); candles[205]=replace(candles[205],open=99.5,low=99.5,close=100.0)
    assert detect_at("BTCUSDT",candles,205).reason == "REVERSAL_BODY_DIRECTION_FAILED"


def test_entry_is_next_candle_open() -> None:
    item=candidate(); assert item.entry_timestamp_ms == series()[206].timestamp_ms and item.requested_entry == series()[206].open


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_stop_is_escape_extreme(direction: str) -> None:
    candles=series(direction); item=candidate(direction)
    assert item.stop == (candles[204].low if direction == "LONG" else candles[204].high)


def test_stop_above_two_atr_is_rejected() -> None:
    candles=series("LONG"); candles[206]=replace(candles[206],open=101.0,high=101.1)
    assert detect_at("BTCUSDT",candles,205).reason == "STOP_DISTANCE_GT_2_ATR"


def test_tp1_and_final_target_use_executed_entry_risk() -> None:
    item=candidate(); cfg=replace(BacktestExecutionConfig(),max_hold_candles=8)
    executed=predicted_market_entry(item.requested_entry,item.direction,cfg);risk=abs(executed-item.stop)
    assert executed-TP1_R*risk == pytest.approx(executed-1.2*risk)
    assert executed-FINAL_R*risk == pytest.approx(executed-2.0*risk)


def test_partial_is_forty_percent() -> None:
    assert BacktestExecutionConfig().tp1_partial_pct == 40.0


def test_break_even_is_fee_adjusted_twelve_bps() -> None:
    cfg=BacktestExecutionConfig(); assert cfg.break_even_policy == "FEE_ADJUSTED" and cfg.break_even_fee_buffer_bps == 12.0


def test_maximum_hold_is_exactly_eight() -> None:
    assert MAX_HOLD_CANDLES == 8


def test_eighth_candle_tp1_remainder_is_closed_once() -> None:
    cfg=replace(BacktestExecutionConfig(),max_hold_candles=8)
    contract=BacktestExecutionContract(cfg)
    candles=[Candle(i*900_000,100,100.1,99.9,100,1,None) for i in range(10)]
    candles[8]=Candle(8*900_000,100,101.3,99.9,101,1,None)
    record=contract.execute(strategy=STRATEGY,symbol="BTCUSDT",timeframe="15m",direction="LONG",signal_timestamp=0,requested_entry=100,stop=99,targets=[101.2,102],candles=candles,equity=1000)
    assert record.final_exit_reason == "OPEN_AT_DATA_END"
    finalize_locked_eighth_candle(record,candles[8],cfg)
    assert record.final_exit_reason == "TIME_EXIT" and record.tp1_quantity > 0 and record.net_pnl != 0


def test_duplicate_suppression() -> None:
    candles=series(); item=candidate().to_dict(); item["reentry_index"]=205
    _,suppressed,funnel=execute_candidates({"BTCUSDT":candles},[item,item],{})
    assert funnel["duplicate_suppressed"] == 1 and suppressed[0]["suppression_reason"] == "DUPLICATE_CANDIDATE"


def test_active_same_symbol_suppression() -> None:
    candles=series(); first=candidate().to_dict();first["reentry_index"]=205
    second={**first,"signal_timestamp_ms":candles[207].timestamp_ms,"entry_timestamp_ms":candles[208].timestamp_ms,"reentry_index":207}
    _,suppressed,funnel=execute_candidates({"BTCUSDT":candles},[first,second],{})
    assert funnel["active_overlap_suppressed"] == 1 and suppressed[0]["suppression_reason"] == "ACTIVE_SAME_SYMBOL"


def test_htf_metadata_does_not_gate_equal_context() -> None:
    item=candidate(); assert item.htf_relationship == "EMA_EQUAL"


def test_htf_uses_only_fully_closed_hour() -> None:
    candles=series(); before=closed_hourly_context(candles,205)
    candles[206]=Candle(206*900_000,1,1000,.5,900,1,None)
    assert closed_hourly_context(candles,205) == before


def test_strategy_is_research_only() -> None:
    assert STRATEGY not in (ROOT/"strategies/__init__.py").read_text()


@pytest.mark.parametrize("path", ["app/runner.py","forward_paper/service.py","scripts/start_forward_paper.sh"])
def test_production_paper_and_live_cannot_load_strategy(path: str) -> None:
    assert STRATEGY not in (ROOT/path).read_text()


def test_development_validation_boundaries_are_enforced() -> None:
    source=(ROOT/"scripts/phase3c_failed_escape_validation.py").read_text()
    assert 'args.mode=="reconcile" and args.period!="development"' in source


def test_validation_is_locked_before_implementation_freeze(tmp_path: Path) -> None:
    result=subprocess.run([sys.executable,str(ROOT/"scripts/phase3c_failed_escape_validation.py"),"--mode","evaluate","--period","validation","--canonical",str(tmp_path),"--preregistration",str(PREREG),"--output",str(tmp_path/"out")],capture_output=True,text=True,env={"PYTHONPATH":str(ROOT)})
    assert result.returncode != 0 and "implementation manifest required" in result.stderr


def test_acceptance_criteria_remain_immutable() -> None:
    document=json.loads(PREREG.read_text()); assert tuple(document["phase3c_evaluation"]["acceptance_criteria"]) == LOCKED_ACCEPTANCE_CRITERIA


def test_preregistration_hash_is_locked() -> None:
    assert verify_contract(PREREG)["document_hash"] == "e7117eefbf5e387646f2a5bceb444d5125a46c56b438eb6f2c8d2e6f69077da9"


def test_detector_result_is_deterministic() -> None:
    assert detect_at("BTCUSDT",series(),205) == detect_at("BTCUSDT",series(),205)


def test_frozen_execution_contract_source_is_unchanged() -> None:
    assert hashlib.sha256((ROOT/"backtesting/execution_contract.py").read_bytes()).hexdigest() == EXECUTION_HASH


def test_no_hidden_strategy_parameter_diff() -> None:
    document=json.loads(PREREG.read_text());values={row["name"]:row["value"] for row in document["parameter_register"]}
    assert values == {"range_lookback":20,"minimum_escape":.1,"minimum_reentry":.15,"minimum_failure_body_fraction":.5,"minimum_tp1_distance":72,"entry_delay":1,"maximum_stop_distance":2.0,"tp1_multiple":1.2,"final_target_multiple":2.0,"maximum_holding_candles":8}


def test_full_manual_reconciliation_fixture() -> None:
    short=candidate("SHORT");long=candidate("LONG")
    for item in (short,long):
        assert item.prior_range_high > item.prior_range_low
        assert item.atr14 > 0 and item.body_fraction >= .5 and item.tp1_distance_bps >= 72
