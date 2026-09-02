import json

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.symbol_allowlist import OWNER_APPROVED_PRODUCTION_SYMBOLS
from funding_pilot.read_only_verify import (ReadOnlyBitgetClient, ReadOnlyBitgetSettings,
    ReadOnlyTransportViolation, main, run)


SECRETS={"BITGET_API_KEY":"key-never-print","BITGET_API_SECRET":"secret-never-print",
         "BITGET_API_PASSPHRASE":"pass-never-print"}


def _settings(monkeypatch):
    for key,value in SECRETS.items(): monkeypatch.setenv(key,value)
    monkeypatch.setenv("APP_ENV","production"); monkeypatch.setenv("APP_MODE","live")
    monkeypatch.setenv("ENABLED_STRATEGIES","invalid-production-strategy")
    return ReadOnlyBitgetSettings()


def test_read_only_loader_ignores_live_strategy_admission(monkeypatch):
    settings=_settings(monkeypatch)
    assert settings.bitget_api_key.get_secret_value()==SECRETS["BITGET_API_KEY"]
    approved=",".join(OWNER_APPROVED_PRODUCTION_SYMBOLS)
    live={"APP_ENV":"production","APP_MODE":"live","EXECUTION_ENABLED":True,
          "EXECUTION_MODE":"LIVE","FORWARD_PAPER_ONLY":False,"MAX_OPEN_POSITIONS":2,
          "EXECUTION_MAX_PER_CYCLE":2,"MAX_SYMBOLS":len(OWNER_APPROVED_PRODUCTION_SYMBOLS),"ALLOW_AUTO_WATCHLIST_REFRESH":False,
          "EXECUTION_REQUIRE_CONFIRMATION":True,"EXECUTION_MARGIN_MODE":"isolated",
          "PRODUCTION_SYMBOL_ALLOWLIST":approved,"STRATEGY_ISOLATION_ENABLED":True,
          "ENABLED_STRATEGIES":"invalid-production-strategy"}
    with pytest.raises(ValidationError,match="microflow_scalper_v1"):
        Settings(**live)


def test_missing_credential_fails_closed(monkeypatch):
    for key in SECRETS: monkeypatch.delenv(key,raising=False)
    with pytest.raises(ValidationError): ReadOnlyBitgetSettings()


def test_transport_rejects_every_mutating_verb_without_network(monkeypatch):
    client=object.__new__(ReadOnlyBitgetClient)
    for method in ("POST","PUT","PATCH","DELETE"):
        with pytest.raises(ReadOnlyTransportViolation): client._request(method,"/mutation")
    with pytest.raises(ReadOnlyTransportViolation):
        client._request("GET","/api/v2/mix/order/place-order")
    with pytest.raises(ReadOnlyTransportViolation): client._assert_order_transport_allowed()


class FakeReads:
    def __init__(self,settings): self.settings=settings; self.bill_payload={"data":{"bills":[]}}
    def get_accounts(self): return {"data":[{"marginCoin":"USDT"}]}
    def get_all_positions(self): return {"data":[]}
    def get_pending_orders(self,**_): return {"data":{"entrustedList":[]}}
    def get_tpsl_orders(self,**_): return {"data":{"entrustedList":[]}}
    def get_order_history(self,**_): return {"data":{"orderList":[]}}
    def get_trade_fee_rate(self,_): return {"data":[{"makerFeeRate":"x","takerFeeRate":"x"}]}
    def _request(self,method,*_,**__):
        assert method=="GET"; return self.bill_payload


class FakePlans(FakeReads):
    def __init__(self,settings,rows): super().__init__(settings); self.rows=rows
    def get_tpsl_orders(self,**_): return {"data":{"entrustedList":list(self.rows)}}


def test_safe_output_contains_no_secret_values(monkeypatch,capsys):
    settings=_settings(monkeypatch)
    code,result=run(settings,client_factory=lambda settings:FakeReads(settings))
    assert code==0 and result["BITGET_AUTH_READ_VERIFIED"] is True
    rendered=json.dumps(result)
    assert not any(value in rendered for value in SECRETS.values())


@pytest.mark.parametrize("row",[
    {"symbol":"BTCUSDT","orderId":"tp1","planType":"profit_plan","triggerPrice":"101"},
    {"symbol":"BTCUSDT","orderId":"conditional1","planType":"normal_plan","triggerPrice":"101"},
])
def test_take_profit_and_generic_conditionals_are_not_native_stops(monkeypatch,row):
    settings=_settings(monkeypatch)
    code,result=run(settings,client_factory=lambda settings:FakePlans(settings,[row]))
    assert code==0
    assert result["PLAN_ORDER_CLASSIFICATION"]["record_count"]==1
    assert result["STOP_CLASSIFICATION"]["record_count"]==0


def test_explicit_loss_plan_is_one_deduplicated_native_stop(monkeypatch):
    settings=_settings(monkeypatch)
    row={"symbol":"BTCUSDT","orderId":"sl1","planType":"loss_plan","triggerPrice":"99"}
    code,result=run(settings,client_factory=lambda settings:FakePlans(settings,[row]))
    assert code==0
    # The same row is returned for each queried plan category but counted once.
    assert result["PLAN_ORDER_CLASSIFICATION"]["record_count"]==1
    assert result["STOP_CLASSIFICATION"]["record_count"]==1


def test_main_missing_credentials_does_not_print_secrets(monkeypatch,capsys):
    for key in SECRETS: monkeypatch.delenv(key,raising=False)
    assert main()==2
    output=capsys.readouterr().out
    assert "BITGET_AUTH_READ_VERIFIED" in output
    assert not any(value in output for value in SECRETS.values())


def _run_with_bills(monkeypatch, bills):
    settings=_settings(monkeypatch)
    def factory(settings):
        client=FakeReads(settings); client.bill_payload={"data":{"bills":bills,"endId":"end"}}
        return client
    return run(settings,client_factory=factory)


def test_valid_funding_bill_response(monkeypatch):
    row={"billId":"fund-1","symbol":"BTCUSDT","amount":"-0.1","fee":"0",
         "businessType":"contract_settle_fee","coin":"USDT","cTime":"1"}
    code,result=_run_with_bills(monkeypatch,[row])
    assert code==0
    assert result["FUNDING_CLASSIFICATION"]=={
        "endpoint_reachable":True,"record_count":1,
        "classification_pass":True,"schema_fields_present":True}


def test_empty_funding_history_is_valid(monkeypatch):
    code,result=_run_with_bills(monkeypatch,[])
    assert code==0
    assert result["FUNDING_CLASSIFICATION"]["record_count"]==0
    assert result["FUNDING_CLASSIFICATION"]["classification_pass"] is True


def test_old_wrong_business_type_fails_closed(monkeypatch):
    settings=_settings(monkeypatch)
    class BusinessTypeGuard(FakeReads):
        def _request(self,method,path,*,params,**kwargs):
            if params.get("businessType") != "contract_settle_fee":
                raise RuntimeError("Parameter businessType error")
            return {"data":{"bills":[]}}
    guard=BusinessTypeGuard(settings)
    with pytest.raises(RuntimeError,match="businessType"):
        guard._request("GET","/api/v2/mix/account/bill",
                       params={"businessType":"funding_fee"},private=True)
    code,result=run(settings,client_factory=lambda settings:guard)
    assert code==0
    assert result["FUNDING_CLASSIFICATION"]["endpoint_reachable"] is True


def test_mixed_bills_count_only_actual_funding(monkeypatch):
    bills=[
        {"billId":"fee","amount":"0","fee":"-.1","businessType":"open_long","cTime":"1"},
        {"billId":"pnl","amount":"1","fee":"0","businessType":"close_long","cTime":"2"},
        {"billId":"fund","amount":"-.1","fee":"0","businessType":"contract_settle_fee","cTime":"3"},
        {"billId":"transfer","amount":"5","fee":"0","businessType":"trans_from_exchange","cTime":"4"},
    ]
    code,result=_run_with_bills(monkeypatch,bills)
    assert code==0
    assert result["FUNDING_CLASSIFICATION"]["record_count"]==1
    assert result["FUNDING_CLASSIFICATION"]["classification_pass"] is True


def test_funding_bill_missing_required_schema_fails(monkeypatch):
    code,result=_run_with_bills(monkeypatch,[{"businessType":"contract_settle_fee","amount":"-.1"}])
    assert code==2
    assert result["FUNDING_CLASSIFICATION"]["schema_fields_present"] is False
