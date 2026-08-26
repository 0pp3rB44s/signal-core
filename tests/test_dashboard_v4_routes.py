"""Every v4 page must render, including with no data at all."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from dashboard_v3.core import sources
from dashboard_v3.panels import adaptive_trend as at

ROUTES = ["/", "/adaptive-trend", "/signals", "/operations", "/funnel", "/strategy",
          "/positions", "/performance", "/risk", "/collectors", "/logs", "/project",
          "/incidents", "/health", "/api/status"]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-only")
    from dashboard_v3.app import app
    from dashboard_v3.core import assembly
    assembly.invalidate()
    app.config["TESTING"] = True
    c = app.test_client()
    with c.session_transaction() as s:
        s["authenticated"] = True
    return c


def test_all_routes_render(client):
    for route in ROUTES:
        r = client.get(route)
        assert r.status_code == 200, f"{route} -> {r.status_code}"


def test_routes_require_authentication(monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-only")
    from dashboard_v3.app import app
    app.config["TESTING"] = True
    anon = app.test_client()
    for route in ["/adaptive-trend", "/signals", "/performance"]:
        assert anon.get(route).status_code in (301, 302)


def test_adaptive_trend_page_has_no_control_surface(client):
    """The new pages must not introduce a mutation endpoint."""
    body = client.get("/adaptive-trend").data.decode().lower()
    assert "<form" not in body or "method=\"post\"" not in body
    assert client.post("/adaptive-trend").status_code in (405, 302)


def test_signals_filters_are_get_only(client):
    body = client.get("/signals").data.decode().lower()
    assert 'method="get"' in body
    assert client.post("/signals").status_code in (405, 302)


def test_signals_window_filter_narrows_rows(client, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    now = datetime.now(timezone.utc)
    p = tmp_path / at.SHADOW_LOG
    p.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"timestamp": (now - timedelta(hours=1)).isoformat(), "symbol": "BTCUSDT",
         "mom": 0.05, "decision": "ACCOUNT_FREEZE_BLOCKED"},
        {"timestamp": (now - timedelta(days=30)).isoformat(), "symbol": "ETHUSDT",
         "mom": 0.04, "decision": "ACCOUNT_FREEZE_BLOCKED"},
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    def body_rows(html: str) -> str:
        """Only the table body — the filter dropdowns legitimately list every
        symbol, so asserting against the whole page would test nothing."""
        return html.split("<tbody>")[-1].split("</tbody>")[0] if "<tbody>" in html else ""

    recent = body_rows(client.get("/signals?window=24h").data.decode())
    every = body_rows(client.get("/signals?window=all").data.decode())
    assert "BTCUSDT" in recent
    assert "ETHUSDT" not in recent, "30-day-old row must not appear in a 24h window"
    assert "ETHUSDT" in every


def test_performance_page_labels_the_retired_strategy(client):
    body = client.get("/performance").data.decode()
    assert "LEGACY" in body or "RETIRED" in body
    assert "adaptive" in body.lower()


def test_empty_state_renders_rather_than_erroring(client, tmp_path, monkeypatch):
    """No data anywhere must produce a clean page, not a traceback."""
    monkeypatch.setattr(sources, "BASE_PATH", tmp_path)
    from dashboard_v3.core import assembly
    assembly.invalidate()
    for route in ["/adaptive-trend", "/signals", "/performance"]:
        r = client.get(route)
        assert r.status_code == 200
        assert b"Traceback" not in r.data


def test_navigation_exposes_adaptive_trend_first(client):
    body = client.get("/").data.decode()
    assert "/adaptive-trend" in body
    assert body.index("/adaptive-trend") < body.index("/funnel")
