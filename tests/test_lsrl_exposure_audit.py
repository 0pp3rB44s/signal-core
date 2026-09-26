"""The read-only, ACCOUNT-WIDE exposure audit: REAL_POSITIONS / REAL_ORDERS / REAL_PLAN_ORDERS must be zero (or exactly reconciled).

The exchange is a fake; what matters is that the audit sees what a symbol-scoped check cannot (another symbol, another plan-order type,
a second page), that unknown is never zero, and that nothing but three GET reads can ever be called.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from lsrl.tools import exposure_audit as ea
from lsrl.tools.exposure_audit import PAGE_LIMIT, PLAN_TYPES, PRODUCT_TYPES, audit, exit_code, load_expectation, main
from lsrl.tools.verify_exchange import ALLOWED_READS, ReadOnlyViolation


def pos(symbol="SOLUSDT", side="long", total="1.0"):
    return {"symbol": symbol, "holdSide": side, "total": total}


def order(oid, symbol="BTCUSDT", side="buy"):
    return {"symbol": symbol, "orderId": str(oid), "clientOid": f"c{oid}", "side": side, "tradeSide": "open", "size": "1", "price": "100"}


def plan(oid, symbol="BTCUSDT", ptype="loss_plan", side="long"):
    return {"symbol": symbol, "orderId": str(oid), "planType": ptype, "posSide": side, "triggerPrice": "90", "size": "1"}


class FakeAccount:
    """An account-wide exchange: only the three allow-listed reads exist; every other attribute is a test failure."""

    def __init__(self, positions=None, orders=None, plans=None, fail=None):
        self.positions, self.orders, self.plans, self.fail = positions or {}, orders or {}, plans or {}, fail or {}
        self.calls: list[tuple] = []
        self.page_size_seen: list[int] = []
        self.stuck_cursor = False

    def _maybe(self, name):
        if name in self.fail:
            raise self.fail[name]

    def get_all_positions(self, product_type=None, margin_coin="USDT"):
        self.calls.append(("get_all_positions", product_type))
        self._maybe("get_all_positions")
        return {"code": "00000", "data": list(self.positions.get(product_type, []))}

    def _page(self, rows, limit, id_less_than):
        rows = sorted(rows, key=lambda r: -int(r["orderId"]))
        if id_less_than and not self.stuck_cursor:                   # a stuck exchange keeps returning the same first page
            rows = [r for r in rows if int(r["orderId"]) < int(id_less_than)]
        page = rows[:limit]
        end = "" if not page else page[-1]["orderId"]
        return {"code": "00000", "data": {"entrustedList": page, "endId": end}}

    def get_pending_orders(self, symbol=None, product_type=None, limit=100, id_less_than=None):
        self.calls.append(("get_pending_orders", product_type, id_less_than))
        self._maybe("get_pending_orders")
        assert symbol is None, "the audit is account-wide: it must not scope to one symbol"
        self.page_size_seen.append(limit)
        return self._page(self.orders.get(product_type, []), limit, id_less_than)

    def get_tpsl_orders(self, product_type=None, symbol=None, plan_type="profit_loss", limit=None, id_less_than=None):
        self.calls.append(("get_tpsl_orders", product_type, plan_type, id_less_than))
        self._maybe("get_tpsl_orders")
        assert symbol is None
        return self._page(self.plans.get((product_type, plan_type), []), limit or 100, id_less_than)

    def __getattr__(self, name):                                    # a write (or any other read) is a test failure
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"the audit reached for {name}, which is not an allow-listed read")


def clean():
    return FakeAccount()


# ==================================================================================== the clean account passes, and says what it read
def test_a_flat_clean_account_passes_with_all_three_counts_zero_and_says_what_it_did_not_cover():
    acct = clean()
    rep = audit(acct)
    assert rep["EXCHANGE_EXPOSURE_GATE"] == "PASS" and rep["complete"] and rep["reason"] == ""
    assert (rep["REAL_POSITIONS"], rep["REAL_ORDERS"], rep["REAL_PLAN_ORDERS"]) == (0, 0, 0)
    assert exit_code(rep) == 0 and rep["read_only"] is True
    assert set(rep["scope"]["NOT_COVERED"]) >= {"COIN-FUTURES", "SPOT"}                  # unchecked areas are named, never silent
    assert rep["read_only_calls_made"] == sorted(ALLOWED_READS_USED := {"get_all_positions", "get_pending_orders", "get_tpsl_orders"})


def test_every_product_type_and_every_plan_order_type_is_queried_without_a_symbol_filter():
    acct = clean()
    audit(acct)
    plan_queries = {(c[1], c[2]) for c in acct.calls if c[0] == "get_tpsl_orders"}
    assert plan_queries == {(pt, q) for pt in PRODUCT_TYPES for q in PLAN_TYPES}
    assert set(PLAN_TYPES) == {"profit_loss", "normal_plan", "track_plan"}               # TP/SL, trigger, trailing
    assert {c[1] for c in acct.calls if c[0] == "get_all_positions"} == set(PRODUCT_TYPES)
    assert {c[1] for c in acct.calls if c[0] == "get_pending_orders"} == set(PRODUCT_TYPES)


# ==================================================================================== what a symbol-scoped check cannot see
@pytest.mark.parametrize("acct,counts", [
    (FakeAccount(positions={"USDT-FUTURES": [pos("BTCUSDT", "short", "0.01")]}), (1, 0, 0)),          # another symbol's position
    (FakeAccount(positions={"USDC-FUTURES": [pos("ETHPERP", "long", "2")]}), (1, 0, 0)),              # another product type
    (FakeAccount(orders={"USDT-FUTURES": [order(5)]}), (0, 1, 0)),                                     # a resting order elsewhere
    (FakeAccount(plans={("USDT-FUTURES", "profit_loss"): [plan(7, "ETHUSDT")]}), (0, 0, 1)),           # a TP/SL elsewhere
    (FakeAccount(plans={("USDT-FUTURES", "normal_plan"): [plan(8, ptype="normal_plan")]}), (0, 0, 1)), # a TRIGGER order
    (FakeAccount(plans={("USDT-FUTURES", "track_plan"): [plan(9, ptype="track_plan")]}), (0, 0, 1)),   # a TRAILING order
])
def test_exposure_the_pilot_did_not_create_is_found_whatever_its_symbol_product_or_plan_type(acct, counts):
    rep = audit(acct)
    assert (rep["REAL_POSITIONS"], rep["REAL_ORDERS"], rep["REAL_PLAN_ORDERS"]) == counts
    assert rep["complete"] and rep["EXCHANGE_EXPOSURE_GATE"] == "FAIL" and exit_code(rep) == 2
    assert "not flat and clean" in rep["reason"]


def test_a_zero_size_position_row_is_not_exposure_but_an_unreadable_size_is_never_treated_as_zero():
    assert audit(FakeAccount(positions={"USDT-FUTURES": [pos(total="0")]}))["EXCHANGE_EXPOSURE_GATE"] == "PASS"
    rep = audit(FakeAccount(positions={"USDT-FUTURES": [{"symbol": "SOLUSDT", "holdSide": "long", "total": "n/a"}]}))
    assert rep["complete"] is False and rep["EXCHANGE_EXPOSURE_GATE"] == "FAIL" and exit_code(rep) == 3


# ==================================================================================== pagination: to the end, or INCOMPLETE
def test_every_page_is_read_and_an_unexpected_item_on_the_last_page_is_found():
    acct = FakeAccount(orders={"USDT-FUTURES": [order(i) for i in range(1, 2 * PAGE_LIMIT + 51)]})     # 250 orders = 3 pages
    rep = audit(acct)
    assert rep["complete"] and rep["REAL_ORDERS"] == 2 * PAGE_LIMIT + 50 and rep["EXCHANGE_EXPOSURE_GATE"] == "FAIL"
    cursors = [c[2] for c in acct.calls if c[0] == "get_pending_orders" and c[1] == "USDT-FUTURES"]
    assert cursors[0] is None and len(cursors) == 3 and all(cursors[1:])
    assert set(acct.page_size_seen) == {PAGE_LIMIT}


def test_a_full_page_whose_cursor_does_not_advance_is_incomplete_not_a_clean_end():
    acct = FakeAccount(orders={"USDT-FUTURES": [order(i) for i in range(1, PAGE_LIMIT + 5)]})
    acct.stuck_cursor = True
    rep = audit(acct)
    assert rep["complete"] is False and exit_code(rep) == 3 and "cursor" in " ".join(rep["errors"])


def test_more_pages_than_the_bound_is_incomplete(monkeypatch):
    monkeypatch.setattr(ea, "MAX_PAGES", 2)
    rep = audit(FakeAccount(orders={"USDT-FUTURES": [order(i) for i in range(1, 3 * PAGE_LIMIT + 1)]}))
    assert rep["complete"] is False and "more than 2 pages" in " ".join(rep["errors"])


def test_exactly_one_full_page_followed_by_an_empty_page_is_a_clean_end():
    acct = FakeAccount(orders={"USDT-FUTURES": [order(i) for i in range(1, PAGE_LIMIT + 1)]})
    rep = audit(acct)
    assert rep["complete"] and rep["REAL_ORDERS"] == PAGE_LIMIT


# ==================================================================================== unknown is never zero
@pytest.mark.parametrize("name", ["get_all_positions", "get_pending_orders", "get_tpsl_orders"])
def test_a_failed_read_makes_the_audit_incomplete_and_a_fail_never_a_zero(name):
    rep = audit(FakeAccount(fail={name: TimeoutError("read timed out")}))
    assert rep["complete"] is False and rep["EXCHANGE_EXPOSURE_GATE"] == "FAIL" and exit_code(rep) == 3
    assert "audit incomplete" in rep["reason"] and name.replace("get_", "").split("_")[0] in " ".join(rep["errors"]).lower() or rep["errors"]


@pytest.mark.parametrize("payload", [None, {}, {"data": "oops"}, {"data": [1, 2]}, {"code": "00000"}, "html error page"])
def test_a_malformed_response_is_incomplete_never_an_empty_account(payload):
    class Broken(FakeAccount):
        def get_pending_orders(self, **kw):
            return payload
    rep = audit(Broken())
    assert rep["complete"] is False and rep["EXCHANGE_EXPOSURE_GATE"] == "FAIL"


def test_credentials_that_appear_in_an_error_are_redacted():
    secret = "K" * 30
    rep = audit(FakeAccount(fail={"get_all_positions": RuntimeError(f"bad key {secret}")}))
    assert secret not in json.dumps(rep) and "<redacted>" in json.dumps(rep)


# ==================================================================================== reconciliation against a declared state
def _expect(tmp_path, **kw):
    path = tmp_path / "expected.json"
    path.write_text(json.dumps(kw))
    return load_expectation(path)


def test_an_account_that_matches_the_declared_state_exactly_passes(tmp_path):
    acct = FakeAccount(positions={"USDT-FUTURES": [pos("SOLUSDT", "long", "1.6")]},
                       orders={"USDT-FUTURES": [order(11, "SOLUSDT")]},
                       plans={("USDT-FUTURES", "profit_loss"): [plan(12, "SOLUSDT")]})
    exp = _expect(tmp_path, positions=["USDT-FUTURES:SOLUSDT:long"], orders=["USDT-FUTURES:SOLUSDT:11"],
                  plan_orders=["USDT-FUTURES:SOLUSDT:12"])
    rep = audit(acct, expected=exp)
    assert rep["EXCHANGE_EXPOSURE_GATE"] == "PASS" and rep["reconciliation"]["orders"] == {"unexpected": [], "missing": []}


def test_one_unexpected_and_one_missing_item_are_each_a_fail_and_named(tmp_path):
    acct = FakeAccount(positions={"USDT-FUTURES": [pos("SOLUSDT", "long", "1.6"), pos("BTCUSDT", "short", "0.1")]})
    rep = audit(acct, expected=_expect(tmp_path, positions=["USDT-FUTURES:SOLUSDT:long", "USDT-FUTURES:SOLUSDT:short"]))
    assert rep["EXCHANGE_EXPOSURE_GATE"] == "FAIL"
    assert rep["reconciliation"]["positions"] == {"unexpected": ["USDT-FUTURES:BTCUSDT:short"],
                                                  "missing": ["USDT-FUTURES:SOLUSDT:short"]}


def test_an_unexpected_item_alone_and_a_missing_item_alone_are_each_a_fail(tmp_path):
    only_unexpected = audit(FakeAccount(positions={"USDT-FUTURES": [pos("BTCUSDT", "short", "0.1")]}), expected=_expect(tmp_path))
    assert only_unexpected["EXCHANGE_EXPOSURE_GATE"] == "FAIL" and only_unexpected["reconciliation"]["positions"]["unexpected"] == ["USDT-FUTURES:BTCUSDT:short"]
    assert only_unexpected["reconciliation"]["positions"]["missing"] == []
    only_missing = audit(clean(), expected=_expect(tmp_path, orders=["USDT-FUTURES:SOLUSDT:11"]))
    assert only_missing["EXCHANGE_EXPOSURE_GATE"] == "FAIL" and only_missing["reconciliation"]["orders"] == {"unexpected": [], "missing": ["USDT-FUTURES:SOLUSDT:11"]}


def test_an_incomplete_audit_fails_even_when_what_was_read_matches_the_expectation(tmp_path):
    rep = audit(FakeAccount(fail={"get_tpsl_orders": TimeoutError("x")}), expected=_expect(tmp_path))
    assert rep["EXCHANGE_EXPOSURE_GATE"] == "FAIL" and rep["complete"] is False


@pytest.mark.parametrize("doc", [{"orders": ["USDT-FUTURES:SOLUSDT:*"]}, {"orders": ["clientprefix"]}, {"orders": "x"},
                                 {"positions": [1]}, {"cancel": ["x:y:z"]}, [], {"orders": ["A:B:C:D"]}])
def test_an_expectation_can_only_name_exact_ids_never_a_pattern_or_a_prefix(tmp_path, doc):
    path = tmp_path / "e.json"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError):
        load_expectation(path)


# ==================================================================================== read-only by construction
def test_the_audit_can_only_reach_the_three_allow_listed_reads_and_each_is_a_get():
    from lsrl.tools.verify_exchange import ReadOnlyClient
    ro = ReadOnlyClient(clean())
    for forbidden in ("place_futures_limit_order", "cancel_futures_order", "cancel_all_futures_tpsl_orders",
                      "close_futures_position_full", "move_futures_stop_loss", "set_leverage", "_request"):
        with pytest.raises(ReadOnlyViolation):
            getattr(ro, forbidden)
    assert {"get_all_positions", "get_pending_orders", "get_tpsl_orders"} <= set(ALLOWED_READS)
    assert audit(FakeAccount(), now_ms=1)["utc_ms"] == 1


def test_the_tool_source_names_no_mutating_call_and_no_credential_value():
    text = Path(ea.__file__).read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(("#", '"""')))
    assert not re.search(r"\b(place|cancel|close|move|set|modify|flash|transfer|withdraw)_[a-z_]+\(", code)
    assert "POST" not in code and "post(" not in code.lower()


# ==================================================================================== the command
def test_the_command_reports_nothing_and_exits_3_without_credentials(monkeypatch, capsys):
    from app.config import Settings
    monkeypatch.setattr("lsrl.tools.exposure_audit._credentials_configured", lambda s: False)
    assert main([], client_factory=lambda s: (_ for _ in ()).throw(AssertionError("no client may be built"))) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["credentials_configured"] is False and out["EXCHANGE_EXPOSURE_GATE"] == "FAIL" and out["complete"] is False


def test_the_command_prints_the_report_and_the_exit_code_follows_the_gate(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("lsrl.tools.exposure_audit._credentials_configured", lambda s: True)
    assert main([], client_factory=lambda s: clean()) == 0
    assert json.loads(capsys.readouterr().out)["EXCHANGE_EXPOSURE_GATE"] == "PASS"
    dirty = FakeAccount(positions={"USDT-FUTURES": [pos()]})
    assert main([], client_factory=lambda s: dirty) == 2
    assert json.loads(capsys.readouterr().out)["REAL_POSITIONS"] == 1
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert main(["--expect-file", str(bad)], client_factory=lambda s: clean()) == 3
