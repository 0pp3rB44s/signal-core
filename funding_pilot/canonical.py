"""Canonical adapter for the frozen funding-crowding live pilot.

Requests contain signal facts only. Risk truth is rebuilt before every
admission from PilotRuntime's durable ledger and its pilot-owned exchange view.
"""

from __future__ import annotations

from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import TradePlan
from funding_pilot.core import FROZEN_SPEC_SHA256, STRATEGY, FailClosed, PilotRuntime, PilotSignal

DAY_MS = 24 * 60 * 60 * 1000


class CanonicalFundingPilot:
    def __init__(self, runtime: PilotRuntime, execution_service, position_manager) -> None:
        self.runtime = runtime
        self.execution_service = execution_service
        self.position_manager = position_manager
        self.execution_service.funding_pilot_state_provider = self.authoritative_state

    def authoritative_state(self) -> dict:
        state = self.runtime.reconcile()
        truth = state["truth"]
        economics = state["economics"]
        gross = sum(float(row.get("notional") or 0.0) for row in truth.pilot_positions)
        safe_margin = float(truth.available_margin) - float(truth.non_pilot_reserved_margin)
        if safe_margin < 0:
            raise FailClosed("protected production margin consumes available balance")
        if any(not str(row.get("client_oid") or "").startswith("cgc-fcp-") for row in truth.pilot_working_orders):
            raise FailClosed("pilot working-order ownership uncertain")
        if any(not str(row.get("client_oid") or "").startswith("cgc-fcp-") for row in truth.pilot_stops):
            raise FailClosed("pilot stop ownership uncertain")
        return {
            "pilot_nav": float(economics["nav"]),
            "available_margin": safe_margin,
            "gross_notional": gross,
            "position_count": len(truth.pilot_positions),
            "kill_switch_latched": self.runtime.ledger.get("status") != "ACTIVE",
            "native_stop_available": all(
                callable(getattr(self.runtime.exchange, name, None))
                for name in ("place_native_stop", "verify_native_stop")
            ),
            "high_water_mark": float(self.runtime.ledger.get("high_water_mark") or 0.0),
        }

    def build_plan(self, signal: PilotSignal) -> TradePlan:
        state = self.runtime.reconcile()
        notional = self.runtime.size(signal, state)
        direction = signal.side.upper()
        if direction not in {"LONG", "SHORT"} or signal.reference_price <= 0:
            raise FailClosed("signal request identity invalid")
        stop = signal.reference_price * (0.90 if direction == "LONG" else 1.10)
        candidate = deterministic_candidate_id(
            STRATEGY, signal.symbol.upper(), direction, signal.timestamp_ms
        )
        return TradePlan(
            candidate_id=candidate,
            candidate_candle_open_timestamp_ms=signal.timestamp_ms,
            plan_id=deterministic_plan_id(candidate),
            symbol=signal.symbol.upper(), strategy=STRATEGY, direction=direction,
            verdict="EXECUTABLE", score=100.0, entry_prices=[signal.reference_price],
            stop_loss=stop, take_profits=[], risk_reward_ratio=0.0,
            account_risk_pct=10.0, leverage=1.0,
            position_notional_usdt=notional,
            notes=[f"pilot_signal_id={signal.signal_id}"], reasons=["frozen funding-crowding signal"],
            geometry_entry=signal.reference_price,
            protection_mode="STOP_ONLY_TIME_EXIT",
            scheduled_exit_at_ms=signal.timestamp_ms + DAY_MS,
            frozen_spec_sha256=FROZEN_SPEC_SHA256,
            pilot_authorized=bool(self.runtime.config.orders_enabled),
        )

    def process_signal(self, signal: PilotSignal):
        plan = self.build_plan(signal)
        entry_oid = self.execution_service.entry_submitter.client_oid_for(plan)
        if not entry_oid.startswith("cgc-fcp-"):
            raise FailClosed("pilot entry ownership namespace unavailable")
        self.runtime.ledger.append(
            "ENTRY_INTENT",
            {"plan_id": plan.plan_id, "symbol": plan.symbol, "entry_client_oid": entry_oid,
             "scheduled_exit_at_ms": plan.scheduled_exit_at_ms},
            signal_id=signal.signal_id, symbol=plan.symbol,
        )
        reports = self.execution_service.execute([plan])
        if not reports or reports[0].status != "EXECUTED":
            raise FailClosed("canonical execution did not produce a protected position")
        stored = self.execution_service.store.load(default=[])
        row = next(
            (item for item in reversed(stored)
             if item.get("plan_id") == plan.plan_id and item.get("status") == "OPEN"),
            None,
        )
        if not row or not row.get("entry_protection_verified"):
            raise FailClosed("protected position persistence not confirmed")
        stop_ack_id = str((row.get("protection_payload") or {}).get("stop_order_id") or "")
        self.runtime.ledger.append(
            "STOP_ACK",
            {"symbol": plan.symbol, "entry_client_oid": entry_oid,
             "stop_order_id": stop_ack_id, "scheduled_exit_at_ms": plan.scheduled_exit_at_ms,
             "exchange_position_id": row.get("exchange_position_id"),
             "entry_order_id": row.get("exchange_entry_order_id")},
            signal_id=signal.signal_id, symbol=plan.symbol,
        )
        truth = self.runtime.exchange.truth()
        exchange_position = next(
            (item for item in truth.pilot_positions
             if str(item.get("symbol") or "").upper() == plan.symbol), None,
        )
        exchange_stop = next(
            (item for item in truth.pilot_stops
             if str(item.get("symbol") or "").upper() == plan.symbol), None,
        )
        persisted_stop_id = stop_ack_id
        exchange_stop_id = str((exchange_stop or {}).get("order_id") or (exchange_stop or {}).get("plan_order_id") or "")
        if not exchange_position or not exchange_stop or not persisted_stop_id or persisted_stop_id != exchange_stop_id:
            # ExecutionService already owns immediate flatten when placement or
            # read-back fails. This second reconciliation prevents a transient
            # acknowledgement from becoming durable pilot state.
            if exchange_position:
                self.runtime.exchange.close_reduce_only(exchange_position, "post_ack_stop_mismatch")
                flattened = self.runtime.exchange.truth()
                if any(str(item.get("symbol") or "").upper() == plan.symbol for item in flattened.pilot_positions):
                    raise FailClosed("post-ack stop mismatch flatten unconfirmed")
                for order in flattened.pilot_working_orders:
                    self.runtime.exchange.cancel_working_order(order)
                for stop_order in flattened.pilot_stops:
                    self.runtime.exchange.cancel_stop(stop_order)
                final = self.runtime.exchange.truth()
                if final.pilot_positions or final.pilot_working_orders or final.pilot_stops:
                    raise FailClosed("post-ack stop mismatch residual exposure")
            raise FailClosed("exchange-native stop identity not reconciled after entry; flattened")
        self.runtime.ledger.append(
            "CANONICAL_OPEN",
            {
                "plan_id": plan.plan_id, "symbol": plan.symbol,
                "scheduled_exit_at_ms": plan.scheduled_exit_at_ms,
                "notional": plan.position_notional_usdt,
                "stop_order_id": (row.get("protection_payload") or {}).get("stop_order_id"),
                "entry_client_oid": row.get("exchange_entry_client_oid"),
                "exchange_position_id": row.get("exchange_position_id"),
                "entry_order_id": row.get("exchange_entry_order_id"),
            },
            signal_id=signal.signal_id, symbol=plan.symbol,
        )
        return reports[0]

    def recover(self) -> dict:
        """Reconcile restart truth and restore scheduled exits from the ledger."""
        state = self.authoritative_state()
        opens = self.runtime.ledger.events("CANONICAL_OPEN")
        truth = self.runtime.exchange.truth()
        schedules = {row["symbol"]: int(row["payload"]["scheduled_exit_at_ms"]) for row in opens}
        for position in truth.pilot_positions:
            symbol = str(position.get("symbol") or "").upper()
            if symbol not in schedules:
                self.runtime.ledger.set("status", "HALTED")
                raise FailClosed(f"open pilot position has no durable schedule: {symbol}")
        local = self.position_manager.store.load(default=[])
        live_symbols = {str(row.get("symbol") or "").upper() for row in truth.pilot_positions}
        changed = False
        for row in local:
            if row.get("protection_mode") == "STOP_ONLY_TIME_EXIT" and row.get("status") == "OPEN":
                symbol = str(row.get("symbol") or "").upper()
                if symbol not in live_symbols:
                    row["status"] = "CLOSED_RECOVERED_EXCHANGE_FLAT"
                    changed = True
        local_open = {str(row.get("symbol") or "").upper() for row in local
                      if row.get("protection_mode") == "STOP_ONLY_TIME_EXIT" and row.get("status") == "OPEN"}
        for exchange_position in truth.pilot_positions:
            symbol = str(exchange_position.get("symbol") or "").upper()
            if symbol in local_open:
                continue
            stop = next((item for item in truth.pilot_stops
                         if str(item.get("symbol") or "").upper() == symbol), None)
            if not stop:
                self.runtime.ledger.set("status", "HALTED")
                raise FailClosed(f"restart position has no native stop: {symbol}")
            local.append({
                "symbol": symbol, "direction": str(exchange_position.get("side") or exchange_position.get("direction") or "").upper(),
                "strategy": "funding_crowding_continuation_24h", "status": "OPEN",
                "protection_mode": "STOP_ONLY_TIME_EXIT", "scheduled_exit_at_ms": schedules[symbol],
                "frozen_spec_sha256": FROZEN_SPEC_SHA256, "pilot_authorized": True,
                "exchange_avg_entry": exchange_position.get("entry_price"),
                "confirmed_position_size": exchange_position.get("size"),
                "position_notional_usdt": exchange_position.get("notional"),
                "stop_loss": stop.get("stop_price") or stop.get("triggerPrice"),
                "protection_payload": {"stop_order_id": stop.get("order_id")},
                "entry_protection_verified": True, "recovered_from_exchange": True,
            })
            changed = True
        if changed:
            self.position_manager.store.save(local)
        return {**state, "scheduled_exits": schedules}

    def process_time_exits(self, *, now_ms: int) -> list[dict]:
        outcomes = self.position_manager.process_stop_only_time_exits(now_ms=now_ms)
        for outcome in outcomes:
            if outcome.get("status") != "POSITION_CLOSED_STOP_CANCELLED":
                continue
            symbol = str(outcome.get("symbol") or "").upper()
            truth = self.runtime.exchange.truth()
            if any(str(row.get("symbol") or "").upper() == symbol for row in truth.pilot_positions):
                raise FailClosed("post-exit exchange position is not zero")
            if any(str(row.get("symbol") or "").upper() == symbol for row in truth.pilot_working_orders):
                raise FailClosed("post-exit pilot working order remains")
            if any(str(row.get("symbol") or "").upper() == symbol for row in truth.pilot_stops):
                raise FailClosed("post-exit orphan stop remains")
            self.runtime.ledger.append("CANONICAL_TIME_EXIT", outcome, symbol=symbol)
        return outcomes
