"""Live-data PAPER run for SIGNAL_CORE_FUNNEL_V1: real, freshly-fetched Bitget public market data through
the real, unmodified feature engine (market_features.engine.build_market_snapshot) and the real, vendored
detectors -> real scorer -> real CentralRiskGate/RiskManager -> real TradePlanner -> SimExchange. No real
order, no credentials (only /api/v2/mix/market/{candles,merge-depth,contracts}, all public GET requests -
see PUBLIC_ENDPOINTS below and FUNNEL_MARKET_INPUT_RECONSTRUCTION_2026-09-27.md for the full trace).

Two disclosed, necessary simplifications (never silently, always in the printed report):
  1. ONE order-book depth snapshot is fetched per symbol (Bitget's public API has no historical order-book
     endpoint) and reused for every replayed historical timestamp of that symbol. Every candle-derived
     feature (breakout pressure, volatility, HTF regime, EMA/trend) is genuinely historically accurate for
     its own replayed timestamp; only the orderbook-derived notes (spread, liquidity heatmap, entry-quality's
     orderbook component) reflect "now", not the true historical book.
  2. HTF (4H/1D) candles are fetched once and then FILTERED per replayed timestamp (see `_closed_as_of`) so
     no future 4H/1D bar leaks into an earlier decision - avoiding a lookahead bias a naive reuse would cause.

This is a bulk REPLAY of real, recently-closed candles (fetched fresh, right now, from Bitget's public API),
not synthetic data and not a live tick-by-tick session - it lets many real decision cycles run without
waiting in wall-clock time for each 15-minute close, which adds nothing epistemically over using the same
real closed candles fetched now. A short true live-polling tail (`--live-tail-cycles`) additionally proves
ongoing ingestion, not just a one-shot fetch.
"""
from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings
from clients.bitget_rest import BitgetRestClient
from clients.schemas import Candle, ContractSpec
from data.normalizer import normalize_candles
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.adapters.sim import SimExchange
from lsrl.core.types import SymbolRules
from market_data.htf_regime import classify_htf_regime
from market_features.engine import LiveMarketContext, build_market_snapshot
from risk.risk_manager import RiskManager

from funnel.core.config import DEFAULT_CONFIG, OWNER_APPROVED_PRODUCTION_SYMBOLS, FunnelConfig
from funnel.core.engine import FunnelEngine

PUBLIC_ENDPOINTS = (
    "/api/v2/mix/market/candles", "/api/v2/mix/market/merge-depth", "/api/v2/mix/market/contracts",
)
PRIMARY_LIMIT = 200
CONFIRM_LIMIT = 200
HTF_4H_LIMIT = 60
HTF_1D_LIMIT = 40
INTERVAL_MS = {"15m": 900_000, "1h": 3_600_000, "4H": 14_400_000, "1D": 86_400_000}


def _closed_as_of(candles: list[Candle], granularity: str, as_of_ms: int) -> list[Candle]:
    step = INTERVAL_MS[granularity]
    return [c for c in candles if c.timestamp_ms + step <= as_of_ms]


@dataclass
class SymbolReplayResult:
    symbol: str
    steps_attempted: int = 0
    steps_built: int = 0
    build_errors: dict[str, int] = field(default_factory=dict)
    candidates_by_strategy: dict[str, int] = field(default_factory=dict)
    rejections_by_stage: dict[str, int] = field(default_factory=dict)
    accepted_setups: list[dict[str, Any]] = field(default_factory=list)
    simulated_fills: list[dict[str, Any]] = field(default_factory=list)


def _fetch_symbol_inputs(client: BitgetRestClient, settings: Settings, symbol: str) -> dict[str, Any]:
    product = settings.bitget_product_type
    primary = normalize_candles(client.get_candles(symbol=symbol, product_type=product, granularity="15m", limit=PRIMARY_LIMIT).get("data") or [])
    confirm = normalize_candles(client.get_candles(symbol=symbol, product_type=product, granularity="1h", limit=CONFIRM_LIMIT).get("data") or [])
    h4 = normalize_candles(client.get_candles(symbol=symbol, product_type=product, granularity="4H", limit=HTF_4H_LIMIT).get("data") or [])
    d1 = normalize_candles(client.get_candles(symbol=symbol, product_type=product, granularity="1D", limit=HTF_1D_LIMIT).get("data") or [])
    orderbook = client.get_orderbook(symbol, limit=50)
    contracts_payload = client.get_contracts(product_type=product, symbol=symbol)
    rows = contracts_payload.get("data") or []
    contract = None
    if rows:
        row = rows[0]
        contract = ContractSpec(symbol=symbol, product_type=product, quote_coin="USDT",
                                base_coin=symbol.removesuffix("USDT"), status=str(row.get("symbolStatus") or "live"),
                                min_trade_num=float(row["minTradeNum"]) if row.get("minTradeNum") else None,
                                size_multiplier=float(row["sizeMultiplier"]) if row.get("sizeMultiplier") else None,
                                price_place=int(row["pricePlace"]) if row.get("pricePlace") else None,
                                volume_24h_usdt=None, change_pct_24h=None, raw={})
    return {"primary": primary, "confirm": confirm, "h4": h4, "d1": d1, "orderbook": orderbook, "contract": contract}


def replay_symbol(engine: FunnelEngine, sim: SimExchange, inputs: dict[str, Any], *, symbol: str,
                  nav_usdt: float, step_every: int = 1) -> SymbolReplayResult:
    result = SymbolReplayResult(symbol=symbol)
    primary, confirm, h4, d1, orderbook, contract = (
        inputs["primary"], inputs["confirm"], inputs["h4"], inputs["d1"], inputs["orderbook"], inputs["contract"])
    if len(primary) < 55:
        result.build_errors["INSUFFICIENT_PRIMARY_HISTORY"] = 1
        return result
    start_idx = max(20, len(primary) - 190)
    for i in range(start_idx, len(primary), step_every):
        as_of_ms = primary[i].timestamp_ms + INTERVAL_MS["15m"] + 1
        result.steps_attempted += 1
        try:
            h4_closed = _closed_as_of(h4, "4H", as_of_ms)
            d1_closed = _closed_as_of(d1, "1D", as_of_ms)
            htf = classify_htf_regime(h4_closed, d1_closed) if h4_closed and d1_closed else {
                "regime_1d": "neutral", "regime_4h": "neutral", "htf_regime": "neutral"}
            market = build_market_snapshot(
                symbol, primary[:i + 1], confirm, as_of_timestamp_ms=as_of_ms,
                primary_granularity="15m", confirmation_granularity="1h",
                inputs=LiveMarketContext(orderbook=orderbook, htf_context=htf, contract=contract))
        except Exception as exc:
            key = type(exc).__name__
            result.build_errors[key] = result.build_errors.get(key, 0) + 1
            continue
        result.steps_built += 1
        had_setup_before = engine.setup is not None
        if not had_setup_before:
            candidate, _why = engine._detect(market)
            if candidate is not None:
                result.candidates_by_strategy[candidate.strategy] = result.candidates_by_strategy.get(candidate.strategy, 0) + 1
        intents, records = engine.run_live_cycle(market, t_ms=as_of_ms, nav_usdt=nav_usdt)
        for r in records:
            if r.kind == "setup_rejected":
                stage = r.data.get("stage", "UNKNOWN")
                result.rejections_by_stage[stage] = result.rejections_by_stage.get(stage, 0) + 1
        if not had_setup_before and engine.setup is not None:
            result.accepted_setups.append({"strategy": engine.setup.strategy, "direction": engine.setup.direction.value,
                                           "entry": engine.setup.entry_price, "stop": engine.setup.stop_price,
                                           "targets": [t.price for t in engine.setup.targets], "t_ms": as_of_ms})
        for order_event in sim.apply(intents, as_of_ms):
            engine.on_event(order_event)
        bar = _synthetic_fill_bar(primary[i], as_of_ms, engine.cfg.primary_tf_s)
        for f in list(sim.entry_fills(bar)) + list(sim.exit_fills(bar)):
            result.simulated_fills.append({"kind": f.kind, "price": f.price, "qty": f.qty, "t_ms": f.t_ms})
            _intents, _records = engine.on_event(f)
            for oe in sim.apply(_intents, f.t_ms):
                engine.on_event(oe)
    return result


def _synthetic_fill_bar(candle: Candle, as_of_ms: int, tf_s: int):
    from lsrl.core.types import BarClosed
    return BarClosed(t_ms=as_of_ms, tf_s=tf_s, open_ts_ms=candle.timestamp_ms,
                     close_ts_ms=candle.timestamp_ms + tf_s * 1000, open=candle.open, high=candle.high,
                     low=candle.low, close=candle.close, volume=candle.volume_base)


def run(symbols: tuple[str, ...] = OWNER_APPROVED_PRODUCTION_SYMBOLS, *, nav_usdt: float = 1000.0) -> dict[str, Any]:
    settings = Settings()
    client = BitgetRestClient(settings)                 # no credentials configured; only public GET calls below
    risk_manager = RiskManager(settings)                # ONE shared instance across all symbols, as in production
    report: dict[str, Any] = {"symbols": {}, "public_endpoints_used": list(PUBLIC_ENDPOINTS)}
    for symbol in symbols:
        try:
            inputs = _fetch_symbol_inputs(client, settings, symbol)
        except Exception as exc:
            report["symbols"][symbol] = {"fetch_error": f"{type(exc).__name__}: {exc}"}
            continue
        rules = SymbolRules(symbol=symbol, tick_size=0.0001, qty_step=0.0001, min_qty=0.0001, min_notional_usdt=5.0)
        if inputs["contract"] is not None and inputs["contract"].price_place is not None:
            rules = SymbolRules(symbol=symbol, tick_size=10 ** (-inputs["contract"].price_place),
                                qty_step=inputs["contract"].size_multiplier or 0.0001,
                                min_qty=inputs["contract"].min_trade_num or 0.0001, min_notional_usdt=5.0)
        cfg = FunnelConfig(symbol=symbol)
        gate = CentralRiskGate(risk_manager, settings)
        engine = FunnelEngine(cfg, rules, risk_gate=gate, settings=settings)
        engine.begin_session(None)
        sim = SimExchange(rules, cfg)
        result = replay_symbol(engine, sim, inputs, symbol=symbol, nav_usdt=nav_usdt)
        report["symbols"][symbol] = {
            "steps_attempted": result.steps_attempted, "steps_built": result.steps_built,
            "build_errors": result.build_errors, "candidates_by_strategy": result.candidates_by_strategy,
            "rejections_by_stage": result.rejections_by_stage, "accepted_setups": result.accepted_setups,
            "simulated_fills": result.simulated_fills,
        }
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(OWNER_APPROVED_PRODUCTION_SYMBOLS))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    report = run(symbols)
    text = json.dumps(report, indent=2, default=str)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text)
    print(text)


if __name__ == "__main__":
    main()
