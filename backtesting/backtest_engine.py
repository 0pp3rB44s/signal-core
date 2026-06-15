from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import List, Dict, Any

from clients.schemas import MarketSnapshot, Candle
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.momentum_breakout import (
    MomentumBreakoutStrategy,
    MomentumBreakdownStrategy,
)
from strategies.strategies.selector import select_best_candidate
from strategies.scoring import StrategyScorer
from strategies.strategies.continuation import ContinuationStrategy
from strategies.strategies.low_vol_reclaim import LowVolReclaimStrategy

from risk.risk_manager import RiskManager
from backtesting.metrics import summarize


@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    result: str
    pnl_pct: float
    candles_held: int
    tp1_hit: bool
    timed_exit: bool
    regime: str


class BacktestEngine:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.sweep = LiquiditySweepStrategy(settings)
        self.momentum = MomentumBreakoutStrategy(settings)
        self.momentum_breakdown = MomentumBreakdownStrategy(settings)
        self.continuation = ContinuationStrategy()
        self.low_vol_reclaim = LowVolReclaimStrategy()
        self.scorer = StrategyScorer(settings)
        self.risk = RiskManager(settings)

    def run(self, market_data: Dict[str, List[Candle]]) -> Dict[str, Any]:
        trades: List[BacktestTrade] = []
        debug = Counter()
        debug_by_symbol: dict[str, Counter] = {}

        for symbol, candles in market_data.items():
            debug_by_symbol[symbol] = Counter()
            for i in range(50, len(candles) - 1):
                snapshot = self._build_snapshot(symbol, candles[: i + 1])

                sweep_cand = self.sweep.detect(snapshot)
                momentum_cand = self.momentum.detect(snapshot)
                momentum_breakdown_cand = self.momentum_breakdown.detect(snapshot)
                continuation_cand = self.continuation.detect(snapshot)
                low_vol_reclaim_cand = self.low_vol_reclaim.detect(snapshot)

                if sweep_cand:
                    debug["sweep_candidates"] += 1
                    debug_by_symbol[symbol]["sweep_candidates"] += 1
                if momentum_cand:
                    debug["momentum_candidates"] += 1
                    debug_by_symbol[symbol]["momentum_candidates"] += 1
                if momentum_breakdown_cand:
                    debug["momentum_breakdown_candidates"] += 1
                    debug_by_symbol[symbol]["momentum_breakdown_candidates"] += 1
                if continuation_cand:
                    debug["continuation_candidates"] += 1
                    debug_by_symbol[symbol]["continuation_candidates"] += 1
                if low_vol_reclaim_cand:
                    debug["low_vol_reclaim_candidates"] += 1
                    debug_by_symbol[symbol]["low_vol_reclaim_candidates"] += 1
                if (
                    not sweep_cand
                    and not continuation_cand
                    and not low_vol_reclaim_cand
                    and not momentum_cand
                    and not momentum_breakdown_cand
                ):
                    debug["no_candidate"] += 1
                    debug_by_symbol[symbol]["no_candidate"] += 1

                candidate, selector_reason = select_best_candidate(
                    sweep_cand,
                    continuation_cand,
                    low_vol_reclaim_cand,
                    momentum_cand,
                    momentum_breakdown_cand,
                )

                if not candidate:
                    debug["selector_rejected"] += 1
                    debug_by_symbol[symbol]["selector_rejected"] += 1
                    if selector_reason:
                        debug[f"selector_reason::{selector_reason}"] += 1
                    continue

                debug["selected_candidate"] += 1
                debug_by_symbol[symbol]["selected_candidate"] += 1
                debug[f"selected_strategy::{candidate.strategy}"] += 1

                score = self.scorer.score(candidate)
                debug["scored_candidate"] += 1
                debug_by_symbol[symbol]["scored_candidate"] += 1
                debug[f"score_bucket::{int(score.total // 10) * 10}"] += 1

                verdict = self.risk.evaluate(candidate, score)

                if not verdict.allowed:
                    debug["risk_rejected"] += 1
                    debug_by_symbol[symbol]["risk_rejected"] += 1
                    for reason in verdict.reasons[:3]:
                        debug[f"risk_reason::{reason}"] += 1
                    continue

                debug["risk_allowed"] += 1
                debug_by_symbol[symbol]["risk_allowed"] += 1

                regime = self._market_regime(snapshot)
                trade = self._simulate_trade(candidate, candles[i + 1 :], regime)
                if trade:
                    debug["simulated_trade"] += 1
                    debug_by_symbol[symbol]["simulated_trade"] += 1
                    trades.append(trade)
                else:
                    debug["simulation_no_exit"] += 1
                    debug_by_symbol[symbol]["simulation_no_exit"] += 1

        result = self._metrics(trades)
        result["debug"] = dict(debug.most_common())
        result["debug_by_symbol"] = {sym: dict(counter.most_common()) for sym, counter in debug_by_symbol.items()}
        return result

    def _simulate_trade(self, candidate, future_candles: List[Candle], regime: str) -> BacktestTrade | None:
        entry = getattr(candidate.detection, "entry_hint", None)
        if entry is None:
            entry = getattr(candidate.detection, "close", None)
        if entry is None:
            entry = getattr(candidate.detection, "reclaim_level", None)
        if entry is None:
            return None

        sl = getattr(candidate.detection, "invalidation", None)
        if sl is None:
            sl = getattr(candidate.detection, "breakout_level", None)
        if sl is None:
            return None

        risk = abs(entry - sl)
        if risk == 0:
            return None

        tp1_rr = 0.8
        tp2_rr = 1.5
        max_hold_candles = 6

        if candidate.direction == "LONG":
            tp1 = entry + risk * tp1_rr
            tp2 = entry + risk * tp2_rr
        else:
            tp1 = entry - risk * tp1_rr
            tp2 = entry - risk * tp2_rr

        tp1_hit = False

        for idx, c in enumerate(future_candles, start=1):
            if candidate.direction == "LONG":
                if c.low <= sl:
                    return BacktestTrade(
                        candidate.symbol,
                        candidate.strategy,
                        candidate.direction,
                        entry,
                        sl,
                        tp2,
                        "SL",
                        -1.0,
                        idx,
                        tp1_hit,
                        False,
                        regime,
                    )

                if not tp1_hit and c.high >= tp1:
                    tp1_hit = True
                    sl = entry

                if c.high >= tp2:
                    return BacktestTrade(
                        candidate.symbol,
                        candidate.strategy,
                        candidate.direction,
                        entry,
                        sl,
                        tp2,
                        "TP",
                        1.5,
                        idx,
                        tp1_hit,
                        False,
                        regime,
                    )

            else:
                if c.high >= sl:
                    return BacktestTrade(
                        candidate.symbol,
                        candidate.strategy,
                        candidate.direction,
                        entry,
                        sl,
                        tp2,
                        "SL",
                        -1.0,
                        idx,
                        tp1_hit,
                        False,
                        regime,
                    )

                if not tp1_hit and c.low <= tp1:
                    tp1_hit = True
                    sl = entry

                if c.low <= tp2:
                    return BacktestTrade(
                        candidate.symbol,
                        candidate.strategy,
                        candidate.direction,
                        entry,
                        sl,
                        tp2,
                        "TP",
                        1.5,
                        idx,
                        tp1_hit,
                        False,
                        regime,
                    )

            if idx >= max_hold_candles:
                exit_price = c.close

                if candidate.direction == "LONG":
                    pnl_r = (exit_price - entry) / risk
                else:
                    pnl_r = (entry - exit_price) / risk

                return BacktestTrade(
                    candidate.symbol,
                    candidate.strategy,
                    candidate.direction,
                    entry,
                    sl,
                    tp2,
                    "TIME_EXIT",
                    round(pnl_r, 2),
                    idx,
                    tp1_hit,
                    True,
                    regime,
                )

        return None

    @staticmethod
    def _market_regime(snapshot: MarketSnapshot) -> str:
        alignment = str(getattr(snapshot, "alignment", "") or "").lower()
        primary_trend = str(getattr(snapshot.primary, "trend", "") or "").lower()
        confirmation_trend = str(getattr(snapshot.confirmation, "trend", "") or "").lower()

        if alignment == "aligned_bullish" or (primary_trend == "bullish" and confirmation_trend == "bullish"):
            return "bullish"
        if alignment == "aligned_bearish" or (primary_trend == "bearish" and confirmation_trend == "bearish"):
            return "bearish"
        return "chop"

    def _metrics(self, trades: List[BacktestTrade]) -> Dict[str, Any]:
        # Use advanced metrics (includes by_strategy and by_symbol)
        return summarize(trades)

    def _build_snapshot(self, symbol: str, candles: List[Candle]) -> MarketSnapshot:
        # Minimal snapshot builder (placeholder — later uitbreiden)
        recent_20 = candles[-20:]
        ema20 = sum(c.close for c in recent_20) / max(len(recent_20), 1)
        recent_high = max(c.high for c in recent_20)
        recent_low = min(c.low for c in recent_20)
        atr_percent = ((recent_high - recent_low) / max(candles[-1].close, 1e-9)) * 100
        volume_ratio_20 = 1.8
        trend = "bullish" if candles[-1].close >= candles[-20].close else "bearish"
        return MarketSnapshot(
            symbol=symbol,
            contract=None,
            primary=type("obj", (), {
                "candles": candles,
                "trend": trend,
                "range_pct": atr_percent,
                "change_pct": ((candles[-1].close - candles[-20].close) / max(candles[-20].close, 1e-9)) * 100,
                "volume_ratio_20": volume_ratio_20,
                "atr_percent": atr_percent,
                "ema20": ema20,
                "granularity": "15m"
            }),
            confirmation=type("obj", (), {
                "candles": candles,
                "trend": trend,
                "range_pct": atr_percent,
                "change_pct": ((candles[-1].close - candles[-20].close) / max(candles[-20].close, 1e-9)) * 100,
                "volume_ratio_20": volume_ratio_20,
                "atr_percent": atr_percent,
                "ema20": ema20,
                "granularity": "1h"
            }),
            alignment="aligned_bullish" if trend == "bullish" else "aligned_bearish",
            score_hint=75.0,
            volatility_rank=20.0,
            notes=[
                "backtest synthetic snapshot",
                "pressure_score=70.0",
                "expansion_prob=75.0",
                "breakout_context ready=true direction=bullish" if trend == "bullish" else "breakout_context ready=true direction=bearish",
                "compression=true",
                "spread 1.0bps",
            ],

        )