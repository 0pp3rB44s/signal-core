"""Directional symbol expectancy from validated LIVE closed trades.

Root cause pinned here: the symbol kill-switch read `by_symbol` from
reports/backtests/latest_summary.json — offline backtest output from
2026-07-05 — and treated it as an indefinite LIVE hard pause. BTCUSDT was
paused on 42 *simulated* trades (expectancy -0.089) pooled across LONG and
SHORT, with no freshness or sample metadata, and the pause could never be
re-earned because a paused symbol produces no new trades.
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id
from clients.schemas import MarketSnapshot, StrategyCandidate, StrategyScore, SweepDetection
from risk import symbol_expectancy as se
from risk.risk_manager import RiskManager

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
CANDLE_MS = 1_785_000_000_000

COLUMNS = ["event_type", "timestamp", "symbol", "direction", "strategy", "status",
           "closed_at", "net_pnl", "tp1_hit", "sync_source"]

VALID_SOURCE = "validated_exchange_position_closed_sync"


def _write(path: Path, rows: list[dict], columns: list[str] | None = None) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns or COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return path


def _close(symbol: str, direction: str, net_pnl: float, days_ago: float,
           tp1: bool = True, source: str = VALID_SOURCE) -> dict:
    ts = (NOW - timedelta(days=days_ago)).isoformat()
    return {"event_type": "CLOSE", "timestamp": ts, "symbol": symbol,
            "direction": direction, "strategy": "low_vol_reclaim", "status": "CLOSED",
            "closed_at": ts, "net_pnl": net_pnl, "tp1_hit": str(tp1),
            "sync_source": source}


def _dataset(tmp_path: Path, rows: list[dict], columns: list[str] | None = None) -> Path:
    return _write(tmp_path / "trade_dataset_v2.csv", rows, columns)


def _rec(tmp_path: Path, rows: list[dict], symbol="BTCUSDT", direction="LONG",
         columns: list[str] | None = None):
    return se.record_for(symbol, direction, now=NOW,
                         dataset_path=_dataset(tmp_path, rows, columns))


@pytest.fixture(autouse=True)
def _clear_cache():
    se.reset_cache()
    yield
    se.reset_cache()


# --- 1. stale offline data cannot indefinitely pause a LIVE symbol -------

def test_latest_summary_by_symbol_is_never_read_for_gating():
    """The offline backtest must be gone from the live gate entirely."""
    src = Path(RiskManager.__module__.replace(".", "/") + ".py")
    body = (Path(__file__).resolve().parents[1] / src).read_text()
    gate = body.split("def _kill_switch_gate")[1].split("def _strategy_weighting_gate")[0]
    assert "by_symbol" not in gate.replace("# by_symbol is deliberately NOT read", ""), \
        "kill-switch gate still touches by_symbol"
    assert "symbol_expectancy.record_for" in gate


def test_expired_negative_evidence_does_not_block():
    """The core repair: aged-out evidence is reported, never enforced, so a
    pause can always be re-earned."""
    rec = se.SymbolExpectancyRecord(
        symbol="BTCUSDT", direction="LONG", source=se.SOURCE_NAME,
        generated_at=NOW.isoformat(), last_trade_at="2026-05-01T00:00:00+00:00",
        window_days=30, sample_size=42, expectancy=-0.089, winrate=0.405,
        tp1_hit_rate=0.476, freshness_state=se.EXPIRED, confidence=se.LOW,
        status=se.SUFFICIENT_NEGATIVE,
    )
    blocked, reason = se.evaluate(rec)
    assert blocked is False and reason is None


@pytest.mark.parametrize("freshness", [se.STALE, se.EXPIRED])
def test_only_fresh_and_aging_may_block(freshness):
    rec = se.SymbolExpectancyRecord(
        "BTCUSDT", "LONG", se.SOURCE_NAME, NOW.isoformat(), NOW.isoformat(),
        30, 20, -0.05, 0.30, 0.50, freshness, se.LOW, se.SUFFICIENT_NEGATIVE)
    assert se.evaluate(rec)[0] is False


def test_old_trades_fall_out_of_the_window_entirely(tmp_path):
    """Closes older than WINDOW_DAYS are not evidence at all."""
    rows = [_close("BTCUSDT", "LONG", -1.0, days_ago=60) for _ in range(20)]
    rec = _rec(tmp_path, rows)
    assert rec.sample_size == 0
    assert rec.status == se.INSUFFICIENT_LIVE_DATA
    assert se.evaluate(rec)[0] is False


# --- 2. LONG and SHORT are isolated -------------------------------------

def test_long_and_short_are_separate_records(tmp_path):
    rows = ([_close("BTCUSDT", "SHORT", -0.5, 1) for _ in range(12)]
            + [_close("BTCUSDT", "LONG", +0.5, 1) for _ in range(12)])
    ds = _dataset(tmp_path, rows)
    short = se.record_for("BTCUSDT", "SHORT", now=NOW, dataset_path=ds)
    long_ = se.record_for("BTCUSDT", "LONG", now=NOW, dataset_path=ds)
    assert short.sample_size == 12 and long_.sample_size == 12
    assert short.expectancy < 0 < long_.expectancy
    assert short.status == se.SUFFICIENT_NEGATIVE
    assert long_.status == se.SUFFICIENT_OK


def test_losing_short_history_does_not_block_long(tmp_path):
    """The exact pooling defect: BTCUSDT shorts lost money, which paused BTCUSDT
    longs too because the old record mixed both directions."""
    rows = ([_close("BTCUSDT", "SHORT", -0.9, 1) for _ in range(20)]
            + [_close("BTCUSDT", "LONG", +0.4, 1) for _ in range(12)])
    ds = _dataset(tmp_path, rows)
    assert se.evaluate(se.record_for("BTCUSDT", "SHORT", now=NOW, dataset_path=ds))[0] is True
    assert se.evaluate(se.record_for("BTCUSDT", "LONG", now=NOW, dataset_path=ds))[0] is False


def test_a_symbols_history_does_not_leak_to_another(tmp_path):
    rows = [_close("ETHUSDT", "LONG", -0.9, 1) for _ in range(20)]
    ds = _dataset(tmp_path, rows)
    assert se.evaluate(se.record_for("ETHUSDT", "LONG", now=NOW, dataset_path=ds))[0] is True
    btc = se.record_for("BTCUSDT", "LONG", now=NOW, dataset_path=ds)
    assert btc.status == se.INSUFFICIENT_LIVE_DATA
    assert se.evaluate(btc)[0] is False


# --- 3. insufficient data -----------------------------------------------

def test_zero_live_trades_is_explicit_insufficient_not_positive(tmp_path):
    rec = _rec(tmp_path, [])
    assert rec.status == se.INSUFFICIENT_LIVE_DATA
    assert rec.confidence == se.LOW
    assert rec.sample_size == 0
    assert rec.expectancy is None, "must not fabricate an expectancy value"
    assert rec.status != se.SUFFICIENT_OK, "absence of data is not a positive edge"
    assert se.evaluate(rec)[0] is False


def test_below_min_sample_is_insufficient_even_when_negative(tmp_path):
    rows = [_close("BTCUSDT", "LONG", -1.0, 1) for _ in range(se.MIN_SAMPLE - 1)]
    rec = _rec(tmp_path, rows)
    assert rec.sample_size == se.MIN_SAMPLE - 1
    assert rec.status == se.INSUFFICIENT_LIVE_DATA
    assert rec.confidence == se.LOW
    assert se.evaluate(rec)[0] is False


def test_min_sample_boundary_engages_the_gate(tmp_path):
    rows = [_close("BTCUSDT", "LONG", -1.0, 1) for _ in range(se.MIN_SAMPLE)]
    rec = _rec(tmp_path, rows)
    assert rec.sample_size == se.MIN_SAMPLE
    assert rec.status == se.SUFFICIENT_NEGATIVE
    assert se.evaluate(rec)[0] is True


def test_absent_source_behaves_like_insufficient(tmp_path):
    rec = se.record_for("BTCUSDT", "LONG", now=NOW,
                        dataset_path=tmp_path / "does_not_exist.csv")
    assert rec.status == se.SOURCE_ABSENT
    assert rec.confidence == se.LOW
    assert se.evaluate(rec)[0] is False, "absent source must not block"


# --- 4. malformed data fails closed -------------------------------------

def test_missing_required_column_fails_closed(tmp_path):
    rows = [{"event_type": "CLOSE", "symbol": "BTCUSDT", "direction": "LONG",
             "net_pnl": "1.0"}]
    rec = _rec(tmp_path, rows, columns=["event_type", "symbol", "direction", "net_pnl"])
    assert rec.status == se.SOURCE_MALFORMED
    blocked, reason = se.evaluate(rec)
    assert blocked is True
    assert "malformed" in reason.lower()


def test_unparseable_file_fails_closed(tmp_path):
    p = tmp_path / "trade_dataset_v2.csv"
    p.write_bytes(b"\xff\xfe\x00\x00 not,valid\x00csv")
    rec = se.record_for("BTCUSDT", "LONG", now=NOW, dataset_path=p)
    assert rec.status == se.SOURCE_MALFORMED
    assert se.evaluate(rec)[0] is True


def test_malformed_raises_a_visible_alert(tmp_path, caplog):
    rows = [{"event_type": "CLOSE"}]
    with caplog.at_level(logging.ERROR, logger="symbol_expectancy"):
        _rec(tmp_path, rows, columns=["event_type"])
    assert any("SYMBOL_EXPECTANCY_SOURCE_MALFORMED" in r.message for r in caplog.records)
    assert any("fail_closed" in str(r.msg) + str(r.args) for r in caplog.records)


def test_malformed_does_not_silently_disable_protection(tmp_path):
    """Distinct from SOURCE_ABSENT: a present-but-broken file must block."""
    rec = _rec(tmp_path, [{"event_type": "CLOSE"}], columns=["event_type"])
    absent = se.record_for("X", "LONG", now=NOW, dataset_path=tmp_path / "nope.csv")
    assert se.evaluate(rec)[0] is True
    assert se.evaluate(absent)[0] is False


# --- 5. fresh negative expectancy still blocks --------------------------

def test_fresh_negative_expectancy_blocks(tmp_path):
    rows = [_close("BTCUSDT", "SHORT", -0.4, 2) for _ in range(15)]
    rec = _rec(tmp_path, rows, direction="SHORT")
    assert rec.freshness_state == se.FRESH
    assert rec.status == se.SUFFICIENT_NEGATIVE
    blocked, reason = se.evaluate(rec)
    assert blocked is True
    assert "symbol paused by expectancy" in reason
    assert "SHORT" in reason and "n=15" in reason


def test_aging_negative_expectancy_still_blocks(tmp_path):
    rows = [_close("BTCUSDT", "LONG", -0.4, 10) for _ in range(15)]
    rec = _rec(tmp_path, rows)
    assert rec.freshness_state == se.AGING
    assert se.evaluate(rec)[0] is True


def test_weak_tp1_hit_rate_blocks_with_its_own_reason(tmp_path):
    rows = [_close("BTCUSDT", "LONG", +0.10, 1, tp1=False) for _ in range(15)]
    rec = _rec(tmp_path, rows)
    assert rec.tp1_hit_rate == 0.0
    assert rec.status == se.SUFFICIENT_NEGATIVE
    blocked, reason = se.evaluate(rec)
    assert blocked is True
    assert "failed TP1 too often" in reason


def test_positive_fresh_expectancy_passes(tmp_path):
    rows = [_close("BTCUSDT", "LONG", +0.4, 1) for _ in range(15)]
    rec = _rec(tmp_path, rows)
    assert rec.status == se.SUFFICIENT_OK
    assert se.evaluate(rec)[0] is False


# --- source purity: live only, never simulation -------------------------

@pytest.mark.parametrize("bogus", ["position_manager", "backtest", "simulation",
                                   "", "30", "0", "unprotected_position_emergency_close"])
def test_non_exchange_confirmed_sources_are_excluded(tmp_path, bogus):
    rows = [_close("BTCUSDT", "LONG", -1.0, 1, source=bogus) for _ in range(20)]
    rec = _rec(tmp_path, rows)
    assert rec.sample_size == 0, f"{bogus!r} must not count as exchange-confirmed"
    assert rec.status == se.INSUFFICIENT_LIVE_DATA


def test_non_close_events_are_ignored(tmp_path):
    rows = [dict(_close("BTCUSDT", "LONG", -1.0, 1), event_type="OPEN") for _ in range(20)]
    assert _rec(tmp_path, rows).sample_size == 0


# --- record shape -------------------------------------------------------

def test_record_carries_every_required_field(tmp_path):
    rows = [_close("BTCUSDT", "LONG", +0.2, 1) for _ in range(12)]
    d = _rec(tmp_path, rows).as_dict()
    for field in ("symbol", "direction", "source", "generated_at", "last_trade_at",
                  "window_days", "sample_size", "expectancy", "winrate",
                  "tp1_hit_rate", "freshness_state", "confidence", "status"):
        assert field in d, f"missing {field}"
    assert d["source"] == se.SOURCE_NAME
    assert d["window_days"] == se.WINDOW_DAYS


@pytest.mark.parametrize(("age", "expected"), [
    (0, se.FRESH), (7, se.FRESH), (7.5, se.AGING), (14, se.AGING),
    (14.5, se.STALE), (30, se.STALE), (31, se.EXPIRED), (None, se.EXPIRED),
])
def test_freshness_ladder(age, expected):
    assert se.freshness_for_age(age) == expected


def test_confidence_requires_both_sample_and_recency():
    assert se.confidence_for(se.MIN_SAMPLE - 1, se.FRESH) == se.LOW
    assert se.confidence_for(se.MIN_SAMPLE * 2, se.FRESH) == se.HIGH
    assert se.confidence_for(se.MIN_SAMPLE, se.AGING) == se.MEDIUM
    assert se.confidence_for(se.MIN_SAMPLE * 5, se.EXPIRED) == se.LOW


# --- 6. strategy expectancy unchanged -----------------------------------

def test_strategy_gate_thresholds_untouched():
    assert RiskManager.SAFE_ALPHA_MAX_LEVERAGE == 8
    assert RiskManager.SAFE_ALPHA_MAX_RISK_PCT == 0.75
    assert RiskManager.PROBE_RISK_MULTIPLIER == 0.5
    import inspect
    body = inspect.getsource(RiskManager._strategy_weighting_gate)
    assert "trades < 5" in body
    assert "expectancy < 0" in body
    assert "return True, reasons, True" in body, "PROBE path must survive"
    assert "clean_strategy_expectancy" in body


def test_strategy_stats_should_pause_untouched():
    import inspect
    body = inspect.getsource(RiskManager._stats_should_pause)
    assert "trades < min_trades" in body
    assert "expectancy < 0" in body
    assert "lossrate >= 0.75" in body


def test_strategy_expectancy_file_still_overrides_by_strategy():
    import inspect
    gate = inspect.getsource(RiskManager._kill_switch_gate)
    assert "clean_by_strategy" in gate
    assert "by_strategy = clean_by_strategy" in gate


# --- 7. migration -------------------------------------------------------

def test_migration_is_logged_once(caplog):
    se._MIGRATION_LOGGED = False
    with caplog.at_level(logging.INFO, logger="symbol_expectancy"):
        se.log_migration_once()
        se.log_migration_once()
    lines = [r for r in caplog.records if "SYMBOL_EXPECTANCY_MIGRATION" in r.message]
    assert len(lines) == 1
    msg = lines[0].getMessage()
    assert "latest_summary.json:by_symbol" in msg
    assert se.SOURCE_NAME in msg
    assert "(symbol,direction)" in msg


def test_migration_logged_on_first_record_lookup(tmp_path, caplog):
    se._MIGRATION_LOGGED = False
    with caplog.at_level(logging.INFO, logger="symbol_expectancy"):
        _rec(tmp_path, [])
    assert any("SYMBOL_EXPECTANCY_MIGRATION" in r.message for r in caplog.records)


def test_provenance_note_is_soft_and_never_a_hard_reason(tmp_path):
    """The note is emitted on every decision; if it classified as a hard gate it
    would block every candidate."""
    from telemetry.funnel import classify_reason_codes
    note = se.observability_note(_rec(tmp_path, []))
    assert classify_reason_codes([note]) == []
    assert note.startswith("symbol expectancy source=")


def test_risk_manager_treats_the_note_as_soft():
    import inspect
    gate = inspect.getsource(RiskManager._kill_switch_gate)
    assert "symbol expectancy source=" in gate
    soft = gate.split("SOFT_PREFIXES")[1]
    assert "symbol expectancy source=" in soft, "note must be in the soft-prefix list"


# --- telemetry codes ----------------------------------------------------

def test_block_reasons_classify_to_the_right_codes(tmp_path):
    from telemetry.funnel import REASON_CODES, classify_reason_codes
    neg = _rec(tmp_path, [_close("BTCUSDT", "LONG", -0.4, 1) for _ in range(15)])
    assert classify_reason_codes([se.evaluate(neg)[1]]) == ["SYMBOL_EXPECTANCY_PAUSE"]
    bad = _rec(tmp_path, [{"event_type": "CLOSE"}], columns=["event_type"])
    assert classify_reason_codes([se.evaluate(bad)[1]]) == ["SYMBOL_EXPECTANCY_SOURCE_MALFORMED"]
    assert "SYMBOL_EXPECTANCY_SOURCE_MALFORMED" in REASON_CODES


def test_dashboard_labels_the_new_code():
    from dashboard_v3.panels.funnel import REASON_LABELS
    assert "SYMBOL_EXPECTANCY_SOURCE_MALFORMED" in REASON_LABELS


# --- integration: the real live dataset ---------------------------------

def test_empty_local_dataset_gives_btcusdt_no_pause_in_either_direction(tmp_path):
    """A names-only checkout has no tracked LIVE closes, so neither direction may
    be paused by symbol expectancy. The fixture remains hermetic."""
    dataset = _dataset(tmp_path, [])
    for direction in ("LONG", "SHORT"):
        rec = se.record_for(
            "BTCUSDT", direction, dataset_path=dataset, use_cache=False
        )
        assert rec.sample_size == 0
        assert rec.status == se.INSUFFICIENT_LIVE_DATA
        assert rec.confidence == se.LOW
        assert se.evaluate(rec)[0] is False


def _settings(**over) -> Settings:
    base = {"EXECUTION_ENABLED": True, "EXECUTION_MODE": "LIVE",
            "EXECUTION_REQUIRE_CONFIRMATION": False, "MAKER_ENTRY_ENABLED": False,
            "SYMBOL_COOLDOWN_MINUTES": 0}
    base.update(over)
    return Settings(_env_file=None, **base)


def _candidate(direction: str, symbol: str = "BTCUSDT") -> StrategyCandidate:
    cid = deterministic_candidate_id("low_vol_reclaim", symbol, direction, CANDLE_MS)
    return StrategyCandidate(
        candidate_id=cid, candidate_candle_open_timestamp_ms=CANDLE_MS, symbol=symbol,
        strategy="low_vol_reclaim", direction=direction, primary_granularity="15m",
        confirmation_granularity="1H", market=MagicMock(spec=MarketSnapshot),
        detection=MagicMock(spec=SweepDetection), notes=[])


def test_live_long_is_no_longer_paused_by_the_retired_offline_data(
    tmp_path, monkeypatch
):
    """Before: BTCUSDT LONG carried 'kill-switch: symbol paused by expectancy'
    from a 25-day-old backtest. After: no symbol-expectancy pause at all."""
    monkeypatch.setattr(se, "DATASET_PATH", _dataset(tmp_path, []))
    se.reset_cache()
    rm = RiskManager(settings=_settings(ENABLE_SHORTS=False))
    monkeypatch.setattr(rm, "_latest_backtest_summary", lambda: {"by_strategy": {}})
    monkeypatch.setattr(rm, "_latest_strategy_expectancy", lambda: {})
    monkeypatch.setattr(rm, "_daily_defensive_status", lambda: {})
    allowed, reasons = rm._kill_switch_gate(_candidate("LONG"))
    joined = " | ".join(reasons)
    assert allowed is True
    assert "symbol paused by expectancy" not in joined
    assert "symbol failed TP1 too often" not in joined
    assert "symbol expectancy source=" in joined, "provenance must still be reported"


def test_shorts_disabled_still_wins_over_everything():
    rm = RiskManager(settings=_settings(ENABLE_SHORTS=False))
    verdict = rm.evaluate(_candidate("SHORT"),
                          StrategyScore(total=92.0, breakdown={}, verdict="GO", reasons=[]))
    assert verdict.allowed is False
    assert len(verdict.reasons) == 1
    assert "shorts disabled by configuration" in verdict.reasons[0]
