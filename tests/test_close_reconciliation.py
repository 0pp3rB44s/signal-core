"""A timeout-close must end up with exchange economics, once, or with none."""

from __future__ import annotations

import csv

import pytest

from execution.close_dedup import economic_close_exists, lifecycle_keys
from execution.close_reconciler import (
    AmbiguousLifecycle,
    CloseReconciliationUnavailable,
    RECONCILED_SOURCE,
    economics_from_history,
    match_lifecycle,
    reconcile_close,
)
from telemetry.close_record_sources import EXCHANGE_CONFIRMED_CLOSE_SOURCES, is_economic_close

OPEN_MS = 1_785_700_000_000


def hist(symbol="TRXUSDT", side="short", ctime=OPEN_MS, size="77", pnl="-0.03619",
         net="-0.06631471", of="-0.01505149", cf="-0.01507321", fund="0", pid="12223747"):
    return {
        "symbol": symbol, "holdSide": side, "ctime": ctime, "utime": ctime + 5_400_000,
        "openTotalPos": size, "closeTotalPos": size, "openAvgPrice": "0.32579",
        "closeAvgPrice": "0.32626", "pnl": pnl, "netProfit": net,
        "openFee": of, "closeFee": cf, "totalFunding": fund, "positionId": pid,
    }


# ── matching ────────────────────────────────────────────────────────────────

def test_matches_on_symbol_side_and_open_time():
    got = match_lifecycle([hist()], symbol="TRXUSDT", direction="short",
                          opened_at_ms=OPEN_MS, size=77.0)
    assert got is not None and got["positionId"] == "12223747"


def test_wrong_side_never_matches():
    assert match_lifecycle([hist(side="long")], symbol="TRXUSDT", direction="short",
                           opened_at_ms=OPEN_MS, size=77.0) is None


def test_open_time_outside_tolerance_is_refused():
    assert match_lifecycle([hist(ctime=OPEN_MS + 60_000)], symbol="TRXUSDT",
                           direction="short", opened_at_ms=OPEN_MS, size=77.0) is None


def test_two_lifecycles_closing_in_the_same_second_stay_separate():
    """Same symbol, same side, closes one second apart: must not merge."""
    a = hist(ctime=OPEN_MS, pid="AAA", size="77")
    b = hist(ctime=OPEN_MS + 600_000, pid="BBB", size="80")
    a["utime"] = b["utime"] = OPEN_MS + 9_000_000  # identical close instant
    first = match_lifecycle([a, b], symbol="TRXUSDT", direction="short",
                            opened_at_ms=OPEN_MS, size=77.0)
    second = match_lifecycle([a, b], symbol="TRXUSDT", direction="short",
                             opened_at_ms=OPEN_MS + 600_000, size=80.0)
    assert first["positionId"] == "AAA"
    assert second["positionId"] == "BBB"


def test_ambiguous_without_open_time_refuses_rather_than_guesses():
    assert match_lifecycle([hist(pid="AAA"), hist(pid="BBB")], symbol="TRXUSDT",
                           direction="short", opened_at_ms=None, size=None) is None


# ── economics ───────────────────────────────────────────────────────────────

def test_money_is_copied_verbatim_from_exchange():
    e = economics_from_history(hist())
    assert e["gross_pnl"] == pytest.approx(-0.03619)
    assert e["net_pnl"] == pytest.approx(-0.06631471)
    assert e["fees"] == pytest.approx(0.01505149 + 0.01507321)
    assert e["sync_source"] in EXCHANGE_CONFIRMED_CLOSE_SOURCES


def test_missing_money_field_fails_closed_not_zero():
    with pytest.raises(CloseReconciliationUnavailable):
        economics_from_history(hist(net=""))


def test_fees_are_magnitudes():
    """Bitget reports fees negative; stored fees must be positive magnitudes."""
    e = economics_from_history(hist())
    assert e["open_fee"] > 0 and e["close_fee"] > 0


# ── polling ─────────────────────────────────────────────────────────────────

def test_reconcile_succeeds_once_exchange_publishes():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return [] if calls["n"] < 3 else [hist()]

    e = reconcile_close(symbol="TRXUSDT", direction="short", opened_at_ms=OPEN_MS,
                        size=77.0, fetch_history=fetch, sleep=lambda s: None)
    assert e["net_pnl"] == pytest.approx(-0.06631471)
    assert calls["n"] == 3


def test_absent_lifecycle_fails_closed_and_is_bounded():
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return []

    with pytest.raises(CloseReconciliationUnavailable):
        reconcile_close(symbol="TRXUSDT", direction="short", opened_at_ms=OPEN_MS,
                        size=77.0, fetch_history=fetch, sleep=lambda s: None)
    assert calls["n"] == 5  # bounded, no infinite retry


def test_transport_failure_is_not_treated_as_no_trade():
    def fetch():
        raise ConnectionError("dns")

    with pytest.raises(CloseReconciliationUnavailable):
        reconcile_close(symbol="TRXUSDT", direction="short", opened_at_ms=OPEN_MS,
                        size=77.0, fetch_history=fetch, sleep=lambda s: None)


# ── dedup ───────────────────────────────────────────────────────────────────

FIELDS = ["event_type", "symbol", "direction", "opened_at", "closed_at", "pnl",
          "net_pnl", "fees", "sync_source", "position_lifecycle_id",
          "exchange_position_id", "confirmed_position_size", "margin_roi_pct"]


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def _econ_row(**kw):
    base = {"event_type": "CLOSE", "symbol": "TRXUSDT", "direction": "SHORT",
            "opened_at": "2026-08-03T06:01:36", "net_pnl": "-0.06631471",
            "fees": "0.0301247", "sync_source": RECONCILED_SOURCE,
            "position_lifecycle_id": "pos-abc"}
    base.update(kw)
    return base


def test_second_reconciliation_is_blocked_by_lifecycle_id(tmp_path):
    p = tmp_path / "trade_dataset_v2.csv"
    _write(p, [_econ_row()])
    assert economic_close_exists(p, {"position_lifecycle_id": "pos-abc"}) is True


def test_provisional_row_does_not_block_reconciliation(tmp_path):
    p = tmp_path / "trade_dataset_v2.csv"
    _write(p, [_econ_row(event_type="CLOSE_PROVISIONAL", sync_source="position_manager")])
    assert economic_close_exists(p, {"position_lifecycle_id": "pos-abc"}) is False


def test_quarantined_row_does_not_block(tmp_path):
    p = tmp_path / "trade_dataset_v2.csv"
    _write(p, [_econ_row(event_type="CLOSE_QUARANTINED")])
    assert economic_close_exists(p, {"position_lifecycle_id": "pos-abc"}) is False


def test_dedup_reads_rotated_segments(tmp_path):
    """A lifecycle written before a schema rotation still blocks a duplicate."""
    p = tmp_path / "trade_dataset_v2.csv"
    _write(p, [])
    _write(tmp_path / "trade_dataset_v2.csv.1", [_econ_row()])
    assert economic_close_exists(p, {"position_lifecycle_id": "pos-abc"}) is True


def test_exchange_position_id_identifies_when_lifecycle_missing(tmp_path):
    p = tmp_path / "trade_dataset_v2.csv"
    _write(p, [_econ_row(position_lifecycle_id="", exchange_position_id="12223747")])
    assert economic_close_exists(p, {"exchange_position_id": "12223747"}) is True


def test_composite_fallback_separates_same_second_lifecycles(tmp_path):
    p = tmp_path / "trade_dataset_v2.csv"
    _write(p, [_econ_row(position_lifecycle_id="", exchange_position_id="",
                         opened_at="2026-08-03T06:01:36", confirmed_position_size="77")])
    same = {"symbol": "TRXUSDT", "direction": "SHORT",
            "opened_at": "2026-08-03T06:01:36", "confirmed_position_size": "77"}
    other = {"symbol": "TRXUSDT", "direction": "SHORT",
             "opened_at": "2026-08-03T06:11:36", "confirmed_position_size": "80"}
    assert economic_close_exists(p, same) is True
    assert economic_close_exists(p, other) is False


def test_reconciled_row_counts_economically():
    row = _econ_row()
    assert is_economic_close(row) is True


def test_lifecycle_keys_ignore_blank_identifiers():
    assert lifecycle_keys({"position_lifecycle_id": "", "symbol": "", "direction": ""}) == set()


# ── recovered-position matching (SOLUSDT pos-dab4e47f4e843fb08a6606047d1f836d) ─
#
# Real incident, 2026-08-21. This lifecycle was recovered_from_exchange=True:
# discovered already open, so its local opened_at (08:12:48.400Z) is the
# discovery timestamp, not the exchange's true open time (ctime 08:10:13.475Z,
# from Bitget position history). The ~155s gap exceeds OPEN_OBSERVATION_MAX_MS
# (120s), so the unmodified open-axis filter rejected the one genuine match.
# closed_at_ms here is our OWN close action's timestamp -- reliable regardless
# of discovery lag -- and is what is_recovered=True relies on instead.

SOL_OPENED_AT_DISCOVERY_MS = 1_787_299_968_400   # local opened_at (discovery time)
SOL_CLOSED_AT_MS = 1_787_299_995_109             # local closed_at (our own close action)


def sol_hist(pid, ctime, utime, side="short", size="1.6"):
    return {
        "symbol": "SOLUSDT", "holdSide": side, "ctime": str(ctime), "utime": str(utime),
        "openTotalPos": size, "closeTotalPos": size, "openAvgPrice": "90.0",
        "closeAvgPrice": "90.0", "pnl": "-0.1728", "netProfit": "-0.347184",
        "openFee": "-0.08714016", "closeFee": "-0.08724384", "totalFunding": "0",
        "positionId": pid,
    }


SOL_TARGET = sol_hist("1474607784862052361", 1_787_299_813_475, 1_787_299_995_389)
SOL_TOO_EARLY_CLOSE = sol_hist("1474601057546563586", 1_787_298_209_558, 1_787_298_300_126)
SOL_TOO_LATE_OPEN = sol_hist("1474627118183378945", 1_787_304_422_898, 1_787_304_465_264)


def test_recovered_position_matches_despite_discovery_time_gap():
    """The real incident: without is_recovered, this returns None (proven bug)."""
    rows = [SOL_TOO_EARLY_CLOSE, SOL_TARGET, SOL_TOO_LATE_OPEN]
    got = match_lifecycle(
        rows, symbol="SOLUSDT", direction="short",
        opened_at_ms=SOL_OPENED_AT_DISCOVERY_MS, size=1.6,
        exchange_position_id="", closed_at_ms=SOL_CLOSED_AT_MS,
        is_recovered=True,
    )
    assert got is not None
    assert got["positionId"] == "1474607784862052361"


def test_recovered_position_without_the_flag_still_fails_the_open_axis():
    """Proves the fix is additive: the un-flagged path is exactly the old bug."""
    rows = [SOL_TOO_EARLY_CLOSE, SOL_TARGET, SOL_TOO_LATE_OPEN]
    got = match_lifecycle(
        rows, symbol="SOLUSDT", direction="short",
        opened_at_ms=SOL_OPENED_AT_DISCOVERY_MS, size=1.6,
        exchange_position_id="", closed_at_ms=SOL_CLOSED_AT_MS,
        is_recovered=False,
    )
    assert got is None


def test_recovered_position_rejects_the_too_early_close_candidate():
    got = match_lifecycle(
        [SOL_TOO_EARLY_CLOSE], symbol="SOLUSDT", direction="short",
        opened_at_ms=SOL_OPENED_AT_DISCOVERY_MS, size=1.6,
        closed_at_ms=SOL_CLOSED_AT_MS, is_recovered=True,
    )
    assert got is None  # close-axis lag ~28 min, far outside CLOSE_OBSERVATION_MAX_MS


def test_recovered_position_rejects_the_too_late_open_candidate():
    got = match_lifecycle(
        [SOL_TOO_LATE_OPEN], symbol="SOLUSDT", direction="short",
        opened_at_ms=SOL_OPENED_AT_DISCOVERY_MS, size=1.6,
        closed_at_ms=SOL_CLOSED_AT_MS, is_recovered=True,
    )
    assert got is None  # close-axis lag negative and far outside tolerance


def test_recovered_position_fails_closed_on_a_genuinely_ambiguous_candidate():
    """A second row that ALSO satisfies the close axis must raise, not guess."""
    decoy = sol_hist("9999999999999999999", 1_787_260_000_000, SOL_CLOSED_AT_MS + 500)
    rows = [SOL_TARGET, decoy]
    with pytest.raises(AmbiguousLifecycle):
        match_lifecycle(
            rows, symbol="SOLUSDT", direction="short",
            opened_at_ms=SOL_OPENED_AT_DISCOVERY_MS, size=1.6,
            closed_at_ms=SOL_CLOSED_AT_MS, is_recovered=True,
        )


def test_normal_locally_opened_lifecycle_matching_is_unchanged():
    """is_recovered defaults to False: every pre-existing behaviour is untouched."""
    got = match_lifecycle([hist()], symbol="TRXUSDT", direction="short",
                           opened_at_ms=OPEN_MS, size=77.0)
    assert got is not None and got["positionId"] == "12223747"
    assert match_lifecycle([hist(ctime=OPEN_MS + 60_000)], symbol="TRXUSDT",
                            direction="short", opened_at_ms=OPEN_MS, size=77.0,
                            is_recovered=False) is None


def test_recovered_position_rejects_wrong_size_candidate():
    wrong_size = sol_hist("8888888888888888888", 1_787_299_813_475,
                           1_787_299_995_389, size="9.9")
    got = match_lifecycle(
        [wrong_size], symbol="SOLUSDT", direction="short",
        opened_at_ms=SOL_OPENED_AT_DISCOVERY_MS, size=1.6,
        closed_at_ms=SOL_CLOSED_AT_MS, is_recovered=True,
    )
    assert got is None
