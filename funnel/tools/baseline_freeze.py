"""Freeze the authoritative SIGNAL_CORE_FUNNEL_V1 baseline before continuous PAPER begins.

Per AGENT_A_START_CLEAN_BASELINE_2026-09-27's mission: "The authoritative baseline is now: restored
signal-core multi-strategy funnel, original strategy semantics, original 120 cluster cap, current
hardened execution architecture, corrected engine from commits bc51531 + 6f382d6, current shared
RiskManager, current process-down protection architecture, current restart reconciliation, current
clock/capacity/exposure safety... Freeze and hash: strategy, config, code, symbol universe,
RiskManager configuration, feature-engine configuration, execution assumptions. Do not change them
after PAPER begins."

This module produces a single canonical JSON document covering every one of those categories and a
sha256 over it (BASELINE_HASH). It reads state, never sets it: nothing here mutates app.config.Settings
defaults, FunnelConfig, or any strategy file - it only observes and hashes what is ALREADY committed at
HEAD. Run it again at any later point and an unchanged BASELINE_HASH is itself the proof that nothing
drifted; a changed hash after PAPER has started is a hard signal something in the frozen surface moved
and must be investigated before trusting further PAPER output.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.config import Settings
from lsrl.runtime import resolve_git_sha

from funnel.core.config import OWNER_APPROVED_PRODUCTION_SYMBOLS, FunnelConfig

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every module on the funnel's pure, deterministic feature-computation path (see
# FUNNEL_MARKET_INPUT_RECONSTRUCTION_2026-09-27.md) - hashing the FILES themselves (not just import
# paths) means a later silent edit to any of these is caught even if no test happens to exercise the
# changed line.
FEATURE_ENGINE_FILES = (
    "market_features/engine.py",
    "market_data/volatility_engine.py",
    "market_data/breakout_engine.py",
    "market_data/orderbook_analyzer.py",
    "market_data/liquidity_heatmap.py",
    "market_data/entry_quality.py",
    "market_data/htf_regime.py",
)

# Every module that decides WHICH strategy a candidate belongs to and how it is scored/selected -
# "strategy" in the mission's freeze list, as distinct from "config" (FunnelConfig) and "code"
# (the engine/adapter/runtime plumbing hashed separately below via git_sha).
STRATEGY_FILES = (
    "strategies/momentum_breakout.py",
    "strategies/liquidity_sweep.py",
    "strategies/scoring.py",
    "strategies/strategies/continuation.py",
    "strategies/strategies/low_vol_reclaim.py",
    "strategies/strategies/selector.py",
    "funnel/core/engine.py",
    "funnel/adapters/strategy_port.py",
    "funnel/runtime.py",
)

RISK_MANAGER_SETTINGS_FIELDS = (
    "account_equity_usdt", "account_risk_per_trade_pct", "default_leverage", "max_leverage",
    "max_open_positions", "max_total_exposure_pct", "max_correlated_positions",
    "max_cluster_exposure_pct", "max_daily_loss_pct", "hard_daily_stop_pct", "weekly_freeze_loss_pct",
    "planner_max_notional_pct_of_equity", "session_risk_reduction_windows_utc", "session_risk_multiplier",
)


def _sha256_file(rel_path: str) -> str:
    return hashlib.sha256((REPO_ROOT / rel_path).read_bytes()).hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def build_freeze_document(*, baseline_cap_pct: float = 120.0) -> dict[str, Any]:
    settings = Settings()  # process defaults: no override anywhere sets MAX_CLUSTER_EXPOSURE_PCT
    if settings.max_cluster_exposure_pct != baseline_cap_pct:
        raise RuntimeError(
            f"Settings().max_cluster_exposure_pct={settings.max_cluster_exposure_pct} != "
            f"expected baseline {baseline_cap_pct} - the ORIGINAL baseline must run under the "
            "unmodified default, not an overridden value; refusing to freeze a mismatched cap")

    per_symbol_config_digest = {sym: FunnelConfig(symbol=sym).digest() for sym in OWNER_APPROVED_PRODUCTION_SYMBOLS}
    risk_manager_settings = {f: getattr(settings, f) for f in RISK_MANAGER_SETTINGS_FIELDS}
    feature_engine_file_hashes = {f: _sha256_file(f) for f in FEATURE_ENGINE_FILES}
    strategy_file_hashes = {f: _sha256_file(f) for f in STRATEGY_FILES}

    doc: dict[str, Any] = {
        "spec": "SIGNAL_CORE_FUNNEL_V1_BASELINE_FREEZE",
        "git_sha": resolve_git_sha(REPO_ROOT),
        "baseline_cluster_exposure_cap_pct": baseline_cap_pct,
        "symbol_universe": list(OWNER_APPROVED_PRODUCTION_SYMBOLS),
        "per_symbol_funnel_config_digest": per_symbol_config_digest,
        "risk_manager_settings": risk_manager_settings,
        "feature_engine_file_sha256": feature_engine_file_hashes,
        "strategy_file_sha256": strategy_file_hashes,
        "engine_fix_commits": ["bc51531", "6f382d6"],
        "note": "execution assumptions (fee/slippage) are inside per_symbol_funnel_config_digest "
                "(FunnelConfig.assumed_maker_fee_rate/assumed_taker_fee_rate/assumed_stop_slippage_bps); "
                "process-down protection, restart reconciliation, and clock/capacity/exposure safety "
                "are LSRL_V1's own shared, unmodified plumbing (lsrl/adapters/process_down.py, "
                "lsrl/runtime.py::LSRLRuntime.restore(), lsrl/adapters/capacity_gate.py) and are not "
                "re-hashed here since this restoration makes zero changes to them.",
    }
    doc["baseline_hash"] = hashlib.sha256(_canon({k: v for k, v in doc.items() if k != "baseline_hash"}).encode()).hexdigest()
    return doc


def main() -> None:
    doc = build_freeze_document()
    out_path = REPO_ROOT / "BASELINE_FREEZE_2026-09-27.json"
    out_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"BASELINE_FROZEN": "YES", "BASELINE_HASH": doc["baseline_hash"],
                      "BASELINE_CAP": doc["baseline_cluster_exposure_cap_pct"],
                      "git_sha": doc["git_sha"], "written_to": str(out_path)}, indent=2))


if __name__ == "__main__":
    main()
