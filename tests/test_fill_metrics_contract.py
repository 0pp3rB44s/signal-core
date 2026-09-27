"""Contract-test voor fill-metrics sleutels (N8).

execution_service las jarenlang aliassen (avg_fill_price/fee_paid/
realized_pnl/raw_order_state) die extract_fill_metrics nooit heeft
geproduceerd — elke fill viel daardoor stil terug op het plan-gemiddelde en
slippage stond altijd op 0.0000. Deze tests pinnen het contract vast aan
beide kanten.
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

from clients.bitget_rest import BitgetRestClient

REPO = Path(__file__).resolve().parents[1]
CANONICAL_KEYS = {"order_id", "avg_price", "filled_qty", "fee", "pnl", "state"}
DEAD_ALIASES = {"avg_fill_price", "fee_paid", "realized_pnl_key", "raw_order_state"}


def _client() -> BitgetRestClient:
    client = BitgetRestClient.__new__(BitgetRestClient)
    client.settings = MagicMock()
    return client


def test_extractor_produces_canonical_keys_from_v2_order_detail():
    payload = {
        "data": {
            "orderId": "12345",
            "priceAvg": "0.33109",
            "baseVolume": "63.0",
            "fee": "-0.0125",
            "totalProfits": "0",
            "state": "filled",
        }
    }
    metrics = _client().extract_fill_metrics(payload)
    assert CANONICAL_KEYS.issubset(metrics.keys())
    assert metrics["avg_price"] == 0.33109
    assert metrics["state"] == "filled"
    assert abs(metrics["fee"]) == 0.0125


def test_extractor_handles_list_payload_and_missing_data():
    metrics = _client().extract_fill_metrics({"data": []})
    assert metrics["avg_price"] == 0.0
    assert metrics["state"] == ""
    metrics = _client().extract_fill_metrics({})
    assert metrics["avg_price"] == 0.0


def test_execution_service_reads_only_keys_the_extractor_produces():
    source = (REPO / "execution" / "execution_service.py").read_text()
    read_keys = set(re.findall(r"fill_metrics\.get\(\"(\w+)\"", source))
    read_keys |= set(re.findall(r"detailed_fill_metrics\.get\(\"(\w+)\"", source))
    unknown = read_keys - CANONICAL_KEYS
    assert not unknown, (
        f"execution_service leest sleutels die extract_fill_metrics nooit produceert: {unknown} "
        "— dit was exact de N8-bug (fill-truth dood door naam-drift)"
    )
