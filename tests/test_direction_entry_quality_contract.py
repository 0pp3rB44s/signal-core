"""Direction-aware entry quality: the producer computes two values, not one.

``market_features.engine`` runs ``EntryQualityAnalyzer`` once per direction.
Two consumers in ``app.runner`` read one value back and attributed it to both
sides. Every fixture here is asymmetric (long != short) on purpose: a
symmetric one cannot distinguish a correct read from either defect.
"""

from __future__ import annotations

import pytest

from app.runner import (
    _DIRECTION_AMBIGUOUS,
    _build_fallback_candidate,
    _direction_entry_quality,
    _execution_aware_score,
    _execution_scores_by_symbol,
    _rankable_plan_directions,
)
from clients.schemas import Candle, TimeframeSnapshot

_STEP_MS = 15 * 60 * 1000
_LAST_CLOSED_MS = 1_760_000_000_000


def _candles(count: int = 60) -> list[Candle]:
    """A closed series whose final bar carries the engine's closed marker."""
    return [
        Candle(
            timestamp_ms=_LAST_CLOSED_MS - (count - 1 - index) * _STEP_MS,
            open=100.0,
            high=100.5,
            low=99.5,
            close=100.0,
            volume_base=1_000.0,
            volume_quote=100_000.0,
        )
        for index in range(count)
    ]


def _timeframe(trend: str) -> TimeframeSnapshot:
    """The real schema, so a field added upstream fails loudly here."""
    return TimeframeSnapshot(
        symbol="TESTUSDT",
        granularity="15m",
        latest_close=100.0,
        change_pct=0.0,
        range_pct=1.0,
        volume_ratio_20=1.5,
        ema20=100.0,
        ema50=100.0,
        trend=trend,
        candles=_candles(),
        closed_candle_timestamp_ms=_LAST_CLOSED_MS,
        as_of_timestamp_ms=_LAST_CLOSED_MS + _STEP_MS,
    )


LONG_QUALITY = 100.0
SHORT_QUALITY = 40.0


def _notes(long_q: float = LONG_QUALITY, short_q: float = SHORT_QUALITY) -> list[str]:
    """The note forms the producer actually emits, verbatim."""
    return [
        f"entry_quality long={long_q:.0f} short={short_q:.0f} close_pos=0.9100",
        f"entry_quality_long={long_q:.0f}",
        f"entry_quality_short={short_q:.0f}",
        "close_position=0.9100",
        "close_pos=0.9100",
    ]


class _Snapshot:
    """Only the attributes the two functions under test read."""

    def __init__(self, *, notes, context=None, score_hint=90.0, symbol="TESTUSDT"):
        self.symbol = symbol
        self.notes = notes
        self.context = context if context is not None else {}
        self.score_hint = score_hint
        self.alignment = "aligned_bearish"
        self.primary = _timeframe("bearish")
        self.confirmation = _timeframe("bearish")


def _context(long_q: float = LONG_QUALITY, short_q: float = SHORT_QUALITY) -> dict:
    return {
        "entry_quality": {
            "LONG": {"entry_quality_score": long_q},
            "SHORT": {"entry_quality_score": short_q},
        }
    }


# --- the reader itself ------------------------------------------------------


def test_reads_each_direction_separately_from_context():
    snap = _Snapshot(notes=_notes(), context=_context())
    assert _direction_entry_quality(snap, "LONG") == LONG_QUALITY
    assert _direction_entry_quality(snap, "SHORT") == SHORT_QUALITY


def test_falls_back_to_notes_when_context_absent():
    snap = _Snapshot(notes=_notes(), context={})
    assert _direction_entry_quality(snap, "LONG") == LONG_QUALITY
    assert _direction_entry_quality(snap, "SHORT") == SHORT_QUALITY


@pytest.mark.parametrize(
    "context",
    [
        {},
        {"entry_quality": None},
        {"entry_quality": {}},
        {"entry_quality": {"SHORT": None}},
        {"entry_quality": {"SHORT": {}}},
        {"entry_quality": {"SHORT": {"entry_quality_score": None}}},
        {"entry_quality": {"SHORT": {"entry_quality_score": "n/a"}}},
        {"entry_quality": {"SHORT": {"entry_quality_score": float("nan")}}},
        {"entry_quality": {"SHORT": {"entry_quality_score": float("inf")}}},
        {"entry_quality": "not-a-dict"},
    ],
)
def test_malformed_context_falls_back_to_notes_rather_than_defaulting(context):
    snap = _Snapshot(notes=_notes(), context=context)
    assert _direction_entry_quality(snap, "SHORT") == SHORT_QUALITY


def test_structured_context_wins_over_the_rendered_notes():
    """The notes are a rendering of the context; the object is the authority.

    Both sources normally agree, which is why they must be made to disagree
    here: otherwise nothing proves which one is actually read.
    """
    snap = _Snapshot(notes=_notes(long_q=100.0, short_q=99.0), context=_context(100.0, 12.0))
    assert _direction_entry_quality(snap, "SHORT") == 12.0


def test_defaults_only_when_neither_source_has_the_value():
    snap = _Snapshot(notes=["close_pos=0.5"], context={})
    assert _direction_entry_quality(snap, "SHORT") == 100.0
    assert _direction_entry_quality(snap, "SHORT", default=55.0) == 55.0


@pytest.mark.parametrize("direction", ["", None, "BOTH", "long "])
def test_unknown_direction_is_not_silently_mapped_to_a_side(direction):
    snap = _Snapshot(notes=_notes(), context=_context())
    assert _direction_entry_quality(snap, direction) == 100.0


def test_lowercase_direction_is_accepted():
    snap = _Snapshot(notes=_notes(), context=_context())
    assert _direction_entry_quality(snap, "short") == SHORT_QUALITY


# --- defect 1: the execution score charged the wrong side -------------------


def test_execution_score_charges_the_side_being_traded():
    """A snapshot that is a clean long is a bad short; the scores must differ."""
    snap = _Snapshot(notes=_notes(), context=_context(), score_hint=90.0)
    long_score = _execution_aware_score(snap, "LONG")
    short_score = _execution_aware_score(snap, "SHORT")

    # short quality 40 -> below the 50 band -> a penalty a long does not carry
    assert short_score < long_score
    assert long_score - short_score == pytest.approx(18.0)


def test_execution_score_without_direction_keeps_the_permissive_reading():
    """Callers that rank before a direction exists must not change behaviour."""
    snap = _Snapshot(notes=_notes(), context=_context(), score_hint=90.0)
    assert _execution_aware_score(snap) == _execution_aware_score(snap, "LONG")


def test_execution_score_is_not_a_maximum_over_directions():
    """The specific defect: max(long, short) hid every bad short."""
    snap = _Snapshot(notes=_notes(long_q=100.0, short_q=10.0), context=_context(100.0, 10.0))
    assert _execution_aware_score(snap, "SHORT") != _execution_aware_score(snap, "LONG")


def test_execution_score_symmetric_when_the_producer_is_symmetric():
    snap = _Snapshot(notes=_notes(70.0, 70.0), context=_context(70.0, 70.0))
    assert _execution_aware_score(snap, "LONG") == _execution_aware_score(snap, "SHORT")


# --- defect 2: the late-entry gate could not fire for shorts ----------------


class _Settings:
    enabled_strategy_set = frozenset({"adaptive_momentum_continuation"})
    disabled_strategy_set = frozenset()


def _bearish_snapshot(short_q: float) -> _Snapshot:
    """A bearish setup that clears every gate except the entry-quality one.

    ``close_pos`` sits mid-candle so the separate ``short_too_low_in_candle``
    guard cannot fire and steal the assertion.
    """
    notes = [
        f"entry_quality long=100 short={short_q:.1f} close_pos=0.5000",
        "entry_quality_long=100",
        f"entry_quality_short={short_q:.1f}",
        "close_pos=0.5000",
        "volume expansion",
        "breakout_ready=true",
        "pressure_score=80",
        "expansion_prob=80",
    ]
    snap = _Snapshot(
        notes=notes,
        context={
            "entry_quality": {
                "LONG": {"entry_quality_score": 100.0},
                "SHORT": {"entry_quality_score": short_q},
            }
        },
        score_hint=95.0,
    )
    return snap


# --- the ranking-critical path: which side gets scored ----------------------


class _Plan:
    def __init__(self, symbol, direction, verdict="EXECUTABLE"):
        self.symbol = symbol
        self.direction = direction
        self.verdict = verdict


def test_rankable_direction_comes_from_the_executable_plan():
    got = _rankable_plan_directions([_Plan("BTCUSDT", "SHORT")])
    assert got == {"BTCUSDT": "SHORT"}


def test_non_executable_plans_do_not_decide_the_side():
    """Only EXECUTABLE plans are ranked, so only they may set the side."""
    plans = [_Plan("BTCUSDT", "LONG", verdict="REJECTED"), _Plan("BTCUSDT", "SHORT")]
    assert _rankable_plan_directions(plans) == {"BTCUSDT": "SHORT"}


def test_symbol_with_both_sides_executable_is_reported_ambiguous():
    """A symbol key cannot carry two sides; it must not silently pick one."""
    plans = [_Plan("BTCUSDT", "LONG"), _Plan("BTCUSDT", "SHORT")]
    assert _rankable_plan_directions(plans) == {"BTCUSDT": _DIRECTION_AMBIGUOUS}


def test_ambiguity_is_per_symbol_not_global():
    plans = [_Plan("BTCUSDT", "LONG"), _Plan("BTCUSDT", "SHORT"), _Plan("SOLUSDT", "LONG")]
    got = _rankable_plan_directions(plans)
    assert got["BTCUSDT"] == _DIRECTION_AMBIGUOUS
    assert got["SOLUSDT"] == "LONG"


def test_symbol_is_matched_case_insensitively():
    assert _rankable_plan_directions([_Plan("btcusdt", "short")]) == {"BTCUSDT": "SHORT"}


@pytest.mark.parametrize(
    "plans",
    [
        [],
        None,
        [_Plan("", "LONG")],
        [_Plan("BTCUSDT", "")],
        [_Plan("BTCUSDT", "SIDEWAYS")],
        [_Plan("BTCUSDT", None)],
    ],
)
def test_unusable_plans_yield_no_direction(plans):
    """No side means the direction-agnostic score, never a guessed side."""
    assert _rankable_plan_directions(plans) == {}


def test_execution_scores_charge_the_side_the_plan_will_trade():
    """The wiring: a resolved side must actually reach the score.

    The snapshot is a clean long and a poor short, so scoring it as a short is
    the only way to produce the lower value.
    """
    snap = _Snapshot(notes=_notes(), context=_context(), score_hint=90.0, symbol="BTCUSDT")
    scores = _execution_scores_by_symbol([snap], [_Plan("BTCUSDT", "SHORT")])
    assert scores["BTCUSDT"] == _execution_aware_score(snap, "SHORT")
    assert scores["BTCUSDT"] < _execution_aware_score(snap, "LONG")


def test_execution_scores_fall_back_when_no_plan_names_a_side():
    snap = _Snapshot(notes=_notes(), context=_context(), score_hint=90.0, symbol="BTCUSDT")
    scores = _execution_scores_by_symbol([snap], [])
    assert scores["BTCUSDT"] == _execution_aware_score(snap)


def test_ambiguous_symbol_falls_back_and_is_logged():
    """A collision must be visible, not silently resolved."""
    snap = _Snapshot(notes=_notes(), context=_context(), score_hint=90.0, symbol="BTCUSDT")
    warnings: list[tuple] = []
    log = type("L", (), {"warning": lambda self, *a: warnings.append(a)})()

    scores = _execution_scores_by_symbol(
        [snap], [_Plan("BTCUSDT", "LONG"), _Plan("BTCUSDT", "SHORT")], log
    )

    assert scores["BTCUSDT"] == _execution_aware_score(snap)
    assert len(warnings) == 1
    assert "EXECUTION_SCORE_DIRECTION_AMBIGUOUS" in warnings[0][0]


def test_every_snapshot_still_gets_a_score():
    """The map must not shrink: a missing key silently re-ranks a plan."""
    snaps = [
        _Snapshot(notes=_notes(), context=_context(), symbol="BTCUSDT"),
        _Snapshot(notes=_notes(), context=_context(), symbol="SOLUSDT"),
    ]
    scores = _execution_scores_by_symbol(snaps, [_Plan("BTCUSDT", "SHORT")])
    assert set(scores) == {"BTCUSDT", "SOLUSDT"}


def test_late_short_entry_is_blocked():
    """Previously unreachable: the marker never matched, so shorts read 100."""
    assert _build_fallback_candidate(_bearish_snapshot(40.0), _Settings()) is None


def test_clean_short_entry_is_not_blocked():
    """The gate must still be a gate, not a blanket rejection of shorts."""
    assert _build_fallback_candidate(_bearish_snapshot(95.0), _Settings()) is not None


def test_short_gate_boundary_is_unchanged():
    """The 75.0 threshold is not being re-tuned by this correction."""
    assert _build_fallback_candidate(_bearish_snapshot(74.9), _Settings()) is None
    assert _build_fallback_candidate(_bearish_snapshot(75.0), _Settings()) is not None
