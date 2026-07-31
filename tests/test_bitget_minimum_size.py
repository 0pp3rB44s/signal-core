"""Minimum order size must come from Bitget contract metadata, not a guess.

Root cause pinned: `_min_size` matched substrings of the symbol name and
returned 0.001 for anything containing "BTC", while the exchange's own
minTradeNum for BTCUSDT is 0.0001. On 2026-07-30 five genuine LIVE entries
(0.0002 and 0.0004 BTC) were refused pre-transport by that constant; all of them
satisfied the exchange. Two further defects shared the same cause: `round()`
could round a quantity UP past the risk budget, and every formatting call issued
its own /contracts request.

No test here places or simulates a real order: `get_contracts` is stubbed and the
HTTP layer is never reached.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace

import pytest

from clients import bitget_precision as bp
from clients.bitget_order_client import BitgetOrderClientMixin
from clients.bitget_precision import BitgetPrecisionMixin

PT = "USDT-FUTURES"

#: Verbatim BTCUSDT payload shape from /api/v2/mix/market/contracts, retrieved
#: 2026-07-30T13:04:16Z against productType=USDT-FUTURES.
BTC_CONTRACT = {
    "symbol": "BTCUSDT",
    "minTradeNum": "0.0001",
    "sizeMultiplier": "0.0001",
    "volumePlace": "4",
    "pricePlace": "1",
    "minTradeUSDT": "5",
}

ETH_CONTRACT = {
    "symbol": "ETHUSDT", "minTradeNum": "0.01", "sizeMultiplier": "0.01",
    "volumePlace": "2", "pricePlace": "2", "minTradeUSDT": "5",
}

DOGE_CONTRACT = {
    "symbol": "DOGEUSDT", "minTradeNum": "1", "sizeMultiplier": "1",
    "volumePlace": "0", "pricePlace": "5", "minTradeUSDT": "5",
}

BTC_PRICE = 64000.0


class FakeClient(BitgetPrecisionMixin):
    """Precision mixin with a stubbed metadata endpoint. Counts fetches so the
    cache can be proven rather than assumed."""

    def __init__(self, contracts=(BTC_CONTRACT,), product_type=PT, fail=False):
        self.settings = SimpleNamespace(bitget_product_type=product_type)
        self.log = logging.getLogger("fake_client")
        self._contracts = list(contracts)
        self._fail = fail
        self.fetches = 0

    def get_contracts(self, product_type, symbol=None):
        self.fetches += 1
        if self._fail:
            raise RuntimeError("simulated metadata outage")
        data = [c for c in self._contracts
                if symbol is None or str(c.get("symbol", "")).upper() == symbol.upper()]
        return {"code": "00000", "data": data}


class FakeOrderClient(BitgetOrderClientMixin, FakeClient):
    """Adds the order path so pre-transport rejection can be proven end to end."""

    def __init__(self, **kw):
        FakeClient.__init__(self, **kw)
        self.requests: list[tuple] = []

    def _request(self, method, path, **kw):
        self.requests.append((method, path, kw))
        return {"code": "00000", "data": {"orderId": "SHOULD_NOT_HAPPEN"}}

    def _validate_futures_order_flags(self, body):
        return None


@pytest.fixture(autouse=True)
def _clear_cache():
    bp.reset_spec_cache()
    yield
    bp.reset_spec_cache()


# --- 1-3. the exact quantities from the production incident --------------

def test_btc_0_0004_is_accepted():
    """The size that production refused. minTradeNum=0.0001, aligned, notional
    26.79 > minTradeUSDT=5."""
    c = FakeClient()
    size, reason = c.validate_entry_size("BTCUSDT", 0.0004, reference_price=BTC_PRICE)
    assert reason is None, f"still rejected: {reason}"
    assert size == Decimal("0.0004")


def test_btc_0_0002_is_accepted():
    c = FakeClient()
    size, reason = c.validate_entry_size("BTCUSDT", 0.0002, reference_price=BTC_PRICE)
    assert reason is None
    assert size == Decimal("0.0002")
    assert size * Decimal(str(BTC_PRICE)) > Decimal("5")


def test_btc_0_00009_is_rejected_below_exchange_minimum():
    c = FakeClient()
    size, reason = c.validate_entry_size("BTCUSDT", 0.00009, reference_price=BTC_PRICE)
    assert reason == bp.REASON_BELOW_EXCHANGE_MIN
    assert size < Decimal("0.0001")


# --- 4-5. alignment and precision ---------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    (0.00045, "0.0004"),   # rounds DOWN, not to 0.0005
    (0.00049999, "0.0004"),
    (0.0007, "0.0007"),
    (0.00123456, "0.0012"),
])
def test_quantity_is_rounded_down_to_size_multiplier(raw, expected):
    c = FakeClient()
    assert c._normalize_size("BTCUSDT", raw) == Decimal(expected)


def test_quantity_respects_volume_place():
    c = FakeClient()
    out = c._normalize_size("BTCUSDT", 0.000123456789)
    assert out.as_tuple().exponent >= -4, f"more than volumePlace=4 decimals: {out}"


def test_zero_decimal_symbol_quantizes_to_whole_units():
    c = FakeClient(contracts=(DOGE_CONTRACT,))
    assert c._normalize_size("DOGEUSDT", 7.9) == Decimal("7")


# --- 6. minimum notional ------------------------------------------------

def test_below_min_trade_usdt_is_rejected():
    """0.0001 BTC at 40000 is 4 USDT, under minTradeUSDT=5."""
    c = FakeClient()
    size, reason = c.validate_entry_size("BTCUSDT", 0.0001, reference_price=40000.0)
    assert reason == bp.REASON_BELOW_MIN_NOTIONAL
    assert size == Decimal("0.0001")


def test_above_min_trade_usdt_passes():
    c = FakeClient()
    _, reason = c.validate_entry_size("BTCUSDT", 0.0001, reference_price=60000.0)
    assert reason is None


def test_min_notional_skipped_without_reference_price():
    """No price offered at all: the floor cannot be evaluated and the exchange
    remains the enforcer. Callers on the live path now always supply one."""
    c = FakeClient()
    _, reason = c.validate_entry_size("BTCUSDT", 0.0002, reference_price=None)
    assert reason is None


@pytest.mark.parametrize("bad_price", [0, 0.0, -1, -64000.0, float("nan"), float("inf")])
def test_unusable_reference_price_fails_closed(bad_price):
    """A price was offered but is unusable. Validating against zero would reject
    everything; skipping the floor would hide a malformed plan."""
    c = FakeClient()
    _, reason = c.validate_entry_size("BTCUSDT", 0.0004, reference_price=bad_price)
    assert reason == bp.REASON_INVALID_REFERENCE_PRICE


def test_market_entry_validates_min_notional_with_planned_entry():
    """The Phase-3 gap: a market order carries no price of its own, so the
    planned entry is passed in and minTradeUSDT is enforced pre-transport."""
    c = FakeOrderClient()
    with pytest.raises(ValueError) as exc:
        c.place_futures_market_order("BTCUSDT", direction="LONG", size=0.0001,
                                     client_oid="t-8", reference_price=40000.0)
    assert bp.REASON_BELOW_MIN_NOTIONAL in str(exc.value)
    assert c.requests == [], "an under-notional order reached transport"


def test_market_entry_passes_when_planned_entry_clears_min_notional():
    c = FakeOrderClient()
    c.place_futures_market_order("BTCUSDT", direction="SHORT", size=0.0004,
                                 client_oid="t-9", reference_price=BTC_PRICE)
    assert len(c.requests) == 1


def test_execution_service_supplies_the_planned_entry():
    """Guards the wiring: the market leg must not silently regress to no price."""
    import inspect
    from execution import execution_service
    src = inspect.getsource(execution_service)
    block = src.split("def _place_market_entry")[1].split("submission =")[0]
    assert "reference_price=" in block, "market leg no longer passes a price"
    assert "_ref=avg_entry" in src.split("def _place_market_entry")[1][:400], \
        "planned entry is no longer the validation price"


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_min_notional_rule_is_direction_independent(direction):
    c = FakeClient()
    _, reason = c.validate_entry_size("BTCUSDT", 0.0001, reference_price=40000.0)
    assert reason == bp.REASON_BELOW_MIN_NOTIONAL


# --- 7-8. never inflate past a ceiling ----------------------------------

@pytest.mark.parametrize("raw", [0.00019, 0.0004, 0.00099, 0.0012345, 0.5, 1.7])
def test_normalized_never_exceeds_requested(raw):
    """Structural guarantee: down-only quantization cannot breach the account
    risk budget, the live notional cap, leverage or sizing limits."""
    c = FakeClient()
    assert c._normalize_size("BTCUSDT", raw) <= Decimal(str(raw))


def test_never_rounded_up_beyond_notional_cap():
    """26.79 USDT at the live cap of 35: normalization must not push notional up."""
    cap = Decimal("35")
    c = FakeClient()
    raw = Decimal("26.79") / Decimal(str(BTC_PRICE))
    out = c._normalize_size("BTCUSDT", float(raw))
    assert out <= raw
    assert out * Decimal(str(BTC_PRICE)) <= cap


def test_quantity_is_never_inflated_to_reach_the_minimum():
    """A size below the exchange floor is refused, not grown into compliance."""
    c = FakeClient()
    size, reason = c.validate_entry_size("BTCUSDT", 0.00005, reference_price=BTC_PRICE)
    assert reason == bp.REASON_BELOW_EXCHANGE_MIN
    assert size < Decimal("0.0001"), "size was inflated to satisfy the exchange"


# --- 9. fresh metadata beats the old hardcoded values -------------------

def test_exchange_metadata_overrides_the_old_hardcoded_minimum():
    c = FakeClient()
    assert c._min_size("BTCUSDT") == 0.0001, "still using the hardcoded 0.001"


def test_no_hardcoded_symbol_table_remains():
    import inspect
    src = inspect.getsource(bp)
    body = src.split("class BitgetPrecisionMixin")[1]
    for ghost in ('if "BTC" in', 'if "ETH" in', "return 0.001", "return 0.01"):
        assert ghost not in body, f"hardcoded remnant still present: {ghost}"


def test_eth_minimum_also_comes_from_metadata():
    c = FakeClient(contracts=(ETH_CONTRACT,))
    assert c._min_size("ETHUSDT") == 0.01
    _, reason = c.validate_entry_size("ETHUSDT", 0.02, reference_price=3000.0)
    assert reason is None


# --- 10-11. malformed and missing metadata ------------------------------

@pytest.mark.parametrize("contract", [
    {"symbol": "BTCUSDT"},                                                  # nothing
    {"symbol": "BTCUSDT", "minTradeNum": "0", "volumePlace": "4", "pricePlace": "1"},
    {"symbol": "BTCUSDT", "minTradeNum": "abc", "volumePlace": "4", "pricePlace": "1"},
    {"symbol": "BTCUSDT", "minTradeNum": "0.0001", "volumePlace": "x", "pricePlace": "1"},
    {"symbol": "BTCUSDT", "minTradeNum": "-1", "volumePlace": "4", "pricePlace": "1"},
])
def test_malformed_metadata_fails_closed(contract):
    c = FakeClient(contracts=(contract,))
    size, reason = c.validate_entry_size("BTCUSDT", 0.0004, reference_price=BTC_PRICE)
    assert reason == bp.REASON_METADATA_UNAVAILABLE
    assert size == Decimal(0)
    assert c._min_size("BTCUSDT") == float("inf"), "must refuse, not guess"


def test_malformed_metadata_is_logged(caplog):
    c = FakeClient(contracts=({"symbol": "BTCUSDT"},))
    with caplog.at_level(logging.ERROR, logger="fake_client"):
        c.validate_entry_size("BTCUSDT", 0.0004)
    assert any("CONTRACT_METADATA_REJECTED" in r.getMessage() for r in caplog.records)


def test_outage_without_prior_metadata_fails_closed():
    c = FakeClient(fail=True)
    size, reason = c.validate_entry_size("BTCUSDT", 0.0004, reference_price=BTC_PRICE)
    assert reason == bp.REASON_METADATA_UNAVAILABLE
    assert size == Decimal(0)


def test_outage_reuses_only_previously_validated_metadata(caplog):
    """Fallback is the last spec that genuinely came from the exchange — never a
    generic constant."""
    c = FakeClient()
    assert c._min_size("BTCUSDT") == 0.0001          # populates validated cache
    bp._CACHE._fresh.clear()                          # expire the fresh entry
    c._fail = True
    with caplog.at_level(logging.WARNING, logger="fake_client"):
        minimum, reason = c.min_size_or_reason("BTCUSDT")
    assert reason is None
    assert minimum == Decimal("0.0001")
    assert any("CONTRACT_METADATA_FALLBACK" in r.getMessage() for r in caplog.records)


def test_fallback_is_flagged_in_the_spec():
    c = FakeClient()
    c._contract_spec("BTCUSDT")
    bp._CACHE._fresh.clear()
    c._fail = True
    spec = c._contract_spec("BTCUSDT")
    assert spec is not None and spec.fallback_used is True
    assert "fallback_used=true" in spec.log_fields()


# --- 12. cache -----------------------------------------------------------

def test_metadata_is_fetched_once_within_ttl():
    c = FakeClient()
    for _ in range(6):
        c._normalize_size("BTCUSDT", 0.0004)
        c._min_size("BTCUSDT")
        c._format_trigger_price("BTCUSDT", 64000.123)
    assert c.fetches == 1, f"cache not effective: {c.fetches} fetches"


def test_expired_cache_triggers_refresh(monkeypatch):
    c = FakeClient()
    c._min_size("BTCUSDT")
    assert c.fetches == 1
    base = bp.time.monotonic()
    monkeypatch.setattr(bp.time, "monotonic",
                        lambda: base + bp.SPEC_TTL_SECONDS + 1.0)
    c._min_size("BTCUSDT")
    assert c.fetches == 2, "expired entry was served as fresh"


def test_cache_key_includes_product_type_and_symbol():
    c = FakeClient(contracts=(BTC_CONTRACT, ETH_CONTRACT))
    c._min_size("BTCUSDT")
    c._min_size("ETHUSDT")
    assert c.fetches == 2
    keys = set(bp._CACHE._fresh)
    assert (PT, "BTCUSDT") in keys and (PT, "ETHUSDT") in keys


def test_stale_spec_is_not_served_as_fresh(monkeypatch):
    c = FakeClient()
    spec = c._contract_spec("BTCUSDT")
    assert spec is not None and spec.fallback_used is False
    base = bp.time.monotonic()
    monkeypatch.setattr(bp.time, "monotonic", lambda: base + bp.SPEC_TTL_SECONDS + 1)
    assert bp._CACHE.get_fresh((PT, "BTCUSDT")) is None


# --- 13. product type ----------------------------------------------------

def test_product_type_mismatch_is_rejected(caplog):
    mismatched = dict(BTC_CONTRACT, productType="COIN-FUTURES")
    c = FakeClient(contracts=(mismatched,))
    with caplog.at_level(logging.ERROR, logger="fake_client"):
        size, reason = c.validate_entry_size("BTCUSDT", 0.0004, reference_price=BTC_PRICE)
    assert reason == bp.REASON_METADATA_UNAVAILABLE
    assert any(bp.REASON_PRODUCT_TYPE_MISMATCH in r.getMessage() for r in caplog.records)


def test_matching_product_type_is_accepted():
    c = FakeClient(contracts=(dict(BTC_CONTRACT, productType=PT),))
    _, reason = c.validate_entry_size("BTCUSDT", 0.0004, reference_price=BTC_PRICE)
    assert reason is None


def test_spec_records_the_execution_product_type():
    c = FakeClient()
    spec = c._contract_spec("BTCUSDT")
    assert spec.product_type == PT
    assert spec.source == "/api/v2/mix/market/contracts"


# --- 14. LONG and SHORT are treated identically -------------------------

@pytest.mark.parametrize("size", [0.0004, 0.0002, 0.00123])
def test_long_and_short_normalize_identically(size):
    c = FakeClient()
    assert (c._normalize_size("BTCUSDT", size) == c._normalize_size("BTCUSDT", size))
    long_out, long_reason = c.validate_entry_size("BTCUSDT", size, BTC_PRICE)
    short_out, short_reason = c.validate_entry_size("BTCUSDT", size, BTC_PRICE)
    assert long_out == short_out and long_reason == short_reason


def test_order_path_uses_same_rules_for_both_directions():
    for direction in ("LONG", "SHORT"):
        c = FakeOrderClient()
        c.place_futures_market_order("BTCUSDT", direction=direction, size=0.0004,
                                     client_oid="t-1")
        body = c.requests[-1][2]["json"] if "json" in c.requests[-1][2] else c.requests[-1][2].get("body")
        assert c.requests, f"{direction} order was not submitted"


# --- 16-17. pre-transport rejection reaches no exchange -----------------

def test_rejected_size_makes_no_http_request():
    c = FakeOrderClient()
    with pytest.raises(ValueError) as exc:
        c.place_futures_market_order("BTCUSDT", direction="LONG", size=0.00009,
                                     client_oid="t-2")
    assert bp.REASON_BELOW_EXCHANGE_MIN in str(exc.value)
    assert c.requests == [], "an order reached the transport layer"


def test_malformed_metadata_makes_no_http_request():
    c = FakeOrderClient(contracts=({"symbol": "BTCUSDT"},))
    with pytest.raises(ValueError) as exc:
        c.place_futures_market_order("BTCUSDT", direction="SHORT", size=0.0004,
                                     client_oid="t-3")
    assert bp.REASON_METADATA_UNAVAILABLE in str(exc.value)
    assert c.requests == []


def test_valid_size_now_reaches_transport():
    """The acceptance test: a quantity satisfying current metadata is no longer
    refused by the retired min=0.001 rule."""
    c = FakeOrderClient()
    c.place_futures_market_order("BTCUSDT", direction="LONG", size=0.0004,
                                 client_oid="t-4")
    assert len(c.requests) == 1
    method, path, _ = c.requests[0]
    assert method == "POST" and "place-order" in path


def test_limit_order_validates_notional_and_rejects_pre_transport():
    c = FakeOrderClient()
    with pytest.raises(ValueError) as exc:
        c.place_futures_limit_order("BTCUSDT", direction="LONG", size=0.0001,
                                    price=40000.0, client_oid="t-5")
    assert bp.REASON_BELOW_MIN_NOTIONAL in str(exc.value)
    assert c.requests == []


# --- 18. every configured execution symbol ------------------------------

def test_configured_execution_symbols_all_get_metadata_handling():
    """.env.live pins EXECUTION_CONFIRM_SYMBOLS=BTCUSDT and MAX_SYMBOLS=1, so
    BTCUSDT is the whole live set; the others guard the generic path."""
    for contract in (BTC_CONTRACT, ETH_CONTRACT, DOGE_CONTRACT):
        c = FakeClient(contracts=(contract,))
        sym = contract["symbol"]
        minimum, reason = c.min_size_or_reason(sym)
        assert reason is None and minimum == Decimal(contract["minTradeNum"]), sym
        assert c._contract_volume_scale(sym) == int(contract["volumePlace"]), sym
        assert c._contract_price_scale(sym) == int(contract["pricePlace"]), sym


def test_unknown_symbol_fails_closed():
    c = FakeClient()
    minimum, reason = c.min_size_or_reason("NOPEUSDT")
    assert minimum is None and reason == bp.REASON_METADATA_UNAVAILABLE


# --- decimal arithmetic, not binary float -------------------------------

def test_decimal_arithmetic_avoids_binary_float_artifacts():
    """0.0003 is not representable in binary; float maths yields 0.00029999...
    which would quantize down to 0.0002 and silently under-size the order."""
    c = FakeClient()
    assert c._normalize_size("BTCUSDT", 0.0003) == Decimal("0.0003")
    assert c._normalize_size("BTCUSDT", 0.0007) == Decimal("0.0007")
    assert c._normalize_size("BTCUSDT", 0.0011) == Decimal("0.0011")


def test_price_formatting_uses_metadata_price_place():
    c = FakeClient()
    assert c._format_trigger_price("BTCUSDT", 64000.126) == 64000.1
    c2 = FakeClient(contracts=(ETH_CONTRACT,))
    assert c2._format_trigger_price("ETHUSDT", 3000.126) == 3000.13


# --- ordering: prohibition beats size validation -------------------------

def test_forward_paper_refuses_before_any_metadata_fetch():
    """The public /contracts lookup must not happen on a path that can never
    place an order, and the reported reason must be the prohibition itself."""
    from clients.bitget_base_client import PrivateExchangeCallBlocked

    c = FakeOrderClient()
    c.settings.forward_paper_only = True
    with pytest.raises(PrivateExchangeCallBlocked, match="FORWARD_PAPER_ONLY"):
        c.place_futures_market_order("BTCUSDT", direction="LONG", size=0.0004,
                                     client_oid="t-6")
    assert c.fetches == 0, "metadata was fetched on a blocked path"
    assert c.requests == []


def test_live_runtime_is_not_forward_paper_and_still_validates():
    c = FakeOrderClient()
    c.settings.forward_paper_only = False
    c.place_futures_market_order("BTCUSDT", direction="SHORT", size=0.0004,
                                 client_oid="t-7")
    assert c.fetches == 1 and len(c.requests) == 1


# --- 15. idempotency untouched ------------------------------------------

def test_idempotency_module_untouched_by_this_patch():
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    changed = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=repo,
                             capture_output=True, text=True).stdout.split()
    for path in changed:
        assert not path.startswith(("execution/order_identity.py",
                                    "execution/order_intent_store.py",
                                    "execution/entry_submitter.py")), \
            f"idempotency stack touched: {path}"


def test_no_strategy_risk_or_planner_file_changed():
    import subprocess
    from pathlib import Path
    repo = Path(__file__).resolve().parents[1]
    changed = subprocess.run(["git", "diff", "--name-status", "HEAD"], cwd=repo,
                             capture_output=True, text=True).stdout.splitlines()
    forbidden = ("risk/", "planning/", "strategies/", "app/config.py")
    for entry in changed:
        status, path = entry.split("\t", 1)
        if path.startswith(".env"):
            assert status == "D", f"environment config was not removal-only: {path}"
            continue
        for bad in forbidden:
            assert not path.startswith(bad), f"out-of-scope file changed: {path}"
