"""Canonical Bitget V2 futures funding-bill read and classification."""
from __future__ import annotations

FUNDING_BILL_ENDPOINT = "/api/v2/mix/account/bill"
FUNDING_BUSINESS_TYPE = "contract_settle_fee"


class FundingBillSchemaError(RuntimeError):
    pass


def classify_funding_bills(payload: object) -> list[dict]:
    """Return only valid funding-settlement rows; an empty history is valid."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get("bills"), list):
        raise FundingBillSchemaError("funding bill response missing data.bills list")
    raw = data["bills"]
    if any(not isinstance(row, dict) for row in raw):
        raise FundingBillSchemaError("funding bill response contains non-object row")
    funding = [row for row in raw
               if str(row.get("businessType") or "").lower() == FUNDING_BUSINESS_TYPE]
    required = ("billId", "amount", "businessType", "cTime")
    if any(any(row.get(field) in (None, "") for field in required) for row in funding):
        raise FundingBillSchemaError("funding settlement row missing required schema")
    return funding


def fetch_funding_bills(client, *, product_type: str, limit: int = 100) -> list[dict]:
    """Execute the one canonical authenticated GET for futures funding bills."""
    payload = client._request("GET", FUNDING_BILL_ENDPOINT, params={
        "productType": str(product_type).upper(),
        "businessType": FUNDING_BUSINESS_TYPE,
        "limit": str(max(1, min(int(limit), 100))),
    }, private=True)
    return classify_funding_bills(payload)
