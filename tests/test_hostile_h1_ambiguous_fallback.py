"""H1: unequal-distance multi-match must fail closed, not pick the nearest.

Hostile review reproduction. Two history rows satisfy symbol, side, size and the
open-time tolerance, but sit at different distances from the requested open
time. The previous code sorted by distance and returned the nearest, raising
AmbiguousLifecycle only on an exact tie — so the common case silently attached
one lifecycle's money to another.
"""

from __future__ import annotations

import pytest

from execution.close_reconciler import (
    AmbiguousLifecycle,
    OPENED_AT_TOLERANCE_MS,
    match_lifecycle,
)

SYMBOL = "SOLUSDT"


def row(ctime, pid, side="short", size="0.3"):
    return {"symbol": SYMBOL, "holdSide": side, "ctime": ctime,
            "utime": ctime + 600_000, "openTotalPos": size, "closeTotalPos": size,
            "openAvgPrice": "73.0", "closeAvgPrice": "73.1", "pnl": "-0.01",
            "netProfit": "-0.02", "openFee": "-0.005", "closeFee": "-0.005",
            "totalFunding": "0", "positionId": pid}


def test_h1_unequal_distance_multi_match_is_ambiguous():
    """The literal hostile case: 1000 and 2000, requested 1400, both within ±5s."""
    a = row(1000, "A")
    b = row(2000, "B")
    assert abs(1000 - 1400) <= OPENED_AT_TOLERANCE_MS
    assert abs(2000 - 1400) <= OPENED_AT_TOLERANCE_MS
    with pytest.raises(AmbiguousLifecycle):
        match_lifecycle([a, b], symbol=SYMBOL, direction="short",
                        opened_at_ms=1400, size=0.3)


def test_h1_error_names_every_candidate():
    with pytest.raises(AmbiguousLifecycle) as exc:
        match_lifecycle([row(1000, "A"), row(2000, "B")], symbol=SYMBOL,
                        direction="short", opened_at_ms=1400, size=0.3)
    assert "A" in str(exc.value) and "B" in str(exc.value)


def test_h1_strong_position_id_still_resolves_the_same_pair():
    """A unique exchange identifier wins before the fallback is reached."""
    got = match_lifecycle([row(1000, "A"), row(2000, "B")], symbol=SYMBOL,
                          direction="short", opened_at_ms=1400, size=0.3,
                          exchange_position_id="B")
    assert got is not None and got["positionId"] == "B"


def test_h1_single_candidate_inside_tolerance_still_matches():
    got = match_lifecycle([row(1400, "A")], symbol=SYMBOL, direction="short",
                          opened_at_ms=1400, size=0.3)
    assert got is not None and got["positionId"] == "A"


def test_h1_long_and_short_are_never_mixed():
    with pytest.raises(AmbiguousLifecycle):
        match_lifecycle([row(1000, "A"), row(2000, "B")], symbol=SYMBOL,
                        direction="short", opened_at_ms=1400, size=0.3)
    assert match_lifecycle([row(1000, "A", side="long")], symbol=SYMBOL,
                           direction="short", opened_at_ms=1000, size=0.3) is None
