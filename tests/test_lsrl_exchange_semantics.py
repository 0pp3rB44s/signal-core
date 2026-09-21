"""What LSRL_V1 assumes about Bitget, pinned to (a) the OFFICIAL classic-futures documentation read on 2026-09-20,
(b) the REAL public SOLUSDT contract config captured the same day, and (c) this repository's own incident notes.

Not established here (no authenticated exchange access was used): the account's actual posMode / marginMode. The
executor verifies both before its first entry and refuses to trade unless the account is HEDGE + ISOLATED; the
owner can run `python -m lsrl.tools.verify_exchange` (GET-only) with the account's environment to see the same.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from clients.bitget_precision import BitgetPrecisionMixin
from clients.bitget_rest import BitgetRestClient
from lsrl.contract import (ExchangeFactsError, rules_from_contract_row, rules_from_contract_spec, tick_from,
                           verify_exchange_facts)
from lsrl.core.types import SymbolRules
from tests.lsrl_fake_exchange import FakeExchange, contract_row_for
from tests.lsrl_fixtures import RULES
from tests.test_lsrl_live_executor import Rig
from tests.test_lsrl_risk_integration import _isolate  # noqa: F401  (autouse isolation fixture)
from tests.lsrl_fixtures import long_sweep

FIXTURE = Path(__file__).parent / "fixtures" / "bitget_contract_solusdt_2026-09-20.json"


@pytest.fixture(scope="module")
def real_row() -> dict:
    payload = json.loads(FIXTURE.read_text())
    assert payload["code"] == "00000"
    return payload["data"][0]


# ------------------------------------------------------------------ contract metadata (real exchange values)
def test_the_real_solusdt_contract_has_tick_0_001_not_the_old_test_value(real_row):
    assert (real_row["pricePlace"], real_row["priceEndStep"]) == ("3", "1")
    rules = rules_from_contract_row(real_row, "SOLUSDT")
    assert rules == SymbolRules("SOLUSDT", tick_size=0.001, qty_step=0.1, min_qty=0.1, min_notional_usdt=5.0)
    assert RULES == rules                                    # the shared test fixtures use the REAL metadata now


def test_tick_honours_price_end_step_not_just_price_place():
    assert tick_from(3, Decimal(1)) == 0.001 and tick_from(3, Decimal(5)) == 0.005 and tick_from(2, Decimal(1)) == 0.01


def test_the_contract_spec_parser_keeps_price_end_step_including_the_outage_fallback(real_row):
    row = dict(real_row, priceEndStep="5")
    spec = BitgetPrecisionMixin._parse_contract(SimpleNamespace(), row, "USDT-FUTURES", "SOLUSDT")
    assert spec.price_end_step == Decimal(5) and rules_from_contract_spec(spec, "SOLUSDT").tick_size == 0.005
    default = BitgetPrecisionMixin._parse_contract(SimpleNamespace(), {k: v for k, v in real_row.items()
                                                                       if k != "priceEndStep"}, "USDT-FUTURES", "SOLUSDT")
    assert default.price_end_step == Decimal(1)              # absent/invalid -> 1, never 0


def test_unknown_or_unusable_metadata_refuses_to_run(real_row):
    with pytest.raises(RuntimeError, match="CONTRACT_METADATA_UNAVAILABLE"):
        rules_from_contract_spec(None, "SOLUSDT")
    for key in ("pricePlace", "priceEndStep", "sizeMultiplier", "minTradeNum", "minTradeUSDT"):
        with pytest.raises(ExchangeFactsError):
            rules_from_contract_row({k: v for k, v in real_row.items() if k != key}, "SOLUSDT")
    with pytest.raises(ExchangeFactsError):
        rules_from_contract_row(dict(real_row, symbol="BTCUSDT"), "SOLUSDT")


# ------------------------------------------------------------------ startup verification matrix (fail closed)
def facts_client(**overrides) -> FakeExchange:
    ex = FakeExchange(rules=RULES)
    for key, value in overrides.items():
        if key == "contract":
            ex.contract = {**ex.contract, **value}
        elif key == "account":
            ex.account = {**ex.account, **value}
    return ex


def verify(client, expected=RULES):
    return verify_exchange_facts(client, symbol="SOLUSDT", product_type="USDT-FUTURES", expected=expected)


def test_a_hedge_isolated_normal_account_verifies_and_reports_the_actual_fee_rates():
    facts = verify(facts_client())
    assert facts.pos_mode == "hedge_mode" and facts.margin_mode == "isolated" and facts.symbol_status == "normal"
    assert (facts.maker_fee_rate, facts.taker_fee_rate) == (0.0002, 0.0006) and facts.rules == RULES


@pytest.mark.parametrize("override, fragment", [
    ({"account": {"posMode": "one_way_mode"}}, "POSITION_MODE_NOT_HEDGE"),
    ({"account": {"posMode": ""}}, "POSITION_MODE_NOT_HEDGE"),
    ({"account": {"marginMode": "crossed"}}, "MARGIN_MODE_NOT_ISOLATED"),
    ({"contract": {"symbolStatus": "limit_open"}}, "SYMBOL_NOT_TRADABLE"),
    ({"contract": {"symbolStatus": "maintain"}}, "SYMBOL_NOT_TRADABLE"),
    ({"contract": {"symbolStatus": "restrictedAPI"}}, "SYMBOL_NOT_TRADABLE"),
    ({"contract": {"limitOpenTime": "1800000000000"}}, "SYMBOL_MAINTENANCE_SCHEDULED"),
    ({"contract": {"offTime": "1800000000000"}}, "SYMBOL_MAINTENANCE_SCHEDULED"),
    ({"contract": {"pricePlace": "2"}}, "CONTRACT_METADATA_DRIFT"),                     # exchange tick != engine tick
    ({"contract": {"priceEndStep": "5"}}, "CONTRACT_METADATA_DRIFT"),
    ({"contract": {"sizeMultiplier": "0.01", "volumePlace": "2"}}, "CONTRACT_METADATA_DRIFT"),
    ({"contract": {"minTradeUSDT": "10"}}, "CONTRACT_METADATA_DRIFT"),
])
def test_startup_verification_refuses_anything_it_cannot_prove(override, fragment):
    with pytest.raises(ExchangeFactsError, match=fragment):
        verify(facts_client(**override))


def test_unreadable_contract_or_account_state_refuses_instead_of_guessing():
    for failing in ("get_contracts", "get_symbol_account"):
        ex = facts_client()
        ex.fail[failing] = RuntimeError("HTTP 500")
        with pytest.raises(ExchangeFactsError, match="UNREADABLE"):
            verify(ex)
    empty = facts_client()
    empty.contract = None
    with pytest.raises(ExchangeFactsError, match="NOT_UNIQUE"):
        verify(empty)


def test_the_executor_refuses_the_first_entry_when_the_account_is_not_hedge_mode(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.account["posMode"] = "one_way_mode"
    rig.run(long_sweep())
    assert rig.ex.orders == {} and rig.exec.halt_reason and "POSITION_MODE_NOT_HEDGE" in rig.exec.halt_reason
    assert [i["kind"] for i in rig.exec.incidents][0] == "EXCHANGE_FACTS_UNVERIFIED"
    assert not rig.ex.by_kind("set_leverage") and not rig.ex.by_kind("place_entry")


def test_the_executor_refuses_when_the_engine_tick_disagrees_with_the_exchange(tmp_path):
    rig = Rig(tmp_path)
    rig.ex.contract = contract_row_for(SymbolRules("SOLUSDT", 0.01, 0.1, 0.1, 5.0))     # the old test-only tick
    rig.run(long_sweep())
    assert rig.ex.orders == {} and "CONTRACT_METADATA_DRIFT" in (rig.exec.halt_reason or "")


# ------------------------------------------------------------------ request shapes == the documented hedge-mode tables
@pytest.fixture()
def real_client(real_row):
    """The REAL order client (unmodified) with only the transport stubbed: we look at the bodies it would POST."""
    spec = BitgetPrecisionMixin._parse_contract(SimpleNamespace(), real_row, "USDT-FUTURES", "SOLUSDT")
    client = BitgetRestClient(settings=Settings(_env_file=None, FORWARD_PAPER_ONLY=False))
    client._contract_spec = lambda symbol, **k: spec
    client._assert_order_transport_allowed = lambda: None
    client._require_safe_clock_for_order = lambda: None
    sent: list[dict] = []
    client._request = lambda **kw: (sent.append(kw), {"data": {"orderId": "1", "clientOid": kw.get("client_oid")}})[1]
    client.sent = sent
    return client


def test_hedge_mode_entries_match_the_documented_open_long_and_open_short_rows(real_client):
    """Docs (Place Order): hedge mode  Open long = side buy + tradeSide open;  Open short = side sell + tradeSide open."""
    real_client.place_futures_limit_order("SOLUSDT", "LONG", 1.6, 149.6, client_oid="a", post_only=True)
    real_client.place_futures_limit_order("SOLUSDT", "SHORT", 1.6, 150.4, client_oid="b", post_only=True)
    long_body, short_body = (s["body"] for s in real_client.sent)
    assert (long_body["side"], long_body["tradeSide"], long_body["orderType"], long_body["force"]) == \
        ("buy", "open", "limit", "post_only")
    assert (short_body["side"], short_body["tradeSide"]) == ("sell", "open")
    assert long_body["marginMode"] == "isolated" and long_body["productType"] == "USDT-FUTURES"


def test_hedge_mode_take_profits_match_the_documented_close_long_and_close_short_rows(real_client):
    """Docs: hedge mode  Close long = side buy + tradeSide close;  Close short = side sell + tradeSide close.
    (`side` names the position being closed, proven against this account's own filled closes.)"""
    real_client.place_futures_limit_close_order("SOLUSDT", "long", 0.4, 150.3, client_oid="t1", post_only=True)
    real_client.place_futures_limit_close_order("SOLUSDT", "short", 0.4, 149.7, client_oid="t2", post_only=True)
    long_body, short_body = (s["body"] for s in real_client.sent)
    assert (long_body["side"], long_body["tradeSide"], long_body["holdSide"]) == ("buy", "close", "long")
    assert (short_body["side"], short_body["tradeSide"], short_body["holdSide"]) == ("sell", "close", "short")
    assert long_body["force"] == "post_only" and long_body["reduceOnly"] == "YES"


def test_quantity_is_floored_to_the_exchange_step_never_rounded_up(real_client):
    real_client.place_futures_limit_close_order("SOLUSDT", "long", 0.48, 150.3, client_oid="t1")
    assert real_client.sent[0]["body"]["size"] == "0.4"


def test_the_fake_exchange_vocabulary_is_the_documented_one():
    """Order `state`: live / partially_filled / filled / canceled (Get Order Detail); cancelling a filled order = 43001."""
    ex = FakeExchange(rules=RULES)
    ex.place_futures_limit_order("SOLUSDT", "LONG", 1.0, 149.0, client_oid="o", post_only=True)
    assert ex.orders["o"]["state"] == "live"
    ex.fill("o", qty=0.4)
    assert ex.orders["o"]["state"] == "partially_filled"
    ex.fill("o", qty=0.6)
    assert ex.orders["o"]["state"] == "filled"
    with pytest.raises(RuntimeError, match="does not exist"):
        ex.cancel_futures_order("SOLUSDT", client_oid="o")
