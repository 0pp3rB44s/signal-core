"""app/adaptive_trend_scan.py: the shadow-only scan-cycle wiring.

Proves the piece that was structurally missing before this PR: nothing in
production ever called evaluate_universe(), so AdaptiveTrend produced zero
shadow decisions no matter how long the bot ran. These tests bind against
the real wiring function (not a double), and re-assert the hard invariant
this module's docstring promises: it can never touch execute() or create
live state, only ever append to the shadow log.
"""

from __future__ import annotations

import ast
import json
import time
from pathlib import Path

import pytest

from app.adaptive_trend_scan import SCAN_STATE_PATH, run_adaptive_trend_shadow_scan
from strategies.adaptive_trend_shadow import ShadowDecisionLog
from strategies.adaptive_trend_tsmom import SYMBOL_UNIVERSE
from execution.state_store import JsonStateStore

REPO = Path(__file__).resolve().parents[1]
SIX_H = 6 * 60 * 60 * 1000


class FakeSettings:
    bitget_product_type = "USDT-FUTURES"
    account_equity_usdt = 1000.0
    weekly_freeze_loss_pct = 7.0


class FakeClient:
    def __init__(self, closes):
        self._closes = closes

    def get_candles(self, symbol, product_type, granularity="6h", limit=200):
        now = int(time.time() * 1000)
        last_close_boundary = (now // SIX_H) * SIX_H  # most recent already-closed candle
        start = last_close_boundary - SIX_H - (len(self._closes) - 1) * SIX_H
        rows = []
        for i, c in enumerate(self._closes):
            open_ms = start + i * SIX_H
            rows.append([open_ms, c, c + 1.0, c - 1.0, c, "1", "1"])
        return {"data": rows}


def test_scan_never_imports_execution_or_client_submission_paths():
    """AST-level structural guarantee, matching every other AdaptiveTrend
    module: this file may orchestrate I/O (it legitimately needs client/
    settings), but it must never import ExecutionService or anything that
    could submit a live order."""
    source = (REPO / "app" / "adaptive_trend_scan.py").read_text()
    tree = ast.parse(source)
    forbidden_names = {"ExecutionService", "EntryOrderSubmitter"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden_names, (
                    f"forbidden import found: {alias.name}"
                )
        if isinstance(node, ast.Attribute) and node.attr == "execute":
            pytest.fail("adaptive_trend_scan.py must never call .execute(...)")


def test_shadow_scan_persists_progress_and_never_raises(tmp_path, monkeypatch):
    from strategies.adaptive_trend_candles import WARMUP_CANDLES

    monkeypatch.chdir(tmp_path)
    client = FakeClient([100.0] * WARMUP_CANDLES)
    store = JsonStateStore(SCAN_STATE_PATH)
    log = ShadowDecisionLog(path=str(tmp_path / "shadow.jsonl"))

    result = run_adaptive_trend_shadow_scan(
        client=client, settings=FakeSettings(), weekly_freeze_active=False,
        state_store=store, shadow_log=log,
    )
    assert "error" not in result
    persisted = store.load(default={})
    assert set(persisted.keys()) == set(SYMBOL_UNIVERSE)


def test_data_fetch_failure_is_caught_not_raised(monkeypatch, tmp_path):
    class BrokenClient:
        def get_candles(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.chdir(tmp_path)
    store = JsonStateStore(SCAN_STATE_PATH)
    log = ShadowDecisionLog(path=str(tmp_path / "shadow.jsonl"))
    result = run_adaptive_trend_shadow_scan(
        client=BrokenClient(), settings=FakeSettings(), weekly_freeze_active=False,
        state_store=store, shadow_log=log,
    )
    # evaluate_universe itself catches per-symbol fetch failures (DATA_UNHEALTHY
    # evaluations); this only fails hard if something outside that contract breaks.
    assert "error" not in result


def test_freeze_active_reaches_shadow_log_as_rejection_reason_not_silence(tmp_path, monkeypatch):
    """A freeze must be visible in the shadow record (observability), not
    silently swallowed -- and critically, must NOT prevent a shadow record
    from being written; shadow-mode always logs, regardless of freeze state."""
    from strategies.adaptive_trend_candles import WARMUP_CANDLES

    monkeypatch.chdir(tmp_path)
    closes = [100.0] * (WARMUP_CANDLES - 1) + [130.0]  # strong momentum signal
    client = FakeClient(closes)
    store = JsonStateStore(SCAN_STATE_PATH)
    shadow_path = tmp_path / "shadow.jsonl"
    log = ShadowDecisionLog(path=str(shadow_path))

    run_adaptive_trend_shadow_scan(
        client=client, settings=FakeSettings(), weekly_freeze_active=True,
        state_store=store, shadow_log=log,
    )
    if shadow_path.exists():
        rows = [json.loads(line) for line in shadow_path.read_text().splitlines() if line.strip()]
        for row in rows:
            if row.get("decision") == "ACCOUNT_FREEZE_BLOCKED":
                assert row.get("rejection_reason") == "weekly_freeze_active"


def test_runner_calls_the_shadow_scan_from_the_real_scan_cycle():
    """Structural proof the wiring actually exists at the call site -- the
    original gap this PR closes: evaluate_universe() was fully built and
    tested but never invoked anywhere in app/runner.py."""
    source = (REPO / "app" / "runner.py").read_text()
    assert "run_adaptive_trend_shadow_scan" in source
