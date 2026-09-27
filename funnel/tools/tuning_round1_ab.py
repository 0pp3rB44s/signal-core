"""Tuning Round 1 A/B: ORIGINAL (max_cluster_exposure_pct=120, baseline) vs TUNED (=40, see
TUNING_ROUND1_CAP_SELECTION_2026-09-27.md) on IDENTICAL market inputs.

Both variants see the exact same MarketSnapshot at every step (fetched once per symbol, shared). Each
variant is one independent 8-symbol portfolio, replayed with all 8 symbols' timestamps INTERLEAVED in true
chronological order (not one symbol's whole history at a time) - this matters mechanically: the cluster-
exposure gate this round tunes only has anything to evaluate when two DIFFERENT symbols from the same
BETA_CLUSTERS group are open AT THE SAME MOMENT, which a sequential per-symbol replay can never produce
(by the time symbol 2 starts, symbol 1's whole timeline - including every close - has already finished).

Harness note, disclosed rather than hidden: risk/risk_manager.py._load_open_positions() reads
open positions from a FILE (<repo root>/state/executed_trades.json, status=="OPEN" rows), not from this
restoration's in-memory FunnelEngine/SimExchange state. The first version of this script never wrote to
that file, so the cluster-exposure check always saw zero existing exposure regardless of variant - a false
negative, not a finding about the tuning value. This version writes each variant's OWN current open
positions to that file before every candidate evaluation (and resets it between variants, since the two
portfolios are independent and the path is fixed by the module, not configurable). This is a harness
correctness fix, not a change to the frozen mechanism or its value.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings
from clients.bitget_rest import BitgetRestClient
from clients.schemas import Candle
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.adapters.sim import SimExchange
from lsrl.core.types import BarClosed, SymbolRules
from market_data.htf_regime import classify_htf_regime
from market_features.engine import LiveMarketContext, build_market_snapshot
from risk.risk_manager import RiskManager

from funnel.core.config import OWNER_APPROVED_PRODUCTION_SYMBOLS, FunnelConfig
from funnel.core.engine import FunnelEngine
from funnel.tools.live_data_run import INTERVAL_MS, _closed_as_of, _fetch_symbol_inputs

TUNED_MAX_CLUSTER_EXPOSURE_PCT = 40.0    # frozen: TUNING_ROUND1_FROZEN_SPEC_2026-09-27.json
OPEN_POSITIONS_PATH = Path(__file__).resolve().parents[2] / "state" / "executed_trades.json"


def _write_open_positions(open_positions: list[dict[str, Any]]) -> None:
    OPEN_POSITIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OPEN_POSITIONS_PATH.write_text(json.dumps({"_state_metadata": {"note": "tuning_round1 scratch, not production"},
                                               "data": open_positions}))


@dataclass
class VariantState:
    name: str
    settings: Settings
    risk_manager: RiskManager
    engines: dict[str, FunnelEngine] = field(default_factory=dict)
    sims: dict[str, SimExchange] = field(default_factory=dict)
    trades: list[dict[str, Any]] = field(default_factory=list)
    accepted: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    rejections_by_stage: dict[str, int] = field(default_factory=dict)
    concurrent_open_log: list[tuple[int, dict[str, int]]] = field(default_factory=list)
    _entry_fee_by_setup: dict[str, float] = field(default_factory=dict)

    def open_positions_rows(self) -> list[dict[str, Any]]:
        rows = []
        for symbol, eng in self.engines.items():
            if eng.setup is not None and eng.setup.state == "IN_POSITION":
                rows.append({"symbol": symbol, "status": "OPEN",
                            "position_notional_usdt": eng.setup.entry_qty * eng.setup.entry_price})
        return rows


def make_variant(name: str, *, max_cluster_exposure_pct: float, rules_by_symbol: dict[str, SymbolRules]) -> VariantState:
    # NOTE: app.config.Settings fields use a pydantic alias (MAX_CLUSTER_EXPOSURE_PCT) and
    # populate_by_name is not enabled, so Settings(max_cluster_exposure_pct=...) silently keeps the
    # default (120.0) instead of raising - the alias form below is the only one that actually applies.
    settings = Settings(MAX_CLUSTER_EXPOSURE_PCT=max_cluster_exposure_pct)
    risk_manager = RiskManager(settings)
    state = VariantState(name=name, settings=settings, risk_manager=risk_manager)
    for symbol, rules in rules_by_symbol.items():
        cfg = FunnelConfig(symbol=symbol)
        gate = CentralRiskGate(risk_manager, settings)
        eng = FunnelEngine(cfg, rules, risk_gate=gate, settings=settings)
        eng.begin_session(None)
        state.engines[symbol] = eng
        state.sims[symbol] = SimExchange(rules, cfg)
    return state


CLUSTER_OF: dict[str, str] = {}
def _cluster_of(symbol: str) -> str | None:
    if not CLUSTER_OF:
        from risk.risk_manager import BETA_CLUSTERS
        for name, syms in BETA_CLUSTERS.items():
            for s in syms:
                CLUSTER_OF[s] = name
    return CLUSTER_OF.get(symbol.upper())


def _open_counts_by_cluster(variant: VariantState) -> dict[str, int]:
    counts: dict[str, int] = {}
    for symbol, eng in variant.engines.items():
        if eng.setup is not None and eng.setup.state == "IN_POSITION":
            cluster = _cluster_of(symbol) or f"UNCLUSTERED:{symbol}"
            counts[cluster] = counts.get(cluster, 0) + 1
    return counts


def step_variant(variant: VariantState, symbol: str, market, *, t_ms: int, nav_usdt: float, candle: Candle) -> dict[str, Any]:
    _write_open_positions(variant.open_positions_rows())          # THIS variant's view, before its own decision
    eng, sim = variant.engines[symbol], variant.sims[symbol]
    had_setup = eng.setup is not None
    outcome = {"signal": None, "accepted": False, "blocked_reason": None}
    if not had_setup:
        candidate, _why = eng._detect(market)
        if candidate is not None:
            outcome["signal"] = candidate.strategy
            variant.signals.append({"symbol": symbol, "strategy": candidate.strategy, "t_ms": t_ms})
    intents, records = eng.run_live_cycle(market, t_ms=t_ms, nav_usdt=nav_usdt)
    for r in records:
        if r.kind == "setup_rejected":
            stage = r.data.get("stage", "UNKNOWN")
            variant.rejections_by_stage[stage] = variant.rejections_by_stage.get(stage, 0) + 1
            if not had_setup and outcome["signal"] is not None:
                outcome["blocked_reason"] = stage
        if r.kind == "setup_accepted":
            outcome["accepted"] = True
            variant.accepted.append({"symbol": symbol, "strategy": r.data.get("strategy"), "t_ms": t_ms,
                                     "entry": r.data.get("entry"), "stop": r.data.get("stop"),
                                     "notional": r.data.get("position_notional_usdt")})
    for order_event in sim.apply(intents, t_ms):
        eng.on_event(order_event)
    bar = BarClosed(t_ms=t_ms, tf_s=eng.cfg.primary_tf_s, open_ts_ms=candle.timestamp_ms,
                    close_ts_ms=candle.timestamp_ms + eng.cfg.primary_tf_s * 1000, open=candle.open,
                    high=candle.high, low=candle.low, close=candle.close, volume=candle.volume_base)
    for f in list(sim.entry_fills(bar)) + list(sim.exit_fills(bar)):
        if f.kind == "ENTRY" and eng.setup is not None:
            variant._entry_fee_by_setup[eng.setup.setup_id] = variant._entry_fee_by_setup.get(eng.setup.setup_id, 0.0) + f.fee_usdt
        if f.kind in ("TP1", "TP2", "TP3", "SL", "OTHER_CLOSE") and eng.setup is not None:
            direction_sgn = 1 if eng.setup.direction.value == "LONG" else -1
            gross = direction_sgn * (f.price - eng.setup.entry_price) * f.qty
            fully_closing = eng.setup.remaining_qty - f.qty <= 1e-9
            entry_fee = variant._entry_fee_by_setup.pop(eng.setup.setup_id, 0.0) if fully_closing else 0.0
            variant.trades.append({"symbol": symbol, "strategy": eng.setup.strategy, "kind": f.kind,
                                   "entry_price": eng.setup.entry_price, "exit_price": f.price, "qty": f.qty,
                                   "fee": f.fee_usdt + entry_fee, "gross_pnl": gross,
                                   "net_pnl": gross - f.fee_usdt - entry_fee,
                                   "opened_t_ms": eng.setup.candidate_t_ms, "closed_t_ms": f.t_ms})
        _intents, _records = eng.on_event(f)
        for oe in sim.apply(_intents, f.t_ms):
            eng.on_event(oe)
    variant.concurrent_open_log.append((t_ms, _open_counts_by_cluster(variant)))
    return outcome


def _fetch_all(symbols: tuple[str, ...]) -> tuple[dict[str, dict], dict[str, SymbolRules]]:
    settings_probe = Settings()
    client = BitgetRestClient(settings_probe)
    per_symbol_inputs: dict[str, dict] = {}
    rules_by_symbol: dict[str, SymbolRules] = {}
    for symbol in symbols:
        inputs = _fetch_symbol_inputs(client, settings_probe, symbol)
        per_symbol_inputs[symbol] = inputs
        contract = inputs["contract"]
        rules_by_symbol[symbol] = (
            SymbolRules(symbol=symbol, tick_size=10 ** (-contract.price_place), qty_step=contract.size_multiplier or 0.0001,
                       min_qty=contract.min_trade_num or 0.0001, min_notional_usdt=5.0)
            if contract is not None and contract.price_place is not None else
            SymbolRules(symbol=symbol, tick_size=0.0001, qty_step=0.0001, min_qty=0.0001, min_notional_usdt=5.0))
    return per_symbol_inputs, rules_by_symbol


def _build_timeline(per_symbol_inputs: dict[str, dict]) -> list[tuple[int, str, Candle]]:
    """Every (as_of_ms, symbol, candle) step across all symbols, in true chronological order."""
    events: list[tuple[int, str, Candle]] = []
    for symbol, inputs in per_symbol_inputs.items():
        primary = inputs["primary"]
        if len(primary) < 55:
            continue
        start_idx = max(20, len(primary) - 190)
        for i in range(start_idx, len(primary)):
            as_of_ms = primary[i].timestamp_ms + INTERVAL_MS["15m"] + 1
            events.append((as_of_ms, symbol, primary[i]))
    events.sort(key=lambda e: (e[0], e[1]))
    return events


def run_variant(name: str, *, max_cluster_exposure_pct: float, per_symbol_inputs: dict[str, dict],
                rules_by_symbol: dict[str, SymbolRules], timeline: list[tuple[int, str, Candle]],
                nav_usdt: float) -> VariantState:
    variant = make_variant(name, max_cluster_exposure_pct=max_cluster_exposure_pct, rules_by_symbol=rules_by_symbol)
    _write_open_positions([])                                      # fresh, independent portfolio
    for as_of_ms, symbol, candle in timeline:
        inputs = per_symbol_inputs[symbol]
        primary, confirm, h4, d1, orderbook, contract = (
            inputs["primary"], inputs["confirm"], inputs["h4"], inputs["d1"], inputs["orderbook"], inputs["contract"])
        try:
            i = primary.index(candle)
            h4_closed = _closed_as_of(h4, "4H", as_of_ms)
            d1_closed = _closed_as_of(d1, "1D", as_of_ms)
            htf = classify_htf_regime(h4_closed, d1_closed) if h4_closed and d1_closed else {
                "regime_1d": "neutral", "regime_4h": "neutral", "htf_regime": "neutral"}
            market = build_market_snapshot(
                symbol, primary[:i + 1], confirm, as_of_timestamp_ms=as_of_ms,
                primary_granularity="15m", confirmation_granularity="1h",
                inputs=LiveMarketContext(orderbook=orderbook, htf_context=htf, contract=contract))
        except Exception:
            continue
        step_variant(variant, symbol, market, t_ms=as_of_ms, nav_usdt=nav_usdt, candle=candle)
    return variant


def run(symbols: tuple[str, ...] = OWNER_APPROVED_PRODUCTION_SYMBOLS, *, nav_usdt: float = 1000.0) -> dict[str, Any]:
    per_symbol_inputs, rules_by_symbol = _fetch_all(symbols)
    timeline = _build_timeline(per_symbol_inputs)

    variant_a = run_variant("ORIGINAL", max_cluster_exposure_pct=120.0, per_symbol_inputs=per_symbol_inputs,
                            rules_by_symbol=rules_by_symbol, timeline=timeline, nav_usdt=nav_usdt)
    variant_b = run_variant("TUNED", max_cluster_exposure_pct=TUNED_MAX_CLUSTER_EXPOSURE_PCT,
                            per_symbol_inputs=per_symbol_inputs, rules_by_symbol=rules_by_symbol,
                            timeline=timeline, nav_usdt=nav_usdt)

    # Blocked-by-cap: at each (symbol, t_ms) where A had a signal and accepted, but B had the SAME signal
    # and did not accept, specifically because of a RISK_MANAGER/SIZING-stage rejection.
    a_accept_keys = {(a["symbol"], a["strategy"], a["t_ms"]) for a in variant_a.accepted}
    b_accept_keys = {(b["symbol"], b["strategy"], b["t_ms"]) for b in variant_b.accepted}
    a_signal_keys = {(s["symbol"], s["strategy"], s["t_ms"]) for s in variant_a.signals}
    b_signal_keys = {(s["symbol"], s["strategy"], s["t_ms"]) for s in variant_b.signals}
    blocked_by_cap = [{"symbol": k[0], "strategy": k[1], "t_ms": k[2]}
                      for k in (a_accept_keys & b_signal_keys) - b_accept_keys]

    return {"variant_a": variant_a, "variant_b": variant_b, "blocked_by_cap": blocked_by_cap, "symbols": symbols,
           "timeline_length": len(timeline)}
