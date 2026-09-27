"""Regression tests for the DEMO/PAPER-only owner ceremony (funnel/tools/demo_ceremony.py).

The central property this file protects: this ceremony has NO code path to real money. That is
checked structurally (no import of the real-capital modules), not just by reading the docstring - a
future edit that accidentally added such an import would fail test_ceremony_module_never_imports_
real_capital_machinery immediately.
"""
from __future__ import annotations

import ast
import sys
import time
from pathlib import Path

import pytest

from funnel.tools import demo_ceremony as dc


def test_ceremony_module_never_imports_real_capital_machinery():
    """Checks actual `import`/`from ... import` STATEMENTS only (via ast), not prose - the module's own
    docstring names these modules to explain what is deliberately NOT imported, which a naive substring
    search over the whole file would misread as a violation."""
    forbidden_modules = ("execution.entry_submitter", "execution.order_intent_store",
                        "lsrl.adapters.live", "lsrl.adapters.authority", "lsrl.tools.owner_ceremony")
    source_path = Path(sys.modules["funnel.tools.demo_ceremony"].__file__)
    tree = ast.parse(source_path.read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
    for forbidden in forbidden_modules:
        assert not any(m == forbidden or m.startswith(forbidden + ".") for m in imported_modules), \
            f"demo_ceremony.py must never import {forbidden}; found imports: {imported_modules}"


def test_ticket_can_only_be_consumed_once():
    ticket = dc.Ticket(ttl_s=10.0)
    ticket.consume()
    with pytest.raises(dc.Refused, match="TICKET_ALREADY_CONSUMED"):
        ticket.consume()


def test_ticket_expires_and_refuses_a_stale_consume():
    ticket = dc.Ticket(ttl_s=0.01)
    time.sleep(0.05)
    with pytest.raises(dc.Refused, match="TICKET_EXPIRED"):
        ticket.consume()


def test_phrase_is_the_exact_demo_paper_wording_never_a_real_money_claim():
    assert dc.PHRASE == "START LIVE PILOT"
    assert "real order" in dc.PHRASE_MEANING.lower()
    assert "never" in dc.PHRASE_MEANING.lower() or "only" in dc.PHRASE_MEANING.lower()


def test_gate_baseline_unchanged_passes_against_the_committed_freeze_file():
    result = dc.gate_baseline_unchanged()
    assert result.passed, result.detail


def test_gate_risk_manager_and_kill_switch_are_callable():
    assert dc.gate_risk_manager().passed
    assert dc.gate_kill_switch().passed
