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
