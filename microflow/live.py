from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from candidate_lifecycle import deterministic_plan_id
from clients.schemas import TradePlan
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


def size_microflow_position(*, equity_usdt: float, risk_pct: float,
                            notional_cap_usdt: float, leverage: float,
                            taker_fee_rate: float, slippage_bps: float) -> MicroflowSizing:
    """Size from stop+fees+entry slippage; leverage is only a margin ceiling."""
    if equity_usdt <= 0 or risk_pct <= 0 or leverage <= 0 or taker_fee_rate <= 0:
        raise ValueError("positive authenticated equity, risk, leverage and taker fee required")
    loss_fraction = (
        FrozenResearchSpec().sl_bps + slippage_bps + taker_fee_rate * 20_000.0
    ) / 10_000.0
    risk_budget = equity_usdt * risk_pct / 100.0
    notional = min(
        risk_budget / loss_fraction,
        notional_cap_usdt,
        equity_usdt * leverage,
    )
    total_loss = notional * loss_fraction
    return MicroflowSizing(
        notional_usdt=round(notional, 8),
        risk_budget_usdt=round(risk_budget, 8),
        total_loss_usdt=round(total_loss, 8),
        total_loss_pct_equity=round(total_loss / equity_usdt * 100.0, 8),
        taker_fee_rate=taker_fee_rate,
    )


class MicroflowLiveRuntime:
    """Public-feed coordinator; all private execution stays in ExecutionService."""

    def __init__(self, *, settings, execution_client, execute_plans) -> None:
        self.settings = settings
        self.client = execution_client
        self.execute_plans = execute_plans
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

    def _fee_rates(self, symbol: str) -> tuple[float, float]:
        data = (self.client.get_trade_fee_rate(symbol=symbol, business_type="mix").get("data") or {})
        maker, taker = float(data.get("makerFeeRate")), float(data.get("takerFeeRate"))
        if maker < 0 or taker <= 0:
            raise RuntimeError("authenticated fee rates invalid")
        return maker, taker

    def _build_plan(self, candidate: dict) -> TradePlan:
        symbol = str(candidate["symbol"]).upper()
        direction = str(candidate["side"]).upper()
        reference = float(candidate["entry_reference"])
        equity = self._account_equity(self.client.get_accounts())
        maker, taker = self._fee_rates(symbol)
        leverage = min(
            float(self.settings.microflow_leverage),
            float(self.settings.default_leverage),
            float(self.settings.max_leverage),
            5.0,
        )
        sizing = size_microflow_position(
            equity_usdt=equity,
            risk_pct=float(self.settings.account_risk_per_trade_pct),
            notional_cap_usdt=float(self.settings.execution_max_live_notional_per_trade_usdt),
            leverage=leverage,
            taker_fee_rate=taker,
            slippage_bps=float(self.settings.microflow_max_slippage_bps),
        )
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
            f"max_planned_loss_usdt={sizing.total_loss_usdt}",
            f"max_planned_loss_pct={sizing.total_loss_pct_equity}",
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
            account_risk_pct=float(self.settings.account_risk_per_trade_pct),
            leverage=leverage, position_notional_usdt=sizing.notional_usdt,
            notes=notes, reasons=[PILOT_STATUS], tp_size_pcts=[100.0],
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
            equity = self._account_equity(self.client.get_accounts())
            maker, taker = self._fee_rates(plan.symbol)
            leverage = min(
                float(self.settings.microflow_leverage),
                float(self.settings.default_leverage), float(self.settings.max_leverage), 5.0,
            )
            sizing = size_microflow_position(
                equity_usdt=equity,
                risk_pct=float(self.settings.account_risk_per_trade_pct),
                notional_cap_usdt=float(self.settings.execution_max_live_notional_per_trade_usdt),
                leverage=leverage, taker_fee_rate=taker,
                slippage_bps=cap_bps,
            )
        except Exception as exc:
            return {"allowed": False, "reason": f"authenticated_risk_recheck_failed:{exc}"}
        if float(plan.position_notional_usdt) > sizing.notional_usdt + 1e-8:
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
