"""Runtime coordinator for dynamic_grid_v1.

SHADOW evaluates and records the exact decisions used by LIVE while making no
order calls. LIVE is intentionally small: one long grid, three equal-notional
post-only entries, one maker TP per fill, and market close only for hard
invalidation. Any ambiguous exchange state stops new actions.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from execution.maker_entry import submit_persistent_maker_entry
from execution.dynamic_grid_shadow import DynamicGridShadow
from execution.order_identity import ENTRY_LEG_MAKER
from execution.state_store import JsonStateStore
from strategies.dynamic_grid_v1 import GridDecision, GridRegime, build_grid_decision, reset_allowed


_FILLED = {"filled", "full_fill", "full-filled"}
_PARTIAL = {"partial_fill", "partially_filled", "partial-filled"}
_DEAD = {"cancelled", "canceled", "rejected", "expired"}


class DynamicGridService:
    def __init__(self, *, settings: Any, client: Any, cache: Any, entry_submitter: Any = None) -> None:
        self.settings = settings
        self.client = client
        self.cache = cache
        self.entry_submitter = entry_submitter
        self.log = logging.getLogger("dynamic_grid_v1")
        self.store = JsonStateStore(settings.dynamic_grid_state_path)
        self.shadow = DynamicGridShadow(
            settings=settings,
            store=JsonStateStore(settings.dynamic_grid_shadow_state_path),
            emit=self._event,
        )
        self.events_path = Path(settings.dynamic_grid_events_path)
        self._fee_rates: dict[str, float] = {}

    @property
    def mode(self) -> str:
        return self.settings.dynamic_grid_mode.strip().upper()

    def _event(self, event_type: str, **details: Any) -> None:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "strategy": "dynamic_grid_v1",
            "mode": self.mode,
            **details,
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, default=str) + "\n")
        self.log.info("DYNAMIC_GRID_EVENT | %s", json.dumps(payload, sort_keys=True, default=str))

    def _authenticated_maker_fee(self, symbol: str) -> float:
        if symbol in self._fee_rates:
            return self._fee_rates[symbol]
        getter = getattr(self.client, "get_trade_fee_rate", None)
        if getter is None:
            raise RuntimeError("authenticated fee endpoint unavailable")
        payload = getter(symbol=symbol, business_type="mix")
        data = payload.get("data") or {}
        rate = float(data.get("makerFeeRate"))
        taker = float(data.get("takerFeeRate"))
        if rate <= 0 or taker <= 0:
            raise RuntimeError(f"invalid authenticated fee rate for {symbol}")
        self._fee_rates[symbol] = rate
        self._event(
            "FEE_RATE_AUTHENTICATED", symbol=symbol, maker_fee_rate=rate,
            taker_fee_rate=taker, source="bitget_account_api",
        )
        return rate

    def _decision(self, symbol: str, equity_usdt: float) -> GridDecision:
        orderbook = self.client.get_orderbook(symbol=symbol, limit=20)
        return build_grid_decision(
            symbol=symbol,
            candles_5m=self.cache.get(symbol, "5m"),
            candles_15m=self.cache.get(symbol, "15m"),
            candles_1h=self.cache.get(symbol, "1h"),
            orderbook=orderbook,
            maker_fee_rate=self._authenticated_maker_fee(symbol),
            equity_usdt=equity_usdt,
            settings=self.settings,
            stale=any(self.cache.is_stale(symbol, tf) for tf in ("5m", "15m", "1h")),
        )

    def cycle(self, *, equity_usdt: float, risk_stop_reason: str = "") -> list[GridDecision]:
        if not self.settings.dynamic_grid_enabled or self.mode == "OFF":
            return []
        grid_state = self.store.load({})
        risk_bucket = (
            grid_state.get("active_grid")
            if isinstance(grid_state.get("active_grid"), dict)
            else grid_state
        )
        strategy_drawdown_usdt = float((risk_bucket or {}).get("max_drawdown_usdt") or 0.0)
        strategy_drawdown_pct = (
            strategy_drawdown_usdt / equity_usdt * 100.0 if equity_usdt > 0 else float("inf")
        )
        if strategy_drawdown_pct >= float(self.settings.dynamic_grid_max_drawdown_pct):
            risk_stop_reason = risk_stop_reason or "dynamic_grid_max_drawdown_reached"
            self._event(
                "GRID_STOP", reason="dynamic_grid_max_drawdown_reached",
                drawdown_usdt=strategy_drawdown_usdt,
                drawdown_pct=strategy_drawdown_pct,
            )
        if int((risk_bucket or {}).get("order_error_count") or 0) >= int(
            self.settings.dynamic_grid_max_order_errors
        ):
            risk_stop_reason = risk_stop_reason or "dynamic_grid_repeated_order_errors"
            self._event(
                "GRID_STOP", reason="dynamic_grid_repeated_order_errors",
                order_error_count=(risk_bucket or {}).get("order_error_count"),
            )
        if bool((risk_bucket or {}).get("pilot_halted")):
            risk_stop_reason = risk_stop_reason or "dynamic_grid_pilot_halted"
            self._event(
                "GRID_STOP", reason="dynamic_grid_pilot_halted",
                halt_reason=(risk_bucket or {}).get("pilot_halt_reason"),
            )
        decisions: list[GridDecision] = []
        for symbol in sorted(self.settings.dynamic_grid_symbol_set):
            try:
                decision = self._decision(symbol, equity_usdt)
            except Exception as exc:
                self._event("GRID_STOP", symbol=symbol, reason="stale_data_or_fee_query_error", error=str(exc))
                continue
            decisions.append(decision)
            self._event(
                "GRID_DECISION", symbol=symbol, score=decision.score,
                regime=decision.regime.value, reason=decision.reason,
                center=decision.center, atr=decision.atr,
                hard_invalidation=decision.hard_invalidation,
                levels=[level.__dict__ if hasattr(level, "__dict__") else {
                    "index": level.index, "entry_price": level.entry_price,
                    "take_profit_price": level.take_profit_price,
                    "notional_usdt": level.notional_usdt, "quantity": level.quantity,
                    "client_oid": level.client_oid,
                } for level in decision.levels],
                economics=decision.to_dict()["economics"],
            )
        allowed = [
            decision for decision in decisions
            if decision.regime is GridRegime.ALLOWED and not risk_stop_reason
        ]
        selected = max(allowed, key=lambda row: (row.score, row.symbol), default=None)
        self._event(
            "GRID_SELECTION", selected_symbol=selected.symbol if selected else "",
            candidates=[{"symbol": item.symbol, "score": item.score, "regime": item.regime.value} for item in decisions],
            risk_stop_reason=risk_stop_reason,
        )
        if self.mode == "SHADOW":
            self.shadow.process(
                decisions=decisions,
                selected=selected,
                candles_by_symbol={
                    item.symbol: self.cache.get(item.symbol, "5m") for item in decisions
                },
            )
        if self.mode == "LIVE":
            state = self.store.load({})
            active_symbol = str((state.get("active_grid") or {}).get("symbol") or "")
            active_decision = next((item for item in decisions if item.symbol == active_symbol), None)
            if active_decision is not None:
                try:
                    self._live_cycle(active_decision, risk_stop_reason=risk_stop_reason)
                except Exception as exc:
                    self._record_uncaught_order_error(exc)
                    raise
            elif selected is not None:
                try:
                    self._live_cycle(selected, risk_stop_reason=risk_stop_reason)
                except Exception as exc:
                    self._record_uncaught_order_error(exc)
                    raise
        return decisions

    def _record_uncaught_order_error(self, exc: Exception) -> None:
        state = self.store.load({})
        active = state.get("active_grid") if isinstance(state, dict) else None
        if not isinstance(active, dict):
            return
        active["order_error_count"] = int(active.get("order_error_count") or 0) + 1
        self.store.save({"active_grid": active})
        self._event(
            "GRID_ORDER_ERROR", symbol=active.get("symbol"),
            action="uncaught_live_cycle_error", error=str(exc),
            order_error_count=active["order_error_count"],
        )

    def _order_state(self, symbol: str, client_oid: str) -> tuple[str, dict[str, Any] | None]:
        result = self.client.find_order_by_client_oid(symbol=symbol, client_oid=client_oid)
        status = str(result.get("status") or "UNKNOWN").upper()
        return status, result.get("order") if status == "FOUND" else None

    def _cycle_close_truth(
        self, *, active: dict[str, Any], symbol: str, confirmed_size: float
    ) -> dict[str, float] | None:
        """Return exchange economics only for one unambiguous lifecycle."""
        from execution.close_reconciler import (
            CloseReconciliationUnavailable,
            economics_from_history,
            match_lifecycle,
        )

        try:
            payload = self.client.get_position_history(symbol=symbol, limit=20)
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict):
                rows = data.get("list") or data.get("positions") or data.get("data") or []
            else:
                rows = data if isinstance(data, list) else []
            opened_at_ms = int(
                datetime.fromisoformat(
                    str(active["created_at"]).replace("Z", "+00:00")
                ).timestamp() * 1000
            )
            selected = match_lifecycle(
                rows,
                symbol=symbol,
                direction="long",
                opened_at_ms=opened_at_ms,
                size=confirmed_size,
                exchange_position_id=None,
            )
            if selected is None:
                raise CloseReconciliationUnavailable("no unambiguous grid lifecycle")
            economics = economics_from_history(selected)
            return {
                "gross_pnl": float(economics.gross_pnl),
                "fees": float(economics.fees),
                "funding": float(economics.funding),
                "net_profit": float(economics.net_profit),
            }
        except Exception as exc:
            self._event(
                "GRID_ECONOMICS_INCOMPLETE", symbol=symbol,
                reason="funding_not_unambiguously_attributed", error=str(exc),
            )
            return None

    def _cycle_funding_truth(
        self, *, active: dict[str, Any], symbol: str, confirmed_size: float
    ) -> tuple[float | None, str]:
        truth = self._cycle_close_truth(
            active=active, symbol=symbol, confirmed_size=confirmed_size
        )
        return (
            (truth["funding"], "bitget_position_history")
            if truth is not None else (None, "unavailable_or_ambiguous")
        )

    @staticmethod
    def _entry_plan(decision: GridDecision, level: Any) -> Any:
        strategy_identity = f"dynamic_grid_v1_level_{level.index}"
        candidate_id = deterministic_candidate_id(
            strategy_identity, decision.symbol, "LONG", decision.candle_timestamp_ms
        )
        return SimpleNamespace(
            candidate_id=candidate_id,
            plan_id=deterministic_plan_id(candidate_id),
            symbol=decision.symbol,
            direction="LONG",
            strategy=strategy_identity,
        )

    def _submit_entry_once(self, decision: GridDecision, level: Any) -> dict[str, Any]:
        if self.entry_submitter is None:
            raise RuntimeError("entry submitter unavailable")
        plan = self._entry_plan(decision, level)
        result = submit_persistent_maker_entry(
            submitter=self.entry_submitter, client=self.client, plan=plan,
            size=level.quantity, price=level.entry_price,
            notional_usdt=level.notional_usdt,
        )
        return {
            "orderId": result.order_id,
            "clientOid": result.client_oid,
            "classification": result.classification,
        }

    def _live_cycle(self, decision: GridDecision, *, risk_stop_reason: str = "") -> None:
        state = self.store.load({})
        active = state.get("active_grid") if isinstance(state, dict) else None
        positions = self.client.get_all_positions().get("data") or []
        position_by_symbol = {
            str(row.get("symbol") or "").upper(): row for row in positions
            if float(row.get("total") or row.get("size") or 0.0) > 0
        }
        # Old-strategy positions remain owned by PositionManager. A grid never
        # overlays them because Bitget's net position cannot preserve lineage.
        if not active and position_by_symbol:
            self._event("GRID_STOP", symbol=decision.symbol, reason="existing_position_cutover_wait")
            return
        if not active and state.get("last_cycle_closed_at"):
            try:
                closed_at = datetime.fromisoformat(
                    str(state["last_cycle_closed_at"]).replace("Z", "+00:00")
                )
                elapsed_minutes = (
                    datetime.now(timezone.utc) - closed_at
                ).total_seconds() / 60.0
            except (TypeError, ValueError):
                self._event("GRID_STOP", symbol=decision.symbol, reason="reset_timestamp_ambiguous")
                return
            if elapsed_minutes < float(self.settings.dynamic_grid_reset_cooldown_minutes):
                self._event(
                    "GRID_STOP", symbol=decision.symbol, reason="cycle_reset_cooldown",
                    elapsed_minutes=elapsed_minutes,
                )
                return
            for client_oid in state.get("last_order_oids") or []:
                lookup, order = self._order_state(decision.symbol, client_oid)
                order_state = str((order or {}).get("state") or (order or {}).get("status") or "").lower()
                if lookup == "UNKNOWN" or (
                    lookup == "FOUND" and order_state not in (_DEAD | _FILLED)
                ):
                    self._event(
                        "GRID_STOP", symbol=decision.symbol,
                        reason="reset_recovery_ambiguity", client_oid=client_oid,
                        lookup=lookup, order_state=order_state,
                    )
                    return
            if not reset_allowed(
                old_center=float(state.get("last_center") or 0.0),
                new_center=decision.center,
                atr_value=decision.atr,
                flat=True,
                working_orders=0,
                minutes_since_reset=elapsed_minutes,
                settings=self.settings,
            ):
                self._event(
                    "GRID_STOP", symbol=decision.symbol,
                    reason="reset_center_move_below_frozen_threshold",
                    old_center=state.get("last_center"), new_center=decision.center,
                    atr=decision.atr,
                )
                return
        if active and str(active.get("symbol")) != decision.symbol:
            self._event("GRID_STOP", symbol=decision.symbol, reason="max_active_grids_reached")
            return
        if active:
            unexpected_symbols = sorted(set(position_by_symbol) - {decision.symbol})
            current = position_by_symbol.get(decision.symbol)
            configured_quantity = sum(
                float(level.get("quantity") or 0.0) for level in active.get("levels") or []
            )
            current_size = float((current or {}).get("total") or (current or {}).get("size") or 0.0)
            current_side = str((current or {}).get("holdSide") or "long").lower()
            if unexpected_symbols or current_side != "long" or current_size > configured_quantity * 1.02:
                risk_stop_reason = risk_stop_reason or "inventory_mismatch"
                self._event(
                    "GRID_STOP", symbol=decision.symbol, reason="inventory_mismatch",
                    unexpected_symbols=unexpected_symbols, current_side=current_side,
                    current_size=current_size, configured_quantity=configured_quantity,
                )

        if not active:
            if state.get("last_cycle_closed_at"):
                self._event(
                    "GRID_RESET", symbol=decision.symbol,
                    old_center=state.get("last_center"), new_center=decision.center,
                    reason="previous_cycle_exchange_confirmed_flat_and_cooldown_elapsed",
                    atr=decision.atr, volatility=decision.atr,
                    regime=decision.regime.value,
                )
            self.client.set_futures_leverage(
                symbol=decision.symbol, leverage=1, hold_side="long", margin_mode="isolated"
            )
            level_state = []
            for level in decision.levels:
                if self.entry_submitter is None:
                    raise RuntimeError("entry submitter unavailable")
                entry_client_oid = self.entry_submitter.client_oid_for(
                    self._entry_plan(decision, level), leg=ENTRY_LEG_MAKER
                )
                level_state.append({
                    "index": level.index, "entry_client_oid": entry_client_oid,
                    "entry_order_id": "", "entry_price": level.entry_price,
                    "tp_price": level.take_profit_price, "quantity": level.quantity,
                    "notional_usdt": level.notional_usdt,
                    "tp_client_oid": level.client_oid.replace("-entry", "-tp"),
                    "entry_submitted": False, "tp_submitted": False,
                    "entry_resolved": False,
                })
            active = {
                "strategy": "dynamic_grid_v1", "symbol": decision.symbol,
                "center": decision.center, "hard_invalidation": decision.hard_invalidation,
                "created_at": datetime.now(timezone.utc).isoformat(), "levels": level_state,
                "reset_count": int(state.get("reset_count") or 0) + (
                    1 if state.get("last_cycle_closed_at") else 0
                ),
                "cumulative_net_usdt": float(state.get("cumulative_net_usdt") or 0.0),
                "peak_cumulative_net_usdt": float(state.get("peak_cumulative_net_usdt") or 0.0),
                "max_drawdown_usdt": float(state.get("max_drawdown_usdt") or 0.0),
                "order_error_count": int(state.get("order_error_count") or 0),
            }
            # Persist intent before the first POST. A crash between submissions
            # is recoverable by deterministic clientOid lookup on the next cycle.
            self.store.save({"active_grid": active})
            for level in decision.levels:
                order = self._submit_entry_once(decision, level)
                level_state[level.index - 1]["entry_order_id"] = str(order.get("orderId") or "")
                level_state[level.index - 1]["entry_client_oid"] = str(order.get("clientOid") or "")
                level_state[level.index - 1]["entry_submitted"] = True
                self.store.save({"active_grid": active})
            self._event("GRID_OPENED", symbol=decision.symbol, center=decision.center, levels=level_state)
            return

        position = position_by_symbol.get(decision.symbol)
        mark = float((position or {}).get("markPrice") or (position or {}).get("marketPrice") or decision.center)
        latest_candles = self.cache.get(decision.symbol, "5m")
        latest_candle = latest_candles[-1] if latest_candles else {}
        observed_low = float(latest_candle.get("low") or mark)
        observed_high = float(latest_candle.get("high") or mark)
        metrics_changed = False
        for level in active.get("levels") or []:
            if not level.get("tp_submitted"):
                continue
            entry_price = float(level.get("actual_entry_price") or level.get("entry_price") or 0.0)
            if entry_price <= 0:
                continue
            level["mae_bps"] = min(
                float(level.get("mae_bps") or 0.0),
                (observed_low - entry_price) / entry_price * 10_000.0,
            )
            level["mfe_bps"] = max(
                float(level.get("mfe_bps") or 0.0),
                (observed_high - entry_price) / entry_price * 10_000.0,
            )
            metrics_changed = True
        if position and mark <= float(active.get("hard_invalidation") or 0.0):
            for level in active.get("levels") or []:
                for oid_key in ("entry_client_oid", "tp_client_oid"):
                    try:
                        self.client.cancel_futures_order(
                            symbol=decision.symbol, client_oid=level.get(oid_key)
                        )
                    except Exception as exc:
                        active["order_error_count"] = int(active.get("order_error_count") or 0) + 1
                        self._event(
                            "GRID_ORDER_ERROR", symbol=decision.symbol,
                            action=f"hard_kill_cancel_{oid_key}", error=str(exc),
                        )
            close_result = self.client.close_futures_position_full(
                symbol=decision.symbol, direction="LONG",
                size=float(position.get("total") or position.get("size") or 0.0),
                margin_mode="isolated", reason="dynamic_grid_hard_invalidation",
                cleanup_tpsl=False,
            )
            if str(close_result.get("status") or "") not in {"CLOSED", "NO_POSITION"}:
                self._event(
                    "GRID_STOP", symbol=decision.symbol,
                    reason="hard_invalidation_flatness_unconfirmed",
                    close_status=close_result.get("status"),
                )
                return
            if self.entry_submitter is not None:
                for level in active.get("levels") or []:
                    self.entry_submitter.mark_closed_out(
                        level.get("entry_client_oid"), reason="dynamic_grid_hard_invalidation"
                    )
            confirmed_size = float(position.get("total") or position.get("size") or 0.0)
            close_truth = self._cycle_close_truth(
                active=active, symbol=decision.symbol, confirmed_size=confirmed_size
            )
            maker_entry_fees = sum(
                abs(float(level.get("entry_fee_usdt") or 0.0))
                for level in active.get("levels") or []
            )
            total_fees = abs(float((close_truth or {}).get("fees") or 0.0))
            emergency_taker_fees = max(total_fees - maker_entry_fees, 0.0)
            cycle_net = (
                float(close_truth["net_profit"]) if close_truth is not None else None
            )
            cumulative_net = float(active.get("cumulative_net_usdt") or 0.0) + (
                cycle_net or 0.0
            )
            peak_cumulative = max(
                float(active.get("peak_cumulative_net_usdt") or 0.0), cumulative_net
            )
            max_drawdown = max(
                float(active.get("max_drawdown_usdt") or 0.0),
                peak_cumulative - cumulative_net,
            )
            self.store.save({
                "last_cycle_closed_at": datetime.now(timezone.utc).isoformat(),
                "last_center": active.get("center"),
                "last_close_reason": "hard_invalidation",
                "reset_count": int(active.get("reset_count") or 0),
                "last_order_oids": [
                    level.get(key) for level in active.get("levels") or []
                    for key in ("entry_client_oid", "tp_client_oid") if level.get(key)
                ],
                "cumulative_net_usdt": cumulative_net,
                "peak_cumulative_net_usdt": peak_cumulative,
                "max_drawdown_usdt": max_drawdown,
                "order_error_count": int(active.get("order_error_count") or 0),
                "pilot_halted": close_truth is None,
                "pilot_halt_reason": (
                    "hard_kill_economics_ambiguous" if close_truth is None else ""
                ),
            })
            created_at = datetime.fromisoformat(str(active["created_at"]).replace("Z", "+00:00"))
            self._event(
                "GRID_HARD_KILL", symbol=decision.symbol, mark=mark,
                reason="hard_invalidation", emergency_behavior=True,
                regime=decision.regime.value,
                duration_minutes=(datetime.now(timezone.utc) - created_at).total_seconds() / 60.0,
                reset_count=int(active.get("reset_count") or 0),
                gross_capture_usdt=(close_truth or {}).get("gross_pnl"),
                maker_fees_usdt=maker_entry_fees,
                taker_fees_usdt=(
                    emergency_taker_fees if close_truth is not None else None
                ),
                funding_usdt=(close_truth or {}).get("funding"),
                net_capture_usdt=cycle_net,
                inventory_before=confirmed_size, inventory_after=0.0,
                economics_source=(
                    "bitget_position_history" if close_truth is not None
                    else "unavailable_or_ambiguous"
                ),
                strategy_max_drawdown_usdt=max_drawdown,
            )
            return

        if decision.regime is not GridRegime.ALLOWED or risk_stop_reason:
            for level in active.get("levels") or []:
                if level.get("tp_submitted"):
                    continue
                if level.get("entry_resolved"):
                    continue
                if not level.get("entry_submitted"):
                    level["entry_resolved"] = True
                    continue
                if level.get("entry_cancel_requested"):
                    continue
                try:
                    self.client.cancel_futures_order(
                        symbol=decision.symbol, client_oid=level.get("entry_client_oid")
                    )
                except Exception as exc:
                    active["order_error_count"] = int(active.get("order_error_count") or 0) + 1
                    self._event("GRID_ORDER_ERROR", symbol=decision.symbol, action="trend_pause_cancel", error=str(exc))
                    continue
                level["entry_cancel_requested"] = True
            self.store.save({"active_grid": active})
            self._event(
                "GRID_PAUSED", symbol=decision.symbol, regime=decision.regime.value,
                risk_stop_reason=risk_stop_reason,
            )
            # Continue into exchange reconciliation. A successful cancel ACK
            # can race a partial fill; only the subsequent order read may call
            # the level resolved or submit a TP for confirmed inventory.

        changed = metrics_changed
        for level in active.get("levels") or []:
            if level.get("entry_resolved") and not level.get("tp_submitted"):
                continue
            if not level.get("entry_submitted"):
                if decision.regime is not GridRegime.ALLOWED or risk_stop_reason:
                    continue
                decision_level = next(
                    (item for item in decision.levels if item.index == int(level.get("index") or 0)),
                    None,
                )
                if decision_level is None:
                    self._event(
                        "GRID_STOP", symbol=decision.symbol,
                        reason="recovery_level_geometry_missing", level=level.get("index"),
                    )
                    return
                order = self._submit_entry_once(decision, decision_level)
                level["entry_order_id"] = str(order.get("orderId") or "")
                level["entry_client_oid"] = str(order.get("clientOid") or "")
                level["entry_submitted"] = True
                changed = True
            lookup, order = self._order_state(decision.symbol, level["entry_client_oid"])
            if lookup == "UNKNOWN":
                self._event("GRID_STOP", symbol=decision.symbol, reason="entry_recovery_ambiguity")
                return
            order_state = str((order or {}).get("state") or (order or {}).get("status") or "").lower()
            if order_state in _DEAD:
                level["entry_resolved"] = True
                if self.entry_submitter is not None:
                    self.entry_submitter.mark_closed_out(
                        level["entry_client_oid"], reason=f"entry_order_{order_state}"
                    )
                changed = True
                continue
            if order_state in _PARTIAL:
                # Stop accumulating inventory, then protect only the confirmed
                # filled quantity. Never assume the requested quantity filled.
                self.client.cancel_futures_order(
                    symbol=decision.symbol, client_oid=level["entry_client_oid"]
                )
            if order_state not in (_FILLED | _PARTIAL) or level.get("tp_submitted"):
                continue
            tp_lookup, tp_order = self._order_state(decision.symbol, level["tp_client_oid"])
            if tp_lookup == "UNKNOWN":
                self._event("GRID_STOP", symbol=decision.symbol, reason="tp_recovery_ambiguity")
                return
            tp_state = str((tp_order or {}).get("state") or (tp_order or {}).get("status") or "").lower()
            if tp_lookup == "FOUND" and tp_state in _DEAD:
                self._event(
                    "GRID_STOP", symbol=decision.symbol, reason="tp_order_dead",
                    level=level["index"], tp_state=tp_state,
                )
                return
            if tp_lookup == "ABSENT":
                metrics = self.client.extract_fill_metrics({"data": order or {}})
                filled_quantity = float(metrics.get("filled_qty") or 0.0)
                if filled_quantity <= 0:
                    self._event("GRID_STOP", symbol=decision.symbol, reason="filled_quantity_unknown")
                    return
                if self.entry_submitter is not None:
                    self.entry_submitter.mark_filled(
                        level["entry_client_oid"], filled_qty=filled_quantity,
                        avg_price=float(metrics.get("avg_price") or level["entry_price"]),
                    )
                raw_metrics = metrics.get("raw") if isinstance(metrics.get("raw"), dict) else {}
                level["filled_quantity"] = filled_quantity
                level["actual_entry_price"] = float(
                    metrics.get("avg_price") or level["entry_price"]
                )
                level["entry_fee_usdt"] = abs(float(metrics.get("fee") or 0.0))
                level["filled_at_ms"] = int(
                    raw_metrics.get("uTime") or raw_metrics.get("cTime")
                    or decision.candle_timestamp_ms
                )
                level.setdefault("mae_bps", 0.0)
                level.setdefault("mfe_bps", 0.0)
                self.client.place_futures_limit_close_order(
                    symbol=decision.symbol, hold_side="long", size=filled_quantity,
                    price=level["tp_price"], margin_mode="isolated", post_only=True,
                    client_oid=level["tp_client_oid"],
                )
            level["tp_submitted"] = True
            level["entry_resolved"] = True
            if self.entry_submitter is not None:
                self.entry_submitter.mark_protected(
                    level["entry_client_oid"], integrity="POST_ONLY_TP_ACCEPTED"
                )
            changed = True
            self._event(
                "GRID_TP_SUBMITTED", symbol=decision.symbol, level=level["index"],
                entry_client_oid=level["entry_client_oid"], tp_client_oid=level["tp_client_oid"],
                gross_capture_bps=decision.economics.gross_capture_bps,
                expected_net_capture_bps=decision.economics.expected_net_capture_bps,
            )
        if changed:
            self.store.save({"active_grid": active})
        if active and not position and all(level.get("entry_resolved") for level in active.get("levels") or []):
            # Do not infer completion from a missing filtered position alone.
            # Every TP must be exchange-confirmed filled before clearing lineage.
            tp_levels = [level for level in active["levels"] if level.get("tp_submitted")]
            states = [self._order_state(decision.symbol, level["tp_client_oid"])[1] for level in tp_levels]
            if all(str((row or {}).get("state") or (row or {}).get("status") or "").lower() in _FILLED for row in states):
                gross_capture = 0.0
                fees = 0.0
                fills_per_level = []
                total_entry_notional = 0.0
                level_economics = []
                for level, tp_row in zip(tp_levels, states):
                    _, entry_row = self._order_state(decision.symbol, level["entry_client_oid"])
                    entry_metrics = self.client.extract_fill_metrics({"data": entry_row or {}})
                    tp_metrics = self.client.extract_fill_metrics({"data": tp_row or {}})
                    quantity = min(
                        float(entry_metrics.get("filled_qty") or 0.0),
                        float(tp_metrics.get("filled_qty") or 0.0),
                    )
                    entry_price = float(entry_metrics.get("avg_price") or level["entry_price"])
                    tp_price = float(tp_metrics.get("avg_price") or level["tp_price"])
                    gross_usdt = (tp_price - entry_price) * quantity
                    entry_fee = abs(float(entry_metrics.get("fee") or level.get("entry_fee_usdt") or 0.0))
                    exit_fee = abs(float(tp_metrics.get("fee") or 0.0))
                    level_fees = entry_fee + exit_fee
                    notional = entry_price * quantity
                    gross_bps = gross_usdt / notional * 10_000.0 if notional > 0 else 0.0
                    execution_drag_bps = (
                        abs(entry_price - float(level["entry_price"])) / float(level["entry_price"]) * 10_000.0
                        + abs(tp_price - float(level["tp_price"])) / float(level["tp_price"]) * 10_000.0
                    )
                    tp_raw = tp_metrics.get("raw") if isinstance(tp_metrics.get("raw"), dict) else {}
                    exit_at_ms = int(
                        tp_raw.get("uTime") or tp_raw.get("cTime")
                        or int(datetime.now(timezone.utc).timestamp() * 1000)
                    )
                    filled_at_ms = int(level.get("filled_at_ms") or active.get("opened_timestamp_ms") or exit_at_ms)
                    gross_capture += gross_usdt
                    fees += level_fees
                    total_entry_notional += notional
                    fills_per_level.append({
                        "level": level["index"], "quantity": quantity,
                        "entry_price": entry_price, "exit_price": tp_price,
                    })
                    level_economics.append({
                        "symbol": decision.symbol, "level": level["index"],
                        "entry": entry_price, "exit": tp_price,
                        "gross_bps": gross_bps, "maker_fees_usdt": level_fees,
                        "taker_fees_usdt": 0.0,
                        "execution_drag_bps": execution_drag_bps,
                        "gross_usdt": gross_usdt, "notional_usdt": notional,
                        "level_fees_usdt": level_fees,
                        "hold_time_minutes": max((exit_at_ms - filled_at_ms) / 60_000.0, 0.0),
                        "mae_bps": float(level.get("mae_bps") or 0.0),
                        "mfe_bps": float(level.get("mfe_bps") or 0.0),
                        "quantity": quantity,
                        "exit_at_ms": exit_at_ms,
                    })
                inventory = sum(item["quantity"] for item in level_economics)
                funding, funding_source = self._cycle_funding_truth(
                    active=active, symbol=decision.symbol, confirmed_size=inventory
                )
                for item in sorted(level_economics, key=lambda row: row["exit_at_ms"]):
                    before = inventory
                    inventory = max(inventory - item["quantity"], 0.0)
                    item["inventory_before"] = before
                    item["inventory_after"] = inventory
                    funding_share = (
                        float(funding) * item["notional_usdt"] / total_entry_notional
                        if funding is not None and total_entry_notional > 0 else None
                    )
                    net_usdt = (
                        item["gross_usdt"] - item["level_fees_usdt"]
                        + (funding_share or 0.0)
                    )
                    item["funding_usdt"] = funding_share
                    item["funding_source"] = funding_source
                    item["net_bps"] = (
                        net_usdt / item["notional_usdt"] * 10_000.0
                        if item["notional_usdt"] > 0 else 0.0
                    )
                    item.pop("quantity", None)
                    item.pop("exit_at_ms", None)
                    item.pop("gross_usdt", None)
                    item.pop("notional_usdt", None)
                    item.pop("level_fees_usdt", None)
                    self._event("GRID_LEVEL_CLOSED", **item)
                created_at = datetime.fromisoformat(str(active["created_at"]).replace("Z", "+00:00"))
                duration_minutes = (
                    datetime.now(timezone.utc) - created_at
                ).total_seconds() / 60.0
                cycle_net_usdt = gross_capture - fees + (funding or 0.0)
                cumulative_net = float(active.get("cumulative_net_usdt") or 0.0) + cycle_net_usdt
                peak_cumulative = max(
                    float(active.get("peak_cumulative_net_usdt") or 0.0),
                    cumulative_net,
                )
                max_drawdown = max(
                    float(active.get("max_drawdown_usdt") or 0.0),
                    peak_cumulative - cumulative_net,
                )
                self.store.save({
                    "last_cycle_closed_at": datetime.now(timezone.utc).isoformat(),
                    "last_center": active.get("center"),
                    "reset_count": int(active.get("reset_count") or 0),
                    "last_order_oids": [
                        level.get(key) for level in active.get("levels") or []
                        for key in ("entry_client_oid", "tp_client_oid") if level.get(key)
                    ],
                    "cumulative_net_usdt": cumulative_net,
                    "peak_cumulative_net_usdt": peak_cumulative,
                    "max_drawdown_usdt": max_drawdown,
                    "order_error_count": int(active.get("order_error_count") or 0),
                })
                self._event(
                    "GRID_CYCLE_CLOSED", symbol=decision.symbol,
                    state="exchange_confirmed_flat", gross_capture_usdt=gross_capture,
                    fees_usdt=fees, net_capture_usdt=cycle_net_usdt,
                    gross_capture_bps=(
                        gross_capture / total_entry_notional * 10_000.0
                        if total_entry_notional > 0 else 0.0
                    ),
                    net_capture_bps=(
                        (gross_capture - fees + (funding or 0.0))
                        / total_entry_notional * 10_000.0
                        if total_entry_notional > 0 else 0.0
                    ),
                    maker_fees_usdt=fees, taker_fees_usdt=0.0,
                    funding_usdt=funding,
                    funding_source=funding_source,
                    duration_minutes=duration_minutes, fills_per_level=fills_per_level,
                    maker_hit_rate=len(tp_levels) / 3.0,
                    opportunity_cost_unfilled_levels=3 - len(tp_levels),
                    reset_count=int(active.get("reset_count") or 0),
                    emergency_behavior=False,
                    strategy_cumulative_net_usdt=cumulative_net,
                    strategy_max_drawdown_usdt=max_drawdown,
                )


__all__ = ["DynamicGridService"]
