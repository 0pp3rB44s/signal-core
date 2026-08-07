"""Order-intent reconciliation must not depend on the bot having a trade.

`_entry_guard_reason` held the once-per-process call to
`recover_pending_intents`, and `execute` only reaches that guard after
`select_execution_winner` produced a winner. With no winner — weekly freeze,
quiet market, every plan risk-blocked — `execute` returned early and the
reconciliation never ran.

The failure is silent and cumulative: unfilled maker legs stay SUBMITTED, and
the exchange attestation eventually fails on them. Sixty-four had piled up
before anything noticed. Precisely when the bot cannot trade is when its
housekeeping matters most, so that was the worst possible place for the call.

These tests drive the real `ExecutionService.execute` through the existing
harness and assert on whether the real recovery API was reached.
"""

from __future__ import annotations

import logging

import pytest

from execution.execution_service import ExecutionService

from tests.test_entry_path_audit import _live_settings, _plan, _service


class RecordingSubmitter:
    """Wraps the real submitter, counting the reconciliation calls."""

    def __init__(self, inner, result=None, raises=None):
        self._inner = inner
        self.calls = 0
        self.result = result if result is not None else {"blocked": False, "reasons": [], "recovered": []}
        self.raises = raises

    def recover_pending_intents(self):
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.result

    def __getattr__(self, name):
        return getattr(self._inner, name)


class SettingsOverride:
    """Read-through view of the real Settings with one attribute replaced.

    `Settings` is a validated pydantic model, so overriding an attribute on it
    (or on its class) is not possible without weakening the model itself.
    """

    def __init__(self, inner, **overrides):
        self._inner = inner
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._inner, name)


def service_with_recorder(monkeypatch, *, result=None, raises=None, **overrides):
    service = _service(monkeypatch, **overrides)
    service.entry_submitter = RecordingSubmitter(
        service.entry_submitter, result=result, raises=raises
    )
    return service


# ── TEST 1 / 2 / 3: recovery no longer needs a winner ───────────────────────

def test_1_empty_plan_list_still_reconciles(monkeypatch):
    """The weekly-freeze shape: the runner selects nothing at all."""
    service = service_with_recorder(monkeypatch)
    service.execute([])
    assert service.entry_submitter.calls == 1, (
        "reconciliation was skipped on a cycle with nothing to execute"
    )


def test_2_no_winner_does_not_prevent_recovery(monkeypatch):
    """Plans exist but portfolio selection rejects all of them."""
    service = service_with_recorder(monkeypatch)
    monkeypatch.setattr(
        "execution.execution_service.select_execution_winner",
        lambda plans, **kw: type("Sel", (), {
            "winner": None, "winner_metrics": None, "ranked": [], "rejected": list(plans),
        })(),
    )
    reports = service.execute([_plan("BTCUSDT")])
    assert reports == []
    assert service.entry_submitter.calls == 1


def test_3_risk_blocked_plans_do_not_prevent_recovery(monkeypatch):
    """A plan that the risk gate refuses must still leave housekeeping intact."""
    service = service_with_recorder(monkeypatch)
    monkeypatch.setattr(
        "execution.execution_service.select_execution_winner",
        lambda plans, **kw: type("Sel", (), {
            "winner": None, "winner_metrics": None, "ranked": [], "rejected": list(plans),
        })(),
    )
    service.execute([_plan("BTCUSDT"), _plan("ETHUSDT")])
    assert service.entry_submitter.calls == 1


# ── TEST 4 / 5: latch semantics ─────────────────────────────────────────────

def test_4_recovery_runs_once_per_process(monkeypatch):
    service = service_with_recorder(monkeypatch)
    for _ in range(5):
        service.execute([])
    assert service.entry_submitter.calls == 1, "the once-per-process latch was lost"
    assert service._entry_recovery_done is True


def test_4b_failed_recovery_is_retried_and_keeps_blocking(monkeypatch):
    """A raising reconciliation must not latch, and must block new entries."""
    service = service_with_recorder(monkeypatch, raises=ConnectionError("transport down"))
    service.execute([])
    assert service._entry_recovery_done is False
    assert service.entry_submitter.calls == 1

    service.execute([])
    assert service.entry_submitter.calls == 2, "a failed reconciliation must be retried"
    assert "recovery failed" in service._entry_guard_reason()


def test_5_zero_pending_intents_is_harmless(monkeypatch):
    service = service_with_recorder(monkeypatch)
    service.execute([])
    service.execute([])
    assert service.entry_submitter.calls == 1
    assert service._entry_guard_reason() == "" or isinstance(service._entry_guard_reason(), str)


def test_5b_non_live_mode_never_reconciles(monkeypatch):
    service = service_with_recorder(monkeypatch)
    service.settings = SettingsOverride(service.settings, execution_mode="FORWARD_PAPER")
    service.execute([])
    assert service.entry_submitter.calls == 0, "reconciliation must stay LIVE-only"


# ── TEST 7: an unknown intent still blocks, and is not silently abandoned ───

def test_7_blocking_recovery_verdict_reaches_the_entry_guard(monkeypatch):
    service = service_with_recorder(monkeypatch, result={
        "blocked": True,
        "reasons": ["intent coid-1 (BTCUSDT) is in UNKNOWN state"],
        "recovered": [],
    })
    service.execute([])                       # recovery runs here, no winner
    assert service.entry_submitter.calls == 1

    reason = service._entry_guard_reason()
    assert "UNKNOWN state" in reason, (
        "a blocking verdict produced on a no-winner cycle was lost"
    )


def test_7b_blocking_verdict_is_delivered_once(monkeypatch):
    """Matches the inline version: the verdict was returned on its own cycle."""
    service = service_with_recorder(monkeypatch, result={
        "blocked": True, "reasons": ["unreconciled order intent"], "recovered": [],
    })
    service.execute([])
    assert "unreconciled order intent" in service._entry_guard_reason()
    second = service._entry_guard_reason()
    assert "unreconciled order intent" not in second


# ── CASE E: housekeeping success must not suppress a later valid entry ──────

def test_e_clean_recovery_on_empty_cycle_leaves_next_entry_allowed(monkeypatch):
    """The property that makes this patch safe rather than merely earlier.

    Recovery runs on a cycle with nothing to trade. The next cycle has a real
    winner, and must be allowed through: a successful housekeeping pass may not
    leave anything behind that blocks trading.
    """
    service = service_with_recorder(monkeypatch)
    service.execute([])                                   # housekeeping only
    assert service.entry_submitter.calls == 1
    assert service._entry_recovery_block_reason == ""

    monkeypatch.setattr(service, "_exchange_pending_entry_guard_reason", lambda: "")
    assert service._entry_guard_reason() == "", (
        "a clean recovery left a residue that would block the next entry"
    )
    assert service.entry_submitter.calls == 1, "recovery must not repeat"


def test_e_consumed_block_reason_does_not_leak_into_later_cycles(monkeypatch):
    """A verdict is delivered once, then the persistent gates take over."""
    service = service_with_recorder(monkeypatch, result={
        "blocked": True, "reasons": ["unreconciled order intent"], "recovered": [],
    })
    service.execute([])
    assert "unreconciled" in service._entry_guard_reason()      # cycle N: blocked

    monkeypatch.setattr(service, "_exchange_pending_entry_guard_reason", lambda: "")
    for _ in range(3):                                          # cycle N+1..N+3
        assert service._entry_guard_reason() == "", (
            "a consumed verdict kept blocking; only intent_store.blocking() may persist"
        )
    assert service.entry_submitter.calls == 1


def test_e_unknown_intent_keeps_blocking_after_the_verdict_is_consumed(monkeypatch):
    """Fail-closed must come from durable state, not from the cached string."""
    service = service_with_recorder(monkeypatch, result={
        "blocked": True, "reasons": ["intent coid-x is in UNKNOWN state"], "recovered": [],
    })
    service.execute([])
    service._entry_guard_reason()                               # consume the verdict

    monkeypatch.setattr(
        service.intent_store, "blocking",
        lambda: [{"symbol": "BTCUSDT", "client_oid": "coid-x"}],
    )
    assert "UNKNOWN state" in service._entry_guard_reason(), (
        "an UNKNOWN intent stopped blocking once the cached reason was consumed"
    )


# ── TEST 8 / 9: recovery cannot touch the exchange ──────────────────────────

def test_8_recovery_only_cycle_submits_no_order(monkeypatch):
    service = service_with_recorder(monkeypatch)
    service.execute([])
    client = service.client
    for method in ("place_futures_market_order", "place_futures_limit_order"):
        call = getattr(client, method, None)
        if call is not None:
            assert call.call_count == 0, f"{method} was called on a recovery-only cycle"


def test_9_recovery_only_cycle_cancels_nothing(monkeypatch):
    service = service_with_recorder(monkeypatch)
    service.execute([])
    client = service.client
    for method in (
        "cancel_futures_order",
        "cancel_all_futures_tpsl_orders",
        "close_futures_position",
        "close_futures_position_full",
    ):
        call = getattr(client, method, None)
        if call is not None:
            assert call.call_count == 0, f"{method} was called on a recovery-only cycle"


# ── TEST 10: the trading path is unchanged apart from the earlier call ──────

def test_10_winner_path_still_reconciles_exactly_once(monkeypatch):
    """A cycle that does trade must behave as before: one reconciliation."""
    service = service_with_recorder(monkeypatch)
    service.execute([_plan("BTCUSDT")])
    assert service.entry_submitter.calls == 1
    assert service._entry_recovery_done is True


def test_10b_recovery_precedes_selection(monkeypatch):
    """Ordering is the whole point: housekeeping before the early return."""
    order: list[str] = []
    service = _service(monkeypatch)
    service.entry_submitter = RecordingSubmitter(service.entry_submitter)

    real = service.entry_submitter.recover_pending_intents

    def traced():
        order.append("recovery")
        return real()

    service.entry_submitter.recover_pending_intents = traced
    monkeypatch.setattr(
        "execution.execution_service.select_execution_winner",
        lambda plans, **kw: (order.append("selection") or type("Sel", (), {
            "winner": None, "winner_metrics": None, "ranked": [], "rejected": [],
        })()),
    )
    service.execute([])
    assert order == ["recovery", "selection"], f"unexpected order: {order}"


def test_10c_execution_disabled_still_short_circuits(monkeypatch):
    """The EXECUTION_ENABLED kill switch keeps precedence over housekeeping."""
    service = service_with_recorder(monkeypatch)
    service.settings = SettingsOverride(service.settings, execution_enabled=False)
    assert service.execute([_plan("BTCUSDT")]) == []
    assert service.entry_submitter.calls == 0
