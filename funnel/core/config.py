"""SIGNAL_CORE_FUNNEL_V1 frozen restoration constants.

Every value in the FROZEN block is read verbatim from the last real-money session
(commit 5a28980d4bc8f1364cb50fbad3a40a41414e947d, live 2026-08-09T18:46:09Z to
2026-08-11T09:00:52Z) as reconstructed in LAST_WORKING_STATE_RECONSTRUCTION_2026-09-27.md
and AGENT_A_HISTORICAL_PERFORMANCE_RECONSTRUCTION_2026-09-27.md. This is the
restoration target, not a redesign: nothing here is an optimizable parameter, and
"no fine-tuning yet" applies to this file exactly as it does to the vendored
strategy modules.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

SPEC = "SIGNAL_CORE_FUNNEL_V1"
STRATEGY_ID = "signal_core_funnel_v1"

# The owner-approved production symbol universe at the last live commit
# (app/symbol_allowlist.py:OWNER_APPROVED_PRODUCTION_SYMBOLS @ 5a28980d).
OWNER_APPROVED_PRODUCTION_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT", "SOLUSDT", "SUIUSDT", "XLMUSDT",
    "AVAXUSDT", "DOGEUSDT", "SEIUSDT", "TRXUSDT",
)


@dataclass(frozen=True, slots=True)
class FunnelConfig:
    # ---------------- FROZEN (the last real-money session) ----------------
    symbol: str = "SOLUSDT"                 # one engine instance per symbol; see funnel/runtime.py
    primary_tf_s: int = 900                 # 15m: candidate detection + fill cadence (== trigger_tf_s)
    confirmation_tf_s: int = 3600           # 1H: higher-timeframe trend confirmation
    trigger_tf_s: int = 900                 # SimDriver/LiveDriver fill+NAV cadence; must equal primary_tf_s

    max_open_positions: int = 2             # LIVE_MAX_OPEN_POSITIONS (app/config.py), enforced by the shared RiskManager, not here
    default_leverage: float = 5.0
    max_leverage: float = 5.0
    planner_max_notional_per_trade_usdt: float = 35.0
    planner_min_live_notional_usdt: float = 10.0

    # ---------------- IMPLEMENTATION (this restoration; not a strategy parameter) ----------------
    # Same fee/slippage assumptions LSRL_V1 uses for its SimExchange when authenticated
    # rates are unavailable; this is a PAPER-mode execution assumption, not part of what
    # any strategy decided, so it is not "strategy semantics" and carries no drift risk
    # against the frozen decision code.
    assumed_maker_fee_rate: float = 0.0002
    assumed_taker_fee_rate: float = 0.0006
    assumed_stop_slippage_bps: float = 2.0
    max_snapshot_age_s: float = 30.0
    max_nav_age_s: float = 300.0
    candle_history_len: int = 240           # rolling bars kept per timeframe for EMA/volume-ratio computation

    def digest(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


DEFAULT_CONFIG = FunnelConfig()
