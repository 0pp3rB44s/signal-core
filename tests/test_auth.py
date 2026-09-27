from clients.bitget_auth import build_query_string, compact_json


def test_build_query_string_orders_keys_as_given():
    query = build_query_string({"symbol": "BTCUSDT", "limit": 100})
    assert "symbol=BTCUSDT" in query
    assert "limit=100" in query


def test_compact_json_has_no_spaces():
    payload = compact_json({"a": 1, "b": "x"})
    assert payload == '{"a":1,"b":"x"}'
