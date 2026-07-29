"""Funnel reason classification: only genuinely blocking reasons get a code.

Telemetry-only. None of this changes a trading decision; it changes how the
decisions that already happened are counted and displayed.

The defect being pinned: classify_reason_codes() joined every reason into one
lowercase blob and matched loose tokens against it, so
  * "expectancy" matched "expectancy-watch: ... not hard-paused" and every
    PROBE/WATCH/source annotation - soft text became a hard block;
  * one symbol kill-switch emitted SYMBOL_EXPECTANCY_PAUSE *and*
    EXPECTANCY_BLOCK - the same rejection counted twice;
  * "rr" matched inside "MTF ove(rr)ide" - a non-blocking watch note was tagged
    RR_GEOMETRY;
  * "momentum-quality blocked", a real hard block, matched nothing at all.
"""

from __future__ import annotations

import pytest

from telemetry.funnel import (REASON_CODES, classify_reason_code,
                              classify_reason_codes)

# --- exact production strings, taken from logs/live.out ------------------

HARD = {
    "kill-switch: symbol paused by expectancy (BTCUSDT)": "SYMBOL_EXPECTANCY_PAUSE",
    "kill-switch: symbol failed TP1 too often (BTCUSDT)": "SYMBOL_EXPECTANCY_PAUSE",
    "blocked: market alignment mixed without MTF confirmation": "HTF_OPPOSITION",
    "blocked: market alignment conflicted": "HTF_OPPOSITION",
    "blocked: long without bullish primary trend": "HTF_OPPOSITION",
    "blocked: short without bearish primary trend": "HTF_OPPOSITION",
    "blocked: sweep requires fully aligned market or MTF confirmation": "HTF_OPPOSITION",
    "blocked: shorts disabled by configuration (ENABLE_SHORTS=false)": "SHORTS_DISABLED",
    "blocked: orderbook risk-off": "ORDERBOOK_RISK",
    "momentum-quality blocked: volume ratio too weak (0.92 < 1.60)": "MOMENTUM_QUALITY",
    "execution-cost blocked: spread too wide (7.10bps >= 5.00bps)": "EXECUTION_COST",
    "score below Safe Mode minimum: 64.0 < 72": "SCORE_THRESHOLD",
    "kill-switch: weekly freeze active (weekly_pnl=-5.10)": "WEEKLY_FREEZE",
    "kill-switch: daily defensive mode active (daily_pnl=-1.20)": "DAILY_DEFENSIVE",
    "kill-switch: consecutive loss limit reached (3)": "CONSECUTIVE_LOSS_LIMIT",
    "Safe Mode blocks unsupported strategy: experimental": "SAFE_MODE_STRATEGY",
}

SOFT = (
    "expectancy-watch: strategy weak but not hard-paused (low_vol_reclaim)",
    "strategy weighting source=clean_strategy_expectancy (low_vol_reclaim, trades=119, exp=-0.026)",
    "strategy weighting PROBE: negative expectancy, trading at reduced size (low_vol_reclaim)",
    "strategy weighting WATCH: neutral expectancy (low_vol_reclaim, exp=0.010)",
    "strategy weighting BOOST: strong expectancy (low_vol_reclaim, exp=0.310)",
    "Negative expectancy detected for strategy momentum_breakout.",
    "reclaim PROBE: geen volledige HTF-consensus (mean-reversion zonder trendrug), halve size",
    "ai-agent: passed (8 decisions checked)",
    "ai-agent PROBE: strategy exposure reduced by coach (momentum_breakout)",
    "momentum-quality PROBE: coil after expansion, reduced size (exhaustion=88.54)",
    "watch: mixed alignment allowed for reclaim MTF override",
    "watch: conflicted alignment allowed for reclaim MTF override",
    "backtest mode: adaptive kill-switch/strategy-weighting disabled",
)


@pytest.mark.parametrize(("reason", "expected"), sorted(HARD.items()))
def test_hard_reason_gets_its_code(reason, expected):
    assert classify_reason_code(reason) == expected


@pytest.mark.parametrize("reason", SOFT)
def test_soft_reason_gets_no_code(reason):
    assert classify_reason_code(reason) is None, f"soft text classified: {reason}"


@pytest.mark.parametrize("reason", SOFT)
def test_soft_reason_contributes_nothing_to_the_list(reason):
    assert classify_reason_codes([reason]) == []


# --- the specific regressions named in the defect report -----------------

def test_expectancy_watch_is_not_a_hard_block():
    assert classify_reason_codes(
        ["expectancy-watch: strategy weak but not hard-paused (low_vol_reclaim)"]) == []


def test_strategy_weighting_probe_is_not_a_hard_block():
    assert classify_reason_codes(
        ["strategy weighting PROBE: negative expectancy, trading at reduced size"]) == []


def test_symbol_kill_switch_yields_only_symbol_expectancy_pause():
    codes = classify_reason_codes(["kill-switch: symbol paused by expectancy (BTCUSDT)"])
    assert codes == ["SYMBOL_EXPECTANCY_PAUSE"]
    assert "EXPECTANCY_BLOCK" not in codes, "one rejection must not emit two codes"


def test_reclaim_probe_is_not_an_htf_hard_block():
    assert classify_reason_codes(
        ["reclaim PROBE: geen volledige HTF-consensus (mean-reversion zonder trendrug), halve size"]) == []


def test_real_mixed_alignment_rejection_is_htf_opposition():
    assert classify_reason_codes(
        ["blocked: market alignment mixed without MTF confirmation"]) == ["HTF_OPPOSITION"]


def test_shorts_disabled_is_classified():
    assert classify_reason_codes(
        ["blocked: shorts disabled by configuration (ENABLE_SHORTS=false)"]) == ["SHORTS_DISABLED"]


def test_mtf_override_note_no_longer_collides_with_rr_geometry():
    """"rr" used to match inside "ove(rr)ide"."""
    codes = classify_reason_codes(["watch: mixed alignment allowed for reclaim MTF override"])
    assert codes == []


def test_momentum_quality_hard_block_is_now_captured():
    """Previously matched no rule at all - a real block that went uncounted."""
    assert classify_reason_codes(
        ["momentum-quality blocked: volume ratio too weak (0.92 < 1.60)"]) == ["MOMENTUM_QUALITY"]


# --- aggregation contract ------------------------------------------------

def test_duplicate_reasons_on_one_candidate_count_once():
    codes = classify_reason_codes([
        "kill-switch: symbol paused by expectancy (BTCUSDT)",
        "kill-switch: symbol paused by expectancy (BTCUSDT)",
        "blocked: market alignment mixed without MTF confirmation",
        "blocked: market alignment conflicted",
    ])
    assert codes == ["SYMBOL_EXPECTANCY_PAUSE", "HTF_OPPOSITION"]
    assert len(codes) == len(set(codes))


def test_real_production_candidate_yields_exactly_two_codes():
    """Verbatim reason list from a 21:00:29Z low_vol_reclaim LONG decision.
    Before the fix this produced SYMBOL_EXPECTANCY_PAUSE + EXPECTANCY_BLOCK +
    HTF_OPPOSITION; only two of those were real."""
    codes = classify_reason_codes([
        "expectancy-watch: strategy weak but not hard-paused (low_vol_reclaim)",
        "kill-switch: symbol paused by expectancy (BTCUSDT)",
        "strategy weighting source=clean_strategy_expectancy (low_vol_reclaim, trades=119, exp=-0.026)",
        "strategy weighting PROBE: negative expectancy, trading at reduced size (low_vol_reclaim)",
        "ai-agent: passed (8 decisions checked)",
        "reclaim PROBE: geen volledige HTF-consensus (mean-reversion zonder trendrug), halve size",
        "blocked: market alignment mixed without MTF confirmation",
    ])
    assert codes == ["SYMBOL_EXPECTANCY_PAUSE", "HTF_OPPOSITION"]


def test_momentum_breakout_candidate_yields_three_codes():
    """Verbatim from a 20:55:02Z momentum_breakout LONG decision."""
    codes = classify_reason_codes([
        "expectancy-watch: strategy weak but not hard-paused (momentum_breakout)",
        "kill-switch: symbol paused by expectancy (BTCUSDT)",
        "strategy weighting PROBE: negative expectancy, trading at reduced size",
        "ai-agent PROBE: strategy exposure reduced by coach (momentum_breakout)",
        "Negative expectancy detected for strategy momentum_breakout.",
        "momentum-quality blocked: volume ratio too weak (0.92 < 1.60)",
        "momentum-quality PROBE: coil after expansion, reduced size (exhaustion=88.54)",
        "blocked: market alignment mixed without MTF confirmation",
    ])
    assert set(codes) == {"SYMBOL_EXPECTANCY_PAUSE", "MOMENTUM_QUALITY", "HTF_OPPOSITION"}


def test_empty_and_none_input_is_safe():
    assert classify_reason_codes([]) == []
    assert classify_reason_codes(None) == []
    assert classify_reason_codes([None, "", "   "]) == []
    assert classify_reason_code(None) is None


# --- schema contract -----------------------------------------------------

def test_every_emitted_code_is_a_registered_reason_code():
    """Funnel validation rejects unknown codes; a mismatch would break writes."""
    from telemetry.funnel import HARD_REASON_RULES
    for code, _ in HARD_REASON_RULES:
        assert code in REASON_CODES, f"{code} missing from REASON_CODES"


def test_dashboard_has_a_label_for_every_hard_code():
    from dashboard_v3.panels.funnel import REASON_LABELS
    from telemetry.funnel import HARD_REASON_RULES
    for code, _ in HARD_REASON_RULES:
        assert code in REASON_LABELS, f"no dashboard label for {code}"


def test_dashboard_counts_each_gate_once_per_candidate():
    src = __import__("pathlib").Path(
        __file__).resolve().parents[1] / "dashboard_v3" / "panels" / "funnel.py"
    body = src.read_text().split("if stage == \"RISK_DECISION\":")[1].split("if e.get(")[0]
    assert "set(" in body or "unique" in body, "aggregation must de-duplicate per candidate"


# --- trading logic untouched --------------------------------------------

def test_no_trade_decision_module_changed_by_this_patch():
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    changed = subprocess.run(["git", "diff", "--name-only", "HEAD"],
                             cwd=repo, capture_output=True, text=True).stdout.split()
    forbidden = ("risk/", "planning/", "execution/", "strategies/", "app/config.py",
                 ".env", "reports/backtests/")
    for path in changed:
        for bad in forbidden:
            assert not path.startswith(bad), f"trade-decision file changed: {path}"
