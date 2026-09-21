"""The strategy the owner froze must not move under a data / execution patch.

The 2026-09-20 hardening replaces the DATA OBSERVATION layer (WebSocket depth + tape, consumption from executed
volume, recorder, executor). It must not touch the ladder, stop, targets, sizing, ATR, lifecycle or daily-stop
rules, nor the three owner-ratified operationalizations:
    #2  "local median" = median size over ALL listed levels of that side of the reconstructed book
    #6  reclaim window: the sweep bar itself (k = 0) up to five completed 1m bars after it
    #8  three eligible opposite pools are required for targets (else NO TRADE)
These pins are SHA-256 of the files/functions as they were at fa3f9b4 (before this patch). Changing a frozen
rule is a deliberate act: it requires updating this file on purpose, in review, with the owner's sign-off.
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.pools import cluster_pools
from lsrl.core.types import Side

CORE = Path(__file__).resolve().parents[1] / "lsrl" / "core"

FROZEN_CONFIG_DIGEST = "32ac85b9bd936ff9e98c1e298cccc43e2c5c6e3deb8347e6ad5a7083d81bca6d"

FROZEN_FILE_SHA256 = {                                    # byte-identical to fa3f9b4
    "config.py": "d19e1d60c0f0727eebf7664895afce431d114f4e7ebdb9695ae04ad0e8aef2ba",
    "ladder.py": "55052a340b8238a734f12f186508fe62f82a5e1c5850821c20300f6c3f30d596",
    "targets.py": "347dc701d98dd91397f9ab879bf7b5b65d3102d5a2cbfad2fbf010c62e9e471e",
    "atr.py": "34ca2878bd907efb380c287db9fd114589a7994c54118ebed6715c7ee38faeaf",
    "lifecycle.py": "7f622174efcdae1fd3b029fe92f790d37068c19b79cd2f1667dc15ab3d970725",
    "risk_state.py": "74cf1378c178dd629835ff487dd98e8a6ec1979f95f0d4f784b11eedf1f0e516",
}

FROZEN_POOL_FUNCTIONS = {                                 # pools.py gained only PoolTracker.reset(); these did not change
    "cluster_pools": "7723e37d80230e918b6fdc2b0c6c94a7cad5d3a501ed831b10e926e934af4ff3",
    "pool_distance": "65cc9f1bed2ffa599b9b89ea8eecafd3261fcfa2752954b9947b140acc33feb5",
    "eligibility_failures": "51ddbae9540e4e3677b2e9b5ee8ec24e84eb5945a5f9f30edd1221ed13b7a8a3",
}


def test_the_frozen_configuration_digest_is_unchanged():
    assert CFG.digest() == FROZEN_CONFIG_DIGEST


@pytest.mark.parametrize("name", sorted(FROZEN_FILE_SHA256))
def test_the_ladder_stop_target_sizing_atr_lifecycle_and_daily_stop_rules_are_byte_identical_to_the_frozen_commit(name):
    assert hashlib.sha256((CORE / name).read_bytes()).hexdigest() == FROZEN_FILE_SHA256[name]


@pytest.mark.parametrize("name", sorted(FROZEN_POOL_FUNCTIONS))
def test_the_owner_ratified_pool_definition_is_unchanged(name):
    src = (CORE / "pools.py").read_text()
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == name)
    assert hashlib.sha256(ast.get_source_segment(src, node).encode()).hexdigest() == FROZEN_POOL_FUNCTIONS[name]


def test_ratified_2_the_local_median_is_taken_over_all_listed_levels_of_that_side():
    levels = tuple((100.0 - i * 0.1, s) for i, s in enumerate((10, 10, 10, 10, 50, 50, 10, 10)))
    pools = cluster_pools(levels, Side.BID, 0, CFG.pool_min_relative_strength)
    assert [(p.lower, p.upper, p.depth) for p in pools] == [(99.5, 99.6, 100.0)]      # median 10 -> 2x = 20
    bigger = levels + tuple((99.0 - i * 0.1, 100) for i in range(9))                   # more listed levels: median rises
    assert cluster_pools(bigger, Side.BID, 0, CFG.pool_min_relative_strength) != pools


def test_ratified_6_and_8_are_configuration_of_the_frozen_engine_paths():
    assert CFG.reclaim_max_bars_after_sweep == 5 and CFG.sweep_confirm_max_bars == 5
    assert CFG.min_rr_to_tp2 == 1.5 and CFG.tp_fractions == (0.30, 0.40, 0.30)
    assert CFG.pool_min_preexistence_s == 180 and CFG.pool_max_age_s == 3600 and CFG.pool_max_distance_atr15 == 1.5
    assert CFG.sweep_min_consumed_fraction == 0.25 and CFG.risk_per_full_trade_frac == 0.005 and CFG.daily_stop_r == -2.0
