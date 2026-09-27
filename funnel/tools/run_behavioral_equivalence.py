"""CLI: run the behavioral-equivalence comparison (funnel.tools.behavioral_equivalence) across the same
real, freshly-fetched market data funnel.tools.live_data_run replays. See that module's docstring for what
this does and does not prove (no raw 2026-08 archive exists on this host; see
FUNNEL_BEHAVIORAL_EQUIVALENCE_2026-09-27.md for the full report)."""
from __future__ import annotations

import json

from app.config import Settings
from clients.bitget_rest import BitgetRestClient
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.core.types import SymbolRules
from market_data.htf_regime import classify_htf_regime
from market_features.engine import LiveMarketContext, build_market_snapshot
from risk.risk_manager import RiskManager

from funnel.core.config import OWNER_APPROVED_PRODUCTION_SYMBOLS, FunnelConfig
from funnel.core.engine import FunnelEngine
from funnel.tools.behavioral_equivalence import ComparisonResult, compare, summary
from funnel.tools.live_data_run import INTERVAL_MS, _closed_as_of, _fetch_symbol_inputs


def main() -> None:
    settings = Settings()
    client = BitgetRestClient(settings)
    risk_manager = RiskManager(settings)
    overall = ComparisonResult()
    per_symbol: dict[str, dict] = {}
    for symbol in OWNER_APPROVED_PRODUCTION_SYMBOLS:
        inputs = _fetch_symbol_inputs(client, settings, symbol)
        primary, confirm, h4, d1, orderbook, contract = (
            inputs["primary"], inputs["confirm"], inputs["h4"], inputs["d1"], inputs["orderbook"], inputs["contract"])
        if len(primary) < 55:
            continue
        rules = SymbolRules(symbol=symbol, tick_size=0.0001, qty_step=0.0001, min_qty=0.0001, min_notional_usdt=5.0)
        engine = FunnelEngine(FunnelConfig(symbol=symbol), rules, risk_gate=CentralRiskGate(risk_manager, settings),
                              settings=settings)
        engine.begin_session(None)
        symbol_result = ComparisonResult()
        start_idx = max(20, len(primary) - 190)
        for i in range(start_idx, len(primary)):
            as_of_ms = primary[i].timestamp_ms + INTERVAL_MS["15m"] + 1
            try:
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
            compare(engine, market, settings, symbol_result)
            compare(engine, market, settings, overall)
        per_symbol[symbol] = summary(symbol_result)
    result = summary(overall)
    result["per_symbol"] = per_symbol
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
