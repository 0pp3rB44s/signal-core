"""TradingBot MVP: emergency stop, execution_status_v1 contract, and the end-to-end
smoke lifecycle against a scripted fake exchange.

EVERY test uses fakes. No network, no credentials, no order is ever sent.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.config import Settings
from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import TradePlan
from execution.emergency_stop import EmergencyStop
from execution.entry_submitter import RESULT_REJECTED, EntryOrderSubmitter
from execution.order_intent_store import OrderIntentStore
from execution.smoke_lifecycle import (
    AUTH_ENV,
    HARD_NOTIONAL_CAP_USDT,
    Journal,
    SmokeRun,
    SmokeSpec,
    authorise,
)
from execution.status_contract import JSON_SCHEMA, build_execution_status, write_execution_status

LOG = logging.getLogger("test_tradingbot_mvp")


# ================================================================ emergency stop
def test_emergency_stop_default_inactive_engage_idempotent_clear_recorded(tmp_path):
    es = EmergencyStop(tmp_path / "ES.json", clock=lambda: 1_700_000_000)
    assert not es.is_active() and es.reason() == ""
    s1 = es.engage("test reason", by="alice")
    s2 = es.engage("a different reason", by="bob")  # idempotent: keeps the original
    assert s1["active"] and s2["reason"] == "test reason" and s2["engaged_by"] == "alice"
    assert es.reason().startswith("EMERGENCY_STOP")
    with pytest.raises(ValueError):
        es.clear(by="")
    es.clear(by="alice", note="resolved")
    assert not es.is_active()
    hist = (tmp_path / "ES.json.history.jsonl").read_text().splitlines()
    assert json.loads(hist[0])["cleared_by"] == "alice"


def test_emergency_stop_corrupt_flag_fails_closed(tmp_path):
    p = tmp_path / "ES.json"
    p.write_text("{not json")
    st = EmergencyStop(p).status()
    assert st["active"] and st["corrupt"]


class _NeverCalledExchange:
    def __getattr__(self, name):  # any exchange access is a test failure
        raise AssertionError(f"exchange touched: {name}")


def _plan() -> TradePlan:
    cid = deterministic_candidate_id("low_vol_reclaim", "BTCUSDT", "LONG", 1_700_000_000_000)
    return TradePlan(
        candidate_id=cid, candidate_candle_open_timestamp_ms=1_700_000_000_000, plan_id=deterministic_plan_id(cid),
        symbol="BTCUSDT", strategy="low_vol_reclaim", direction="LONG", verdict="EXECUTABLE", score=80.0,
        entry_prices=[100.0], stop_loss=95.0, take_profits=[110.0], risk_reward_ratio=2.0, account_risk_pct=1.0,
        leverage=5.0, position_notional_usdt=50.0, notes=[], reasons=[], geometry_entry=100.0,
    )


def test_entry_submitter_blocks_every_entry_while_emergency_stop_engaged_and_never_posts():
    EmergencyStop().engage("operator halt", by="test")  # default relative path under the test cwd
    posted: list[str] = []
    sub = EntryOrderSubmitter(client=_NeverCalledExchange(), intent_store=OrderIntentStore("state/order_intents.json"),
                              session_id="s", execution_mode="LIVE", log=LOG)
    res = sub.submit_entry(plan=_plan(), size=1.0, side="buy", order_type="market", leg="market",
                           place=lambda oid: posted.append(oid) or {"data": {}}, notional_usdt=50.0)
    assert res.status == RESULT_REJECTED and res.classification == "EMERGENCY_STOP"
    assert posted == []
    assert OrderIntentStore("state/order_intents.json").blocking() == []  # nothing even persisted as an intent


# ============================================================ status contract
def _settings(**kw) -> Settings:
    return Settings(FORWARD_PAPER_ONLY=False, **kw)


def test_status_contract_without_exchange_is_unknown_and_blocks(tmp_path):
    doc = build_execution_status(settings=_settings(), root=tmp_path, exchange=None, now=1_700_000_000,
                                 emergency=EmergencyStop(tmp_path / "ES.json"))
    assert doc["schema"] == "execution_status_v1"
    assert doc["connection"]["status"] == "UNKNOWN"
    assert doc["positions"]["source"] == "UNAVAILABLE" and doc["positions"]["count"] is None
    assert doc["account"]["equity_usdt"] is None and doc["account"]["state"] == "UNKNOWN"  # unknown is never 0
    assert doc["risk"]["entries_blocked"] and doc["risk"]["state"] == "BLOCKED"
    assert doc["mode"]["live_trading_enabled"] is False  # default LIVE_TRADING off
    assert doc["heartbeat"]["state"] == "MISSING"
    for key in JSON_SCHEMA["required"]:
        assert key in doc


def test_status_contract_with_exchange_flags_unprotected_and_emergency(tmp_path):
    ex = {"ok": True, "errors": [], "t": 1_700_000_000.0, "rtt_ms": 120,
          "accounts": [{"marginCoin": "USDT", "accountEquity": "50.5", "available": "40.1", "unrealizedPL": "-0.2"}],
          "account_cfg": {"posMode": "hedge_mode", "marginMode": "isolated", "isolatedLongLever": 10},
          "positions": [{"symbol": "SOLUSDT", "holdSide": "long", "total": "0.2", "openPriceAvg": "100", "stopLoss": ""}],
          "open_orders": [], "tpsl_orders": []}
    es = EmergencyStop(tmp_path / "ES.json"); es.engage("halt", by="t")
    doc = build_execution_status(settings=_settings(), root=tmp_path, exchange=ex, now=1_700_000_000, emergency=es)
    assert doc["connection"]["status"] == "OK" and doc["account"]["equity_usdt"] == 50.5
    pos = doc["positions"]["rows"][0]
    assert pos["stop_loss_confirmed"] is False
    assert doc["risk"]["operator_attention_required"] is True
    assert any("UNPROTECTED" in r for r in doc["risk"]["reasons"])
    assert doc["emergency_stop"]["active"] is True
    out = write_execution_status(doc, tmp_path / "export")
    assert json.loads(out.read_text())["content_sha256"] == doc["content_sha256"]


def test_status_contract_position_stop_unconfirmed_when_plan_orders_unreadable(tmp_path):
    ex = {"ok": True, "errors": [], "accounts": [], "positions": [{"symbol": "BTCUSDT", "holdSide": "short", "total": "0.001"}], "open_orders": []}
    doc = build_execution_status(settings=_settings(), root=tmp_path, exchange=ex, emergency=EmergencyStop(tmp_path / "x.json"))
    assert doc["positions"]["rows"][0]["stop_loss_confirmed"] is None  # UNKNOWN, never assumed protected
    assert any("UNCONFIRMED" in r for r in doc["risk"]["reasons"])


# ===================================================== smoke lifecycle (mocked)
class FakeExchange:
    """Scriptable exchange that obeys the `ExchangePort` protocol; every call is recorded."""

    def __init__(self, **kw):
        self.cfg = {"equity": 100.0, "available": 100.0, "pos_mode": "hedge_mode", "margin_mode": "isolated",
                    "long_lever": 10.0, "short_lever": 10.0}
        self.cfg.update(kw.pop("cfg", {}))
        self.price = kw.pop("price", 100.0)
        self.contract_status = kw.pop("contract_status", "normal")
        self.entry_effect = kw.pop("entry_effect", "fill")        # fill | exception_fill | exception_nofill | exception_unknown | nofill
        self.protect_ok = kw.pop("protect_ok", True)
        self.close_works = kw.pop("close_works", True)
        self.read_fails = kw.pop("read_fails", False)
        self.orphan_plans_after_close = kw.pop("orphan_plans_after_close", False)
        self.position: dict | None = None
        self.plans: list = []
        self.calls: list[tuple] = []
        assert not kw, kw

    def _rec(self, *a):
        self.calls.append(a)

    # reads
    def account(self, symbol):
        self._rec("account"); return dict(self.cfg)

    def positions(self):
        self._rec("positions")
        if self.read_fails:
            raise RuntimeError("exchange unreachable")
        return [self.position] if self.position else []

    def open_orders(self):
        self._rec("open_orders")
        if self.read_fails:
            raise RuntimeError("exchange unreachable")
        return []

    def plan_orders(self):
        self._rec("plan_orders")
        if self.read_fails:
            raise RuntimeError("exchange unreachable")
        return list(self.plans)

    def last_price(self, symbol):
        self._rec("last_price"); return self.price

    def contract(self, symbol):
        self._rec("contract"); return {"status": self.contract_status, "min_trade_usdt": 5.0, "min_trade_num": 0.0001}

    def normalize_size(self, symbol, size, ref_price):
        self._rec("normalize_size")
        n = int(size * 10_000) / 10_000
        return (n, None) if n * ref_price >= 5.0 else (n, "BELOW_MIN_NOTIONAL")

    # writes
    def place_market_entry(self, symbol, direction, size, client_oid, ref_price):
        self._rec("place_market_entry", client_oid, size)
        if self.entry_effect in {"fill", "exception_fill"}:
            self.position = {"symbol": symbol, "holdSide": "long" if direction == "LONG" else "short", "total": str(size), "openPriceAvg": str(ref_price)}
        if self.entry_effect.startswith("exception"):
            raise TimeoutError("response lost")
        return {"code": "00000", "data": {"orderId": "ord-1", "clientOid": client_oid}}

    def lookup_order(self, symbol, client_oid):
        self._rec("lookup_order", client_oid)
        if self.entry_effect == "exception_unknown":
            return {"status": "UNKNOWN", "order": None}
        if self.entry_effect in {"exception_nofill"}:
            return {"status": "ABSENT", "order": None}
        if self.entry_effect == "nofill":
            return {"status": "FOUND", "order": {"state": "live"}}
        return {"status": "FOUND", "order": {"state": "filled"}}

    def place_protection(self, symbol, direction, size, stop, tps):
        self._rec("place_protection", stop, tuple(tps))
        if self.protect_ok:
            self.plans = [{"symbol": symbol, "planType": "loss_plan", "holdSide": "long"}, {"symbol": symbol, "planType": "profit_plan"}]
            return {"stop_loss_verified": True, "protection_verified": True}
        return {"stop_loss_verified": False, "protection_verified": False}

    def close_full(self, symbol, direction, size, reason):
        self._rec("close_full", reason)
        if self.close_works:
            self.position = None
            self.plans = [{"symbol": symbol, "planType": "orphan"}] if self.orphan_plans_after_close else []
        return {"code": "00000", "data": {"orderId": "close-1"}}


def make_spec(**kw) -> SmokeSpec:
    base = dict(symbol="BTCUSDT", direction="LONG", notional_usdt=10.0, stop_pct=0.5, take_profit_pct=0.8, required_leverage=10, fill_timeout_s=3.0)
    base.update(kw)
    return SmokeSpec(**base)


def make_run(ex, spec=None, tmp=None, **kw):
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    return SmokeRun(ex, spec or make_spec(), state_path="state/smoke_state.json", journal_path="state/smoke_journal.jsonl",
                    emergency=EmergencyStop("state/EMERGENCY_STOP.json"), sleep=sleep, clock=lambda: t["now"], poll_s=0.5, **kw)


def phases(res) -> list[str]:
    return [json.loads(l)["phase"] for l in Path(res["journal"]).read_text().splitlines()]


def n(ex, name):
    return sum(1 for c in ex.calls if c[0] == name)


def test_authorise_requires_full_tuple(tmp_path):
    spec = make_spec()
    auth = tmp_path / "AUTH"
    assert authorise(spec, typed_phrase="", env={}, auth_file=auth)  # everything missing
    auth.write_text(f"spec_sha256={spec.sha256()}\nauthorised_by=owner\n")
    ok = authorise(spec, typed_phrase=spec.confirm_phrase(), env={AUTH_ENV: spec.sha256()}, auth_file=auth)
    assert ok == []
    assert authorise(make_spec(notional_usdt=HARD_NOTIONAL_CAP_USDT + 1), typed_phrase="x", env={AUTH_ENV: "bad"}, auth_file=auth)
    assert authorise(spec, typed_phrase="wrong", env={AUTH_ENV: spec.sha256()}, auth_file=auth)
    other = make_spec(symbol="SOLUSDT")  # token for a different spec must not authorise this one
    assert authorise(other, typed_phrase=other.confirm_phrase(), env={AUTH_ENV: other.sha256()}, auth_file=auth)


def test_preflight_dry_run_sends_no_order_and_journals():
    ex = FakeExchange()
    res = make_run(ex).run(execute=False)
    assert res["outcome"] == "DRY_RUN_COMPLETE" and res["journal_chain_valid"]
    assert n(ex, "place_market_entry") == 0 and n(ex, "place_protection") == 0 and n(ex, "close_full") == 0
    assert phases(res) == ["PRECHECK", "SIZING", "DRY_RUN_COMPLETE"]


def test_happy_path_all_ten_steps():
    """1 auth/2 market data/3 sizing/4 order/5 fill/6 SL+TP/7 exit/8 journal/(9 restart + 10 stop below)."""
    ex = FakeExchange()
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "DONE" and res["journal_chain_valid"]
    assert phases(res) == ["PRECHECK", "SIZING", "ENTRY_PERSISTED", "ENTRY_SUBMITTED", "FILL_CONFIRMED",
                           "PROTECTION_ATTEMPT", "PROTECTION_CONFIRMED", "EXIT_SUBMITTED", "FLAT_CONFIRMED", "DONE"]
    assert n(ex, "place_market_entry") == 1 and n(ex, "place_protection") == 1 and n(ex, "close_full") == 1
    assert ex.position is None and ex.plans == []
    # Size was normalised DOWN and never inflated to hit the minimum.
    assert ex.calls[[c[0] for c in ex.calls].index("place_market_entry")][2] == 0.1


def test_ack_is_not_a_fill_no_position_means_no_protection_and_no_phantom_exit():
    ex = FakeExchange(entry_effect="nofill")
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "BLOCKED_UNKNOWN"
    assert n(ex, "place_protection") == 0 and n(ex, "close_full") == 0
    assert EmergencyStop("state/EMERGENCY_STOP.json").is_active()


def test_ambiguous_submit_is_reconciled_never_resent_and_position_is_protected_then_closed():
    ex = FakeExchange(entry_effect="exception_fill")
    res = make_run(ex).run(execute=True, authorised=True)
    assert n(ex, "place_market_entry") == 1  # exactly one POST
    assert res["outcome"] == "DONE"
    assert "ENTRY_SUBMIT_EXCEPTION" in phases(res) and "FILL_CONFIRMED" in phases(res)


def test_ambiguous_submit_definitively_absent_aborts_cleanly():
    ex = FakeExchange(entry_effect="exception_nofill")
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "ABORTED" and n(ex, "place_market_entry") == 1 and n(ex, "close_full") == 0


def test_ambiguous_submit_unknown_blocks_and_engages_emergency_stop():
    ex = FakeExchange(entry_effect="exception_unknown")
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "BLOCKED_UNKNOWN" and n(ex, "place_market_entry") == 1
    assert EmergencyStop("state/EMERGENCY_STOP.json").is_active()


def test_stop_loss_unconfirmed_triggers_failsafe_close():
    ex = FakeExchange(protect_ok=False)
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "FAILSAFE_CLOSED"
    assert n(ex, "place_protection") == 2 and n(ex, "close_full") == 1 and ex.position is None
    assert "PROTECTION_FAILED" in phases(res)


def test_close_not_confirmed_escalates_operator_attention_and_blocks_entries():
    ex = FakeExchange(close_works=False)
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "OPERATOR_ATTENTION" and res["reason"] == "close_not_confirmed"
    assert EmergencyStop("state/EMERGENCY_STOP.json").is_active()
    assert ex.position is not None  # honestly reported as still open


def test_orphan_plan_orders_after_flat_escalate():
    ex = FakeExchange(orphan_plans_after_close=True)
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "OPERATOR_ATTENTION" and res["reason"] == "orphan_plan_orders"


@pytest.mark.parametrize("kw,needle", [
    ({"read_fails": True}, "UNKNOWN"),
    ({"cfg": {"pos_mode": "one_way_mode"}}, "posMode"),
    ({"cfg": {"margin_mode": "crossed"}}, "marginMode"),
    ({"cfg": {"long_lever": 100.0}}, "leverage"),
    ({"cfg": {"available": 0.09}}, "insufficient available margin"),
    ({"contract_status": "halt"}, "contract status"),
])
def test_precheck_failures_abort_before_any_order(kw, needle):
    ex = FakeExchange(**kw)
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "ABORTED" and needle in res["reason"]
    assert n(ex, "place_market_entry") == 0


def test_account_not_flat_aborts():
    ex = FakeExchange()
    ex.position = {"symbol": "ETHUSDT", "holdSide": "long", "total": "1"}
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "ABORTED" and "not flat" in res["reason"] and n(ex, "place_market_entry") == 0


def test_emergency_stop_blocks_smoke_run():
    EmergencyStop("state/EMERGENCY_STOP.json").engage("halt", by="t")
    ex = FakeExchange()
    res = make_run(ex).run(execute=True, authorised=True)
    assert res["outcome"] == "ABORTED" and "EMERGENCY_STOP" in res["reason"] and n(ex, "place_market_entry") == 0


def test_execute_without_authorisation_places_nothing():
    ex = FakeExchange()
    res = make_run(ex).run(execute=True, authorised=False)
    assert res["outcome"] == "ABORTED" and n(ex, "place_market_entry") == 0


def test_restart_reconciliation_after_crash_between_submit_and_protection_never_resubmits():
    """Process dies after the entry filled. A fresh process resumes: confirms the position,
    protects it, exits it - and sends NO second entry."""
    ex = FakeExchange()
    run = make_run(ex)
    # simulate the crash: run everything up to FILL by hand, then 'die'
    run.run(execute=False)
    state = json.loads(Path("state/smoke_state.json").read_text())
    state.update(phase="ENTRY_SUBMITTED", client_oid="smokecrashed", size=0.1, stop=99.5, take_profit=100.8)
    Path("state/smoke_state.json").write_text(json.dumps(state))
    ex.position = {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.1", "openPriceAvg": "100"}
    res = SmokeRun.resume(ex, state_path="state/smoke_state.json", journal_path="state/smoke_journal.jsonl",
                          emergency=EmergencyStop("state/EMERGENCY_STOP.json"), sleep=lambda s: None, poll_s=0.1)
    assert res["outcome"] == "DONE"
    assert n(ex, "place_market_entry") == 0 and n(ex, "place_protection") == 1 and n(ex, "close_full") == 1
    assert Journal.verify(Path("state/smoke_journal.jsonl"))


def test_resume_with_no_persisted_entry_is_a_noop():
    ex = FakeExchange()
    make_run(ex).run(execute=False)
    res = SmokeRun.resume(ex, state_path="state/smoke_state.json", journal_path="state/smoke_journal.jsonl", sleep=lambda s: None)
    assert res["outcome"] == "DRY_RUN_COMPLETE" and n(ex, "place_market_entry") == 0 and n(ex, "close_full") == 0


def test_journal_chain_detects_tampering():
    ex = FakeExchange()
    res = make_run(ex).run(execute=True, authorised=True)
    p = Path(res["journal"])
    assert Journal.verify(p)
    lines = p.read_text().splitlines()
    rec = json.loads(lines[2]); rec["data"]["size"] = 999
    lines[2] = json.dumps(rec, sort_keys=True)
    p.write_text("\n".join(lines) + "\n")
    assert not Journal.verify(p)


def test_execution_service_entry_guard_returns_emergency_reason_in_every_mode():
    from types import SimpleNamespace

    from execution.execution_service import ExecutionService

    svc = object.__new__(ExecutionService)  # no network/client construction needed for the guard
    svc.settings = SimpleNamespace(execution_mode="DRY_RUN")
    svc.emergency_stop = EmergencyStop("state/EMERGENCY_STOP.json")
    assert svc._entry_guard_reason() == ""            # DRY_RUN, not engaged
    svc.emergency_stop.engage("owner halt", by="t")
    assert svc._entry_guard_reason().startswith("EMERGENCY_STOP")  # applies before the LIVE-only checks
