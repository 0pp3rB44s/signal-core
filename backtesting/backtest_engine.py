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
from market_features.engine import FeatureInputs, aggregate_candles, build_market_snapshot
from backtesting.execution_contract import BacktestExecutionConfig, BacktestExecutionContract, ExecutionRecord


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
    timeframe: str = ""
    signal_timestamp: int = 0
    requested_entry: float = 0.0
    executed_entry: float = 0.0
    entry_type: str = "MARKET"
    fill_timestamp: int | None = None
    fill_status: str = "FILLED"
    gross_pnl: float = 0.0
    entry_fees: float = 0.0
    exit_fees: float = 0.0
    total_fees: float = 0.0
    net_pnl: float = 0.0
    initial_quantity: float = 0.0
    equity_before: float = 0.0
    equity_after: float = 0.0
    intrabar_ambiguous: bool = False
    intrabar_policy_used: str = "CONSERVATIVE"


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
        self.execution_config = BacktestExecutionConfig.from_settings(settings)
        self.execution = BacktestExecutionContract(self.execution_config)

    def run(self, market_data: Dict[str, List[Candle]]) -> Dict[str, Any]:
        trades: List[BacktestTrade] = []
        execution_records: list[ExecutionRecord] = []
        equity = self.execution_config.starting_equity
        debug = Counter()
        debug_by_symbol: dict[str, Counter] = {}

        for symbol, candles in market_data.items():
            debug_by_symbol[symbol] = Counter()
            for i in range(50, len(candles) - 1):
                snapshot = self._build_snapshot(symbol, candles[: i + 1], as_of_timestamp_ms=candles[i + 1].timestamp_ms)

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
                record = self._execute_candidate(candidate, candles[i + 1 :], equity)
                execution_records.append(record)
                trade = self._trade_from_execution(candidate, record, regime)
                if trade:
                    debug["simulated_trade"] += 1
                    debug_by_symbol[symbol]["simulated_trade"] += 1
                    trades.append(trade)
                    equity = record.equity_after
                else:
                    debug["simulation_no_exit"] += 1
                    debug_by_symbol[symbol]["simulation_no_exit"] += 1

        result = self._metrics(trades)
        result["debug"] = dict(debug.most_common())
        result["debug_by_symbol"] = {sym: dict(counter.most_common()) for sym, counter in debug_by_symbol.items()}
        result["execution_records"] = [record.__dict__ for record in execution_records]
        result["starting_equity"] = self.execution_config.starting_equity
        result["ending_equity"] = equity
        result["execution_assumptions"] = self.execution_config.__dict__
        return result

    def _simulate_trade(self, candidate, future_candles: List[Candle], regime: str) -> BacktestTrade | None:
        """Compatibility adapter using the shared deterministic execution contract."""
        record = self._execute_candidate(candidate, future_candles, self.execution_config.starting_equity)
        return self._trade_from_execution(candidate, record, regime)

    def _execute_candidate(self, candidate, future_candles: List[Candle], equity: float) -> ExecutionRecord:
        entry = getattr(candidate.detection, "entry_hint", None)
        if entry is None:
            entry = getattr(candidate.detection, "close", None)
        if entry is None:
            entry = getattr(candidate.detection, "reclaim_level", None)
        if entry is None:
            entry = 0.0

        sl = getattr(candidate.detection, "invalidation", None)
        if sl is None:
            sl = getattr(candidate.detection, "breakout_level", None)
        if sl is None:
            sl = 0.0

        risk = abs(entry - sl)
        if risk == 0:
            risk = 0.0

        tp1_rr = 0.8
        tp2_rr = 1.5
        max_hold_candles = 6

        if candidate.direction == "LONG":
            tp1 = entry + risk * tp1_rr
            tp2 = entry + risk * tp2_rr
        else:
            tp1 = entry - risk * tp1_rr
            tp2 = entry - risk * tp2_rr

        signal_timestamp = int(getattr(candidate, "candidate_candle_open_timestamp_ms", 0) or 0)
        timeframe = str(getattr(candidate, "primary_granularity", "") or "")
        return self.execution.execute(
            strategy=candidate.strategy, symbol=candidate.symbol, timeframe=timeframe,
            direction=candidate.direction, signal_timestamp=signal_timestamp,
            requested_entry=float(entry), stop=float(sl), targets=[tp1, tp2],
            candles=future_candles, equity=equity,
        )

    @staticmethod
    def _trade_from_execution(candidate, record: ExecutionRecord, regime: str) -> BacktestTrade | None:
        if record.fill_status != "FILLED" or record.final_exit_reason in {"", "OPEN_AT_DATA_END"}:
            return None
        pnl_pct = record.net_pnl / record.equity_before * 100.0 if record.equity_before else 0.0
        result = "TP" if record.net_pnl > 0 else "SL" if record.net_pnl < 0 else "BE"
        return BacktestTrade(
            symbol=candidate.symbol, strategy=candidate.strategy, direction=candidate.direction,
            entry=record.executed_entry, stop_loss=record.initial_stop,
            take_profit=record.tp1_price, result=result, pnl_pct=pnl_pct,
            candles_held=record.candles_held, tp1_hit=record.tp1_quantity > 0,
            timed_exit=record.timed_exit, regime=regime, timeframe=record.timeframe,
            signal_timestamp=record.signal_timestamp, requested_entry=record.requested_entry,
            executed_entry=record.executed_entry, entry_type=record.entry_type,
            fill_timestamp=record.fill_timestamp, fill_status=record.fill_status,
            gross_pnl=record.gross_pnl, entry_fees=record.entry_fee,
            exit_fees=record.total_fees - record.entry_fee, total_fees=record.total_fees,
            net_pnl=record.net_pnl, initial_quantity=record.initial_quantity,
            equity_before=record.equity_before, equity_after=record.equity_after,
            intrabar_ambiguous=record.intrabar_ambiguous,
            intrabar_policy_used=record.intrabar_policy_used,
        )

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

    def _build_snapshot(self, symbol: str, candles: List[Candle], *, as_of_timestamp_ms: int, inputs: FeatureInputs | None = None) -> MarketSnapshot:
        hourly = aggregate_candles(candles, "15m", "1h", as_of_timestamp_ms)
        return build_market_snapshot(symbol, candles, hourly, as_of_timestamp_ms=as_of_timestamp_ms, inputs=inputs or FeatureInputs())
