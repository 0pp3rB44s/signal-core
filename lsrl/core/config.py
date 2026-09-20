"""LSRL_V1 frozen specification constants.

Every value in the FROZEN block is copied verbatim from the owner/architect
specification (2026-09-20) and must not be tuned. The IMPLEMENTATION block holds
operational constants the specification leaves open; each is documented in
AGENT_A_LSRL_V1_IMPLEMENTATION_2026-09-20.md as an operationalization decision
for independent review. Nothing here is an optimizable parameter.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

SPEC = "LSRL_V1"
STRATEGY_ID = "lsrl_v1"


@dataclass(frozen=True, slots=True)
class LSRLConfig:
    # ---------------- FROZEN (owner/architect) ----------------
    symbol: str = "SOLUSDT"
    setup_tf_s: int = 900
    trigger_tf_s: int = 60

    pool_min_preexistence_s: int = 180
    pool_min_relative_strength: float = 2.0
    pool_max_distance_atr15: float = 1.5
    pool_max_age_s: int = 3600

    sweep_min_consumed_fraction: float = 0.25
    reclaim_max_bars_after_sweep: int = 5

    # E1 = reclaim boundary, E2..E4 = 25/50/75% into the sweep range, E5 = extreme.
    ladder_fractions: tuple[float, ...] = (0.0, 0.25, 0.50, 0.75, 1.0)
    entry_expiry_s: int = 900

    sl_atr1m_mult: float = 0.15

    risk_per_full_trade_frac: float = 0.005  # 0.50% NAV
    daily_stop_r: float = -2.0

    min_rr_to_tp2: float = 1.5
    tp_fractions: tuple[float, float, float] = (0.30, 0.40, 0.30)

    # ---------------- IMPLEMENTATION (spec left open) ----------------
    # Wilder ATR period; the spec names "ATR15m"/"ATR1m" without a period.
    atr_period: int = 14
    # Mirrors the existing archiver's stale threshold (lag_ms > 30_000).
    max_snapshot_age_s: float = 30.0
    # Mirrors RiskManager._execution_cost_gate's hard spread limit for SOLUSDT.
    max_spread_bps: float = 7.5
    # A sweep must be confirmed within this many completed 1m bars of the
    # first interaction, otherwise the attempt is dropped (bounds pending state).
    sweep_confirm_max_bars: int = 5
    # Ladder rungs must remain distinct exchange ticks.
    min_ladder_range_ticks: int = 4
    # Fee / slippage ASSUMPTIONS used only when authenticated rates are not
    # supplied; an authenticated live execution adapter passes the account's real rates.
    assumed_maker_fee_rate: float = 0.0002
    assumed_taker_fee_rate: float = 0.0006
    assumed_stop_slippage_bps: float = 2.0
    # Snapshot/pool bookkeeping retention (seconds of history kept for freezing).
    history_retention_s: int = 600
    # NAV used for sizing must be this fresh; older = NO-TRADE (fail closed).
    max_nav_age_s: float = 300.0

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


DEFAULT_CONFIG = LSRLConfig()
