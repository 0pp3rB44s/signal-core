"""The capital feasibility table: the frozen sizing at each capital, never an upsize, never a recommendation."""
from __future__ import annotations

import json

import pytest

from lsrl.capital import CAPITAL_MAX_USDT, CAPITAL_MIN_USDT, RISK_FRACTION
from lsrl.tools import capital_feasibility as cf


def test_every_row_risks_no_more_than_the_frozen_fraction_of_its_nav_and_never_rounds_up():
    t = cf.table()
    assert t["NEVER_INCREASES_RISK"] is True
    for r in t["rows"]:
        if r["status"] == "TRADE":
            assert r["planned_loss_usdt"] <= r["nav_usdt"] * RISK_FRACTION + 1e-9
            assert r["rung_qty"] >= cf.SOL_RULES.min_qty and abs(r["rung_qty"] / cf.SOL_RULES.qty_step - round(r["rung_qty"] / cf.SOL_RULES.qty_step)) < 1e-9


def test_a_setup_the_budget_cannot_fund_is_no_trade_not_a_bigger_position():
    wide = cf.evaluate(100.0, 150.0, 3.2)
    assert wide["status"] == "NO_TRADE" and wide["reason"] == "BELOW_MIN_QTY" and "rung_qty" not in wide
    assert cf.evaluate(100.0, 150.0, 1.6)["rung_qty"] == 0.1                        # exactly the minimum, at less than the budget


def test_a_larger_capital_can_trade_everything_a_smaller_one_can():
    for span in cf.SPANS:
        tradeable = [cf.evaluate(nav, 150.0, span)["status"] == "TRADE" for nav in cf.NAVS]
        assert tradeable == sorted(tradeable), (span, tradeable)                    # once True it stays True as NAV grows


def test_the_widest_tradeable_distance_is_the_minimum_quantity_rule_and_matches_the_sizing():
    t = cf.table()
    for nav, widest in t["widest_mean_rung_to_stop_usdt_by_nav"].items():
        assert widest == pytest.approx(float(nav) * RISK_FRACTION / (5 * cf.SOL_RULES.min_qty))


def test_the_band_and_the_table_recommend_nothing(capsys):
    assert cf.NAVS[0] == CAPITAL_MIN_USDT and cf.NAVS[-1] == CAPITAL_MAX_USDT
    assert cf.main(["--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "recommends no capital" in out["OWNER_APPROVAL_REQUIRED"]
    assert cf.main([]) == 0
    text = capsys.readouterr().out.lower()
    assert "recommend" in text and "best" not in text and "optimal" not in text and "should use" not in text


def test_a_ladder_above_the_five_times_exposure_ceiling_is_flagged():
    tight = cf.evaluate(100.0, 150.0, 0.2)
    assert tight["status"] == "TRADE" and tight["within_5x_exposure_ceiling"] is False and tight["exposure_multiple_of_nav"] > 5
