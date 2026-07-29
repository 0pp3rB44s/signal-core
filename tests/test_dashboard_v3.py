"""Dashboard v3: safety, resilience and honesty.

The load-bearing tests here are the ones that assert the dashboard cannot act on
the account, and that missing or corrupt data is never rendered as health.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from dashboard_v3.core import assembly
from dashboard_v3.core.status import Signal, SignalSet, Status, freshness, worst

REPO = Path(__file__).resolve().parents[1]


# --- status ladder -------------------------------------------------------

def test_unknown_outranks_healthy_in_aggregation():
    assert worst(Status.HEALTHY, Status.UNKNOWN) is Status.UNKNOWN
    assert worst(Status.HEALTHY, Status.STALE) is Status.STALE
    assert worst(Status.BLOCKED, Status.OFFLINE) is Status.OFFLINE
    assert worst() is Status.UNKNOWN


def test_empty_signal_set_is_unknown_not_healthy():
    assert SignalSet().status is Status.UNKNOWN


def test_missing_age_is_unknown_never_healthy():
    assert freshness(None, stale_after=600) is Status.UNKNOWN
    assert freshness(10, stale_after=600) is Status.HEALTHY
    assert freshness(700, stale_after=600) is Status.STALE
    assert freshness(99999, stale_after=600, offline_after=3600) is Status.OFFLINE


def test_every_status_has_a_non_colour_glyph():
    from dashboard_v3.core.status import GLYPH, TONE
    for status in Status:
        assert GLYPH[status] and TONE[status]


# --- source resilience ---------------------------------------------------

def test_missing_file_reports_unknown_and_does_not_raise(tmp_path, monkeypatch):
    from dashboard_v3.core import sources
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    loaded = sources.load_json("state/nope.json", default={})
    assert loaded.value == {}
    assert loaded.provenance.exists is False
    assert loaded.status is Status.UNKNOWN


def test_corrupt_json_is_degraded_not_a_crash(tmp_path, monkeypatch):
    from dashboard_v3.core import sources
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "x.json").write_text("{ truncated", encoding="utf-8")
    loaded = sources.load_json("state/x.json", default={})
    assert loaded.provenance.parsed is False
    assert "corrupt" in loaded.provenance.error.lower()
    assert loaded.status is Status.DEGRADED


def test_empty_file_is_not_treated_as_valid(tmp_path, monkeypatch):
    from dashboard_v3.core import sources
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "e.json").write_text("", encoding="utf-8")
    assert sources.load_json("state/e.json", default={}).provenance.parsed is False


def test_jsonl_tail_skips_bad_lines_and_reports_them(tmp_path, monkeypatch):
    from dashboard_v3.core import sources
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    (tmp_path / "d").mkdir()
    p = tmp_path / "d" / "e.jsonl"
    p.write_text('{"a":1}\nNOT JSON\n{"a":2}\n', encoding="utf-8")
    loaded = sources.load_jsonl_tail("d/e.jsonl")
    assert len(loaded.value) == 2
    assert "unparseable" in loaded.provenance.note


# --- panel isolation -----------------------------------------------------

def test_a_failing_panel_does_not_break_the_page():
    assembly.invalidate()

    def boom():
        raise RuntimeError("panel exploded")

    result = assembly.cached("explode_test", boom, ttl=0)
    assert "panel_error" in result
    assert result["status"] is Status.UNKNOWN
    assert result["signals"].status is Status.UNKNOWN


def test_cache_prevents_repeated_expensive_builds():
    assembly.invalidate()
    calls = []

    def build():
        calls.append(1)
        return {"status": Status.HEALTHY}

    assembly.cached("cache_test", build, ttl=60)
    assembly.cached("cache_test", build, ttl=60)
    assert len(calls) == 1


# --- heartbeat honesty ---------------------------------------------------

def _runtime_with_heartbeat(tmp_path, monkeypatch, payload, age_seconds):
    import os
    import time
    from dashboard_v3.core import sources
    from dashboard_v3.panels import runtime

    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    monkeypatch.setattr(runtime.src, "BASE_PATH", tmp_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    hb = tmp_path / "state" / "runtime_heartbeat.json"
    if payload is not None:
        hb.write_text(json.dumps(payload), encoding="utf-8")
        old = time.time() - age_seconds
        os.utime(hb, (old, old))
    return runtime.build()


def test_stale_heartbeat_renders_as_stale(tmp_path, monkeypatch):
    data = _runtime_with_heartbeat(
        tmp_path, monkeypatch, {"stage": "scan_cycle_complete", "schema_version": 2}, 1200)
    hb = data["signals"].by_key("heartbeat")
    assert hb.status is Status.STALE


def test_missing_heartbeat_is_unknown_never_healthy(tmp_path, monkeypatch):
    data = _runtime_with_heartbeat(tmp_path, monkeypatch, None, 0)
    hb = data["signals"].by_key("heartbeat")
    assert hb.status is Status.UNKNOWN
    assert hb.status is not Status.HEALTHY


def test_very_old_heartbeat_is_offline(tmp_path, monkeypatch):
    data = _runtime_with_heartbeat(
        tmp_path, monkeypatch, {"stage": "process_started", "schema_version": 2}, 99999)
    assert data["signals"].by_key("heartbeat").status is Status.OFFLINE


def test_schema_v1_heartbeat_is_flagged_as_degraded_telemetry(tmp_path, monkeypatch):
    data = _runtime_with_heartbeat(
        tmp_path, monkeypatch, {"stage": "process_started", "schema_version": 1}, 10)
    assert data["heartbeat_capable"] is False
    assert data["signals"].by_key("heartbeat_schema").status is Status.DEGRADED


# --- exchange: unreachable, empty, populated -----------------------------

class _FakeClient:
    def __init__(self, positions=None, fail=False):
        self._positions = positions or []
        self._fail = fail

    def get_accounts(self, product_type=None):
        if self._fail:
            raise ConnectionError("network down")
        return {"data": [{"accountEquity": "53.57", "available": "53.57",
                          "marginCoin": "USDT", "unrealizedPL": "0"}]}

    def get_all_positions(self, product_type=None):
        return {"data": self._positions}

    def get_tpsl_orders(self, product_type=None):
        return {"data": []}

    def get_pending_orders(self, product_type=None):
        return {"data": {"entrustedList": None}}

    @staticmethod
    def _order_rows(_payload):
        return []


def _exchange(positions=None, fail=False):
    from dashboard_v3.panels import exchange
    return exchange.build(client_factory=lambda: (_FakeClient(positions, fail), "USDT-FUTURES"))


def test_unreachable_exchange_is_unknown_and_shows_no_positions():
    data = _exchange(fail=True)
    assert data["reachable"] is False
    assert data["status"] is Status.UNKNOWN
    assert data["positions"] == []
    assert data["signals"].by_key("exchange").status is Status.UNKNOWN


def test_no_open_positions_is_healthy_and_flat():
    data = _exchange(positions=[])
    assert data["reachable"] is True
    assert data["position_count"] == 0
    assert data["signals"].by_key("protection").status is Status.HEALTHY


def test_unprotected_position_is_blocked():
    data = _exchange(positions=[{
        "symbol": "SOLUSDT", "total": "6.5", "holdSide": "long",
        "openPriceAvg": "74.8", "markPrice": "73.0", "leverage": "10",
        "unrealizedPL": "-11.09", "stopLoss": "", "takeProfit": "",
    }])
    assert data["position_count"] == 1
    assert data["unprotected_count"] == 1
    assert data["positions"][0]["protection"] == "UNPROTECTED"
    assert data["signals"].by_key("protection").status is Status.BLOCKED


def test_protected_position_is_healthy_and_never_guesses_strategy():
    data = _exchange(positions=[{
        "symbol": "BTCUSDT", "total": "0.5", "holdSide": "long",
        "openPriceAvg": "63000", "markPrice": "63500", "leverage": "3",
        "stopLoss": "62000", "takeProfit": "65000",
    }])
    pos = data["positions"][0]
    assert pos["protection"] == "PROTECTED"
    # Strategy/score are not on the exchange payload -> must stay UNKNOWN.
    assert pos["strategy"] == "UNKNOWN"
    assert pos["score"] == "UNKNOWN"


# --- funnel --------------------------------------------------------------

def test_funnel_names_the_decisive_gate(tmp_path, monkeypatch):
    from dashboard_v3.core import sources
    from dashboard_v3.panels import funnel
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    monkeypatch.setattr(funnel.src, "BASE_PATH", tmp_path)
    (tmp_path / "data_store").mkdir()
    events = []
    for i in range(5):
        events.append({"event_type": "DETECTOR_ATTEMPT", "pass_fail": "PASS",
                       "event_timestamp_utc": "2026-07-29T05:00:00+00:00"})
        events.append({"event_type": "RISK_DECISION", "pass_fail": "FAIL",
                       "primary_reason_code": "RISK_BLOCKED",
                       "secondary_reason_codes": ["SYMBOL_EXPECTANCY_PAUSE", "EXPECTANCY_BLOCK"],
                       "event_timestamp_utc": "2026-07-29T05:00:00+00:00"})
    (tmp_path / "data_store" / "funnel_events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events), encoding="utf-8")

    data = funnel.build()
    assert data["decisive"] is not None
    assert data["decisive"]["key"] == "RISK_DECISION"
    assert data["status"] is Status.BLOCKED
    assert data["blockers"][0]["code"] in {"SYMBOL_EXPECTANCY_PAUSE", "EXPECTANCY_BLOCK"}


def test_empty_funnel_is_unknown_not_healthy(tmp_path, monkeypatch):
    from dashboard_v3.core import sources
    from dashboard_v3.panels import funnel
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    monkeypatch.setattr(funnel.src, "BASE_PATH", tmp_path)
    data = funnel.build()
    assert data["status"] is Status.UNKNOWN


# --- history / cohorts ---------------------------------------------------

def test_empty_trade_history_does_not_crash(tmp_path, monkeypatch):
    from dashboard_v3.core import sources
    from dashboard_v3.panels import history
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    monkeypatch.setattr(history.src, "BASE_PATH", tmp_path)
    data = history.build()
    assert data["trades"] == []
    assert data["live_stats"]["trades"] == 0
    assert data["live_stats"]["expectancy"] is None


def test_recovery_cohort_is_separated_from_live(tmp_path, monkeypatch):
    import csv
    from datetime import datetime, timezone
    from dashboard_v3.core import sources
    from dashboard_v3.panels import history
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    monkeypatch.setattr(history.src, "BASE_PATH", tmp_path)
    (tmp_path / "logs").mkdir()
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"status": "CLOSED", "symbol": "BTCUSDT", "strategy": "low_vol_reclaim",
         "net_pnl": "-0.5", "closed_at": now, "data_confidence": "EXCHANGE_TRUTH", "tp1_hit": "false"},
        {"status": "CLOSED", "symbol": "SOLUSDT", "strategy": "recovered_exchange_position",
         "net_pnl": "1.0", "closed_at": now, "data_confidence": "LOW_CONFIDENCE", "tp1_hit": ""},
    ]
    with (tmp_path / "logs" / "trade_dataset_v2.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    data = history.build()
    assert data["live_stats"]["trades"] == 1
    assert data["recovery_stats"]["trades"] == 1
    # The profitable recovery trade must NOT inflate the live cohort.
    assert data["live_stats"]["total"] == pytest.approx(-0.5)
    assert data["signals"].by_key("cohort") is not None


# --- the safety guarantees ----------------------------------------------

MUTATING = [
    "place_futures_market_order", "place_futures_limit_order", "place_futures_order",
    "close_futures_position", "close_futures_position_full", "cancel_futures_order",
    "cancel_futures_plan_order", "cancel_all_futures_tpsl_orders", "place_position_tpsl",
    "place_futures_protection_orders", "move_futures_stop_loss", "set_futures_leverage",
    "emergency_flatten_all", "submit_entry", "start_bot", "stop_bot",
]


def test_dashboard_never_references_an_order_mutating_method():
    offenders = []
    for path in (REPO / "dashboard_v3").rglob("*.py"):
        text = path.read_text(errors="ignore")
        for name in MUTATING:
            if name in text and "MUTATING" not in text.split(name)[0][-80:]:
                offenders.append(f"{path.relative_to(REPO)}::{name}")
    assert offenders == [], f"dashboard must be read-only, found: {offenders}"


def test_dashboard_does_not_import_execution_services():
    banned = ("ExecutionService", "EntryOrderSubmitter", "PositionManager",
              "entry_submitter", "execution_service", "position_manager")
    offenders = []
    for path in (REPO / "dashboard_v3").rglob("*.py"):
        text = path.read_text(errors="ignore")
        for name in banned:
            if re.search(rf"(import|from)\s+.*{name}", text):
                offenders.append(f"{path.relative_to(REPO)}::{name}")
    assert offenders == []


def test_dashboard_exposes_no_control_endpoints():
    app_src = (REPO / "dashboard_v3" / "app.py").read_text()
    for forbidden in ("/api/bot/start", "/api/bot/stop", "bot_control"):
        assert forbidden not in app_src, f"control surface present: {forbidden}"
    # Every route must be GET-only except the login form.
    routes = re.findall(r'@app\.route\("([^"]+)"(?:,\s*methods=\[([^\]]+)\])?\)', app_src)
    for path, methods in routes:
        if path == "/login":
            continue
        assert "POST" not in (methods or ""), f"{path} accepts POST"


def test_only_readonly_client_methods_are_allowlisted():
    from dashboard_v3.panels.exchange import ALLOWED_CLIENT_METHODS
    for name in ALLOWED_CLIENT_METHODS:
        assert name.startswith("get_"), f"non-GET method allow-listed: {name}"


def test_no_secret_bearing_settings_are_exposed(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    import importlib
    import dashboard_v3.app as mod
    importlib.reload(mod)
    for key in mod.SAFE_SETTINGS:
        assert not any(tok in key.lower() for tok in
                       ("key", "secret", "password", "token", "passphrase", "webhook"))


def test_templates_never_render_a_credential_field():
    banned = ("api_key", "api_secret", "passphrase", "dashboard_password",
              "secret_key", "TELEGRAM_BOT_TOKEN", "DISCORD_WEBHOOK_URL")
    offenders = []
    for path in (REPO / "dashboard_v3" / "templates").rglob("*.html"):
        text = path.read_text(errors="ignore")
        for name in banned:
            if name in text:
                offenders.append(f"{path.name}::{name}")
    assert offenders == []


# --- app wiring ----------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    monkeypatch.setenv("DASHBOARD_SECRET_KEY", "test-secret")
    import importlib
    import dashboard_v3.app as mod
    importlib.reload(mod)
    mod.app.config["TESTING"] = True
    return mod.app.test_client()


def test_all_pages_require_authentication(client):
    for path in ("/", "/funnel", "/positions", "/performance", "/risk",
                 "/incidents", "/health", "/api/status"):
        resp = client.get(path)
        assert resp.status_code == 302, path
        assert "/login" in resp.headers["Location"]


def test_login_rejects_a_wrong_password(client):
    resp = client.post("/login", data={"password": "nope"})
    assert resp.status_code == 200
    assert b"Onjuist" in resp.data


def test_pages_render_after_login(client):
    client.post("/login", data={"password": "test-password"})
    for path in ("/", "/funnel", "/positions", "/performance", "/risk",
                 "/incidents", "/health"):
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        assert b"TERMINAL" in resp.data


def test_login_does_not_allow_open_redirect(client):
    resp = client.post("/login?next=https://evil.example/x",
                       data={"password": "test-password"})
    assert "evil.example" not in resp.headers.get("Location", "")


def test_api_status_is_json_and_has_no_secrets(client):
    client.post("/login", data={"password": "test-password"})
    resp = client.get("/api/status")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "overall" in body
    for token in ("secret", "password", "passphrase", "api_key"):
        assert token not in body.lower()


def test_responsive_breakpoints_exist():
    css = (REPO / "dashboard_v3" / "static" / "terminal.css").read_text()
    for width in ("900px", "560px"):
        assert f"max-width: {width}" in css
    assert "prefers-reduced-motion" in css


# --- macOS ps parsing (regression: etimes is Linux-only) -----------------

@pytest.mark.parametrize(("text", "expected"), [
    ("10:23:45", 37425),
    ("1-02:03:04", 93784),
    ("05:09", 309),
    ("00:00", 0),
    ("bad", None),
    ("", None),
])
def test_parse_etime_handles_bsd_format(text, expected):
    from dashboard_v3.core.sources import parse_etime
    assert parse_etime(text) == expected


def test_process_info_uses_etime_not_etimes():
    """macOS ps has no `etimes`; requesting it fails the whole -o spec and
    silently produced 'uptime UNKNOWN'."""
    source = (REPO / "dashboard_v3" / "core" / "sources.py").read_text()
    assert "etimes=" not in source
    assert "etime=" in source


def test_signal_hint_is_block_level():
    css = (REPO / "dashboard_v3" / "static" / "terminal.css").read_text()
    hint = css.split(".signal .hint")[1].split("}")[0]
    assert "display: block" in hint


# --- promotion guards: v2 retired, v3 is production ----------------------

def test_dashboard_v2_control_endpoints_are_removed():
    src = (REPO / "dashboard_v2" / "app.py").read_text()
    for route in ("/api/bot/start", '"/api/bot/stop"'):
        assert f'@app.route("{route}"' not in src
    assert "bot_control.start_bot" not in src
    assert "bot_control.stop_bot" not in src


def test_dashboard_v2_bot_control_cannot_spawn_a_process():
    """The retired module must not shell out to any launcher script."""
    src = (REPO / "dashboard_v2" / "bot_control.py").read_text()
    for script in ("start_bot.sh", "stop_all.sh", "launch_live.sh"):
        # Only allowed inside the explanatory message / docstring, never as an
        # argument list handed to subprocess.
        assert f'["{script}' not in src and f"['{script}" not in src
    assert "pgrep" in src, "read-only status check should remain"


def test_retired_control_functions_never_report_success():
    import dashboard_v2.bot_control as bc
    for fn in (bc.start_bot, bc.stop_bot):
        result = fn("anything")
        assert result["ok"] is False
        assert result["pid"] is None


def test_production_launcher_starts_v3():
    launcher = (REPO / "scripts" / "start_dashboard.sh").read_text()
    assert "-m dashboard_v3.app" in launcher
    assert "nohup python3 -u -m dashboard_v2.app" not in launcher


def test_stop_all_terminates_both_dashboards():
    stopper = (REPO / "scripts" / "stop_all.sh").read_text()
    assert "dashboard_v3.app" in stopper
    assert "dashboard_v2.app" in stopper
