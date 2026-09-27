from __future__ import annotations

import importlib
import sys

import pytest


def _import_dashboard(monkeypatch, *, password: str | None):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    if password is None:
        monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("DASHBOARD_PASSWORD", password)

    sys.modules.pop("dashboard_v2.app", None)
    return importlib.import_module("dashboard_v2.app")


def test_dashboard_fails_closed_without_password(monkeypatch):
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD is required"):
        _import_dashboard(monkeypatch, password=None)


def test_dashboard_uses_configured_password_without_logging_it(monkeypatch, caplog, capsys):
    configured_password = "configured-test-password"

    module = _import_dashboard(monkeypatch, password=configured_password)

    assert module.DASHBOARD_PASSWORD == configured_password
    assert configured_password not in caplog.text
    assert configured_password not in capsys.readouterr().out


# --- the dashboard is a read-only control plane -----------------------------
#
# app/dashboard.py exposed POST /api/control/{start_bot,stop_all,restart_bot,
# execution_off,execution_on_dryrun} and rewrote .env in place. start_bot()
# shelled out to scripts/start_bot.sh, which bypasses all four authorisation
# layers in scripts/launch_live.sh: open critical risks, the owner-signed
# LIVE_PILOT_AUTHORISATION token, the .env.live invariants, and the typed
# confirmation. One HTTP POST could begin real-money trading, and stop_all
# could kill an engine holding an open position.
#
# It was removed. These tests exist so the surface cannot come back quietly.

from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]


def test_legacy_control_dashboard_stays_removed():
    assert not (_REPO / "app" / "dashboard.py").exists(), (
        "app/dashboard.py is back. It could rewrite .env and start the engine "
        "outside launch_live.sh's authorisation layers."
    )


def _v3_app(monkeypatch):
    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    sys.modules.pop("dashboard_v3.app", None)
    return importlib.import_module("dashboard_v3.app").app


def test_v3_exposes_no_process_or_config_control_routes(monkeypatch):
    """Only /login may accept a POST, and only to authenticate."""
    app = _v3_app(monkeypatch)
    posting = {
        str(rule): sorted(rule.methods - {"HEAD", "OPTIONS"})
        for rule in app.url_map.iter_rules()
        if rule.methods & {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert set(posting) == {"/login"}, f"unexpected mutating routes: {posting}"


def test_v3_has_no_control_namespace(monkeypatch):
    app = _v3_app(monkeypatch)
    control = [str(r) for r in app.url_map.iter_rules() if "/control" in str(r)]
    assert control == [], f"control endpoints reappeared: {control}"


def test_v3_never_writes_env_or_launches_the_engine():
    """Read-only means no writable open(), no launcher, no .env mutation.

    The subprocess calls dashboard_v3 does make are read-only probes -- pgrep,
    sysctl, git rev-parse -- so this checks for the launcher scripts by name
    rather than banning subprocess outright.
    """
    forbidden = ("start_bot.sh", "launch_live.sh", "stop_all.sh", "write_text(")
    offenders = []
    for path in sorted((_REPO / "dashboard_v3").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                offenders.append(f"{path.relative_to(_REPO)}: {token}")
    assert offenders == [], f"dashboard_v3 gained a mutating surface: {offenders}"
