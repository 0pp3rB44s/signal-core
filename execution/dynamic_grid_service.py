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
        if self.mode == "LIVE":
            state = self.store.load({})
            active_symbol = str((state.get("active_grid") or {}).get("symbol") or "")
            active_decision = next((item for item in decisions if item.symbol == active_symbol), None)
            if active_decision is not None:
                self._live_cycle(active_decision, risk_stop_reason=risk_stop_reason)
            elif selected is not None:
                self._live_cycle(selected, risk_stop_reason=risk_stop_reason)
        return decisions

    def _order_state(self, symbol: str, client_oid: str) -> tuple[str, dict[str, Any] | None]:
        result = self.client.find_order_by_client_oid(symbol=symbol, client_oid=client_oid)
        status = str(result.get("status") or "UNKNOWN").upper()
        return status, result.get("order") if status == "FOUND" else None

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
                    atr=decision.atr, regime=decision.regime.value,
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
        if position and mark <= float(active.get("hard_invalidation") or 0.0):
            for level in active.get("levels") or []:
                for oid_key in ("entry_client_oid", "tp_client_oid"):
                    try:
                        self.client.cancel_futures_order(
                            symbol=decision.symbol, client_oid=level.get(oid_key)
                        )
                    except Exception as exc:
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
            self.store.save({
                "last_cycle_closed_at": datetime.now(timezone.utc).isoformat(),
                "last_center": active.get("center"),
                "last_close_reason": "hard_invalidation",
                "reset_count": int(active.get("reset_count") or 0),
                "last_order_oids": [
                    level.get(key) for level in active.get("levels") or []
                    for key in ("entry_client_oid", "tp_client_oid") if level.get(key)
                ],
            })
            created_at = datetime.fromisoformat(str(active["created_at"]).replace("Z", "+00:00"))
            self._event(
                "GRID_HARD_KILL", symbol=decision.symbol, mark=mark,
                reason="hard_invalidation", emergency_behavior=True,
                duration_minutes=(datetime.now(timezone.utc) - created_at).total_seconds() / 60.0,
                reset_count=int(active.get("reset_count") or 0),
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

        changed = False
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
                    gross_capture += max(tp_price - entry_price, 0.0) * quantity
                    fees += abs(float(entry_metrics.get("fee") or 0.0))
                    fees += abs(float(tp_metrics.get("fee") or 0.0))
                    fills_per_level.append({
                        "level": level["index"], "quantity": quantity,
                        "entry_price": entry_price, "exit_price": tp_price,
                    })
                created_at = datetime.fromisoformat(str(active["created_at"]).replace("Z", "+00:00"))
                duration_minutes = (
                    datetime.now(timezone.utc) - created_at
                ).total_seconds() / 60.0
                self.store.save({
                    "last_cycle_closed_at": datetime.now(timezone.utc).isoformat(),
                    "last_center": active.get("center"),
                    "reset_count": int(active.get("reset_count") or 0),
                    "last_order_oids": [
                        level.get(key) for level in active.get("levels") or []
                        for key in ("entry_client_oid", "tp_client_oid") if level.get(key)
                    ],
                })
                self._event(
                    "GRID_CYCLE_CLOSED", symbol=decision.symbol,
                    state="exchange_confirmed_flat", gross_capture_usdt=gross_capture,
                    fees_usdt=fees, net_capture_usdt=gross_capture - fees,
                    duration_minutes=duration_minutes, fills_per_level=fills_per_level,
                    maker_hit_rate=len(tp_levels) / 3.0,
                    opportunity_cost_unfilled_levels=3 - len(tp_levels),
                    reset_count=int(active.get("reset_count") or 0),
                    emergency_behavior=False,
                )


__all__ = ["DynamicGridService"]
