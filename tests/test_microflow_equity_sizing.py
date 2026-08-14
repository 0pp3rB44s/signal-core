"""Equity-based, portfolio-aware MicroFlow sizing.

The fixed 35 USDT notional cap was removed on 2026-08-14 by owner request. The
account balance is now the sizing basis. Two properties matter more than the
arithmetic and are pinned here:

* **Slot fairness.** Filling position 1 must never make position 2 unfundable.
* **Fail closed.** Every bad input — no equity, no free margin, a loss above the
  configured ceiling — must raise, never quietly return a smaller trade. A silent
  shrink hides a sizing bug behind a plausible-looking order.

Leverage is a margin multiplier here and nothing else. It does not appear in any
risk bound, and no test asserts that raising it improves anything.
"""

from __future__ import annotations

import pytest

from microflow.live import MicroflowSizing, size_microflow_position

EQUITY = 46.52184608
TAKER = 0.0006  # 6 bps per side -> 12 bps round trip
SLIP = 1.0
#: SL 20 + slippage 1 + fee 12 = 33 bps of notional lost when the stop is hit.
LOSS_FRACTION = (20.0 + SLIP + TAKER * 20_000.0) / 10_000.0


def size(**over) -> MicroflowSizing:
    kwargs = dict(
        equity_usdt=EQUITY, available_usdt=EQUITY, committed_margin_usdt=0.0,
        leverage=10.0, taker_fee_rate=TAKER, slippage_bps=SLIP,
        margin_reserve_pct=10.0, max_notional_pct_equity=500.0,
        max_loss_pct_equity=2.0, max_open_positions=2,
    )
    kwargs.update(over)
    return size_microflow_position(**kwargs)


# --- the owner-approved model -----------------------------------------------


def test_margin_slot_is_half_the_usable_balance(tmp_path=None):
    s = size()
    assert s.margin_per_slot_usdt == pytest.approx(EQUITY * 0.90 / 2, rel=1e-9)
    assert s.margin_usdt == pytest.approx(EQUITY * 0.90 / 2, rel=1e-9)


def test_notional_is_margin_times_leverage(tmp_path=None):
    s = size()
    assert s.notional_usdt == pytest.approx(EQUITY * 0.90 / 2 * 10.0, rel=1e-9)
    assert s.binding_constraint == "margin_slot"


def test_the_fixed_35_usdt_cap_is_gone(tmp_path=None):
    """The old ceiling would have pinned this at 35."""
    assert size().notional_usdt > 200.0


def test_loss_at_stop_matches_the_reported_percentage(tmp_path=None):
    s = size()
    assert s.total_loss_usdt == pytest.approx(s.notional_usdt * LOSS_FRACTION, rel=1e-7)
    assert s.total_loss_pct_equity == pytest.approx(s.total_loss_usdt / EQUITY * 100, rel=1e-7)


def test_sizing_scales_with_equity_not_a_constant(tmp_path=None):
    """Double the balance, double the trade — this is what 'equity-based' means."""
    small = size(equity_usdt=EQUITY, available_usdt=EQUITY)
    big = size(equity_usdt=EQUITY * 2, available_usdt=EQUITY * 2)
    assert big.notional_usdt == pytest.approx(small.notional_usdt * 2, rel=1e-9)


# --- two positions must both fit --------------------------------------------


def test_second_position_is_still_fundable_after_the_first(tmp_path=None):
    first = size()
    # The first position's margin is now committed; available drops by that much.
    remaining = EQUITY - first.margin_usdt
    second = size(available_usdt=remaining, committed_margin_usdt=0.0)
    assert second.notional_usdt > 0
    assert first.margin_usdt + second.margin_usdt <= EQUITY, "two positions cannot exceed the balance"


def test_committed_margin_reduces_the_next_position(tmp_path=None):
    free = size(committed_margin_usdt=0.0)
    busy = size(committed_margin_usdt=EQUITY * 0.80)
    assert busy.notional_usdt < free.notional_usdt


def test_no_free_margin_fails_closed(tmp_path=None):
    """Everything committed must raise, not return a zero-size order."""
    with pytest.raises(ValueError, match="below the exchange minimum"):
        size(committed_margin_usdt=EQUITY)


def test_balance_too_small_to_trade_fails_closed(tmp_path=None):
    with pytest.raises(ValueError, match="below the exchange minimum"):
        size(equity_usdt=0.5, available_usdt=0.5)


def test_notional_just_above_the_minimum_is_allowed(tmp_path=None):
    s = size(equity_usdt=2.0, available_usdt=2.0)
    assert s.notional_usdt >= 5.0


def test_one_slot_would_double_the_trade_but_the_ceiling_catches_it(tmp_path=None):
    """With a single slot the margin model wants 2x, and the safety net binds first.

    This is the ceiling doing its job: 500% of equity is below what one slot at 10x
    would otherwise commit, so the cap is not decorative.
    """
    one = size(max_open_positions=1)
    assert one.binding_constraint == "notional_pct_equity"
    assert one.notional_usdt == pytest.approx(EQUITY * 5.0, rel=1e-7)
    assert one.notional_usdt < size().notional_usdt * 2


# --- the safety net ----------------------------------------------------------


def test_notional_ceiling_binds_when_margin_would_exceed_it(tmp_path=None):
    s = size(max_notional_pct_equity=100.0)
    assert s.notional_usdt == pytest.approx(EQUITY, rel=1e-9)
    assert s.binding_constraint == "notional_pct_equity"


def test_loss_ceiling_refuses_rather_than_shrinking(tmp_path=None):
    """A trade that would risk more than allowed is refused outright."""
    with pytest.raises(ValueError, match="exceeds"):
        size(max_loss_pct_equity=0.5)


def test_reserve_is_actually_held_back(tmp_path=None):
    assert size(margin_reserve_pct=0.0).margin_usdt > size(margin_reserve_pct=10.0).margin_usdt
    assert size(margin_reserve_pct=10.0).margin_usdt == pytest.approx(EQUITY * 0.9 / 2, rel=1e-9)


# --- fail closed on bad input ------------------------------------------------


@pytest.mark.parametrize("bad", [
    {"equity_usdt": 0.0},
    {"equity_usdt": -1.0},
    {"leverage": 0.0},
    {"taker_fee_rate": 0.0},
    {"available_usdt": -1.0},
    {"committed_margin_usdt": -1.0},
    {"max_open_positions": 0},
    {"margin_reserve_pct": 100.0},
    {"margin_reserve_pct": -1.0},
    {"max_notional_pct_equity": 0.0},
    {"max_loss_pct_equity": 0.0},
])
def test_invalid_inputs_raise(bad):
    with pytest.raises(ValueError):
        size(**bad)


# --- leverage is a multiplier, not a risk control ----------------------------


def test_leverage_multiplies_notional_and_therefore_loss(tmp_path=None):
    """Stated explicitly so nobody reads higher leverage as lower risk."""
    low, high = size(leverage=3.0), size(leverage=10.0)
    assert high.notional_usdt == pytest.approx(low.notional_usdt / 3.0 * 10.0, rel=1e-9)
    assert high.total_loss_usdt > low.total_loss_usdt
    assert high.margin_usdt == pytest.approx(low.margin_usdt, rel=1e-9), \
        "margin committed is the same; only exposure changes"
