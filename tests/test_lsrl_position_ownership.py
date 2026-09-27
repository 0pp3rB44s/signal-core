"""Exactly ONE owner of live exposure: the legacy position monitor must never adopt an LSRL position, and no other
strategy may enter a (symbol, side) LSRL holds. Ownership is restored deterministically from the persisted registry."""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import pytest

from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id

from execution.entry_submitter import EntryOrderSubmitter, RESULT_ACCEPTED, RESULT_REJECTED
from execution.order_intent_store import OrderIntentStore
from execution.position_manager import ExchangeSnapshot
from execution.position_reconciler import RECOVERED_EXCHANGE_POSITION_STRATEGY
from execution.strategy_ownership import (OwnershipConflict, OwnershipUnreadable, StrategyOwnershipRegistry,
                                          owner_family)
from tests.lsrl_fake_exchange import FakeExchange
from tests.lsrl_fixtures import RULES
from tests.test_position_lifecycle_safety import _live_payload, _manager


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STRATEGY_OWNERSHIP_PATH", raising=False)
    monkeypatch.setattr(time, "sleep", lambda _s: None)      # the manager's protection-heal backoff


def registry() -> StrategyOwnershipRegistry:
    return StrategyOwnershipRegistry()                       # the default path, exactly as production code builds it


def sync_with(positions: list[dict]):
    manager = _manager()
    live = [{"symbol": p["symbol"], **p} for p in positions]
    snapshot = ExchangeSnapshot(positions_live=live, open_symbols={p["symbol"] for p in positions}, ok=True,
                                fetched_at_ms=time.time() * 1000)
    manager.sync([], exchange_snapshot=snapshot, recovery_stats={"blocked": False})
    return {p["symbol"]: p for p in manager.store.load(default=[])}


def adopted(symbols: dict, symbol: str) -> bool:
    return symbol in symbols and symbols[symbol].get("strategy") == RECOVERED_EXCHANGE_POSITION_STRATEGY


# ------------------------------------------------------------------ the position monitor
def test_control_an_untracked_exchange_position_is_adopted_by_the_position_monitor():
    store = sync_with([_live_payload("SOLUSDT", 3.0)])
    assert adopted(store, "SOLUSDT")


def test_a_position_owned_by_lsrl_is_not_adopted_managed_or_healed(caplog):
    registry().claim("SOLUSDT", "long", "lsrl_v1", "setup-1")
    with caplog.at_level(logging.WARNING):
        store = sync_with([_live_payload("SOLUSDT", 3.0), _live_payload("BTCUSDT", 1.0)])
    assert "SOLUSDT" not in store                            # no local record was created for it
    assert adopted(store, "BTCUSDT")                         # other symbols are unaffected
    assert "POSITION_OWNED_BY_STRATEGY | SOLUSDT | owner=lsrl_v1" in caplog.text


def test_an_unreadable_registry_protects_the_frozen_symbol_and_only_that_symbol(caplog):
    path = registry().path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{corrupt")
    with caplog.at_level(logging.WARNING):
        store = sync_with([_live_payload("SOLUSDT", 3.0), _live_payload("BTCUSDT", 1.0)])
    assert "SOLUSDT" not in store and adopted(store, "BTCUSDT")
    assert "owner=UNREADABLE_REGISTRY" in caplog.text


def test_once_released_the_symbol_is_ordinary_again():
    reg = registry()
    reg.claim("SOLUSDT", "long", "lsrl_v1", "setup-1")
    assert reg.release("SOLUSDT", "long", "lsrl_v1") is True
    assert adopted(sync_with([_live_payload("SOLUSDT", 3.0)]), "SOLUSDT")


# ------------------------------------------------------------------ the registry itself
def test_a_claim_is_idempotent_for_its_owner_and_refused_for_anyone_else():
    reg = registry()
    assert reg.claim("SOLUSDT", "long", "lsrl_v1", "a") is True
    assert reg.claim("SOLUSDT", "long", "lsrl_v1_e3", "b") is False          # leg suffix = the same owner family
    with pytest.raises(OwnershipConflict):
        reg.claim("SOLUSDT", "long", "microflow_scalper_v1", "c")
    with pytest.raises(OwnershipConflict):
        reg.release("SOLUSDT", "long", "microflow_scalper_v1")
    assert reg.claim("SOLUSDT", "short", "microflow_scalper_v1", "d") is True   # the other hold side is independent


def test_claims_are_durable_and_atomic_across_instances_and_restarts():
    registry().claim("SOLUSDT", "long", "lsrl_v1", "setup-1")
    restarted = StrategyOwnershipRegistry()                                   # a new process reads the same file
    assert restarted.owner_of("SOLUSDT", "long") == "lsrl_v1" and restarted.owner_of("SOLUSDT") == "lsrl_v1"
    assert restarted.claims()["SOLUSDT:long"]["ref"] == "setup-1"
    assert not list(restarted.path.parent.glob("*.tmp*"))                     # no torn temp file left behind


def test_a_corrupt_registry_is_reported_never_silently_reset():
    reg = registry()
    reg.claim("SOLUSDT", "long", "lsrl_v1", "x")
    reg.path.write_text(reg.path.read_text()[:10])
    with pytest.raises(OwnershipUnreadable):
        reg.owner_of("SOLUSDT", "long")
    with pytest.raises(OwnershipUnreadable):
        reg.claim("SOLUSDT", "long", "lsrl_v1", "y")


def test_owner_family_strips_the_entry_leg_only():
    assert owner_family("lsrl_v1_e4") == "lsrl_v1" and owner_family("LSRL_V1") == "lsrl_v1"
    assert owner_family("microflow_scalper_v1") == "microflow_scalper_v1" and owner_family("lsrl_v10") == "lsrl_v10"


# ------------------------------------------------------------------ the audited entry boundary
def plan(strategy: str, symbol: str = "SOLUSDT", direction: str = "LONG") -> SimpleNamespace:
    cid = deterministic_candidate_id(strategy, symbol, direction, 1_800_000_000_000)
    return SimpleNamespace(candidate_id=cid, plan_id=deterministic_plan_id(cid), symbol=symbol,
                           direction=direction, strategy=strategy)


def submit(tmp_path, the_plan):
    client = FakeExchange(rules=RULES)
    submitter = EntryOrderSubmitter(client=client, intent_store=OrderIntentStore(str(tmp_path / "intents.json")),
                                    session_id="t", execution_mode="LIVE")
    placed = []

    def place(client_oid):
        placed.append(client_oid)
        return {"data": {"orderId": "OID1", "clientOid": client_oid}}
    result = submitter.submit_entry(plan=the_plan, size=1.0, side="buy" if the_plan.direction == "LONG" else "sell",
                                    order_type="limit", leg="maker", place=place, notional_usdt=150.0)
    return result, placed


def test_another_strategy_cannot_enter_a_side_lsrl_owns(tmp_path):
    registry().claim("SOLUSDT", "long", "lsrl_v1", "setup-1")
    result, placed = submit(tmp_path, plan("microflow_scalper_v1"))
    assert result.status == RESULT_REJECTED and result.classification == "OWNED_BY_OTHER_STRATEGY" and placed == []


def test_lsrl_itself_and_unrelated_sides_or_symbols_are_not_blocked(tmp_path):
    registry().claim("SOLUSDT", "long", "lsrl_v1", "setup-1")
    assert submit(tmp_path, plan("lsrl_v1_e2"))[0].status == RESULT_ACCEPTED
    assert submit(tmp_path, plan("microflow_scalper_v1", direction="SHORT"))[0].status == RESULT_ACCEPTED
    assert submit(tmp_path, plan("microflow_scalper_v1", symbol="BTCUSDT"))[0].status == RESULT_ACCEPTED


def test_with_an_unreadable_registry_only_the_frozen_symbol_is_protected_from_other_strategies(tmp_path):
    path = registry().path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("nope")
    blocked, placed = submit(tmp_path, plan("microflow_scalper_v1"))
    assert blocked.classification == "OWNED_BY_OTHER_STRATEGY" and placed == []
    assert submit(tmp_path, plan("microflow_scalper_v1", symbol="BTCUSDT"))[0].status == RESULT_ACCEPTED
    assert submit(tmp_path, plan("lsrl_v1_e1"))[0].status == RESULT_ACCEPTED
