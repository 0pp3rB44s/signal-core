from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.runner import route_new_entry_plans


def plan(strategy: str | None, plan_id: str = "plan-1") -> SimpleNamespace:
    return SimpleNamespace(strategy=strategy, plan_id=plan_id)


def route(plans):
    return route_new_entry_plans(
        plans,
        enabled_strategies={"low_vol_reclaim_v2"},
        legacy_new_entries_enabled=False,
        require_explicit_allowlist=True,
    )


def test_v2_survives_legacy_new_entry_kill_switch():
    v2 = plan("low_vol_reclaim_v2")
    allowed, blocked = route([v2])
    assert allowed == [v2]
    assert blocked == []


@pytest.mark.parametrize(
    "strategy",
    [
        "low_vol_reclaim",
        "trend_continuation",
        "momentum_breakout",
        "momentum_breakdown",
        "dynamic_grid_v1",
        "dynamic_grid_v1.1",
        "liquidity_sweep_reversal",
    ],
)
def test_every_non_v2_strategy_is_rejected(strategy):
    allowed, blocked = route([plan(strategy)])
    assert allowed == []
    assert blocked[0][1] == "strategy_not_entry_allowlisted"


def test_mixed_ranked_list_preserves_order_but_only_v2_can_proceed():
    legacy = plan("trend_continuation", "legacy")
    first_v2 = plan("low_vol_reclaim_v2", "v2-first")
    unknown = plan("experimental_alpha", "unknown")
    second_v2 = plan("low_vol_reclaim_v2", "v2-second")

    allowed, blocked = route([legacy, first_v2, unknown, second_v2])

    assert allowed == [first_v2, second_v2]
    assert [item for item, _ in blocked] == [legacy, unknown]


@pytest.mark.parametrize("strategy", [None, "", "unknown"])
def test_unknown_or_missing_strategy_fails_closed(strategy):
    allowed, blocked = route([plan(strategy)])
    assert allowed == []
    assert blocked


def test_live_without_an_explicit_allowlist_fails_closed_even_for_v2():
    allowed, blocked = route_new_entry_plans(
        [plan("low_vol_reclaim_v2")],
        enabled_strategies=set(),
        legacy_new_entries_enabled=False,
        require_explicit_allowlist=True,
    )
    assert allowed == []
    assert blocked[0][1] == "live_entry_allowlist_missing"
