from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import (
    ContractSpec,
    MarketSnapshot,
    StrategyCandidate,
    StrategyScore,
    SweepDetection,
    TimeframeSnapshot,
    TradePlan,
)
from microflow.candidates import CandidateEpisodeSampler, FrozenResearchSpec
from microflow.collector import MicroflowCollector


STRATEGY_ID = "microflow_scalper_v1"
PILOT_STATUS = "EXPERIMENTAL_LIVE_PILOT"


class MicroflowPhase(str, Enum):
    IDLE = "IDLE"
    PRESSURE_FORMING = "PRESSURE_FORMING"
    CANDIDATE = "CANDIDATE"
    ENTRY_PENDING = "ENTRY_PENDING"
    OPEN = "OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    COOLDOWN = "COOLDOWN"


@dataclass(frozen=True)
class MicroflowSizing:
    notional_usdt: float
    risk_budget_usdt: float
    total_loss_usdt: float
    total_loss_pct_equity: float
    taker_fee_rate: float
    margin_usdt: float = 0.0
    margin_per_slot_usdt: float = 0.0
    binding_constraint: str = ""


def size_microflow_position(*, equity_usdt: float, available_usdt: float,
                            committed_margin_usdt: float, leverage: float,
                            taker_fee_rate: float, slippage_bps: float,
                            margin_reserve_pct: float, max_notional_pct_equity: float,
                            max_loss_pct_equity: float,
                            max_open_positions: int,
                            min_notional_usdt: float = 5.0) -> MicroflowSizing:
    """Equity-based, portfolio-aware sizing for one MicroFlow position.

    The account balance is the basis, not a fixed USDT ceiling. Usable margin is
    split evenly across the position slots so filling slot 1 cannot starve slot 2,
    and the reserve keeps fees and execution variation out of the margin that gets
    committed.

    Risk stays explicit but as a *bound*, not as the sizer: the resulting loss at
    the stop is computed and the trade is refused if it exceeds
    ``max_loss_pct_equity``. Leverage is a margin multiplier and never a reason to
    size larger than that bound allows.
    """
    if equity_usdt <= 0 or leverage <= 0 or taker_fee_rate <= 0:
        raise ValueError("positive authenticated equity, leverage and taker fee required")
    if available_usdt < 0 or committed_margin_usdt < 0:
        raise ValueError("balances may not be negative")
    if max_open_positions < 1:
        raise ValueError("max_open_positions must be >=1")
    if not (0.0 <= margin_reserve_pct < 100.0):
        raise ValueError("margin_reserve_pct must be >=0 and <100")
    if max_notional_pct_equity <= 0 or max_loss_pct_equity <= 0:
        raise ValueError("notional and loss ceilings must be positive")

    loss_fraction = (
        FrozenResearchSpec().sl_bps + slippage_bps + taker_fee_rate * 20_000.0
    ) / 10_000.0
    keep = 1.0 - margin_reserve_pct / 100.0

    # One slot's share of the usable balance, so two positions can coexist.
    margin_per_slot = available_usdt * keep / max_open_positions
    # Never commit more than what is actually free right now.
    free_margin = max(0.0, available_usdt - committed_margin_usdt) * keep
    margin = min(margin_per_slot, free_margin)

    notional_from_margin = margin * leverage
    notional_ceiling = equity_usdt * max_notional_pct_equity / 100.0
    notional = min(notional_from_margin, notional_ceiling)
    binding = "margin_slot" if notional_from_margin <= notional_ceiling else "notional_pct_equity"

    if notional < min_notional_usdt:
        # Covers "no free margin left" as well as a balance too small to trade.
        # Refusing is the honest outcome; a zero- or dust-sized order would look
        # like a successful trade and hide the fact that sizing had nothing to work
        # with.
        raise ValueError(
            f"sizable notional {notional:.8f} is below the exchange minimum "
            f"{min_notional_usdt} (margin available {margin:.8f})"
        )

    total_loss = notional * loss_fraction
    loss_pct = total_loss / equity_usdt * 100.0
    if loss_pct > max_loss_pct_equity:
        # Fail closed rather than silently shrinking: a sizing input that lands
        # here is wrong, and a quietly smaller trade would hide it.
        raise ValueError(
            f"planned loss {loss_pct:.4f}% of equity exceeds "
            f"MICROFLOW_MAX_LOSS_PCT_EQUITY={max_loss_pct_equity}"
        )
    return MicroflowSizing(
        notional_usdt=round(notional, 8),
        risk_budget_usdt=round(equity_usdt * max_loss_pct_equity / 100.0, 8),
        total_loss_usdt=round(total_loss, 8),
        total_loss_pct_equity=round(loss_pct, 8),
        taker_fee_rate=taker_fee_rate,
        margin_usdt=round(margin, 8),
        margin_per_slot_usdt=round(margin_per_slot, 8),
        binding_constraint=binding,
    )


class MicroflowLiveRuntime:
    """Public-feed coordinator; all private execution stays in ExecutionService."""

    def __init__(self, *, settings, execution_client, execute_plans, risk_manager) -> None:
        self.settings = settings
        self.client = execution_client
        self.execute_plans = execute_plans
        self.risk_manager = risk_manager
        self.log = logging.getLogger(self.__class__.__name__)
        self.spec = FrozenResearchSpec()
        self._validator = CandidateEpisodeSampler(self.spec)
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=128)
        self._phases = {symbol: MicroflowPhase.IDLE for symbol in settings.microflow_symbol_set}
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self.collector = MicroflowCollector(
            symbols=tuple(settings.production_symbols),
            data_dir=Path(settings.microflow_data_dir),
            candidate_callback=self._candidate,
            snapshot_callback=self._snapshot_state,
        )
        self.collector_thread: threading.Thread | None = None
        self.worker_thread: threading.Thread | None = None

    @property
    def phases(self) -> dict[str, str]:
        return {symbol: phase.value for symbol, phase in self._phases.items()}

    def start(self) -> None:
        if self.collector_thread is not None:
            return
        self.collector_thread = threading.Thread(
            target=self.collector.run, name="microflow-public-feed", daemon=True
        )
        self.worker_thread = threading.Thread(
            target=self._worker, name="microflow-live-worker", daemon=True
        )
        self.collector_thread.start()
        self.worker_thread.start()
        self.log.critical(
            "MICROFLOW_LIVE_RUNTIME_STARTED | status=%s | strategy=%s | symbols=%s | "
            "tp_bps=40 | sl_bps=20 | max_hold_ms=600000 | execution=IOC | slippage_cap_bps=%s",
            PILOT_STATUS, STRATEGY_ID, len(self.settings.microflow_symbol_set),
            self.settings.microflow_max_slippage_bps,
        )

    def stop(self) -> None:
        self._stop.set()
        self.collector.stop()

    def _candidate(self, candidate: dict) -> None:
        candidate_id = str(candidate.get("candidate_id") or "")
        symbol = str(candidate.get("symbol") or "").upper()
        if not candidate_id or candidate_id in self._seen or symbol not in self._phases:
            return
        self._seen.add(candidate_id)
        self._phases[symbol] = MicroflowPhase.CANDIDATE
        try:
            self._queue.put_nowait(candidate)
        except queue.Full:
            self._phases[symbol] = MicroflowPhase.COOLDOWN
            self.log.error("MICROFLOW_CANDIDATE_QUEUE_FULL | id=%s | symbol=%s", candidate_id, symbol)

    def _snapshot_state(self, snapshot: dict) -> None:
        symbol = str(snapshot.get("symbol") or "").upper()
        if symbol not in self._phases or self._phases[symbol] in {
            MicroflowPhase.ENTRY_PENDING, MicroflowPhase.OPEN,
            MicroflowPhase.EXIT_PENDING, MicroflowPhase.COOLDOWN,
        }:
            return
        sampler = self.collector.samplers[symbol]
        self._phases[symbol] = (
            MicroflowPhase.PRESSURE_FORMING
            if sampler.forming_direction else MicroflowPhase.IDLE
        )

    @staticmethod
    def _account_equity(payload: dict) -> float:
        for row in payload.get("data") or []:
            if str(row.get("marginCoin") or "").upper() == "USDT":
                value = float(row.get("accountEquity") or row.get("usdtEquity") or row.get("equity") or 0)
                if value > 0:
                    return value
        raise RuntimeError("authenticated USDT account equity unavailable")

    @staticmethod
    def _margin_state(payload: dict) -> tuple[float, float]:
        """Free balance and margin already committed, from the same authenticated read.

        Taken from one payload rather than two calls so sizing cannot be built from
        an available balance and a committed figure observed at different moments.
        """
        for row in payload.get("data") or []:
            if str(row.get("marginCoin") or "").upper() != "USDT":
                continue
            available = float(row.get("available") or 0)
            equity = float(row.get("accountEquity") or row.get("usdtEquity") or 0)
            # Whatever equity is not available is already working: isolated margin
            # on open positions, plus anything locked by resting orders.
            committed = max(0.0, equity - available)
            if available >= 0 and equity > 0:
                return available, committed
        raise RuntimeError("authenticated USDT margin state unavailable")

    def _effective_leverage(self) -> float:
        """Lowest of every configured ceiling. Config validation owns the upper bound."""
        return min(
            float(self.settings.microflow_leverage),
            float(self.settings.default_leverage),
            float(self.settings.max_leverage),
        )

    def _fee_rates(self, symbol: str) -> tuple[float, float]:
        data = (self.client.get_trade_fee_rate(symbol=symbol, business_type="mix").get("data") or {})
        maker, taker = float(data.get("makerFeeRate")), float(data.get("takerFeeRate"))
        if maker < 0 or taker <= 0:
            raise RuntimeError("authenticated fee rates invalid")
        return maker, taker

    @staticmethod
    def _risk_candidate(candidate: dict) -> StrategyCandidate:
        """Translate the frozen feed episode into the canonical risk schema."""
        symbol = str(candidate["symbol"]).upper()
        direction = str(candidate["side"]).upper()
        signal_ts = int(candidate["signal_ts"])
        reference = float(candidate["entry_reference"])
        features = candidate.get("features") or {}
        book = features.get("book") or {}
        trend = "bullish" if direction == "LONG" else "bearish"
        timeframe = TimeframeSnapshot(
            symbol=symbol,
            granularity="microflow",
            latest_close=reference,
            change_pct=0.0,
            range_pct=0.0,
            volume_ratio_20=0.0,
            ema20=reference,
            ema50=reference,
            trend=trend,
            candles=[],
            closed_candle_timestamp_ms=signal_ts,
            as_of_timestamp_ms=signal_ts,
        )
        market = MarketSnapshot(
            symbol=symbol,
            contract=ContractSpec(
                symbol=symbol,
                product_type="USDT-FUTURES",
                quote_coin="USDT",
                base_coin=symbol.removesuffix("USDT"),
                status="live",
                min_trade_num=None,
                size_multiplier=None,
                price_place=None,
                volume_24h_usdt=None,
                change_pct_24h=None,
                raw={},
            ),
            primary=timeframe,
            confirmation=timeframe,
            alignment=f"aligned_{trend}",
            score_hint=100.0,
            notes=[],
        )
        detection = SweepDetection(
            side=direction,
            swept_level=reference,
            sweep_extreme=reference,
            reclaim_level=reference,
            entry_hint=reference,
            invalidation=float(candidate["stop_loss"]),
            displacement_pct=0.0,
            bars_since_sweep=0,
            volume_ratio_on_sweep=0.0,
            local_range_size_pct=0.0,
            reason_flags=[],
        )
        canonical_id = deterministic_candidate_id(
            STRATEGY_ID, symbol, direction, signal_ts
        )
        return StrategyCandidate(
            candidate_id=canonical_id,
            candidate_candle_open_timestamp_ms=signal_ts,
            symbol=symbol,
            strategy=STRATEGY_ID,
            direction=direction,
            primary_granularity="microflow",
            confirmation_granularity="microflow",
            market=market,
            detection=detection,
            notes=[
                f"microflow_source_candidate_id={candidate['candidate_id']}",
                f"spread_bps={book.get('spread_bps')}",
                "risk_schema_adapter=microflow_v1",
            ],
        )

    def _build_plan(self, candidate: dict) -> TradePlan:
        symbol = str(candidate["symbol"]).upper()
        direction = str(candidate["side"]).upper()
        reference = float(candidate["entry_reference"])
        accounts = self.client.get_accounts()
        equity = self._account_equity(accounts)
        available, committed = self._margin_state(accounts)
        maker, taker = self._fee_rates(symbol)
        leverage = self._effective_leverage()
        sizing = size_microflow_position(
            equity_usdt=equity,
            available_usdt=available,
            committed_margin_usdt=committed,
            leverage=leverage,
            taker_fee_rate=taker,
            slippage_bps=float(self.settings.microflow_max_slippage_bps),
            margin_reserve_pct=float(self.settings.microflow_margin_reserve_pct),
            max_notional_pct_equity=float(self.settings.microflow_max_notional_pct_equity),
            max_loss_pct_equity=float(self.settings.microflow_max_loss_pct_equity),
            max_open_positions=int(self.settings.max_open_positions),
        )
        risk_candidate = self._risk_candidate(candidate)
        risk = self.risk_manager.evaluate(
            risk_candidate,
            StrategyScore(total=100.0, breakdown={"frozen_microflow": 100.0}, verdict="GO", reasons=[]),
            observed_equity=equity,
            proposed_notional_usdt=sizing.notional_usdt,
        )
        if not risk.allowed:
            raise RuntimeError("RiskManager blocked MicroFlow: " + " | ".join(risk.reasons))
        base_risk_pct = min(
            float(self.settings.account_risk_per_trade_pct),
            float(self.risk_manager.SAFE_ALPHA_MAX_RISK_PCT),
        )
        if base_risk_pct <= 0:
            raise RuntimeError("RiskManager returned an unusable base risk percentage")
        allocation_multiplier = min(1.0, max(0.0, float(risk.account_risk_pct) / base_risk_pct))
        allocated_notional = round(sizing.notional_usdt * allocation_multiplier, 8)
        if allocated_notional < 5.0:
            raise RuntimeError("RiskManager allocation reduces MicroFlow below exchange minimum")
        features = candidate.get("features") or {}
        flow = features.get("trade_flow") or {}
        book = features.get("book") or {}
        microprice = features.get("microprice") or {}
        freshness = features.get("freshness") or {}
        notes = [
            f"pilot_status={PILOT_STATUS}", f"strategy_version={STRATEGY_ID}",
            f"signal_timestamp_ms={int(candidate['signal_ts'])}",
            *(f"ofi_{window}={((flow.get(window) or {}).get('ofi'))}" for window in ("1s", "5s", "15s", "30s", "60s")),
            f"book_imbalance_top1={book.get('book_imbalance_top1')}",
            f"book_imbalance_top5={book.get('book_imbalance_top5')}",
            f"microprice={microprice.get('microprice')}",
            f"microprice_edge_bps={microprice.get('microprice_vs_mid_bps')}",
            f"spread_bps={book.get('spread_bps')}",
            f"persistence_ms={candidate.get('persistence_ms')}",
            f"trade_feed_age_ms={freshness.get('trade_stream_age_ms')}",
            f"book_feed_age_ms={freshness.get('book_stream_age_ms')}",
            f"maker_fee_rate={maker}", f"taker_fee_rate={taker}",
            f"max_planned_loss_usdt={round(sizing.total_loss_usdt * allocation_multiplier, 8)}",
            f"max_planned_loss_pct={round(sizing.total_loss_pct_equity * allocation_multiplier, 8)}",
            f"risk_allocation_multiplier={allocation_multiplier}",
            "execution_method=capped_marketable_limit_ioc",
            f"slippage_cap_bps={self.settings.microflow_max_slippage_bps}",
        ]
        candidate_id = str(candidate["candidate_id"])
        return TradePlan(
            candidate_id=candidate_id,
            candidate_candle_open_timestamp_ms=int(candidate["signal_ts"]),
            plan_id=deterministic_plan_id(candidate_id),
            symbol=symbol, strategy=STRATEGY_ID, direction=direction,
            verdict="EXECUTABLE", score=100.0, entry_prices=[reference],
            stop_loss=float(candidate["stop_loss"]),
            take_profits=[float(candidate["take_profit"])],
            risk_reward_ratio=2.0,
            account_risk_pct=float(risk.account_risk_pct),
            leverage=leverage, position_notional_usdt=allocated_notional,
            notes=notes, reasons=[PILOT_STATUS, *risk.reasons], tp_size_pcts=[100.0],
            geometry_entry=reference,
        )

    def pre_submit_guard(self, plan: TradePlan) -> dict:
        """Re-check frozen signal and derive the one-bps marketable limit."""
        snapshot = self.collector.latest_snapshot(plan.symbol)
        now_ms = int(time.time() * 1000)
        if snapshot is None:
            return {"allowed": False, "reason": "snapshot_unavailable"}
        direction = self._validator._direction(snapshot)
        if direction != plan.direction:
            return {"allowed": False, "reason": "signal_no_longer_valid"}
        if now_ms - int(plan.candidate_candle_open_timestamp_ms) > 60_000:
            return {"allowed": False, "reason": "candidate_stale"}
        book = snapshot["book"]
        current = float(book["best_ask"] if plan.direction == "LONG" else book["best_bid"])
        reference = float(plan.geometry_entry)
        cap_bps = float(self.settings.microflow_max_slippage_bps)
        limit = reference * (1 + cap_bps / 10_000.0) if plan.direction == "LONG" else reference * (1 - cap_bps / 10_000.0)
        marketable = min(current, limit) if plan.direction == "LONG" else max(current, limit)
        if (plan.direction == "LONG" and current > limit) or (plan.direction == "SHORT" and current < limit):
            return {"allowed": False, "reason": "slippage_cap_exceeded"}
        remaining_tp_bps = abs(float(plan.take_profits[0]) - current) / current * 10_000.0
        try:
            accounts = self.client.get_accounts()
            equity = self._account_equity(accounts)
            available, committed = self._margin_state(accounts)
            maker, taker = self._fee_rates(plan.symbol)
            leverage = self._effective_leverage()
            sizing = size_microflow_position(
                equity_usdt=equity,
                available_usdt=available,
                committed_margin_usdt=committed,
                leverage=leverage, taker_fee_rate=taker,
                slippage_bps=cap_bps,
                margin_reserve_pct=float(self.settings.microflow_margin_reserve_pct),
                max_notional_pct_equity=float(self.settings.microflow_max_notional_pct_equity),
                max_loss_pct_equity=float(self.settings.microflow_max_loss_pct_equity),
                max_open_positions=int(self.settings.max_open_positions),
            )
            source_candidate = {
                "candidate_id": plan.candidate_id,
                "symbol": plan.symbol,
                "side": plan.direction,
                "signal_ts": plan.candidate_candle_open_timestamp_ms,
                "entry_reference": plan.geometry_entry,
                "stop_loss": plan.stop_loss,
                "take_profit": plan.take_profits[0],
                "features": snapshot,
            }
            risk = self.risk_manager.evaluate(
                self._risk_candidate(source_candidate),
                StrategyScore(total=100.0, breakdown={"frozen_microflow": 100.0}, verdict="GO", reasons=[]),
                observed_equity=equity,
                proposed_notional_usdt=float(plan.position_notional_usdt),
            )
            if not risk.allowed:
                return {"allowed": False, "reason": "risk_manager_recheck_blocked:" + " | ".join(risk.reasons)}
            base_risk_pct = min(
                float(self.settings.account_risk_per_trade_pct),
                float(self.risk_manager.SAFE_ALPHA_MAX_RISK_PCT),
            )
            allocation_multiplier = min(1.0, max(0.0, float(risk.account_risk_pct) / base_risk_pct))
            risk_allowed_notional = sizing.notional_usdt * allocation_multiplier
        except Exception as exc:
            return {"allowed": False, "reason": f"authenticated_risk_recheck_failed:{exc}"}
        if float(plan.position_notional_usdt) > risk_allowed_notional + 1e-8:
            return {"allowed": False, "reason": "risk_budget_shrank"}
        required_move_bps = taker * 20_000.0 + cap_bps
        if remaining_tp_bps <= required_move_bps:
            return {"allowed": False, "reason": "remaining_tp_does_not_clear_fees"}
        return {
            "allowed": True, "reason": "frozen_signal_revalidated",
            "limit_price": marketable, "current_price": current,
            "decision_latency_ms": now_ms - int(plan.candidate_candle_open_timestamp_ms),
            "remaining_tp_bps": remaining_tp_bps,
            "maker_fee_rate": maker, "taker_fee_rate": taker,
            "max_planned_loss_usdt": sizing.total_loss_usdt,
            "max_planned_loss_pct": sizing.total_loss_pct_equity,
        }

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                candidate = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            symbol = str(candidate.get("symbol") or "").upper()
            try:
                self._phases[symbol] = MicroflowPhase.ENTRY_PENDING
                plan = self._build_plan(candidate)
                reports = self.execute_plans([plan])
                executed = any(getattr(report, "status", "") == "EXECUTED" for report in reports)
                self._phases[symbol] = MicroflowPhase.OPEN if executed else MicroflowPhase.COOLDOWN
            except Exception:
                self._phases[symbol] = MicroflowPhase.COOLDOWN
                self.log.exception(
                    "MICROFLOW_LIVE_DECISION_FAILED_CLOSED | id=%s | symbol=%s",
                    candidate.get("candidate_id"), symbol,
                )


__all__ = [
    "MicroflowLiveRuntime", "MicroflowPhase", "MicroflowSizing", "PILOT_STATUS",
    "STRATEGY_ID", "size_microflow_position",
]
