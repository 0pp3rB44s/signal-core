"""Sunday shadow observability: health classification, trade aggregation,
pre/post-hardening split, and the safety rollups. Every case here exists
because a wrong answer would either hide a real problem or manufacture a
false alarm - both are worse than UNKNOWN.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from dashboard_v3.core import shadow_sources as ssrc
from dashboard_v3.core.status import Status
from dashboard_v3.panels import shadow


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_shadow_root(tmp_path, *, heartbeat=None, runtime_state=None,
                        outcomes_rows=None, events=None):
    (tmp_path / "state").mkdir()
    (tmp_path / "data_store").mkdir()
    if heartbeat is not None:
        (tmp_path / "state" / "runtime_heartbeat.json").write_text(json.dumps(heartbeat))
    if runtime_state is not None:
        lines = "\n".join(f"{k}={v}" for k, v in runtime_state.items())
        (tmp_path / "state" / "forward_paper_runtime.state").write_text(lines)
    header = ("git_commit,symbol,direction,strategy,exit_timestamp,signal_to_fill_bps,"
              "spread_bps,gross_pnl,net_pnl,result_r,metrics_available,open_interest,"
              "open_interest_change_5m,open_interest_change_15m,taker_buy_sell_volume_ratio,"
              "top_trader_long_short_ratio,global_long_short_ratio,mfe_pct,mae_pct,"
              "final_exit_reason")
    rows = outcomes_rows or []
    csv_text = header + "\n" + "\n".join(rows)
    (tmp_path / "data_store" / "forward_paper_outcomes.csv").write_text(csv_text)
    lines = [json.dumps(e) for e in (events or [])]
    (tmp_path / "data_store" / "forward_paper_events.jsonl").write_text("\n".join(lines))


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Every test sets its own SHADOW_ROOT explicitly - never touch the real one."""
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", None)
    monkeypatch.setattr(ssrc, "SHADOW_ROOT_ERROR", "not set in test")
    monkeypatch.setattr(shadow, "EXPECTED_SHADOW_SHA", "b1d9fa9")
    monkeypatch.setattr(shadow, "HARDENING_BOUNDARY_SHA", "b1d9fa9")
    yield


# --- health classification ---------------------------------------------

def test_missing_shadow_root_is_unknown_not_healthy(monkeypatch):
    r = shadow.load_runtime()
    assert r.root_available is False
    assert r.status is Status.UNKNOWN


def test_healthy_shadow_requires_recent_heartbeat_and_scan(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    now = datetime.now(timezone.utc)
    _write_shadow_root(tmp_path, heartbeat={
        "pid": os.getpid(), "commit": "b1d9fa95a70cd742d170e451f53df150b7c033f0",
        "timestamp": _iso(now), "last_successful_scan_utc": _iso(now),
        "scan_cycles_completed": 3, "scan_cycles_started": 3, "last_error_type": None,
    }, runtime_state={"scan_interval_sec": "60"})
    monkeypatch.setattr(ssrc, "process_cwd", lambda pid: str(tmp_path))
    r = shadow.load_runtime()
    assert r.status is Status.HEALTHY
    assert r.sha_match == "YES"
    assert r.market_data_receiving is True
    assert r.signal_loop_running is True


def test_pid_exists_but_stale_heartbeat_is_stale_not_healthy(tmp_path, monkeypatch):
    """The core requirement: PID_EXISTS != HEALTHY."""
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    old = datetime.now(timezone.utc) - timedelta(seconds=400)
    _write_shadow_root(tmp_path, heartbeat={
        "pid": os.getpid(), "commit": "b1d9fa9", "timestamp": _iso(old),
        "last_successful_scan_utc": _iso(old),
    }, runtime_state={"scan_interval_sec": "60"})
    monkeypatch.setattr(ssrc, "process_cwd", lambda pid: str(tmp_path))
    r = shadow.load_runtime()
    assert r.status is Status.STALE
    assert r.pid_alive is True


def test_pid_exists_but_scan_stopped_long_ago_is_hung(tmp_path, monkeypatch):
    """Reproduces the exact prior incident: PID alive, nothing actually happening."""
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    old = datetime.now(timezone.utc) - timedelta(seconds=5000)
    _write_shadow_root(tmp_path, heartbeat={
        "pid": os.getpid(), "commit": "b1d9fa9", "timestamp": _iso(old),
        "last_successful_scan_utc": _iso(old),
    }, runtime_state={"scan_interval_sec": "60"})
    monkeypatch.setattr(ssrc, "process_cwd", lambda pid: str(tmp_path))
    r = shadow.load_runtime()
    assert r.status is Status.OFFLINE
    assert "HUNG" in r.status_reason


def test_dead_pid_is_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    now = datetime.now(timezone.utc)
    _write_shadow_root(tmp_path, heartbeat={
        "pid": 999999999, "commit": "b1d9fa9", "timestamp": _iso(now),
        "last_successful_scan_utc": _iso(now),
    })
    r = shadow.load_runtime()
    assert r.status is Status.OFFLINE
    assert r.pid_alive is False


def test_wrong_runtime_sha_is_reported_as_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    now = datetime.now(timezone.utc)
    _write_shadow_root(tmp_path, heartbeat={
        "pid": os.getpid(), "commit": "f2a68740abb85e376d2d818bef194abced695485",
        "timestamp": _iso(now), "last_successful_scan_utc": _iso(now),
    }, runtime_state={"scan_interval_sec": "60"})
    monkeypatch.setattr(ssrc, "process_cwd", lambda pid: str(tmp_path))
    r = shadow.load_runtime()
    assert r.sha_match == "NO"


def test_is_ancestor_against_the_real_shadow_repo(monkeypatch):
    """Not mocked: exercises the real git call against the actual shadow
    checkout, using the actual pre-hardening and boundary commits. This is
    the test that would have caught the inverted-argument bug directly.
    The autouse fixture clears SHADOW_ROOT for isolation; this test opts
    back into the real one deliberately, read-only (git merge-base only)."""
    import shutil
    import subprocess
    real_root, _ = ssrc.resolve_shadow_root()
    if real_root is None or shutil.which("git") is None:
        pytest.skip("real shadow checkout not available in this environment")
    pre_sha = "f2a68740abb85e376d2d818bef194abced695485"
    known = subprocess.run(["git", "cat-file", "-e", pre_sha], cwd=str(real_root),
                            capture_output=True).returncode == 0
    if not known:
        pytest.skip("pre-hardening commit not present in this checkout's history")
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", real_root)
    assert ssrc.is_ancestor(pre_sha, "b1d9fa9") == "YES"
    assert ssrc.is_ancestor("b1d9fa9", pre_sha) == "NO"


def test_short_and_full_sha_prefixes_match():
    assert shadow._sha_match("b1d9fa9", "b1d9fa95a70cd742d170e451f53df150b7c033f0")
    assert not shadow._sha_match("b1d9fa9", "f2a68740abb85e376d2d818bef194abced695485")


def test_production_cwd_mismatch_is_not_classified_as_shadow_healthy(tmp_path, monkeypatch):
    """Never trust process name alone - a PID matching app.main from a different
    checkout must not be read as this shadow engine being healthy."""
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    now = datetime.now(timezone.utc)
    _write_shadow_root(tmp_path, heartbeat={
        "pid": os.getpid(), "commit": "b1d9fa9", "timestamp": _iso(now),
        "last_successful_scan_utc": _iso(now),
    }, runtime_state={"scan_interval_sec": "60"})
    monkeypatch.setattr(ssrc, "process_cwd", lambda pid: "/some/other/checkout")
    r = shadow.load_runtime()
    assert r.cwd_matches_shadow is False
    assert r.status is Status.DEGRADED


# --- trade aggregation ----------------------------------------------------

_ROW_WIN = "commitA,BTCUSDT,LONG,momentum_breakout,2026-09-12T13:00:00+00:00,5.0,2.0,1.5,1.2,0.8,True,100,1,2,1.1,1.2,1.0,0.5,-0.1,TP1"
_ROW_LOSS = "commitA,ETHUSDT,SHORT,low_vol_reclaim_v2,2026-09-12T14:00:00+00:00,-3.0,1.5,-2.0,-2.3,-1.0,,,,,,,,,STOP_LOSS"
_PRE_SHA = "f2a68740abb85e376d2d818bef194abced695485"
_ROW_PRE = f"{_PRE_SHA},ICPUSDT,SHORT,low_vol_reclaim_v2,2026-09-12T13:00:00+00:00,48.0,3.6,-0.13,-0.07,-1.32,False,,,,,,,,STOP_LOSS"

#: Encodes the REAL git relationship (boundary b1d9fa9 vs. the actual
#: pre-hardening commit f2a68740) so a mock cannot mask the exact bug this
#: suite exists to catch: is_ancestor's argument order was once inverted and
#: silently reclassified pre-hardening trades as post-hardening.
_REAL_ANCESTRY = {
    (_PRE_SHA, "b1d9fa9"): "YES",   # the old commit is an ancestor of the boundary -> PRE
    ("b1d9fa9", _PRE_SHA): "NO",    # the boundary is NOT an ancestor of the old commit
}


def _real_is_ancestor(older: str, newer: str) -> str:
    return _REAL_ANCESTRY.get((older, newer), "UNKNOWN")


def test_closed_trade_aggregation_counts_and_pnl(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(ssrc, "is_ancestor", lambda older, newer: "UNKNOWN")
    _write_shadow_root(tmp_path, outcomes_rows=[_ROW_WIN, _ROW_LOSS])
    closed, _ = shadow.load_closed_trades()
    assert len(closed) == 2
    wins = [r for r in closed if r["is_win"]]
    losses = [r for r in closed if r["is_loss"]]
    assert len(wins) == 1 and len(losses) == 1
    assert wins[0]["symbol"] == "BTCUSDT"
    total_net = sum(r["net_pnl"] for r in closed)
    assert total_net == pytest.approx(1.2 + (-2.3))


def test_pre_hardening_trade_is_never_classified_post(tmp_path, monkeypatch):
    """Known pre-hardening trades (e.g. ICPUSDT SHORT low_vol_reclaim_v2) must
    stay marked as such, never counted as post-fix validation evidence. Uses
    the real ancestry relationship, not a mock tuned to whatever the code
    currently does - this is the test that caught the inverted-argument bug."""
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(ssrc, "is_ancestor", _real_is_ancestor)
    _write_shadow_root(tmp_path, outcomes_rows=[_ROW_PRE])
    closed, _ = shadow.load_closed_trades()
    assert closed[0]["hardening"] == "PRE_HARDENING"
    metrics = shadow.build_metrics_panel(closed)
    assert metrics["post_fix_trades_total"] == 0
    assert metrics["validation_status"] == "WAITING_FOR_FIRST_ORGANIC_POST_FIX_TRADE"


def test_unknown_ancestry_never_promoted_to_post_hardening(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(ssrc, "is_ancestor", lambda older, newer: "UNKNOWN")
    _write_shadow_root(tmp_path, outcomes_rows=[_ROW_WIN])
    closed, _ = shadow.load_closed_trades()
    assert closed[0]["hardening"] == "UNKNOWN"


def test_metrics_available_when_post_hardening_trade_has_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(shadow, "HARDENING_BOUNDARY_SHA", "commitA")
    monkeypatch.setattr(ssrc, "is_ancestor", lambda older, newer: "YES" if older == newer else "NO")
    _write_shadow_root(tmp_path, outcomes_rows=[_ROW_WIN])
    closed, _ = shadow.load_closed_trades()
    assert closed[0]["hardening"] == "POST_HARDENING"
    metrics = shadow.build_metrics_panel(closed)
    assert metrics["post_fix_trades_total"] == 1
    assert metrics["post_fix_trades_with_metrics"] == 1
    assert metrics["metrics_availability_rate"] == 1.0


def test_signal_to_fill_aggregation_percentiles(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    monkeypatch.setattr(ssrc, "is_ancestor", lambda older, newer: "UNKNOWN")
    _write_shadow_root(tmp_path, outcomes_rows=[_ROW_WIN, _ROW_LOSS])
    closed, _ = shadow.load_closed_trades()
    eq = shadow.build_execution_quality(closed)
    assert eq["all"]["n"] == 2
    assert eq["all"]["signal_to_fill"]["avg"] == pytest.approx((5.0 + -3.0) / 2)
    assert eq["all"]["favorable_fill_count"] == 1  # -3.0 <= 0
    assert eq["all"]["adverse_fill_count"] == 1    # 5.0 > 0
    assert eq["reference_label"] == "REFERENCE_ONLY_NOT_UNIVERSAL"


# --- tolerance ---------------------------------------------------------

def test_malformed_event_line_is_skipped_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "data_store").mkdir()
    (tmp_path / "data_store" / "forward_paper_outcomes.csv").write_text("git_commit\n")
    bad = "{not valid json"
    good = json.dumps({"trade_id": "t1", "event_type": "TRADE_OPENED", "sequence": 0,
                        "timestamp": "2026-09-12T12:00:00+00:00",
                        "payload": {"symbol": "BTCUSDT", "direction": "LONG"}})
    (tmp_path / "data_store" / "forward_paper_events.jsonl").write_text(bad + "\n" + good + "\n")
    open_trades, prov = shadow.load_open_trades()
    assert len(open_trades) == 1
    assert open_trades[0]["symbol"] == "BTCUSDT"
    assert "malformed" in (prov.note or "")


def test_missing_outcomes_file_returns_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "data_store").mkdir()
    closed, prov = shadow.load_closed_trades()
    assert closed == []
    assert prov.exists is False


def test_missing_events_file_returns_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ssrc, "SHADOW_ROOT", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "data_store").mkdir()
    open_trades, prov = shadow.load_open_trades()
    assert open_trades == []


# --- safety rollups ------------------------------------------------------

def _fake_runtime(status: Status) -> shadow.ShadowRuntime:
    r = shadow.ShadowRuntime(root_available=True, root_error="")
    r.status = status
    r.sha_match = "YES"
    return r


def test_real_exposure_forces_red(monkeypatch):
    runtime = _fake_runtime(Status.HEALTHY)
    prod_safety = {"production_engine_running": True, "live_stop_present": True,
                   "supervisor_stop_present": True, "production_sha_match": True}
    real_safety = {"source": "LIVE"}
    metrics = {"validation_status": "x"}
    assert shadow.overall_status(runtime, prod_safety, real_safety, metrics) is Status.OFFLINE


def test_missing_stop_flag_forces_red(monkeypatch):
    runtime = _fake_runtime(Status.HEALTHY)
    prod_safety = {"production_engine_running": False, "live_stop_present": False,
                   "supervisor_stop_present": True, "production_sha_match": True}
    real_safety = {"source": "LIVE"}
    metrics = {"validation_status": "x"}
    assert shadow.overall_status(runtime, prod_safety, real_safety, metrics) is Status.OFFLINE


def test_local_only_exchange_state_is_amber_not_red(monkeypatch):
    runtime = _fake_runtime(Status.HEALTHY)
    prod_safety = {"production_engine_running": False, "live_stop_present": True,
                   "supervisor_stop_present": True, "production_sha_match": True}
    real_safety = {"source": "LOCAL_ONLY"}
    metrics = {"validation_status": "METRICS_OBSERVED_ON_POST_FIX_TRADES"}
    assert shadow.overall_status(runtime, prod_safety, real_safety, metrics) is Status.DEGRADED


def test_waiting_for_first_post_fix_trade_is_amber_not_red(monkeypatch):
    runtime = _fake_runtime(Status.HEALTHY)
    prod_safety = {"production_engine_running": False, "live_stop_present": True,
                   "supervisor_stop_present": True, "production_sha_match": True}
    real_safety = {"source": "LIVE"}
    metrics = {"validation_status": "WAITING_FOR_FIRST_ORGANIC_POST_FIX_TRADE"}
    assert shadow.overall_status(runtime, prod_safety, real_safety, metrics) is Status.DEGRADED


def test_everything_healthy_is_green(monkeypatch):
    runtime = _fake_runtime(Status.HEALTHY)
    prod_safety = {"production_engine_running": False, "live_stop_present": True,
                   "supervisor_stop_present": True, "production_sha_match": True}
    real_safety = {"source": "LIVE"}
    metrics = {"validation_status": "METRICS_OBSERVED_ON_POST_FIX_TRADES"}
    assert shadow.overall_status(runtime, prod_safety, real_safety, metrics) is Status.HEALTHY


def test_hung_shadow_forces_red(monkeypatch):
    runtime = _fake_runtime(Status.OFFLINE)
    prod_safety = {"production_engine_running": False, "live_stop_present": True,
                   "supervisor_stop_present": True, "production_sha_match": True}
    real_safety = {"source": "LIVE"}
    metrics = {"validation_status": "x"}
    assert shadow.overall_status(runtime, prod_safety, real_safety, metrics) is Status.OFFLINE


def test_wrong_shadow_sha_forces_red(monkeypatch):
    runtime = _fake_runtime(Status.HEALTHY)
    runtime.sha_match = "NO"
    prod_safety = {"production_engine_running": False, "live_stop_present": True,
                   "supervisor_stop_present": True, "production_sha_match": True}
    real_safety = {"source": "LIVE"}
    metrics = {"validation_status": "x"}
    assert shadow.overall_status(runtime, prod_safety, real_safety, metrics) is Status.OFFLINE
