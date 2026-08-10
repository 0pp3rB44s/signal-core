"""Order-incapable state machine for dynamic_grid_v1 operational shadowing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from strategies.dynamic_grid_v1 import GridDecision, GridRegime, reset_allowed


class DynamicGridShadow:
    def __init__(self, *, settings: Any, store: Any, emit: Callable[..., None]) -> None:
        self.settings = settings
        self.store = store
        self.emit = emit

    @staticmethod
    def _iso(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).isoformat()

    def process(
        self,
        *,
        decisions: list[GridDecision],
        selected: GridDecision | None,
        candles_by_symbol: dict[str, list[dict[str, Any]]],
    ) -> None:
        state = self.store.load({})
        active = state.get("active_grid") if isinstance(state, dict) else None
        if active:
            decision = next(
                (item for item in decisions if item.symbol == active.get("symbol")), None
            )
            if decision is None:
                self.emit(
                    "SHADOW_GRID_STOP", symbol=active.get("symbol"),
                    reason="active_symbol_decision_missing",
                )
                return
        else:
            decision = selected
        if decision is None:
            return

        candle = (candles_by_symbol.get(decision.symbol) or [])[-1]
        timestamp_ms = int(candle.get("timestamp") or decision.candle_timestamp_ms)
        low = float(candle.get("low") or 0.0)
        high = float(candle.get("high") or 0.0)

        if not active:
            if state.get("last_resolved_at"):
                try:
                    old_ms = int(state["last_resolved_timestamp_ms"])
                    elapsed = max((timestamp_ms - old_ms) / 60_000.0, 0.0)
                except (KeyError, TypeError, ValueError):
                    self.emit(
                        "SHADOW_GRID_STOP", symbol=decision.symbol,
                        reason="shadow_reset_timestamp_ambiguous",
                    )
                    return
                if not reset_allowed(
                    old_center=float(state.get("last_center") or 0.0),
                    new_center=decision.center,
                    atr_value=decision.atr,
                    flat=True,
                    working_orders=0,
                    minutes_since_reset=elapsed,
                    settings=self.settings,
                ):
                    self.emit(
                        "SHADOW_RESET_DEFERRED", symbol=decision.symbol,
                        old_center=state.get("last_center"), new_center=decision.center,
                        reason="cooldown_or_center_move_below_threshold",
                        volatility=decision.atr, regime=decision.regime.value,
                    )
                    return
                self.emit(
                    "SHADOW_GRID_RESET", symbol=decision.symbol,
                    old_center=state.get("last_center"), new_center=decision.center,
                    reason="flat_resolved_material_reference_move",
                    volatility=decision.atr, regime=decision.regime.value,
                )
            active = {
                "symbol": decision.symbol,
                "center": decision.center,
                "hard_invalidation": decision.hard_invalidation,
                "opened_timestamp_ms": timestamp_ms,
                "levels": [
                    {
                        "index": level.index,
                        "entry_price": level.entry_price,
                        "target_price": level.take_profit_price,
                        "quantity": level.quantity,
                        "notional_usdt": level.notional_usdt,
                        "status": "WAITING",
                        "mae_bps": 0.0,
                        "mfe_bps": 0.0,
                    }
                    for level in decision.levels
                ],
            }
            self.store.save({"active_grid": active})
            self.emit(
                "SHADOW_GRID_OPENED", symbol=decision.symbol, center=decision.center,
                atr=decision.atr, regime=decision.regime.value,
                levels=active["levels"], economics=decision.to_dict()["economics"],
            )

        if decision.regime is not GridRegime.ALLOWED:
            cancelled = []
            for level in active["levels"]:
                if level["status"] == "WAITING":
                    level["status"] = "CANCELLED"
                    cancelled.append(level["index"])
            self.emit(
                "SHADOW_KILL_SWITCH", symbol=decision.symbol,
                regime=decision.regime.value, reason=decision.reason,
                stopped_deeper_levels=cancelled,
            )

        filled_inventory = sum(
            float(level["quantity"]) for level in active["levels"]
            if level["status"] == "FILLED"
        )
        if filled_inventory > 0 and low <= float(active["hard_invalidation"]):
            self.emit(
                "SHADOW_HARD_KILL", symbol=decision.symbol,
                hard_invalidation=active["hard_invalidation"], observed_low=low,
                inventory_before=filled_inventory, inventory_after=0.0,
                emergency_behavior=True,
            )
            self.store.save({
                "last_resolved_at": self._iso(timestamp_ms),
                "last_resolved_timestamp_ms": timestamp_ms,
                "last_center": active["center"],
                "last_reason": "hard_invalidation",
            })
            return

        inventory = filled_inventory
        for level in active["levels"]:
            prior_status = level["status"]
            if prior_status == "FILLED":
                entry = float(level["entry_price"])
                level["mae_bps"] = min(
                    float(level["mae_bps"]), (low - entry) / entry * 10_000.0
                )
                level["mfe_bps"] = max(
                    float(level["mfe_bps"]), (high - entry) / entry * 10_000.0
                )
                if high >= float(level["target_price"]):
                    before = inventory
                    inventory = max(inventory - float(level["quantity"]), 0.0)
                    level["status"] = "CLOSED"
                    gross_bps = (
                        (float(level["target_price"]) - entry) / entry * 10_000.0
                    )
                    self.emit(
                        "SHADOW_TP_HIT", symbol=decision.symbol, level=level["index"],
                        entry_price=entry, exit_price=level["target_price"],
                        gross_bps=gross_bps,
                        expected_cost_bps=decision.economics.hurdle_bps,
                        expected_net_capture_bps=decision.economics.expected_net_capture_bps,
                        hold_time_minutes=(timestamp_ms - int(level["filled_timestamp_ms"])) / 60_000.0,
                        mae_bps=level["mae_bps"], mfe_bps=level["mfe_bps"],
                        inventory_before=before, inventory_after=inventory,
                    )
            elif prior_status == "WAITING" and decision.regime is GridRegime.ALLOWED:
                if low <= float(level["entry_price"]):
                    before = inventory
                    inventory += float(level["quantity"])
                    level["status"] = "FILLED"
                    level["filled_timestamp_ms"] = timestamp_ms
                    self.emit(
                        "SHADOW_LEVEL_FILLED", symbol=decision.symbol,
                        level=level["index"], entry_price=level["entry_price"],
                        target_price=level["target_price"],
                        gross_capture_bps=decision.economics.gross_capture_bps,
                        expected_cost_bps=decision.economics.hurdle_bps,
                        expected_net_capture_bps=decision.economics.expected_net_capture_bps,
                        inventory_before=before, inventory_after=inventory,
                    )

        self.store.save({"active_grid": active})
        if all(level["status"] in {"CLOSED", "CANCELLED"} for level in active["levels"]):
            self.emit(
                "SHADOW_CYCLE_RESOLVED", symbol=decision.symbol,
                center=active["center"], duration_minutes=(
                    timestamp_ms - int(active["opened_timestamp_ms"])
                ) / 60_000.0,
                fills_per_level=[
                    level["index"] for level in active["levels"]
                    if "filled_timestamp_ms" in level
                ],
                emergency_behavior=False,
            )
            self.store.save({
                "last_resolved_at": self._iso(timestamp_ms),
                "last_resolved_timestamp_ms": timestamp_ms,
                "last_center": active["center"],
                "last_reason": "resolved",
            })


__all__ = ["DynamicGridShadow"]
