"""Current-architecture Dashboard V3 adapters and read-only boundary."""

from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from dashboard_v3.core import sources
from dashboard_v3.core.status import Status
from dashboard_v3.panels import operations

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    monkeypatch.setattr(operations.src, "BASE_PATH", tmp_path)
    monkeypatch.setattr(operations, "Settings", lambda: SimpleNamespace(
        hard_daily_stop_pct=2.0, weekly_freeze_loss_pct=7.0,
        microflow_scalper_enabled=True, account_risk_per_trade_pct=0.75,
        microflow_symbol_set=("BTCUSDT", "ETHUSDT"),
    ))
    return tmp_path


def _write(root: Path, rel: str, value) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")
    return path


def test_runtime_missing_is_unknown_not_zero(isolated):
    from dashboard_v3.panels import runtime
    result = runtime.build()
    assert result["engine"]["pid"] is None
    assert result["signals"].by_key("engine").status is Status.UNKNOWN


def test_risk_manager_state_and_breaker_red_are_displayed(isolated):
    _write(isolated, "state/portfolio_equity_guard.json", {
        "high_water_equity": 100.0, "last_equity": 97.5,
        "drawdown_pct": 2.5, "hard_daily_stop_pct": 2.0,
    })
    _write(isolated, "data_store/trades/daily_learning_report.json", {
        "daily_total_net_pnl": -1.25, "consecutive_losses": 3,
    })
    _write(isolated, "logs/live.out",
           "2026-08-20T08:00:00+00:00 | RISK_EVALUATION | decision=RISK_REJECTED | "
           "symbol=BTCUSDT | session_multiplier=0.5 | reasons=daily_loss_pct=2.5 hard_daily_stop_pct=2.0\n")
    result = operations.build_risk()
    assert result["breaker"] == "BLOCKED"
    assert result["breaker_status"] is Status.BLOCKED
    assert result["daily_realized_loss"] == -1.25
    assert result["consecutive_losses"] == 3
    assert result["last_decision"] == "RISK_REJECTED"


def test_risk_rejected_parser_exposes_reason_value_limit(isolated):
    _write(isolated, "logs/live.out",
           "2026-08-20T08:00:00+00:00 | RISK_EVALUATION | decision=RISK_REJECTED | "
           "symbol=SOLUSDT | reasons=drawdown_pct=2.5 threshold=2.0\n")
    result = operations.build_risk()
    assert result["rejections"][0]["symbol"] == "SOLUSDT"
    assert result["rejections"][0]["value"] == "2.5"
    assert result["rejections"][0]["limit"] == "2.0"


@pytest.mark.parametrize("name,rel,payload", [
    ("Binance futures", "data_store/research_binance/status.json",
     {"rows": {"book": 2, "trade": 3, "mark": 4, "oi": 1, "depth": 5, "liq": 0},
      "streams": {"book": {"rows": 2, "last_event_age_s": 1, "status": "HEALTHY"}},
      "write_timestamp_ms": 1_900_000_000_000, "research_only": True, "orders_allowed": False}),
    ("Binance spot", "data_store/research_binance_spot/status.json",
     {"rows": {"book": 2, "trade": 3}, "book_stream_health": "HEALTHY",
      "trade_stream_health": "HEALTHY", "write_timestamp_ms": 1_900_000_000_000}),
    ("Bitget research", "data_store/research_liq_oi/status.json",
     {"liq_rows": 2, "oi_rows": 3, "updated_ms": 1_900_000_000_000,
      "research_only": True, "orders_allowed": False}),
])
def test_collector_status_parsing(isolated, name, rel, payload):
    _write(isolated, rel, payload)
    result = operations._collector(name, rel, rel.replace("status.json", "collector.pid"))
    assert result["name"] == name
    assert result["last_update"] is not None
    assert result["payload"]


def test_stale_collector_is_offline(isolated):
    path = _write(isolated, "data_store/research_binance/status.json", {"rows": {"book": 1}})
    old = time.time() - 1000
    os.utime(path, (old, old))
    result = operations._collector("Binance", "data_store/research_binance/status.json",
                                   "data_store/research_binance/collector.pid")
    assert result["status"] is Status.OFFLINE


def test_obsidian_current_state_read_only(isolated, tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    _write(vault, "00_PROJECT_STATE/CURRENT_PRODUCTION.md", "# Production\nSHA c984fd8")
    _write(vault, "00_PROJECT_STATE/CURRENT_TASK.md", "# Task\nMicroFlow dashboard")
    _write(vault, "00_PROJECT_STATE/NEXT_ACTIONS.md", "# Next\n- [ ] P1 deploy")
    _write(vault, "00_PROJECT_STATE/CURRENT_STATE.md", "# State\nMicroFlow = live baseline")
    (vault / "04_TRADES").mkdir(); _write(vault, "04_TRADES/review.md", "# Review")
    (vault / "05_DECISIONS").mkdir(); _write(vault, "05_DECISIONS/decision.md", "# Decision")
    monkeypatch.setenv("DASHBOARD_OBSIDIAN_VAULT", str(vault))
    result = operations.build_project()
    assert result["root_available"] is True
    assert result["open_p1"] == 1
    assert "MicroFlow" in result["strategy_disposition"]


@pytest.mark.parametrize("secret", [
    "api_key=supersecret", "password: hunter2", "Authorization=Bearer.ABC",
    "cookie=session-value", "Bearer abc.def.ghi",
])
def test_log_redaction(secret):
    rendered = operations.redact(f"ERROR request {secret}")
    assert "supersecret" not in rendered
    assert "hunter2" not in rendered
    assert "session-value" not in rendered
    assert "abc.def.ghi" not in rendered
    assert "REDACTED" in rendered


def test_log_panel_never_exposes_secret(isolated):
    _write(isolated, "logs/live.out", "CRITICAL api_secret=do-not-render token=also-hidden\n")
    result = operations.build_logs()
    blob = json.dumps(result["rows"], default=str)
    assert "do-not-render" not in blob and "also-hidden" not in blob


def test_sha_mismatch_is_blocked(isolated, monkeypatch):
    _write(isolated, "state/live_runtime.state", "commit=runtime-sha\n")
    _write(isolated, "state/deployed_commit.txt", "marker-sha\n")
    monkeypatch.setattr(operations.src, "repo_head", lambda: "runner-sha")
    monkeypatch.setattr(operations.src, "git_ref", lambda _ref: "production-sha")
    result = operations.build_deployment()
    assert result["sha"]["match"] == "MISMATCH"
    assert result["sha"]["status"] is Status.BLOCKED


def test_unresolved_intents_are_not_reported_as_clean(isolated):
    _write(isolated, "state/order_intents.json", [{"symbol": "BTCUSDT", "state": "UNKNOWN"}])
    result = operations.build_reconciliation()
    assert result["unresolved_count"] == 1
    assert result["status"] is Status.BLOCKED


def test_dashboard_has_no_execution_or_mutating_client_imports():
    forbidden_modules = {"execution", "risk.risk_manager"}
    offenders = []
    for path in (REPO / "dashboard_v3").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden_modules):
                    offenders.append(f"{path.name}:{name}")
    assert offenders == []


def test_dashboard_source_has_no_order_mutation_path():
    forbidden = ("place_order(", "cancel_order(", "close_position(", "submit_order(",
                 "set_leverage(", "execution_service")
    offenders = []
    for path in (REPO / "dashboard_v3").rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        offenders.extend(f"{path.name}:{token}" for token in forbidden if token in text)
    assert offenders == []


def test_all_authenticated_pages_start_and_render(monkeypatch):
    import importlib
    import sys
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("EXECUTION_ENABLED", "false")
    monkeypatch.setenv("FORWARD_PAPER_ONLY", "true")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    sys.modules.pop("dashboard_v3.app", None)
    module = importlib.import_module("dashboard_v3.app")
    monkeypatch.setattr(module.assembly, "build_all", lambda: {
        "overall": Status.UNKNOWN, "permission": {"status": Status.UNKNOWN, "reasons": []},
        "generated_at": None, "panels": {
            "runtime": {}, "exchange": {}, "funnel": {}, "scores": {}, "expectancy": {},
            "health": {}, "incidents": {}, "history": {}, "strategy": {}, "operations": {},
        },
    })
    # Route wiring and authentication gate are the startup contract; detailed
    # templates are exercised against real builders in the local page smoke test.
    client = module.app.test_client()
    assert client.get("/login").status_code == 200
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    assert client.get("/api/status").status_code in {200, 500}
    expected = {"/", "/operations", "/funnel", "/positions", "/performance", "/strategy",
                "/risk", "/collectors", "/logs", "/project", "/incidents", "/health"}
    assert expected.issubset({str(rule) for rule in module.app.url_map.iter_rules()})
