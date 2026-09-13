"""STRATEGY_HARDENING_2026-09-13: app/runner.py now sorts a *copy* of `plans`
by (verdict == EXECUTABLE, score) before handing it to
ForwardPaperService.process(), so shadow's limited max_open_positions
capacity favors the higher-scored candidate when more than one executable
plan exists in the same scan cycle. Before this, process() admitted plans in
raw watchlist-iteration order -- the real cross-symbol ranking
(select_execution_winner) only runs afterward and never fed the shadow
simulator at all.

This suite exercises ForwardPaperService.process() directly (no runner/network
fixtures needed): first it pins the pre-existing list-order admission
behaviour, then proves the exact sort expression used in app/runner.py's
patch makes the higher-scored plan win the single capacity slot instead.
"""

from __future__ import annotations

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import Candle, ContractSpec, MarketSnapshot, TimeframeSnapshot, TradePlan
from forward_paper.service import ForwardPaperService

TS = 1_767_225_600_000


def _contract(symbol: str) -> ContractSpec:
    return ContractSpec(
        symbol=symbol, product_type="USDT-FUTURES", quote_coin="USDT", base_coin=symbol[:-4],
        status="normal", min_trade_num=0.1, size_multiplier=1.0, price_place=3,
        volume_24h_usdt=1_000_000.0, change_pct_24h=0.0, raw={},
    )


def _timeframe(symbol: str, close: float) -> TimeframeSnapshot:
    candle = Candle(TS, close - 0.1, close + 0.2, close - 0.2, close, 1_000.0, 100_000.0)
    return TimeframeSnapshot(
        symbol=symbol, granularity="15m", latest_close=close, change_pct=0.0, range_pct=0.4,
        volume_ratio_20=1.0, ema20=close, ema50=close, trend="bullish",
        candles=[candle], closed_candle_timestamp_ms=TS, as_of_timestamp_ms=TS,
    )


def _snapshot(symbol: str, close: float = 100.0) -> MarketSnapshot:
    tf = _timeframe(symbol, close)
    return MarketSnapshot(
        symbol=symbol, contract=_contract(symbol), primary=tf, confirmation=tf,
        alignment="aligned_bullish", score_hint=80.0, notes=[],
    )


def _plan(symbol: str, score: float) -> TradePlan:
    candidate_id = deterministic_candidate_id("momentum_breakout", symbol, "LONG", TS)
    return TradePlan(
        candidate_id=candidate_id, candidate_candle_open_timestamp_ms=TS,
        plan_id=deterministic_plan_id(candidate_id), symbol=symbol, strategy="momentum_breakout",
        direction="LONG", verdict="EXECUTABLE", score=score,
        entry_prices=[100.0], stop_loss=99.0, take_profits=[102.0],
        risk_reward_ratio=2.0, account_risk_pct=0.5, leverage=3.0,
        position_notional_usdt=35.0, notes=[], reasons=[],
    )


def _service(tmp_path) -> ForwardPaperService:
    settings = Settings(_env_file=None, FORWARD_PAPER_ENABLED=True, EXECUTION_ENABLED=False, MAX_OPEN_POSITIONS=1)
    return ForwardPaperService(
        settings,
        events_path=tmp_path / "events.jsonl",
        outcomes_path=tmp_path / "outcomes.csv",
        quality_path=tmp_path / "quality.json",
        git_commit="test",
    )


def test_process_admits_by_list_order_when_capacity_limited_pre_existing_behaviour(tmp_path):
    """Documents the behaviour app/runner.py's sort now compensates for:
    process() itself does not rank by score at all."""
    service = _service(tmp_path)
    low_score_first = _plan("AAAUSDT", 50.0)
    high_score_second = _plan("ZZZUSDT", 90.0)
    snapshots = [_snapshot("AAAUSDT"), _snapshot("ZZZUSDT")]

    service.process([low_score_first, high_score_second], snapshots, scan_id="scan-1")

    opened = {state["symbol"] for state in service.trade_states().values()}
    assert opened == {"AAAUSDT"}, "capacity=1 should go to whichever plan is first in the list, not the higher score"


def test_score_sort_before_process_lets_the_higher_scored_plan_win_capacity(tmp_path):
    """The exact expression app/runner.py now applies before calling
    forward_paper.process() -- pinned at the unit level."""
    service = _service(tmp_path)
    low_score_first = _plan("AAAUSDT", 50.0)
    high_score_second = _plan("ZZZUSDT", 90.0)
    snapshots = [_snapshot("AAAUSDT"), _snapshot("ZZZUSDT")]

    paper_plans = sorted(
        [low_score_first, high_score_second],
        key=lambda plan: (plan.verdict == "EXECUTABLE", plan.score),
        reverse=True,
    )
    service.process(paper_plans, snapshots, scan_id="scan-1")

    opened = {state["symbol"] for state in service.trade_states().values()}
    assert opened == {"ZZZUSDT"}, "higher-scored plan should win the single capacity slot after sorting"


def test_sort_never_promotes_a_blocked_plan_over_an_executable_one(tmp_path):
    """verdict must still dominate score in the sort key: a BLOCKED plan with a
    higher raw score must not be admitted ahead of an EXECUTABLE one."""
    service = _service(tmp_path)
    blocked_high_score = _plan("AAAUSDT", 99.0)
    blocked_high_score.verdict = "BLOCKED"
    executable_lower_score = _plan("ZZZUSDT", 40.0)
    snapshots = [_snapshot("AAAUSDT"), _snapshot("ZZZUSDT")]

    paper_plans = sorted(
        [blocked_high_score, executable_lower_score],
        key=lambda plan: (plan.verdict == "EXECUTABLE", plan.score),
        reverse=True,
    )
    service.process(paper_plans, snapshots, scan_id="scan-1")

    opened = {state["symbol"] for state in service.trade_states().values()}
    assert opened == {"ZZZUSDT"}
