"""The owner's read-only exchange verification: no order, no cancel, no setting change, no secret, no unproven claim."""
from __future__ import annotations

import inspect
import json
import re

import pytest

from clients.bitget_rest import BitgetRestClient
from lsrl.tools import verify_exchange as tool
from lsrl.tools.verify_exchange import ALLOWED_READS, ReadOnlyClient, ReadOnlyViolation, build_report, exit_code, main, redact
from tests.lsrl_fake_exchange import FakeExchange
from tests.lsrl_fixtures import RULES
from tests.test_lsrl_risk_integration import _isolate  # noqa: F401  (autouse isolation fixture)

MUTATING = ("place_futures_limit_order", "place_futures_limit_close_order", "cancel_futures_order", "set_futures_leverage",
            "move_futures_stop_loss", "close_futures_position_full", "cancel_all_futures_tpsl_orders",
            "set_futures_margin_mode", "place_futures_market_order")
SECRET = "SECRETSECRETSECRETSECRETSECRET1234567890"


def fake(**account):
    ex = FakeExchange(rules=RULES)
    ex.account.update(account)
    return ex


def report_for(ex, **kw):
    return build_report(ex, symbol="SOLUSDT", product_type="USDT-FUTURES", public_only=kw.pop("public_only", False), now_ms=1)


# ------------------------------------------------------------------------------------------------ structure: GET only
def _sources(name: str) -> str:
    method = getattr(BitgetRestClient, name)
    text = inspect.getsource(method)
    for helper in re.findall(r"self\.(_[a-z_]+)\(", text):
        candidate = getattr(BitgetRestClient, helper, None)
        if candidate is not None and inspect.isfunction(candidate):
            text += inspect.getsource(candidate)
    return text


@pytest.mark.parametrize("name,path", sorted(ALLOWED_READS.items()))
def test_every_allow_listed_method_is_a_GET_against_the_documented_read_endpoint(name, path):
    text = _sources(name)
    assert path in text and '"GET"' in text
    assert not re.search(r'"(POST|PUT|DELETE)"', text), f"{name} can send a mutating request"


def test_no_allow_listed_name_looks_like_a_mutation_and_none_is_a_known_mutating_client_method():
    assert not [n for n in ALLOWED_READS if re.search(r"place|cancel|set_|close|move|modify|flatten|leverage", n)]
    assert not set(ALLOWED_READS) & set(MUTATING)


@pytest.mark.parametrize("name", MUTATING)
def test_the_wrapper_refuses_every_mutating_method_before_anything_reaches_the_client(name):
    ex = fake()
    ro = ReadOnlyClient(ex)
    with pytest.raises(ReadOnlyViolation):
        getattr(ro, name)
    assert ex.calls == []


def test_the_tool_source_never_mentions_a_mutating_call():
    text = inspect.getsource(tool)
    assert not re.search(r"\.(place_|cancel_|set_|close_|move_)[a-z_]*\(", text)


# ------------------------------------------------------------------------------------------------ what it reports
def test_a_hedge_isolated_clean_account_verifies_every_field_from_reads_that_succeeded():
    ex = fake()
    rep = report_for(ex)
    assert rep["read_only"] is True and rep["all_reads_ok"] and rep["all_verified"] and exit_code(rep) == 0
    assert rep["verified"] == {"CONTRACT_METADATA_VERIFIED": True, "POSITION_MODE_HEDGE_VERIFIED": True,
                               "MARGIN_MODE_ISOLATED_VERIFIED": True, "FEE_RATES_READ": True, "SYMBOL_FLAT_AND_CLEAN": True}
    assert rep["contract"]["tick_size"] == 0.001 and rep["contract"]["qty_step"] == 0.1 and rep["contract"]["tradable"] is True
    assert rep["account"]["pos_mode"] == "hedge_mode" and rep["account"]["margin_mode"] == "isolated"
    assert rep["fees"]["maker_fee_rate"] == 0.0002 and rep["fees"]["taker_fee_rate"] == 0.0006
    assert rep["symbol_state"]["flat_and_clean"] is True
    assert set(rep["read_only_calls_made"]) <= set(ALLOWED_READS)


def test_only_allow_listed_reads_reach_the_client_and_nothing_is_placed_cancelled_or_changed():
    ex = fake()
    report_for(ex)
    names = {c[0] for c in ex.calls}
    assert not names & {"place_entry", "place_tp", "cancel", "stop", "cancel_stop", "emergency_close", "set_leverage"}
    assert ex.orders == {} and ex.plans == [] and ex.leverage_calls == []


def test_a_one_way_account_is_reported_as_it_is_and_never_marked_verified():
    rep = report_for(fake(posMode="one_way_mode"))
    assert rep["account"]["read_ok"] is True and rep["account"]["pos_mode"] == "one_way_mode"
    assert rep["verified"]["POSITION_MODE_HEDGE_VERIFIED"] is False and rep["all_verified"] is False and exit_code(rep) == 2


def test_a_cross_margin_account_is_reported_and_never_marked_verified():
    rep = report_for(fake(marginMode="crossed"))
    assert rep["verified"]["MARGIN_MODE_ISOLATED_VERIFIED"] is False and rep["account"]["margin_mode"] == "crossed"


def test_an_unreadable_account_claims_nothing_and_the_error_is_redacted():
    ex = fake()
    ex.fail["get_symbol_account"] = RuntimeError(f"HTTP 401 apiKey={SECRET} sign=abcdefghijklmnopqrstuvwxyz0123456789")
    rep = report_for(ex)
    assert rep["account"]["read_ok"] is False and rep["verified"]["POSITION_MODE_HEDGE_VERIFIED"] is False
    assert rep["verified"]["MARGIN_MODE_ISOLATED_VERIFIED"] is False and rep["all_verified"] is False and exit_code(rep) == 3
    assert SECRET not in json.dumps(rep) and "<redacted>" in rep["account"]["error"]


def test_a_total_outage_verifies_nothing():
    ex = fake()
    ex.outage = True
    rep = report_for(ex)
    assert not any(rep["verified"].values()) and rep["all_verified"] is False and exit_code(rep) == 3


def test_foreign_state_on_the_symbol_is_visible_and_blocks_the_clean_flag():
    ex = fake()
    ex.position["long"] = 2.0
    ex.plans.append({"planType": "loss_plan", "posSide": "long", "orderId": "x"})
    rep = report_for(ex)
    state = rep["symbol_state"]
    assert state["position_long_size"] == 2.0 and state["loss_plans_long"] == 1 and state["flat_and_clean"] is False
    assert rep["verified"]["SYMBOL_FLAT_AND_CLEAN"] is False and rep["verified"]["POSITION_MODE_HEDGE_VERIFIED"] is True


def test_public_only_reads_the_contract_and_nothing_else():
    ex = fake()
    rep = report_for(ex, public_only=True)
    assert set(rep) >= {"contract", "verified"} and "account" not in rep and "symbol_state" not in rep
    assert rep["verified"] == {"CONTRACT_METADATA_VERIFIED": True} and rep["read_only_calls_made"] == ["get_contracts"]


def test_no_balance_or_equity_is_ever_reported():
    rep = report_for(fake(accountEquity="123456.78", available="99999.9", usdtEquity="5555.5"))
    text = json.dumps(rep)
    assert "123456.78" not in text and "99999.9" not in text and "5555.5" not in text and "equity" not in text.lower()


# ------------------------------------------------------------------------------------------------ the command itself
def test_without_credentials_the_command_claims_nothing_and_exits_3(monkeypatch, capsys):
    for name in ("BITGET_API_KEY", "BITGET_API_SECRET", "BITGET_API_PASSPHRASE"):
        monkeypatch.setenv(name, "")
    assert main([], client_factory=lambda s, p: fake()) == 3
    out = json.loads(capsys.readouterr().out)
    assert out["credentials_configured"] is False and out["all_verified"] is False


def test_with_credentials_the_command_prints_the_report_and_never_a_credential(monkeypatch, capsys):
    monkeypatch.setenv("BITGET_API_KEY", "KEY-" + SECRET)
    monkeypatch.setenv("BITGET_API_SECRET", "SEC-" + SECRET)
    monkeypatch.setenv("BITGET_API_PASSPHRASE", "PASS-" + SECRET)
    assert main([], client_factory=lambda s, p: fake()) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["all_verified"] is True
    assert SECRET not in printed and "KEY-" not in printed and "SEC-" not in printed and "PASS-" not in printed


def test_redact_masks_anything_key_shaped():
    assert redact(f"apiKey {SECRET} rest") == "apiKey <redacted> rest"
    assert redact("short text stays") == "short text stays"
