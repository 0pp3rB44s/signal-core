from types import SimpleNamespace

from clients.bitget_account_client import BitgetAccountClientMixin
from clients.bitget_order_client import BitgetOrderClientMixin


class _Account(BitgetAccountClientMixin):
    settings = SimpleNamespace(bitget_product_type="USDT-FUTURES")

    def _request(self, method, path, **kwargs):
        return {"method": method, "path": path, **kwargs}


def test_authenticated_fee_endpoint_contract():
    payload = _Account().get_trade_fee_rate("btcusdt")
    assert payload["method"] == "GET"
    assert payload["path"] == "/api/v2/common/trade-rate"
    assert payload["params"] == {"symbol": "BTCUSDT", "businessType": "mix"}
    assert payload["private"] is True


class _Orders(BitgetOrderClientMixin):
    settings = SimpleNamespace(
        bitget_product_type="USDT-FUTURES", bitget_margin_coin="USDT",
    )
    log = SimpleNamespace(warning=lambda *args, **kwargs: None)

    def _assert_order_transport_allowed(self):
        return None

    def _format_trigger_price(self, symbol, price):
        return price

    def _format_size(self, symbol, size):
        return size

    def _validate_futures_order_flags(self, body):
        return None

    def _request(self, method, path, **kwargs):
        return {"method": method, "path": path, **kwargs}


def test_limit_tp_is_post_only_close_with_lineage_and_no_blind_retry():
    payload = _Orders().place_futures_limit_close_order(
        symbol="BTCUSDT", hold_side="long", size=0.001, price=100_000,
        client_oid="dgv1-btc-l1-tp", post_only=True,
    )
    body = payload["body"]
    assert body["tradeSide"] == "close"
    assert body["reduceOnly"] == "YES"
    assert body["orderType"] == "limit"
    assert body["force"] == "post_only"
    assert body["clientOid"] == "dgv1-btc-l1-tp"
    assert payload["allow_blind_retry"] is False
