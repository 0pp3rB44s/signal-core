"""The dashboard may run from its own checkout, but must read production truth.

Running the dashboard from an isolated checkout is what stops a dashboard update
from moving the trading engine's working tree. The hazard it introduces is
subtle: the isolated checkout has its own empty `state/`, `logs/` and
`data_store/`, so a dashboard that resolved paths relative to its own code would
render a confident, fully-populated page in which every value is UNKNOWN — which
looks exactly like a dead trading bot. These tests pin the seam.
"""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest


def _reload(monkeypatch, prod_root: str | None):
    if prod_root is None:
        monkeypatch.delenv("CGC_PROD_ROOT", raising=False)
    else:
        monkeypatch.setenv("CGC_PROD_ROOT", prod_root)
    from dashboard_v3.core import sources
    return importlib.reload(sources)


def _make_prod(tmp_path: Path) -> Path:
    root = tmp_path / "trading_runtime"
    (root / "state").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "data_store").mkdir()
    return root


def test_unset_env_keeps_single_checkout_behaviour(monkeypatch):
    """Existing deployments must not change behaviour by upgrading."""
    src = _reload(monkeypatch, None)
    assert src.BASE_PATH == src.DASHBOARD_ROOT
    assert src.ISOLATED_RUNTIME is False


def test_state_reads_resolve_against_production_not_the_code_checkout(monkeypatch, tmp_path):
    prod = _make_prod(tmp_path)
    (prod / "state" / "runtime_heartbeat.json").write_text('{"beat": 1}', encoding="utf-8")
    src = _reload(monkeypatch, str(prod))
    assert src.BASE_PATH == prod
    assert src.ISOLATED_RUNTIME is True
    assert src.load_json("state/runtime_heartbeat.json").value == {"beat": 1}


def test_empty_local_state_beside_the_code_is_never_used(monkeypatch, tmp_path):
    """The exact trap: a decoy state/ next to the dashboard code must be ignored."""
    prod = _make_prod(tmp_path)
    (prod / "state" / "runtime_heartbeat.json").write_text('{"source": "production"}', encoding="utf-8")
    src = _reload(monkeypatch, str(prod))
    decoy = src.DASHBOARD_ROOT / "state" / "runtime_heartbeat.json"
    assert src.load_json("state/runtime_heartbeat.json").value == {"source": "production"}
    assert src.BASE_PATH / "state" / "runtime_heartbeat.json" != decoy


@pytest.mark.parametrize("bad", ["/nonexistent/path/xyz", ""])
def test_missing_production_root_fails_closed(monkeypatch, tmp_path, bad):
    """A wrong path must refuse to start, not serve an all-UNKNOWN dashboard."""
    if bad == "":
        src = _reload(monkeypatch, None)          # empty means "unset"
        assert src.BASE_PATH == src.DASHBOARD_ROOT
        return
    with pytest.raises(Exception):
        _reload(monkeypatch, bad)


def test_directory_without_state_is_rejected(monkeypatch, tmp_path):
    """A fresh worktree has no state/ — that is the decoy this guards against."""
    empty = tmp_path / "fresh_worktree"
    (empty / "dashboard_v3").mkdir(parents=True)
    # Matched by name and message rather than by class identity: importlib.reload
    # rebinds ProductionRootError to a NEW class object, so a reference captured
    # before the reload is not the class the reloaded module raises.
    with pytest.raises(Exception) as exc:
        _reload(monkeypatch, str(empty))
    assert type(exc.value).__name__ == "ProductionRootError"
    assert "not a production checkout" in str(exc.value)
    assert "missing: state" in str(exc.value)


def test_a_file_is_not_a_production_root(monkeypatch, tmp_path):
    f = tmp_path / "afile"; f.write_text("x")
    with pytest.raises(Exception):
        _reload(monkeypatch, str(f))


def test_git_helpers_report_the_trading_repo(monkeypatch, tmp_path):
    """The deployment panel must show the TRADING runtime SHA, not the
    dashboard's own — they are deliberately different checkouts."""
    prod = _make_prod(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=prod, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "trading"], cwd=prod, check=True)
    expected = subprocess.run(["git", "rev-parse", "HEAD"], cwd=prod,
                              capture_output=True, text=True).stdout.strip()
    src = _reload(monkeypatch, str(prod))
    assert src.repo_head() == expected
    assert src.worktree_status()["known"] is True


def test_dashboard_root_still_points_at_the_code(monkeypatch, tmp_path):
    src = _reload(monkeypatch, str(_make_prod(tmp_path)))
    assert (src.DASHBOARD_ROOT / "dashboard_v3" / "core" / "sources.py").exists()


@pytest.fixture(autouse=True)
def _restore():
    """Return the module to its unset-env state after every test.

    Uses os.environ directly rather than monkeypatch: monkeypatch's own teardown
    runs after this fixture's, so relying on it would reload the module while a
    bad CGC_PROD_ROOT was still set and raise out of teardown.
    """
    import os
    yield
    os.environ.pop("CGC_PROD_ROOT", None)
    from dashboard_v3.core import sources
    importlib.reload(sources)
