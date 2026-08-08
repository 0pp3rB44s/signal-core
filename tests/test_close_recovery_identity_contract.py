"""Recovery must reconcile a slow-confirmed lifecycle without ever guessing.

Reproduction of the 2026-08-08 startup block. Two real lifecycles (XLM 08:45,
AVAX 15:05) each left two CLOSE_PROVISIONAL rows and none of the four could be
matched, so `STARTUP_CLOSE_RECOVERY_BLOCKED` held new entries out.

Two independent defects, both fixed here and both pinned below:

  * identity never reached the row. `exchange_position_id` and the exchange's
    own open time were captured at EXCHANGE_POSITION_CONFIRMED and then dropped,
    so recovery fell through to the composite fallback.

  * the composite fallback compared Bitget's `ctime` (exchange EVENT time)
    against our `opened_at` (local OBSERVATION time) with a symmetric +/-5 s
    window. Observation necessarily trails the event by the maker wait plus fill
    confirmation: 0.7-2.7 s for 43 of 45 lifecycles that day, but 5.678 s and
    20.828 s for these two.

The window is now asymmetric — generous backwards, tight forwards — and every
filter still ends in the same uniqueness rule, so a wider window can only turn a
refusal into an ambiguity, never into a wrong match.
"""

from __future__ import annotations

import pytest

from execution.close_dedup import matches_same_lifecycle
from execution.close_reconciler import (
    AmbiguousLifecycle,
    CLOSE_OBSERVATION_MAX_MS,
    OPENED_AT_TOLERANCE_MS,
    OPEN_OBSERVATION_MAX_MS,
    economics_from_history,
    match_lifecycle,
)
from execution.closed_lifecycle_recorder import _collapse_duplicate_obligations

# ── production fixtures, verbatim from Bitget position history ──────────────

XLM_CTIME = 1_786_178_726_700          # 2026-08-08T08:45:26.700Z
XLM_UTIME = 1_786_180_704_217          # 2026-08-08T09:18:24.217Z
XLM_OBSERVED_OPEN = 1_786_178_747_528  # 2026-08-08T08:45:47.528512Z
XLM_OBSERVED_CLOSE = 1_786_180_709_550  # 2026-08-08T09:18:29.550238Z
XLM_OPEN_LAG = XLM_OBSERVED_OPEN - XLM_CTIME      # 20_828 ms

AVAX_CTIME = 1_786_201_496_072         # 2026-08-08T15:04:56.072Z
AVAX_UTIME = 1_786_206_917_216         # 2026-08-08T16:35:17.216Z
AVAX_OBSERVED_OPEN = 1_786_201_501_750  # 2026-08-08T15:05:01.750886Z
AVAX_OBSERVED_CLOSE = 1_786_206_917_946  # 2026-08-08T16:35:17.946176Z
AVAX_OPEN_LAG = AVAX_OBSERVED_OPEN - AVAX_CTIME   # 5_678 ms


def xlm_history(**over):
    row = {
        "symbol": "XLMUSDT", "holdSide": "long", "ctime": XLM_CTIME, "utime": XLM_UTIME,
        "openTotalPos": "118", "closeTotalPos": "118",
        "openAvgPrice": "0.16352", "closeAvgPrice": "0.16379",
        "pnl": "0.03186", "netProfit": "0.00868645",
        "openFee": "-0.01157721", "closeFee": "-0.01159633", "totalFunding": "0",
        "positionId": "1469905606117322754",
    }
    row.update(over)
    return row


def avax_history(**over):
    row = {
        "symbol": "AVAXUSDT", "holdSide": "long", "ctime": AVAX_CTIME, "utime": AVAX_UTIME,
        "openTotalPos": "3.7", "closeTotalPos": "3.7",
        "openAvgPrice": "6.555", "closeAvgPrice": "6.543",
        "pnl": "-0.0407", "netProfit": "-0.07086885",
        "openFee": "-0.0145521", "closeFee": "-0.01452768", "totalFunding": "-0.00108907",
        "positionId": "1470001107785379842",
    }
    row.update(over)
    return row


def thin_provisional(symbol, opened_at, order_id=""):
    """What the close-detection path actually writes.

    Verbatim shape of csv lines 10 and 55 on 2026-08-08: no size, no lifecycle
    id, no exchange positionId — but it does carry the entry `exchange_order_id`,
    which is what lets the collapse prove it is the same lifecycle as the
    enriched row without ever falling back to symbol+side.
    """
    row = {"event_type": "CLOSE_PROVISIONAL", "symbol": symbol, "direction": "LONG",
           "opened_at": opened_at}
    if order_id:
        row["exchange_order_id"] = order_id
    return row


def enriched_provisional(symbol, opened_at, closed_at, size, lifecycle_id, order_id):
    """What the exchange-truth path writes once the close is confirmed."""
    return {"event_type": "CLOSE_PROVISIONAL", "symbol": symbol, "direction": "LONG",
            "opened_at": opened_at, "closed_at": closed_at,
            "confirmed_position_size": str(size), "position_size": str(size),
            "position_lifecycle_id": lifecycle_id, "exchange_order_id": order_id,
            "exchange_entry_order_id": order_id}


# ── 1. strong identity beats any observation lag ───────────────────────────

def test_strong_exchange_id_matches_regardless_of_observation_lag():
    """With a positionId the time axes are never consulted at all."""
    got = match_lifecycle(
        [xlm_history(), avax_history()], symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN + 86_400_000,   # absurd lag
        size=118.0, exchange_position_id="1469905606117322754",
    )
    assert got is not None and got["positionId"] == "1469905606117322754"


def test_strong_exchange_id_does_not_match_a_different_lifecycle():
    assert match_lifecycle(
        [xlm_history()], symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
        exchange_position_id="not-this-one",
    ) is None


# ── 2/3. the two production lifecycles now reconcile ────────────────────────

def test_historical_xlm_20828ms_observation_lag_matches_uniquely():
    assert XLM_OPEN_LAG == 20_828
    got = match_lifecycle(
        [xlm_history()], symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
        closed_at_ms=XLM_OBSERVED_CLOSE,
    )
    assert got is not None and got["positionId"] == "1469905606117322754"
    assert economics_from_history(got)["net_pnl"] == 0.00868645


def test_historical_avax_5678ms_observation_lag_matches_uniquely():
    """AVAX shares symbol+side+size with three older lifecycles; only the
    observation windows separate them, and they sit days away."""
    assert AVAX_OPEN_LAG == 5_678
    decoys = [
        avax_history(positionId="OLD-1", ctime=AVAX_CTIME - 244_103_578,
                     utime=AVAX_UTIME - 245_634_840),
        avax_history(positionId="OLD-2", ctime=AVAX_CTIME - 380_201_724,
                     utime=AVAX_UTIME - 384_994_252),
    ]
    got = match_lifecycle(
        [*decoys, avax_history()], symbol="AVAXUSDT", direction="long",
        opened_at_ms=AVAX_OBSERVED_OPEN, size=3.7,
        closed_at_ms=AVAX_OBSERVED_CLOSE,
    )
    assert got is not None and got["positionId"] == "1470001107785379842"
    assert economics_from_history(got)["net_pnl"] == -0.07086885


# ── 4-7. every direction and bound still fails closed ──────────────────────

def test_exchange_open_after_local_observation_is_refused():
    """We cannot observe a position before the exchange creates it. Beyond the
    small clock-skew grace this is refused, not tolerated."""
    assert match_lifecycle(
        [xlm_history(ctime=XLM_OBSERVED_OPEN + OPENED_AT_TOLERANCE_MS + 1)],
        symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
    ) is None


def test_small_forward_clock_skew_is_still_absorbed():
    """Ordinary host drift must not break a real match."""
    got = match_lifecycle(
        [xlm_history(ctime=XLM_OBSERVED_OPEN + 1_000)],
        symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
    )
    assert got is not None


def test_open_observation_lag_beyond_bound_is_refused():
    assert match_lifecycle(
        [xlm_history(ctime=XLM_OBSERVED_OPEN - OPEN_OBSERVATION_MAX_MS - 1)],
        symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
    ) is None


def test_close_direction_violation_is_refused():
    """Exchange close stamped after we observed the close: impossible."""
    assert match_lifecycle(
        [xlm_history(utime=XLM_OBSERVED_CLOSE + OPENED_AT_TOLERANCE_MS + 1)],
        symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
        closed_at_ms=XLM_OBSERVED_CLOSE,
    ) is None


def test_close_observation_lag_beyond_bound_is_refused():
    assert match_lifecycle(
        [xlm_history(utime=XLM_OBSERVED_CLOSE - CLOSE_OBSERVATION_MAX_MS - 1)],
        symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
        closed_at_ms=XLM_OBSERVED_CLOSE,
    ) is None


def test_close_axis_only_narrows_never_widens():
    """A row the open axis already rejected cannot be rescued by the close axis."""
    row = xlm_history(ctime=XLM_OBSERVED_OPEN - OPEN_OBSERVATION_MAX_MS - 1)
    assert match_lifecycle([row], symbol="XLMUSDT", direction="long",
                           opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
                           closed_at_ms=XLM_OBSERVED_CLOSE) is None


# ── 8. two survivors are still ambiguous ───────────────────────────────────

def test_two_candidates_inside_both_windows_raise_ambiguous():
    """The wider backward window must never resolve by proximity."""
    twin = xlm_history(positionId="TWIN", ctime=XLM_CTIME + 3_000,
                       utime=XLM_UTIME + 3_000)
    with pytest.raises(AmbiguousLifecycle):
        match_lifecycle([xlm_history(), twin], symbol="XLMUSDT", direction="long",
                        opened_at_ms=XLM_OBSERVED_OPEN, size=118.0,
                        closed_at_ms=XLM_OBSERVED_CLOSE)


# ── 9. thin rows still refuse ──────────────────────────────────────────────

def test_thin_provisional_without_size_or_strong_id_still_refuses():
    """No size and no exchange id is not enough evidence, however close the
    timestamps happen to be."""
    assert match_lifecycle(
        [xlm_history()], symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=None,
        closed_at_ms=XLM_OBSERVED_CLOSE,
    ) is None


def test_zero_size_is_not_a_size():
    assert match_lifecycle(
        [xlm_history()], symbol="XLMUSDT", direction="long",
        opened_at_ms=XLM_OBSERVED_OPEN, size=0.0,
    ) is None


# ── 10/11. obligation collapse ─────────────────────────────────────────────

def test_two_representations_of_one_lifecycle_are_one_obligation():
    """Second-resolution thin row plus microsecond enriched row: one debt."""
    thin = thin_provisional("XLMUSDT", "2026-08-08T08:45:47+00:00",
                            order_id="1469905605874053121")
    rich = enriched_provisional(
        "XLMUSDT", "2026-08-08T08:45:47.528512+00:00",
        "2026-08-08T09:18:29.550238+00:00", 118.0,
        "pos-3c7801a1c67efa98f186ede1574cde36", "1469905605874053121",
    )
    kept, merged = _collapse_duplicate_obligations([thin, rich])
    assert merged == 1
    assert len(kept) == 1
    # The survivor must be the one that can actually resolve.
    assert kept[0]["confirmed_position_size"] == "118.0"


def test_collapse_keeps_the_richer_row_regardless_of_input_order():
    thin = thin_provisional("AVAXUSDT", "2026-08-08T15:05:01+00:00",
                            order_id="1470001107533721601")
    rich = enriched_provisional(
        "AVAXUSDT", "2026-08-08T15:05:01.750886+00:00",
        "2026-08-08T16:35:17.946176+00:00", 3.7,
        "pos-53e79fc9a251a10ad923db4abcc56793", "1470001107533721601",
    )
    for order in ([thin, rich], [rich, thin]):
        kept, merged = _collapse_duplicate_obligations(order)
        assert merged == 1 and len(kept) == 1
        assert kept[0].get("position_lifecycle_id") == "pos-53e79fc9a251a10ad923db4abcc56793"


def test_two_real_lifecycles_on_one_symbol_are_never_merged():
    """Same symbol and side, different lifecycles: two obligations, always."""
    first = enriched_provisional(
        "XLMUSDT", "2026-08-08T08:45:47.528512+00:00",
        "2026-08-08T09:18:29.550238+00:00", 118.0, "pos-AAA", "order-AAA")
    second = enriched_provisional(
        "XLMUSDT", "2026-08-08T09:51:01.043000+00:00",
        "2026-08-08T10:10:11.451000+00:00", 146.0, "pos-BBB", "order-BBB")
    kept, merged = _collapse_duplicate_obligations([first, second])
    assert merged == 0 and len(kept) == 2


def test_same_symbol_same_size_different_open_second_is_not_merged():
    """Size alone must never be mistaken for identity."""
    a = {"event_type": "CLOSE_PROVISIONAL", "symbol": "XLMUSDT", "direction": "LONG",
         "opened_at": "2026-08-08T08:45:47+00:00", "confirmed_position_size": "118.0"}
    b = {"event_type": "CLOSE_PROVISIONAL", "symbol": "XLMUSDT", "direction": "LONG",
         "opened_at": "2026-08-08T14:45:47+00:00", "confirmed_position_size": "118.0"}
    kept, merged = _collapse_duplicate_obligations([a, b])
    assert merged == 0 and len(kept) == 2


def test_thin_rows_alone_are_not_merged_without_evidence():
    """Two size-less rows share nothing provable but symbol and side."""
    a = thin_provisional("XLMUSDT", "2026-08-08T08:45:47+00:00")
    b = thin_provisional("XLMUSDT", "2026-08-08T09:51:01+00:00")
    kept, merged = _collapse_duplicate_obligations([a, b])
    assert merged == 0 and len(kept) == 2


def test_different_lifecycle_ids_are_never_merged_however_alike():
    """A strong identifier that disagrees outranks any weak route that agrees.

    Same symbol, same side, same open second, same size — the composite route
    says "identical". The lifecycle ids say otherwise, and they win. Without this
    veto a scale-out re-entry at the same second and size would have its second
    leg silently absorbed into the first.
    """
    common = dict(symbol="XLMUSDT", direction="LONG",
                  opened_at="2026-08-08T08:45:47+00:00",
                  closed_at="2026-08-08T09:18:29+00:00",
                  confirmed_position_size="118.0", event_type="CLOSE_PROVISIONAL")
    a = {**common, "position_lifecycle_id": "pos-AAA"}
    b = {**common, "position_lifecycle_id": "pos-BBB"}
    kept, merged = _collapse_duplicate_obligations([a, b])
    assert merged == 0 and len(kept) == 2


def test_different_exchange_position_ids_are_never_merged():
    common = dict(symbol="AVAXUSDT", direction="LONG",
                  opened_at="2026-08-08T15:05:01+00:00",
                  confirmed_position_size="3.7", event_type="CLOSE_PROVISIONAL")
    a = {**common, "exchange_position_id": "1470001107785379842"}
    b = {**common, "exchange_position_id": "1470001107785379999"}
    kept, merged = _collapse_duplicate_obligations([a, b])
    assert merged == 0 and len(kept) == 2


def test_comparator_is_the_shared_dedup_identity():
    """The collapse must not invent its own notion of sameness."""
    rich = enriched_provisional(
        "XLMUSDT", "2026-08-08T08:45:47.528512+00:00",
        "2026-08-08T09:18:29.550238+00:00", 118.0, "pos-X", "order-X")
    thin = thin_provisional("XLMUSDT", "2026-08-08T08:45:47+00:00",
                            order_id="order-X")
    assert matches_same_lifecycle([rich], thin) is True
    assert matches_same_lifecycle([rich], thin_provisional(
        "XLMUSDT", "2026-08-08T11:03:21+00:00", order_id="order-OTHER")) is False
