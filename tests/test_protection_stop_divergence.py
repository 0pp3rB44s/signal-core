"""A stop that exists is not a stop that protects.

Forensic case, BTCUSDT 2026-08-01 (sanitised from the live account):

    entry          62410.4      SHORT, size 0.0004
    entry stop     62665.0      pos_loss  1467517375415836672  live
    profit lock    62298.1      loss_plan 1467519838684409856  created 18:45:15,
                                                               cancelled 18:45:16
    take profit    62143.1      pos_profit 1467517375399059456 live

Entry protection installs a *position-level* pos_loss. `move_futures_stop_loss`
placed a `loss_plan` beside it; Bitget accepted the plan and cancelled it about a
second later because the position already carried a position-level stop. The
immediate read-back landed inside that window, returned verified=True, and local
state adopted 62298.1 while the exchange kept 62665.

From then on the position ran with a 366-point gap between what the bot believed
and what the exchange would execute, and every time price crossed the local stop
the failsafe was suppressed by a presence-only check — 697 times over three
hours. These tests pin each link of that chain.
"""

from __future__ import annotations

import pytest

from clients.bitget_tpsl_client import (
    POSITION_LEVEL_LOSS_TYPES,
    BitgetTPSLClientMixin,
)
from execution.position_model import PROTECTION_DIVERGED
from execution.tp_sl_lifecycle import TpSlLifecycleMixin

ENTRY = 62410.4
LOCAL_STOP = 62298.1
EXCHANGE_STOP = 62665.0
TAKE_PROFIT = 62143.1
SIZE = 0.0004


# ---------------------------------------------------------------- divergence

def test_local_and_exchange_stop_of_the_forensic_case_do_not_agree():
    assert TpSlLifecycleMixin._stops_agree(LOCAL_STOP, EXCHANGE_STOP) is False


def test_identical_stops_agree():
    assert TpSlLifecycleMixin._stops_agree(EXCHANGE_STOP, EXCHANGE_STOP) is True


def test_tick_rounding_still_agrees():
    """A one-tick formatting difference is not a divergence."""
    assert TpSlLifecycleMixin._stops_agree(62665.0, 62665.1) is True


def test_just_outside_tolerance_is_a_divergence():
    outside = EXCHANGE_STOP * (1 + TpSlLifecycleMixin.STOP_TICK_TOLERANCE_PCT * 2)
    assert TpSlLifecycleMixin._stops_agree(outside, EXCHANGE_STOP) is False


@pytest.mark.parametrize("missing", [None, "", 0, -1])
def test_unknown_exchange_stop_never_agrees(missing):
    """Without proof the two agree, suppression must not happen."""
    assert TpSlLifecycleMixin._stops_agree(LOCAL_STOP, missing) is False
    assert TpSlLifecycleMixin._stops_agree(missing, EXCHANGE_STOP) is False


def test_exchange_stop_price_never_falls_back_to_local_intent():
    """`stop_loss` is what the bot wants; it is not evidence of what Bitget holds."""
    position = {"stop_loss": LOCAL_STOP}
    assert TpSlLifecycleMixin._exchange_stop_price(position) is None

    position["exchange_stop_loss"] = EXCHANGE_STOP
    assert TpSlLifecycleMixin._exchange_stop_price(position) == EXCHANGE_STOP


def test_exchange_stop_price_prefers_exchange_field_over_confirmed():
    position = {"exchange_stop_loss": EXCHANGE_STOP, "confirmed_stop": 1.0}
    assert TpSlLifecycleMixin._exchange_stop_price(position) == EXCHANGE_STOP


def test_forensic_position_is_classified_diverged():
    """The exact live state: stop_loss 62298.1 against confirmed_stop 62665."""
    position = {
        "symbol": "BTCUSDT",
        "direction": "SHORT",
        "stop_loss": LOCAL_STOP,
        "exchange_stop_loss": EXCHANGE_STOP,
        "confirmed_stop": EXCHANGE_STOP,
        "protection_state": "PROFIT_LOCK_CONFIRMED",
    }
    exchange_stop = TpSlLifecycleMixin._exchange_stop_price(position)
    assert TpSlLifecycleMixin._stops_agree(position["stop_loss"], exchange_stop) is False
    assert PROTECTION_DIVERGED == "DIVERGED"


# ------------------------------------------------------- direction symmetry

@pytest.mark.parametrize(
    "direction,high,low,stop,hit",
    [
        # SHORT profit lock below entry: triggers when price rises through it.
        ("SHORT", 62315.3, 62299.1, LOCAL_STOP, True),
        ("SHORT", 62290.0, 62280.0, LOCAL_STOP, False),
        # LONG mirror image: triggers when price falls through it.
        ("LONG", 62520.0, 62290.0, LOCAL_STOP, True),
        ("LONG", 62520.0, 62310.0, LOCAL_STOP, False),
    ],
)
def test_stop_hit_is_direction_correct(direction, high, low, stop, hit):
    assert TpSlLifecycleMixin._stop_hit_range(direction, high, low, stop) is hit


def test_long_and_short_use_the_same_tolerance():
    assert TpSlLifecycleMixin.STOP_TICK_TOLERANCE_PCT == \
        BitgetTPSLClientMixin.STOP_TICK_TOLERANCE_PCT


# ------------------------------------------------------------- plan model

class _FakeClient(BitgetTPSLClientMixin):
    """Drives the real decision logic against scripted exchange responses."""

    def __init__(self, plans, *, place_result=None, plans_after=None):
        self._plans = plans
        self._plans_after = plans if plans_after is None else plans_after
        self._fetched = 0
        self.placed_position_tpsl = []
        self.placed_plan_orders = []
        self.cancelled = []
        self._place_result = place_result or {"code": "00000"}
        self.log = _NullLog()
        self.STOP_REPLACEMENT_SETTLE_SECONDS = 0.0

    def _fetch_tpsl_orders_broad(self, symbol, product_type=None):
        self._fetched += 1
        return (self._plans if self._fetched == 1 else self._plans_after), {}

    def place_position_tpsl(self, **kwargs):
        self.placed_position_tpsl.append(kwargs)
        return self._place_result

    def _format_trigger_price(self, symbol, price):
        return round(float(price), 1)


class _NullLog:
    def __getattr__(self, _name):
        return lambda *a, **k: None


def _pos_loss(trigger=EXCHANGE_STOP):
    return {"planType": "pos_loss", "triggerPrice": str(trigger),
            "orderId": "1467517375415836672", "posSide": "short", "size": "0"}


def _pos_profit(trigger=TAKE_PROFIT):
    return {"planType": "pos_profit", "triggerPrice": str(trigger),
            "orderId": "1467517375399059456", "posSide": "short", "size": "0"}


def test_position_level_stop_is_detected():
    client = _FakeClient([_pos_loss(), _pos_profit()])
    active = client._active_position_stop("BTCUSDT", "short", "USDT-FUTURES")
    assert active["plan_type"] == "pos_loss"
    assert active["plan_type"] in POSITION_LEVEL_LOSS_TYPES
    assert active["trigger_price"] == EXCHANGE_STOP


def test_take_profit_is_read_back_for_preservation():
    client = _FakeClient([_pos_loss(), _pos_profit()])
    assert client._active_position_take_profit("BTCUSDT", "short", "USDT-FUTURES") == TAKE_PROFIT


def test_move_on_a_pos_loss_position_uses_place_pos_tpsl_not_a_loss_plan():
    """The core fix: never place a competing plan beside a position-level stop."""
    after = [_pos_loss(trigger=LOCAL_STOP), _pos_profit()]
    client = _FakeClient([_pos_loss(), _pos_profit()], plans_after=after)

    result = client._move_position_level_stop(
        symbol="BTCUSDT", hold_side="short", formatted_trigger=LOCAL_STOP,
        product="USDT-FUTURES", margin_coin="USDT", margin_mode="isolated",
        existing={"plan_type": "pos_loss", "trigger_price": EXCHANGE_STOP},
        reason="PROFIT_LOCK_BE", result={},
    )

    assert client.placed_position_tpsl, "must use the position-level endpoint"
    assert client.placed_plan_orders == [], "must not create a loss_plan"
    assert result["verified"] is True
    assert result["preserved_take_profit"] == TAKE_PROFIT


def test_take_profit_is_preserved_across_the_move():
    after = [_pos_loss(trigger=LOCAL_STOP), _pos_profit()]
    client = _FakeClient([_pos_loss(), _pos_profit()], plans_after=after)
    client._move_position_level_stop(
        symbol="BTCUSDT", hold_side="short", formatted_trigger=LOCAL_STOP,
        product="USDT-FUTURES", margin_coin="USDT", margin_mode="isolated",
        existing={"plan_type": "pos_loss", "trigger_price": EXCHANGE_STOP},
        reason="PROFIT_LOCK_BE", result={},
    )
    assert client.placed_position_tpsl[0]["take_profit"] == TAKE_PROFIT
    assert client.placed_position_tpsl[0]["stop_loss"] == LOCAL_STOP


def test_move_aborts_rather_than_dropping_an_unknown_take_profit():
    """place-pos-tpsl sets both legs; sending it without the TP would drop it."""
    client = _FakeClient([_pos_loss()])  # no pos_profit present
    result = client._move_position_level_stop(
        symbol="BTCUSDT", hold_side="short", formatted_trigger=LOCAL_STOP,
        product="USDT-FUTURES", margin_coin="USDT", margin_mode="isolated",
        existing={"plan_type": "pos_loss", "trigger_price": EXCHANGE_STOP},
        reason="PROFIT_LOCK_BE", result={},
    )
    assert result["verified"] is False
    assert client.placed_position_tpsl == []
    assert "no_active_take_profit" in result["reason_detail"]


def test_replacement_that_vanishes_is_not_reported_verified():
    """The live failure: the new stop was gone one second after placement."""
    client = _FakeClient([_pos_loss(), _pos_profit()], plans_after=[_pos_profit()])
    result = client._move_position_level_stop(
        symbol="BTCUSDT", hold_side="short", formatted_trigger=LOCAL_STOP,
        product="USDT-FUTURES", margin_coin="USDT", margin_mode="isolated",
        existing={"plan_type": "pos_loss", "trigger_price": EXCHANGE_STOP},
        reason="PROFIT_LOCK_BE", result={},
    )
    assert result["verified"] is False
    assert result["reason_detail"] == "replacement_absent_after_settle"


def test_replacement_at_the_wrong_price_is_reported_diverged():
    """Exchange kept 62665 while we asked for 62298.1 — never call that confirmed."""
    client = _FakeClient([_pos_loss(), _pos_profit()],
                         plans_after=[_pos_loss(trigger=EXCHANGE_STOP), _pos_profit()])
    result = client._move_position_level_stop(
        symbol="BTCUSDT", hold_side="short", formatted_trigger=LOCAL_STOP,
        product="USDT-FUTURES", margin_coin="USDT", margin_mode="isolated",
        existing={"plan_type": "pos_loss", "trigger_price": EXCHANGE_STOP},
        reason="PROFIT_LOCK_BE", result={},
    )
    assert result["verified"] is False
    assert "divergence" in result["reason_detail"]
    assert result["drift"] == pytest.approx(abs(EXCHANGE_STOP - LOCAL_STOP))


def test_failed_placement_keeps_the_existing_stop():
    class _Failing(_FakeClient):
        def place_position_tpsl(self, **kwargs):
            raise RuntimeError("bitget rejected")

    client = _Failing([_pos_loss(), _pos_profit()])
    result = client._move_position_level_stop(
        symbol="BTCUSDT", hold_side="short", formatted_trigger=LOCAL_STOP,
        product="USDT-FUTURES", margin_coin="USDT", margin_mode="isolated",
        existing={"plan_type": "pos_loss", "trigger_price": EXCHANGE_STOP},
        reason="PROFIT_LOCK_BE", result={},
    )
    assert result["verified"] is False
    assert "place_pos_tpsl_failed" in result["reason_detail"]


def test_settle_delay_is_longer_than_the_observed_cancel_window():
    """Bitget cancelled the competing plan one second after accepting it."""
    assert BitgetTPSLClientMixin.STOP_REPLACEMENT_SETTLE_SECONDS >= 2.0


def test_presence_only_can_no_longer_pass():
    """A pos_loss at the wrong price is present, yet must not count as agreement."""
    present_but_wrong = {"exchange_stop_loss": EXCHANGE_STOP}
    assert TpSlLifecycleMixin._exchange_stop_price(present_but_wrong) == EXCHANGE_STOP
    assert TpSlLifecycleMixin._stops_agree(LOCAL_STOP, EXCHANGE_STOP) is False


# ------------------------------------------------------- multi-symbol safety

def test_two_symbols_do_not_share_stop_state():
    btc = {"symbol": "BTCUSDT", "exchange_stop_loss": EXCHANGE_STOP}
    xlm = {"symbol": "XLMUSDT", "exchange_stop_loss": 0.17029}
    assert TpSlLifecycleMixin._exchange_stop_price(btc) == EXCHANGE_STOP
    assert TpSlLifecycleMixin._exchange_stop_price(xlm) == 0.17029
    assert TpSlLifecycleMixin._stops_agree(0.17029, EXCHANGE_STOP) is False


def test_stop_lookup_is_scoped_to_hold_side():
    plans = [
        {"planType": "pos_loss", "triggerPrice": "1.0", "orderId": "long-side", "posSide": "long"},
        _pos_loss(),
    ]
    client = _FakeClient(plans)
    active = client._active_position_stop("BTCUSDT", "short", "USDT-FUTURES")
    assert active["order_id"] == "1467517375415836672"


def test_restart_recovery_reads_exchange_not_local():
    """After a restart the exchange stop is the only trustworthy figure."""
    recovered = {"symbol": "BTCUSDT", "stop_loss": LOCAL_STOP, "exchange_stop_loss": EXCHANGE_STOP}
    assert TpSlLifecycleMixin._exchange_stop_price(recovered) == EXCHANGE_STOP
    assert TpSlLifecycleMixin._stops_agree(recovered["stop_loss"],
                                           TpSlLifecycleMixin._exchange_stop_price(recovered)) is False
