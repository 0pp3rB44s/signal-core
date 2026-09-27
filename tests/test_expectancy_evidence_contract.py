"""The ranker may only use expectancy the producer calls sufficient.

``symbol_expectancy`` computes a sample size, decides below MIN_SAMPLE that the
sample cannot gate anything, and records that as ``status`` -- then prints the
mean anyway on a line it describes as evidence rather than a decision. These
tests pin that the consumer now reads the verdict alongside the number.
"""

from __future__ import annotations

import pytest

from clients.schemas import TradePlan, deterministic_plan_id
from execution.portfolio_selector import _expectancy, select_execution_winner
from risk.symbol_expectancy import (
    INSUFFICIENT_LIVE_DATA,
    MIN_SAMPLE,
    SOURCE_ABSENT,
    SOURCE_MALFORMED,
    SUFFICIENT_NEGATIVE,
    SUFFICIENT_OK,
    SymbolExpectancyRecord,
    observability_note,
)


def _record(**overrides) -> SymbolExpectancyRecord:
    base = dict(
        symbol="SUIUSDT", direction="SHORT", source="trade_dataset_v2.csv",
        generated_at="2026-08-05T00:00:00+00:00",
        last_trade_at="2026-08-04T23:00:00+00:00",
        window_days=30, sample_size=1, expectancy=-0.1005, winrate=0.0,
        tp1_hit_rate=0.0, freshness_state="fresh", confidence="low",
        status=INSUFFICIENT_LIVE_DATA,
    )
    base.update(overrides)
    return SymbolExpectancyRecord(**base)


def _plan(symbol="SUIUSDT", direction="SHORT", reasons=(), notes=(), score=90.0,
          plan_id=None, candidate_id="cand-1") -> TradePlan:
    return TradePlan(
        candidate_id=candidate_id, candidate_candle_open_timestamp_ms=1,
        plan_id=deterministic_plan_id(candidate_id), symbol=symbol, strategy="low_vol_reclaim",
        direction=direction, verdict="EXECUTABLE", score=score,
        entry_prices=[100.0], stop_loss=99.0, take_profits=[101.0],
        risk_reward_ratio=1.3, account_risk_pct=0.5, leverage=3.0,
        position_notional_usdt=26.0, notes=list(notes), reasons=list(reasons),
    )


def _note(**overrides) -> str:
    return observability_note(_record(**overrides))


# --- 1, 9, 10: sufficient evidence still ranks --------------------------------


def test_sufficient_ok_returns_the_value():
    note = _note(sample_size=25, expectancy=0.0312, status=SUFFICIENT_OK)
    assert _expectancy(_plan(reasons=[note])) == pytest.approx(0.0312)


def test_sufficient_negative_expectancy_stays_negative():
    note = _note(sample_size=25, expectancy=-0.0312, status=SUFFICIENT_OK)
    assert _expectancy(_plan(reasons=[note])) == pytest.approx(-0.0312)


def test_sufficient_positive_expectancy_stays_positive():
    note = _note(sample_size=40, expectancy=0.0900, status=SUFFICIENT_OK)
    assert _expectancy(_plan(reasons=[note])) == pytest.approx(0.0900)


# --- 2, 3: the defect ---------------------------------------------------------


def test_insufficient_live_data_is_neutral_despite_a_numeric_value():
    note = _note(sample_size=1, expectancy=-0.1005, status=INSUFFICIENT_LIVE_DATA)
    assert "exp=-0.1005" in note        # the producer still prints it
    assert _expectancy(_plan(reasons=[note])) == 0.0


@pytest.mark.parametrize("sample_size", [0, 1, 2, 5, MIN_SAMPLE - 1])
def test_every_sample_below_the_minimum_is_neutral(sample_size):
    note = _note(sample_size=sample_size, expectancy=0.25,
                 status=INSUFFICIENT_LIVE_DATA)
    assert _expectancy(_plan(reasons=[note])) == 0.0


def test_sufficient_negative_is_admitted_because_it_can_legitimately_arrive():
    """It looks like a kill-switch verdict; past AGING it stops blocking.

    symbol_expectancy.evaluate only pauses SUFFICIENT_NEGATIVE while the
    evidence is FRESH or AGING. Beyond fourteen days it returns (False, None) so
    a pause can be re-earned, RiskManager finds no hard reason, and the
    candidate proceeds. The mean is then real evidence over at least ten closes.
    """
    note = _note(sample_size=25, expectancy=-0.20, status=SUFFICIENT_NEGATIVE,
                 freshness_state="STALE")
    assert _expectancy(_plan(reasons=[note])) == pytest.approx(-0.20)


def test_sufficient_negative_ranks_below_a_sufficient_positive():
    losing = _plan(candidate_id="losing-cand",
                   reasons=[_note(sample_size=25, expectancy=-0.20,
                                  status=SUFFICIENT_NEGATIVE, freshness_state="STALE")])
    winning = _plan(candidate_id="winning-cand",
                    reasons=[_note(sample_size=25, expectancy=0.05, status=SUFFICIENT_OK)])

    assert select_execution_winner([losing, winning]).winner.candidate_id == "winning-cand"


def test_a_fresh_sufficient_negative_is_blocked_upstream_not_here():
    """Proves the carve-out this contract depends on, at the producer."""
    from risk.symbol_expectancy import evaluate

    fresh = _record(sample_size=25, expectancy=-0.20, status=SUFFICIENT_NEGATIVE,
                    freshness_state="FRESH", tp1_hit_rate=0.5)
    stale = _record(sample_size=25, expectancy=-0.20, status=SUFFICIENT_NEGATIVE,
                    freshness_state="STALE", tp1_hit_rate=0.5)

    assert evaluate(fresh)[0] is True     # never reaches ranking
    assert evaluate(stale)[0] is False    # does reach ranking


@pytest.mark.parametrize("status", [SOURCE_ABSENT, SOURCE_MALFORMED])
def test_source_level_failures_are_neutral(status):
    assert _expectancy(_plan(reasons=[_note(status=status)])) == 0.0


# --- 4, 5, 6, 7: degenerate evidence -----------------------------------------


def test_missing_status_is_neutral():
    line = "symbol expectancy source=trade_dataset_v2.csv (SUIUSDT SHORT, n=25, exp=0.0312)"
    assert _expectancy(_plan(reasons=[line])) == 0.0


def test_malformed_status_is_neutral():
    line = ("symbol expectancy source=trade_dataset_v2.csv "
            "(SUIUSDT SHORT, n=25, exp=0.0312, status=PROBABLY_FINE)")
    assert _expectancy(_plan(reasons=[line])) == 0.0


def test_missing_expectancy_is_neutral():
    note = _note(sample_size=0, expectancy=None, status=SUFFICIENT_OK)
    assert "exp=n/a" in note
    assert _expectancy(_plan(reasons=[note])) == 0.0


def test_malformed_expectancy_is_neutral():
    line = ("symbol expectancy source=trade_dataset_v2.csv "
            f"(SUIUSDT SHORT, n=25, exp=abc, status={SUFFICIENT_OK})")
    assert _expectancy(_plan(reasons=[line])) == 0.0


def test_duplicate_status_tokens_are_neutral():
    line = ("symbol expectancy source=trade_dataset_v2.csv "
            f"(SUIUSDT SHORT, n=25, exp=0.03, status={SUFFICIENT_OK}, status={SUFFICIENT_OK})")
    assert _expectancy(_plan(reasons=[line])) == 0.0


# --- 8: no cross-pairing ------------------------------------------------------


def test_status_and_value_cannot_be_taken_from_different_lines():
    """Two evidence lines is ambiguity, not an opportunity to mix and match."""
    good = _note(sample_size=25, expectancy=0.05, status=SUFFICIENT_OK)
    bad = _note(sample_size=1, expectancy=0.99, status=INSUFFICIENT_LIVE_DATA)
    assert _expectancy(_plan(reasons=[good, bad])) == 0.0


def test_a_sufficient_line_for_another_pair_cannot_authorise_this_one():
    other = observability_note(_record(symbol="BTCUSDT", direction="LONG",
                                       sample_size=40, expectancy=0.5,
                                       status=SUFFICIENT_OK))
    thin = _note(sample_size=1, expectancy=0.5, status=INSUFFICIENT_LIVE_DATA)
    assert _expectancy(_plan(reasons=[other, thin])) == 0.0


# --- 11, 12: ranking behaviour ------------------------------------------------


def test_ranking_still_orders_by_expectancy_when_evidence_is_sufficient():
    strong = _plan(candidate_id="strong-cand",
                   reasons=[_note(sample_size=30, expectancy=0.09, status=SUFFICIENT_OK)])
    weak = _plan(candidate_id="weak-cand",
                 reasons=[_note(sample_size=30, expectancy=-0.09, status=SUFFICIENT_OK)])

    winner = select_execution_winner([weak, strong]).winner
    assert winner.candidate_id == "strong-cand"


def test_frozen_defect_regression_insufficient_evidence_cannot_pick_the_winner():
    """The exact shape seen in all 64 frozen trades: n=2, numeric exp, insufficient.

    Before the fix the plan carrying exp=0.9 won on expectancy. Now both
    expectancies are neutral, so the tie falls through to the next sort keys and
    the outcome no longer depends on a two-trade average.
    """
    loud = _plan(candidate_id="loud-cand", symbol="SUIUSDT",
                 reasons=[_note(sample_size=2, expectancy=0.9,
                                status=INSUFFICIENT_LIVE_DATA)])
    quiet = _plan(candidate_id="quiet-cand", symbol="SUIUSDT",
                  reasons=[_note(sample_size=2, expectancy=-0.9,
                                 status=INSUFFICIENT_LIVE_DATA)])

    selection = select_execution_winner([loud, quiet])

    # Both neutralised: a two-trade average no longer decides anything.
    assert [row.expectancy for row in selection.ranked] == [0.0, 0.0]
    # And the order is now stable under input order, which it was not before.
    assert (select_execution_winner([quiet, loud]).winner.candidate_id
            == selection.winner.candidate_id)


def test_neutralised_expectancy_does_not_disturb_a_clear_score_ordering():
    better = _plan(candidate_id="better-cand", score=95.0,
                   reasons=[_note(sample_size=1, expectancy=-0.5,
                                  status=INSUFFICIENT_LIVE_DATA)])
    worse = _plan(candidate_id="worse-cand", score=70.0,
                  reasons=[_note(sample_size=1, expectancy=0.5,
                                 status=INSUFFICIENT_LIVE_DATA)])

    assert select_execution_winner([worse, better]).winner.candidate_id == "better-cand"


def test_status_token_is_the_producer_constant_not_a_local_string():
    import execution.portfolio_selector as selector
    import risk.symbol_expectancy as producer

    assert selector.SUFFICIENT_OK is producer.SUFFICIENT_OK
