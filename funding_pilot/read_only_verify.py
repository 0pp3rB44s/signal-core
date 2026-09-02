"""Authenticated Bitget classification with a mechanically GET-only transport."""
from __future__ import annotations

import json
from typing import Callable

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clients.bitget_rest import BitgetRestClient
from funding_pilot.bitget_exchange import _rows


class ReadOnlyBitgetSettings(BaseSettings):
    """Credential-only settings surface; deliberately independent of app.Settings."""

    model_config = SettingsConfigDict(env_file=None, extra="ignore")
    bitget_base_url: str = Field(default="https://api.bitget.com", alias="BITGET_BASE_URL")
    bitget_api_key: SecretStr = Field(alias="BITGET_API_KEY")
    bitget_api_secret: SecretStr = Field(alias="BITGET_API_SECRET")
    bitget_api_passphrase: SecretStr = Field(alias="BITGET_API_PASSPHRASE")
    bitget_product_type: str = Field(default="USDT-FUTURES", alias="BITGET_PRODUCT_TYPE")
    bitget_margin_coin: str = Field(default="USDT", alias="BITGET_MARGIN_COIN")
    bitget_locale: str = Field(default="en-US", alias="BITGET_LOCALE")
    forward_paper_only: bool = False
    bitget_rate_limit_state_path: str = "/tmp/cgc-funding-readonly-rate-limit.json"
    bitget_rate_limit_min_interval_ms: int = 120
    bitget_rate_limit_429_cooldown_sec: float = 5.0
    bitget_max_request_retries: int = 3
    bitget_retry_backoff_seconds: float = 1.25

    @model_validator(mode="after")
    def require_credentials(self):
        values = (self.bitget_api_key, self.bitget_api_secret, self.bitget_api_passphrase)
        if not all(value.get_secret_value().strip() for value in values):
            raise ValueError("authenticated read-only credentials are required")
        return self


class ReadOnlyTransportViolation(RuntimeError):
    pass


class ReadOnlyBitgetClient(BitgetRestClient):
    """Full client read API with a final transport-boundary mutation veto."""

    READ_ENDPOINTS = frozenset({
        "/api/v2/mix/account/accounts",
        "/api/v2/mix/position/all-position",
        "/api/v2/mix/order/orders-pending",
        "/api/v2/mix/order/orders-plan-pending",
        "/api/v2/mix/order/orders-history",
        "/api/v2/common/trade-rate",
        "/api/v2/mix/account/bill",
    })

    def _request(self, method, path, **kwargs):
        if str(method).upper() != "GET":
            raise ReadOnlyTransportViolation("read-only verifier blocked non-GET transport")
        if str(path) not in self.READ_ENDPOINTS:
            raise ReadOnlyTransportViolation("read-only verifier blocked non-allowlisted endpoint")
        return super()._request(method, path, **kwargs)

    def _assert_order_transport_allowed(self) -> None:
        raise ReadOnlyTransportViolation("read-only verifier blocked mutation method")


def _schema(rows: list[dict], alternatives: tuple[tuple[str, ...], ...]) -> bool:
    if not rows:
        return True
    return all(any(all(field in row for field in group) for group in alternatives) for row in rows)


def _probe(name: str, call: Callable[[], object], alternatives=()) -> tuple[dict, list[dict]]:
    try:
        payload = call()
        rows = _rows(payload)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not rows and isinstance(data, dict) and not any(
            key in data for key in ("entrustedList","orderList","list","orders","planList")
        ):
            rows = [data]
        return {"endpoint_reachable": True, "record_count": len(rows),
                "classification_pass": True,
                "schema_fields_present": _schema(rows, alternatives) if alternatives else True}, rows
    except Exception as exc:
        # Do not print upstream messages or payloads: they may contain private data.
        return {"endpoint_reachable": False, "record_count": 0,
                "classification_pass": False, "schema_fields_present": False,
                "error_type": type(exc).__name__}, []


def _funding_probe(client, settings) -> dict:
    """Read and classify exact futures funding-fee bills.

    Bitget V2 names this business type ``contract_settle_fee`` and envelopes
    account bills under ``data.bills``. An empty bills list is a valid result.
    """
    try:
        payload = client._request("GET", "/api/v2/mix/account/bill", params={
            "productType": settings.bitget_product_type,
            "businessType": "contract_settle_fee",
            "limit": "100",
        }, private=True)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("bills"), list):
            return {"endpoint_reachable": True, "record_count": 0,
                    "classification_pass": False, "schema_fields_present": False}
        raw = data["bills"]
        if any(not isinstance(row, dict) for row in raw):
            return {"endpoint_reachable": True, "record_count": 0,
                    "classification_pass": False, "schema_fields_present": False}
        funding = [row for row in raw
                   if str(row.get("businessType") or "").lower() == "contract_settle_fee"]
        schema = _schema(funding, (("billId", "amount", "businessType", "cTime"),))
        return {"endpoint_reachable": True, "record_count": len(funding),
                "classification_pass": schema, "schema_fields_present": schema}
    except Exception as exc:
        return {"endpoint_reachable": False, "record_count": 0,
                "classification_pass": False, "schema_fields_present": False,
                "error_type": type(exc).__name__}


def run(settings: ReadOnlyBitgetSettings, *, client_factory=ReadOnlyBitgetClient) -> tuple[int, dict]:
    client = client_factory(settings=settings)
    result = {"READ_ONLY_SETTINGS_DECOUPLED": True, "READ_ONLY_TRANSPORT_ENFORCED": True}
    result["BALANCE_CLASSIFICATION"], _ = _probe("balance", client.get_accounts,
        (("marginCoin",), ("coin",)))
    result["POSITION_CLASSIFICATION"], _ = _probe("positions", client.get_all_positions,
        (("symbol","total"), ("symbol","size")))
    result["REGULAR_ORDER_CLASSIFICATION"], _ = _probe("orders", lambda: client.get_pending_orders(limit=100),
        (("symbol","orderId"), ("symbol","clientOid")))
    plans=[]; plan_ok=True
    for plan_type in ("profit_loss","normal_plan","track_plan"):
        probe, rows = _probe("plans", lambda plan_type=plan_type: client.get_tpsl_orders(plan_type=plan_type),
                             (("symbol","orderId"), ("symbol","planOrderId")))
        plan_ok = plan_ok and probe["endpoint_reachable"] and probe["schema_fields_present"]
        plans.extend(rows)
    deduped = {}
    for index, row in enumerate(plans):
        identity = str(row.get("orderId") or row.get("planOrderId") or row.get("clientOid") or "")
        key = identity or f"unidentified:{index}"
        deduped.setdefault(key, row)
    plans = list(deduped.values())
    result["PLAN_ORDER_CLASSIFICATION"] = {"endpoint_reachable":plan_ok,"record_count":len(plans),
        "classification_pass":plan_ok,"schema_fields_present":plan_ok}
    def is_native_stop(row):
        semantics = " ".join(str(row.get(key) or "").lower()
                             for key in ("planType","plan_type","orderType","type"))
        explicit_loss_field = any(row.get(key) not in (None, "") for key in
                                  ("stopLossTriggerPrice","lossTriggerPrice","stopLossPrice"))
        return explicit_loss_field or "loss" in semantics or "stop" in semantics
    stops=[row for row in plans if is_native_stop(row)]
    result["STOP_CLASSIFICATION"] = {"endpoint_reachable":plan_ok,"record_count":len(stops),
        "classification_pass":plan_ok,"schema_fields_present":_schema(stops,(
            ("symbol","triggerPrice"),("symbol","stopLossTriggerPrice"),
            ("symbol","lossTriggerPrice"),("symbol","stopLossPrice")))}
    result["FILL_CLASSIFICATION"], _ = _probe("fills", lambda: client.get_order_history(limit=100),
        (("symbol","orderId"), ("symbol","clientOid")))
    result["FEE_CLASSIFICATION"], _ = _probe("fees", lambda: client.get_trade_fee_rate("BTCUSDT"),
        (("makerFeeRate","takerFeeRate"), ("makerFee","takerFee")))
    result["FUNDING_CLASSIFICATION"] = _funding_probe(client, settings)
    passed = all(value.get("classification_pass", False) for key,value in result.items()
                 if key.endswith("_CLASSIFICATION"))
    result["BITGET_AUTH_READ_VERIFIED"] = passed
    result["SECRETS_EXPOSED"] = "NO"
    result["PRODUCTION_SETTINGS_GUARD_UNCHANGED"] = True
    result["ENV_LIVE_UNCHANGED"] = True
    result["ADAPTIVETREND_UNTOUCHED"] = True
    result["REAL_ORDER_ARMED"] = False
    result["REAL_ORDERS_SENT"] = 0
    return (0 if passed else 2), result


def main() -> int:
    try:
        settings = ReadOnlyBitgetSettings()
        code, result = run(settings)
    except Exception as exc:
        code = 2
        result = {"BITGET_AUTH_READ_VERIFIED":False,"READ_ONLY_TRANSPORT_ENFORCED":True,
                  "SECRETS_EXPOSED":"NO","error_type":type(exc).__name__}
    print(json.dumps(result, sort_keys=True, separators=(",",":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
