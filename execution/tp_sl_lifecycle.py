"""TP/SL protection lifecycle for PositionManager (extracted, behavior-neutral).

Mixin: SL moves with verify, protection repair, emergency close of unprotected
positions, hit/stop predicates and failed-continuation tightening. Methods are
moved verbatim from position_manager.py.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from clients.schemas import MarketSnapshot
from execution.position_model import (
    BE_PLUS_FEES_CONFIRMED,
    BE_PLUS_FEES_PENDING,
    INITIAL_PROTECTION_CONFIRMED,
    PROFIT_LOCK_CONFIRMED,
    PROFIT_LOCK_PENDING,
    PROTECTION_UPDATE_FAILED,
    TRAILING_CONFIRMED,
    TRAILING_PENDING,
    BreakEvenResult,
    PositionLifecycleMismatch,
    PositionModelError,
    calculate_break_even_plus_fees,
    confirm_exchange_position,
    confirmed_position_size,
    decimal_float,
    decimal_value,
    position_prices,
    select_opening_fee,
    stop_is_legal,
    stop_is_monotonic,
)


class TpSlLifecycleMixin:
    def _extract_live_protection_payload(self, position: dict) -> dict:
        stop_loss = 0.0
        for key in ("stopLoss", "stop_loss", "sl", "presetStopLossPrice"):
            stop_loss = self._safe_float(position.get(key), 0.0)
            if stop_loss > 0:
                break

        take_profits: list[float] = []
        for key in ("takeProfit", "take_profit", "tp", "presetTakeProfitPrice"):
            value = self._safe_float(position.get(key), 0.0)
            if value > 0:
                take_profits.append(value)

        raw_tps = position.get("take_profits") or position.get("takeProfits") or []
        if isinstance(raw_tps, list):
            for value in raw_tps:
                parsed = self._safe_float(value, 0.0)
                if parsed > 0:
                    take_profits.append(parsed)

        return {
            "stop_loss": stop_loss,
            "take_profits": sorted(set(take_profits)),
            "source": "bitget_position_recovery",
        }

    def _fallback_protection_from_execution_log(
        self,
        symbol: str,
        *,
        lifecycle_id: str,
    ) -> dict:
        if not lifecycle_id:
            return {
                "stop_loss": 0.0,
                "take_profits": [],
                "source": "execution_log_lifecycle_required",
            }
        path = Path("logs/executions.csv")
        if not path.exists():
            return {"stop_loss": 0.0, "take_profits": [], "source": "execution_log_missing"}

        latest: dict | None = None
        try:
            with path.open("r", newline="") as handle:
                reader = csv.DictReader(handle)
                for row in reader:
                    row_symbol = str(row.get("symbol") or "").upper()
                    status = str(row.get("status") or "").upper()
                    row_lifecycle_id = str(
                        row.get("position_lifecycle_id") or ""
                    )
                    if (
                        row_symbol != symbol.upper()
                        or status != "EXECUTED"
                        or row_lifecycle_id != lifecycle_id
                    ):
                        continue

                    stop_loss = self._safe_float(row.get("stop_loss"), 0.0)
                    take_profits = []
                    for raw_tp in str(row.get("take_profits") or "").split("|"):
                        parsed = self._safe_float(raw_tp.strip(), 0.0)
                        if parsed > 0:
                            take_profits.append(parsed)

                    if stop_loss > 0 and take_profits:
                        latest = {
                            "stop_loss": stop_loss,
                            "take_profits": take_profits,
                            "source": "logs/executions.csv:lifecycle_verified",
                            "position_lifecycle_id": lifecycle_id,
                        }
        except Exception as exc:
            self.log.warning("STATE_RECOVERY_PROTECTION_FALLBACK_FAILED | %s | error=%s", symbol, exc)
            return {"stop_loss": 0.0, "take_profits": [], "source": "execution_log_error"}

        if latest:
            return latest
        return {"stop_loss": 0.0, "take_profits": [], "source": "execution_log_no_match"}

    def _heal_missing_protection_from_fallback(self, position: dict) -> None:
        symbol = str(position.get("symbol") or "").upper()
        if not symbol:
            return

        current_stop = self._safe_float(position.get("stop_loss"), 0.0)
        current_tps = position.get("take_profits") or []
        has_tps = isinstance(current_tps, list) and any(self._safe_float(tp, 0.0) > 0 for tp in current_tps)
        if current_stop > 0 and has_tps:
            return

        fallback = self._fallback_protection_from_execution_log(
            symbol,
            lifecycle_id=str(position.get("position_lifecycle_id") or ""),
        )
        fallback_stop = self._safe_float(fallback.get("stop_loss"), 0.0)
        fallback_tps = [self._safe_float(tp, 0.0) for tp in fallback.get("take_profits", [])]
        fallback_tps = [tp for tp in fallback_tps if tp > 0]

        if fallback_stop <= 0 or not fallback_tps:
            return

        position["stop_loss"] = fallback_stop
        position["take_profits"] = fallback_tps
        position["protection_verified"] = True
        position["protection_payload"] = fallback
        notes = position.setdefault("notes", [])
        if isinstance(notes, list):
            notes.append("PROTECTION_HEALED_FROM_EXECUTION_LOG")

        self.log.warning(
            "STATE_PROTECTION_HEALED | %s | stop=%s tps=%s source=%s",
            symbol,
            fallback_stop,
            fallback_tps,
            fallback.get("source"),
        )

    @staticmethod
    def _position_age_minutes(position: dict) -> float:
        opened_at = str(position.get("opened_at") or position.get("created_at") or "")
        if not opened_at:
            return 0.0
        try:
            opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=timezone.utc)
            return max(0.0, (now - opened).total_seconds() / 60.0)
        except Exception:
            return 0.0

    @staticmethod
    def _snapshot_note_text(snapshot: MarketSnapshot | None) -> str:
        if snapshot is None:
            return ""
        return " | ".join(str(note).lower() for note in (getattr(snapshot, "notes", []) or []))

    @staticmethod
    def _extract_note_float_from_text(note_text: str, marker: str, default: float = 0.0) -> float:
        marker = marker.lower()
        if marker not in note_text:
            return default
        try:
            raw = note_text.split(marker, 1)[1].split()[0].strip("|,;")
            return float(raw)
        except Exception:
            return default

    def _distance_to_target_pct(self, direction: str, current_price: float, target: float) -> float:
        if current_price <= 0 or target <= 0:
            return 999.0
        if direction == "LONG":
            if current_price >= target:
                return 0.0
            return ((target - current_price) / current_price) * 100.0
        if current_price <= target:
            return 0.0
        return ((current_price - target) / current_price) * 100.0

    def _next_unhit_target(self, position: dict) -> float:
        tps = [float(x) for x in position.get("take_profits", []) if float(x) > 0]
        if not tps:
            return 0.0
        if not position.get("tp1_hit") and len(tps) >= 1:
            return tps[0]
        if not position.get("tp2_hit") and len(tps) >= 2:
            return tps[1]
        if not position.get("tp3_hit") and len(tps) >= 3:
            return tps[2]
        return 0.0

    def _directional_pressure_failed(self, direction: str, snapshot: MarketSnapshot | None) -> tuple[bool, dict[str, object]]:
        note_text = self._snapshot_note_text(snapshot)
        wanted = "bullish" if direction == "LONG" else "bearish"
        opposite = "bearish" if direction == "LONG" else "bullish"
        pressure_score = self._extract_note_float_from_text(note_text, "pressure_score=", 0.0)
        expansion_prob = self._extract_note_float_from_text(note_text, "expansion_prob=", 0.0)
        volume_ratio = float(getattr(getattr(snapshot, "primary", None), "volume_ratio_20", 0.0) or 0.0) if snapshot else 0.0
        primary_trend = str(getattr(getattr(snapshot, "primary", None), "trend", "") or "").lower() if snapshot else ""
        confirmation_trend = str(getattr(getattr(snapshot, "confirmation", None), "trend", "") or "").lower() if snapshot else ""
        alignment = str(getattr(snapshot, "alignment", "") or "").lower() if snapshot else ""

        pressure_against = f"pressure={opposite}" in note_text or f"direction={opposite}" in note_text
        pressure_missing = (
            f"pressure={wanted}" not in note_text
            and f"direction={wanted}" not in note_text
            and "breakout_context ready=true" not in note_text
            and pressure_score < 45.0
        )
        volume_dead = volume_ratio < 0.35
        trend_against = (
            primary_trend == opposite
            or confirmation_trend == opposite
            or alignment == "conflicted"
        )

        failed = bool(pressure_against or (pressure_missing and volume_dead) or (trend_against and volume_dead))
        return failed, {
            "pressure_score": pressure_score,
            "expansion_prob": expansion_prob,
            "volume_ratio": volume_ratio,
            "primary_trend": primary_trend,
            "confirmation_trend": confirmation_trend,
            "alignment": alignment,
            "pressure_against": pressure_against,
            "pressure_missing": pressure_missing,
            "volume_dead": volume_dead,
            "trend_against": trend_against,
        }

    def _failed_continuation_target_stop(
        self,
        position: dict,
        direction: str,
        current_price: float,
        *,
        live_position: dict | None = None,
    ) -> float:
        fee_be = decimal_float(
            self._break_even_plus_fees(position, live_position=live_position).target
        )
        buffer = self.failed_continuation_sl_buffer_pct / 100.0
        if direction == "LONG":
            protective_stop = current_price * (1.0 - buffer)
            return max(fee_be, protective_stop)
        protective_stop = current_price * (1.0 + buffer)
        return min(fee_be, protective_stop)

    def _should_tighten_failed_continuation(
        self,
        position: dict,
        snapshot: MarketSnapshot | None,
        direction: str,
        exchange_entry: float,
        current_price: float,
        price_return_pct: float,
    ) -> tuple[bool, float, dict[str, object]]:
        authoritative_entry = position_prices(
            position,
            require_executed=True,
        ).require_executed().price
        if decimal_value(exchange_entry) != authoritative_entry:
            raise PositionLifecycleMismatch(
                "failed-continuation entry does not match exchange_avg_entry"
            )
        if position.get("failed_continuation_protection_active"):
            return False, 0.0, {"reason": "already_active"}
        if position.get("status") != "OPEN":
            return False, 0.0, {"reason": "not_open"}

        strategy = str(position.get("strategy") or "").lower()
        if "continuation" not in strategy and "reclaim" not in strategy and "breakout" not in strategy:
            return False, 0.0, {"reason": "strategy_not_lifecycle_managed"}

        age_minutes = self._position_age_minutes(position)
        if age_minutes < self.failed_continuation_min_age_minutes:
            return False, 0.0, {"reason": "too_young", "age_minutes": age_minutes}

        next_target = self._next_unhit_target(position)
        if next_target <= 0:
            return False, 0.0, {"reason": "no_next_target"}

        distance_to_tp_pct = self._distance_to_target_pct(direction, current_price, next_target)
        near_tp = distance_to_tp_pct <= self.tp_miss_near_pct
        had_progress = bool(position.get("tp1_hit")) or price_return_pct > 0.10
        pressure_failed, pressure_context = self._directional_pressure_failed(direction, snapshot)

        if not near_tp and not had_progress:
            return False, 0.0, {"reason": "not_near_tp_or_in_profit", **pressure_context}
        if not pressure_failed:
            return False, 0.0, {"reason": "pressure_not_failed", **pressure_context}
        if price_return_pct < self.failed_continuation_min_unrealized_pct:
            return False, 0.0, {"reason": "already_too_negative", "price_return_pct": price_return_pct, **pressure_context}

        new_stop = self._failed_continuation_target_stop(
            position,
            direction,
            current_price,
        )
        current_stop = float(position.get("stop_loss") or 0.0)
        if direction == "LONG" and new_stop <= current_stop:
            return False, 0.0, {"reason": "new_stop_not_tighter", "new_stop": new_stop, "current_stop": current_stop, **pressure_context}
        if direction == "SHORT" and current_stop > 0 and new_stop >= current_stop:
            return False, 0.0, {"reason": "new_stop_not_tighter", "new_stop": new_stop, "current_stop": current_stop, **pressure_context}

        return True, new_stop, {
            "reason": "failed_continuation_detected",
            "age_minutes": age_minutes,
            "next_target": next_target,
            "distance_to_tp_pct": distance_to_tp_pct,
            "near_tp": near_tp,
            "had_progress": had_progress,
            "price_return_pct": price_return_pct,
            **pressure_context,
        }

    def _has_local_protection_payload(self, position: dict) -> bool:
        payload = position.get("protection_payload") or {}
        if not isinstance(payload, dict):
            return False

        has_sl = bool(payload.get("stop_loss"))
        take_profits = payload.get("take_profits") or []
        has_tp = isinstance(take_profits, list) and len(take_profits) > 0

        stop_loss = float(position.get("stop_loss") or 0)
        expected_tps = [float(x) for x in position.get("take_profits", []) if float(x) > 0]

        return has_sl and has_tp and stop_loss > 0 and len(expected_tps) > 0

    def _ensure_exchange_protection_with_retries(self, position: dict) -> bool:
        for attempt in range(1, self.protection_repair_retries + 1):
            if self._ensure_exchange_protection(position):
                return True
            self.log.warning(
                "Protection repair attempt %s/%s failed for %s",
                attempt,
                self.protection_repair_retries,
                position.get("symbol"),
            )
        return False

    def _ensure_exchange_protection(self, position: dict) -> bool:
        symbol = str(position.get("symbol") or "")
        direction = str(position.get("direction") or "")
        stop_loss = float(position.get("stop_loss") or 0)
        take_profits = [float(x) for x in position.get("take_profits", []) if float(x) > 0]
        try:
            context = self._refresh_protection_context(position)
            size = decimal_float(context["size"].quantity, places=8)
        except Exception as exc:
            self.log.critical(
                "PROTECTION_REPAIR_DATA_UNAVAILABLE | %s | confirmed_stop_retained=%s | error=%s",
                symbol,
                position.get("confirmed_stop") or position.get("stop_loss"),
                exc,
            )
            return False

        if not symbol or not direction or stop_loss <= 0 or not take_profits or size <= 0:
            self.log.warning(
                "Protection repair skipped for %s: missing data stop=%s tps=%s size=%s",
                symbol,
                stop_loss,
                take_profits,
                size,
            )
            return False

        if not stop_is_legal(
            direction=direction,
            target=decimal_value(stop_loss),
            current_mark=context["mark"],
            tick_size=context["tick_size"],
            safety_ticks=int(
                getattr(self.settings, "break_even_mark_safety_ticks", 2) or 0
            ),
        ):
            self.log.error(
                "PROTECTION_REPAIR_STOP_NOT_LEGAL | %s | direction=%s | stop=%s | mark=%s | "
                "confirmed_stop_retained=%s",
                symbol,
                direction,
                stop_loss,
                context["mark"],
                position.get("confirmed_stop") or position.get("stop_loss"),
            )
            return False

        placer = getattr(self.client, "place_futures_protection_orders", None)
        if not callable(placer):
            self.log.warning("Protection repair pending for %s: place_futures_protection_orders missing", symbol)
            return False

        try:
            payload = placer(
                symbol=symbol,
                direction=direction,
                size=size,
                stop_loss=stop_loss,
                take_profits=take_profits,
                margin_mode="isolated",
            )
            has_sl = bool(
                payload
                and (
                    payload.get("stop_loss_verified")
                    or payload.get("stop_loss")
                )
            )
            tps = payload.get("take_profits") if payload else None
            actual_tp_count = int(
                payload.get("take_profit_count")
                or (len(tps) if isinstance(tps, list) else 0)
            ) if payload else 0
            has_tp = actual_tp_count >= len(take_profits)
            exchange_verified = bool(
                payload and payload.get("protection_verified")
            )
            if has_sl and has_tp and exchange_verified:
                position["protection_payload"] = payload
                position["exchange_stop_loss"] = stop_loss
                position["confirmed_stop"] = stop_loss
                position["confirmed_stop_size"] = size
                position["protection_state"] = INITIAL_PROTECTION_CONFIRMED
                return True
        except Exception as exc:
            self.log.error("Protection repair failed for %s: %s", symbol, exc)

        return False

    def _extract_stop_loss_order_ids(self, position: dict) -> list[str]:
        order_ids: list[str] = []

        direct_order_id = position.get("exchange_stop_loss_order_id")
        if direct_order_id:
            order_ids.append(str(direct_order_id))

        protection_payload = position.get("protection_payload") or {}
        if isinstance(protection_payload, dict):
            stop_payload = protection_payload.get("stop_loss") or {}
            if isinstance(stop_payload, dict):
                stop_data = stop_payload.get("data") or {}
                if isinstance(stop_data, dict):
                    stop_order_id = stop_data.get("orderId") or stop_data.get("order_id")
                    if stop_order_id:
                        order_ids.append(str(stop_order_id))

        extra_ids = position.get("active_stop_loss_order_ids") or []
        if isinstance(extra_ids, list):
            order_ids.extend(str(item) for item in extra_ids if item)

        seen: set[str] = set()
        unique_ids: list[str] = []
        for order_id in order_ids:
            if order_id in seen:
                continue
            seen.add(order_id)
            unique_ids.append(order_id)

        return unique_ids

    def _cancel_existing_exchange_stop_losses(self, position: dict, reason: str) -> bool:
        symbol = str(position.get("symbol") or "").upper()
        order_ids = self._extract_stop_loss_order_ids(position)

        if not symbol or not order_ids:
            self.log.info(
                "EXCHANGE_SL_CANCEL_SKIP | %s | reason=%s | no_existing_sl_order_ids",
                symbol or "UNKNOWN",
                reason,
            )
            return True

        all_cancelled = True
        cancelled_ids: list[str] = []

        for order_id in order_ids:
            try:
                payload = self.client.cancel_futures_plan_order(
                    symbol=symbol,
                    order_id=order_id,
                    )
                cancelled_ids.append(order_id)
                self.log.warning(
                    "EXCHANGE_SL_CANCELLED | %s | order_id=%s | reason=%s | payload=%s",
                        symbol,
                    order_id,
                    reason,
                    payload,
                    )
            except Exception as exc:
                all_cancelled = False
                self.log.error(
                    "EXCHANGE_SL_CANCEL_FAILED | %s | order_id=%s | reason=%s | error=%s",
                        symbol,
                    order_id,
                    reason,
                    exc,
                    )

        if all_cancelled:
            position["active_stop_loss_order_ids"] = []
            position["exchange_stop_loss_order_id"] = ""
            protection_payload = position.get("protection_payload") or {}
            if isinstance(protection_payload, dict):
                protection_payload["previous_stop_loss_order_ids"] = cancelled_ids
                position["protection_payload"] = protection_payload

        return all_cancelled

    @staticmethod
    def _store_new_stop_loss_order_id(position: dict, payload: dict | None) -> None:
        if not isinstance(payload, dict):
            return

        order_id = ""
        placed_payload = payload.get("placed")
        if isinstance(placed_payload, dict):
            nested_data = placed_payload.get("data") or {}
            if isinstance(nested_data, dict):
                order_id = str(
                    nested_data.get("orderId")
                    or nested_data.get("order_id")
                    or nested_data.get("planOrderId")
                    or nested_data.get("id")
                    or ""
                )
        data = payload.get("data") or {}
        if not order_id and isinstance(data, dict):
            order_id = str(
                data.get("orderId")
                or data.get("order_id")
                or data.get("planOrderId")
                or data.get("id")
                or ""
            )

        if not order_id:
            order_id = str(payload.get("placed_order_id") or payload.get("orderId") or payload.get("planOrderId") or "")

        if not order_id and isinstance(data, dict):
            success_list = data.get("successList") or data.get("success_list") or []
            if isinstance(success_list, list):
                for item in success_list:
                    if isinstance(item, dict):
                        order_id = str(item.get("orderId") or item.get("planOrderId") or item.get("id") or "")
                        if order_id:
                            break

        if not order_id:
            return

        position["exchange_stop_loss_order_id"] = order_id
        position["active_stop_loss_order_ids"] = [order_id]

    @staticmethod
    def _pending_protection_state(reason: str) -> str:
        upper = str(reason or "").upper()
        if "TRAIL" in upper:
            return TRAILING_PENDING
        if "PROFIT" in upper or "FAILED_CONTINUATION" in upper or "TP2" in upper:
            return PROFIT_LOCK_PENDING
        return BE_PLUS_FEES_PENDING

    @staticmethod
    def _confirmed_protection_state(reason: str) -> str:
        upper = str(reason or "").upper()
        if "TRAIL" in upper:
            return TRAILING_CONFIRMED
        if "PROFIT" in upper or "FAILED_CONTINUATION" in upper or "TP2" in upper:
            return PROFIT_LOCK_CONFIRMED
        return BE_PLUS_FEES_CONFIRMED

    def _exchange_tick_size(self, position: dict, *, force_refresh: bool = False) -> Decimal:
        persisted = decimal_value(position.get("exchange_tick_size"))
        if persisted > 0 and not force_refresh:
            return persisted

        symbol = str(position.get("symbol") or "")
        resolver = getattr(self.client, "_contract_price_scale", None)
        if callable(resolver):
            scale = resolver(symbol, force_refresh=force_refresh)
            if isinstance(scale, int) and scale >= 0:
                tick = Decimal("1").scaleb(-scale)
                position["exchange_tick_size"] = decimal_float(tick)
                return tick
        raise PositionModelError(f"exchange tick size unavailable for {symbol}")

    def _break_even_plus_fees(
        self,
        position: dict,
        *,
        live_position: dict | None = None,
    ) -> BreakEvenResult:
        entry = position_prices(position, require_executed=True).require_executed().price
        size = confirmed_position_size(
            position,
            live_position=live_position,
            critical=True,
        ).quantity
        configured_close_rate = decimal_value(
            getattr(self.settings, "break_even_expected_close_fee_rate", 0.0006)
        )
        configured_open_rate = decimal_value(
            getattr(self.settings, "break_even_open_fee_fallback_rate", 0.0006)
        )
        opening_fee = select_opening_fee(
            position,
            exchange_entry=entry,
            remaining_quantity=size,
            configured_fallback_rate=configured_open_rate,
        )
        result = calculate_break_even_plus_fees(
            direction=str(position.get("direction") or ""),
            exchange_entry=entry,
            remaining_quantity=size,
            tick_size=self._exchange_tick_size(position),
            opening_fee=opening_fee,
            expected_close_fee_rate=configured_close_rate,
            spread_buffer_pct=decimal_value(
                getattr(self.settings, "break_even_spread_buffer_pct", 0.02)
            ),
            slippage_buffer_pct=decimal_value(
                getattr(self.settings, "break_even_slippage_buffer_pct", 0.03)
            ),
            extra_buffer_pct=decimal_value(
                getattr(self.settings, "break_even_extra_buffer_pct", 0.01)
            ),
            legacy_fee_buffer_pct=decimal_value(
                getattr(self.settings, "break_even_fee_buffer_pct", 0.12)
            ),
        )
        position["calculated_be_plus_fees"] = decimal_float(result.target)
        position["break_even_fee_source"] = result.fee_source
        position["break_even_required_recovery_usdt"] = decimal_float(
            result.required_recovery_usdt
        )
        position["break_even_expected_net_usdt"] = decimal_float(
            result.expected_net_usdt
        )
        position["break_even_costs"] = {
            "opening_fee_usdt": decimal_float(result.opening_fee_usdt),
            "expected_closing_fee_usdt": decimal_float(result.expected_closing_fee_usdt),
            "spread_allowance_usdt": decimal_float(result.spread_allowance_usdt),
            "slippage_allowance_usdt": decimal_float(result.slippage_allowance_usdt),
            "extra_safety_allowance_usdt": decimal_float(result.extra_safety_allowance_usdt),
        }
        self.log.info(
            "BE_PLUS_FEES_CALCULATED | %s | lifecycle_id=%s | target=%s | fee_source=%s | "
            "recovery_usdt=%s | expected_net_usdt=%s | legacy=%s",
            position.get("symbol"),
            position.get("position_lifecycle_id"),
            result.target,
            result.fee_source,
            result.required_recovery_usdt,
            result.expected_net_usdt,
            result.used_legacy_fallback,
        )
        return result

    def _candidate_break_even_stop(
        self,
        position: dict,
        *,
        live_position: dict | None = None,
    ) -> float:
        try:
            return decimal_float(
                self._break_even_plus_fees(
                    position,
                    live_position=live_position,
                ).target
            )
        except Exception as exc:
            position["protection_state"] = PROTECTION_UPDATE_FAILED
            position["protection_update_error"] = str(exc)
            self.log.critical(
                "PROTECTION_DATA_UNAVAILABLE | %s | lifecycle_id=%s | confirmed_stop_retained=%s | error=%s",
                position.get("symbol"),
                position.get("position_lifecycle_id") or "UNKNOWN",
                position.get("confirmed_stop") or position.get("stop_loss"),
                exc,
            )
            return 0.0

    def _refresh_protection_context(self, position: dict) -> dict:
        lifecycle_id = str(position.get("position_lifecycle_id") or "")
        symbol = str(position.get("symbol") or "")
        payload = self.client.get_all_positions()
        positions_live = payload.get("data") or []
        live_position = self._find_live_position(symbol, positions_live)
        if not live_position:
            raise PositionLifecycleMismatch("position no longer exists on exchange")
        confirm_exchange_position(
            position,
            live_position,
            source="BITGET_OPEN_POSITION_RETRY_REFRESH",
        )
        if lifecycle_id and str(position.get("position_lifecycle_id") or "") != lifecycle_id:
            raise PositionLifecycleMismatch("position lifecycle changed during protection retry")

        mark = decimal_value(self._live_mark_price(live_position))
        size = confirmed_position_size(
            position,
            live_position=live_position,
            critical=True,
        )
        direction = str(position.get("direction") or "").upper()
        hold_side = "long" if direction == "LONG" else "short"
        protection_reader = getattr(self.client, "get_active_protection_snapshot", None)
        protection_snapshot = (
            protection_reader(symbol=symbol, hold_side=hold_side)
            if callable(protection_reader)
            else {}
        )
        if not isinstance(protection_snapshot, dict):
            protection_snapshot = {}
        stop_orders = protection_snapshot.get("stop_orders") or []
        stop_prices = [
            decimal_value(order.get("trigger_price"))
            for order in stop_orders
            if isinstance(order, dict) and decimal_value(order.get("trigger_price")) > 0
        ]
        exchange_active_stop = (
            max(stop_prices)
            if direction == "LONG" and stop_prices
            else min(stop_prices)
            if direction == "SHORT" and stop_prices
            else Decimal("0")
        )
        active_stop = decimal_value(
            exchange_active_stop
            or live_position.get("stopLoss")
            or live_position.get("stop_loss")
            or position.get("confirmed_stop")
            or position.get("stop_loss")
        )
        if mark <= 0:
            raise PositionModelError("current exchange mark unavailable")
        return {
            "live_position": live_position,
            "mark": mark,
            "size": size,
            "direction": direction,
            "active_stop": active_stop,
            "tick_size": self._exchange_tick_size(position, force_refresh=True),
            "lifecycle_id": str(position.get("position_lifecycle_id") or ""),
            "protection_snapshot": protection_snapshot,
        }

    def _retry_target(
        self,
        position: dict,
        *,
        requested_stop: float,
        reason: str,
        context: dict,
    ) -> Decimal:
        upper = str(reason or "").upper()
        mark = context["mark"]
        direction = context["direction"]
        if any(
            token in upper
            for token in ("BE", "PROFIT_LOCK", "FAILED_CONTINUATION", "NEAR_TP")
        ):
            be_result = self._break_even_plus_fees(
                position,
                live_position=context["live_position"],
            )
            target = be_result.target
            if "FAILED_CONTINUATION" in upper:
                buffer_rate = decimal_value(self.failed_continuation_sl_buffer_pct) / Decimal("100")
                protective = (
                    mark * (Decimal("1") - buffer_rate)
                    if direction == "LONG"
                    else mark * (Decimal("1") + buffer_rate)
                )
                target = max(target, protective) if direction == "LONG" else min(target, protective)
                tick = context["tick_size"]
                rounding = "ROUND_CEILING" if direction == "LONG" else "ROUND_FLOOR"
                target = (target / tick).to_integral_value(rounding=rounding) * tick
            return target
        return decimal_value(requested_stop)

    def _cancel_replaced_stop_ids(
        self,
        *,
        position: dict,
        old_order_ids: list[str],
        new_order_id: str,
        reason: str,
    ) -> bool:
        symbol = str(position.get("symbol") or "").upper()
        all_cancelled = True
        for order_id in old_order_ids:
            if not order_id or order_id == new_order_id:
                continue
            try:
                self.client.cancel_futures_plan_order(
                    symbol=symbol,
                    order_id=order_id,
                )
            except Exception as exc:
                # Both stops are protective. Leaving the older one active is
                # safer than rolling back the newly confirmed tighter stop.
                all_cancelled = False
                self.log.error(
                    "REPLACED_SL_CLEANUP_FAILED | %s | old_order_id=%s | new_order_id=%s | "
                    "reason=%s | both_stops_may_remain_active=True | error=%s",
                    symbol,
                    order_id,
                    new_order_id,
                    reason,
                    exc,
                )
        return all_cancelled

    @staticmethod
    def _protection_reason_flags(position: dict, reason: str) -> None:
        upper = str(reason or "").upper()
        if any(
            token in upper
            for token in ("BE", "NEAR_TP", "PROFIT_LOCK", "TP1", "TP2", "FAILED_CONTINUATION")
        ):
            position["break_even_active"] = True
            position["tp1_locked_stop_active"] = True
        if "NEAR_TP" in upper:
            position["near_tp_protection_active"] = True
        if "PROFIT_LOCK" in upper:
            position["profit_lock_active"] = True
        if "FAILED_CONTINUATION" in upper:
            position["failed_continuation_protection_active"] = True
            position["failed_continuation_tighten_pending"] = False

    @staticmethod
    def _record_pending_protection_update(
        position: dict,
        *,
        requested_stop: float,
        reason: str,
    ) -> None:
        position["protection_update_pending"] = True
        position["pending_protection_requested_stop"] = float(requested_stop)
        position["pending_protection_reason"] = str(reason)
        position["pending_protection_lifecycle_id"] = str(
            position.get("position_lifecycle_id") or ""
        )

    def _move_exchange_stop_loss_with_retries(
        self,
        position: dict,
        new_stop: float,
        reason: str,
    ) -> bool:
        symbol = str(position.get("symbol") or "")
        previous_confirmed = decimal_value(
            position.get("confirmed_stop") or position.get("stop_loss")
        )
        old_order_ids = self._extract_stop_loss_order_ids(position)
        position["protection_state"] = self._pending_protection_state(reason)
        safest_confirmed = previous_confirmed

        for attempt in range(1, self.be_move_retries + 1):
            try:
                context = self._refresh_protection_context(position)
                target = self._retry_target(
                    position,
                    requested_stop=new_stop,
                    reason=reason,
                    context=context,
                )
                position["last_protection_retry_mark"] = decimal_float(context["mark"])
                position["last_protection_retry_target"] = decimal_float(target)
                position["last_protection_retry_attempt"] = attempt

                refreshed_ids = [
                    str(order.get("order_id") or "")
                    for order in (context["protection_snapshot"].get("stop_orders") or [])
                    if isinstance(order, dict) and order.get("order_id")
                ]
                old_order_ids = list(dict.fromkeys([*old_order_ids, *refreshed_ids]))
                active_stop = decimal_value(context.get("active_stop"))
                if active_stop > 0:
                    safest_confirmed = (
                        max(safest_confirmed, active_stop)
                        if context["direction"] == "LONG"
                        else min(safest_confirmed, active_stop)
                        if safest_confirmed > 0
                        else active_stop
                    )

                if not stop_is_monotonic(
                    direction=context["direction"],
                    previous=safest_confirmed,
                    proposed=target,
                ):
                    raise PositionModelError(
                        f"non-monotonic stop rejected: previous={previous_confirmed} proposed={target}"
                    )

                if not stop_is_legal(
                    direction=context["direction"],
                    target=target,
                    current_mark=context["mark"],
                    tick_size=context["tick_size"],
                    safety_ticks=int(
                        getattr(self.settings, "break_even_mark_safety_ticks", 2) or 0
                    ),
                ):
                    status = (
                        "BE_WINDOW_MISSED"
                        if (
                            context["direction"] == "LONG"
                            and context["mark"] <= target
                        )
                        or (
                            context["direction"] == "SHORT"
                            and context["mark"] >= target
                        )
                        else "BE_PLUS_FEES_NOT_LEGAL"
                    )
                    position["be_plus_fees_status"] = status
                    position["protection_state"] = PROTECTION_UPDATE_FAILED
                    position["protection_update_error"] = status
                    self._record_pending_protection_update(
                        position,
                        requested_stop=new_stop,
                        reason=reason,
                    )
                    self.log.warning(
                        "%s | %s | lifecycle_id=%s | mark=%s | target=%s | "
                        "confirmed_stop_retained=%s | attempt=%s",
                        status,
                        symbol,
                        context["lifecycle_id"],
                        context["mark"],
                        target,
                        safest_confirmed,
                        attempt,
                    )
                    return False

                payload = self._move_exchange_stop_loss(
                    position,
                    decimal_float(target),
                    reason,
                )
                if not payload:
                    raise PositionModelError("exchange stop submission failed")

                verifier = getattr(self.client, "verify_active_stop_loss", None)
                verification = (
                    verifier(
                        symbol=symbol,
                        hold_side="long" if context["direction"] == "LONG" else "short",
                        expected_trigger_price=decimal_float(target),
                    )
                    if callable(verifier)
                    else {"verified": bool(payload.get("verified"))}
                )
                if not bool(verification.get("verified")):
                    raise PositionModelError(
                        f"replacement stop not exchange-verified: {verification.get('reason')}"
                    )

                self._store_new_stop_loss_order_id(position, payload)
                new_order_id = str(position.get("exchange_stop_loss_order_id") or "")
                position["stop_loss"] = decimal_float(target)
                position["exchange_stop_loss"] = decimal_float(target)
                position["confirmed_stop"] = decimal_float(target)
                position["confirmed_stop_at"] = datetime.now(timezone.utc).isoformat()
                position["confirmed_stop_size"] = decimal_float(context["size"].quantity)
                position["confirmed_stop_size_source"] = context["size"].source
                position["protection_state"] = self._confirmed_protection_state(reason)
                position["protection_integrity"] = "VERIFIED"
                position["last_sl_verification"] = verification
                position["protection_update_pending"] = False
                position["pending_protection_requested_stop"] = 0.0
                position["pending_protection_reason"] = ""
                position["pending_protection_lifecycle_id"] = ""
                position["be_plus_fees_status"] = (
                    "CONFIRMED"
                    if position["protection_state"] == BE_PLUS_FEES_CONFIRMED
                    else position.get("be_plus_fees_status", "")
                )
                old_stops_removed = self._cancel_replaced_stop_ids(
                    position=position,
                    old_order_ids=old_order_ids,
                    new_order_id=new_order_id,
                    reason=reason,
                )
                position["old_stop_loss_removed"] = old_stops_removed
                self._protection_reason_flags(position, reason)
                position["active_stop_loss_order_ids"] = (
                    [new_order_id] if new_order_id else []
                )
                return True
            except Exception as exc:
                position["protection_update_error"] = str(exc)
                self.log.warning(
                    "PROTECTION_RETRY_FAILED | %s | lifecycle_id=%s | attempt=%s/%s | "
                    "reason=%s | confirmed_stop_retained=%s | error=%s",
                    symbol,
                    position.get("position_lifecycle_id") or "UNKNOWN",
                    attempt,
                    self.be_move_retries,
                    reason,
                    previous_confirmed,
                    exc,
                )

        position["protection_state"] = PROTECTION_UPDATE_FAILED
        position["stop_loss"] = decimal_float(safest_confirmed)
        position["exchange_stop_loss"] = decimal_float(safest_confirmed)
        position["confirmed_stop"] = decimal_float(safest_confirmed)
        self._record_pending_protection_update(
            position,
            requested_stop=new_stop,
            reason=reason,
        )
        return False

    def _protect_after_tp_fill(
        self,
        position: dict,
        target_stop: float,
        reason: str,
        note_parts: list[str],
    ) -> bool:
        symbol = str(position.get("symbol") or "")
        previous_stop = float(position.get("stop_loss") or 0.0)

        self.log.warning(
            "TP_PROTECTION_REQUEST | %s | reason=%s | previous_stop=%s | target_stop=%s | tp1=%s | tp2=%s | tp3=%s",
            symbol,
            reason,
            previous_stop,
            target_stop,
            position.get("tp1_hit"),
            position.get("tp2_hit"),
            position.get("tp3_hit"),
        )

        moved = self._move_exchange_stop_loss_with_retries(position, target_stop, reason)
        if not moved:
            position["stop_loss"] = previous_stop
            position["exchange_stop_loss"] = previous_stop
            position["last_sl_move_reason"] = f"{reason}_FAILED"
            position["old_stop_loss_removed"] = False
            position["protection_integrity"] = "FAILED"
            note_parts.append(f"CRITICAL: {reason} SL move failed; local SL unchanged")
            self.log.error(
                "TP_PROTECTION_FAILED | %s | reason=%s | attempted_stop=%s | local_stop_kept=%s",
                symbol,
                reason,
                target_stop,
                previous_stop,
            )
            return False
        confirmed_stop = float(position.get("confirmed_stop") or position.get("stop_loss") or 0.0)
        position["break_even_active"] = True
        position["tp1_locked_stop_active"] = True
        position["tp1_lock_price"] = confirmed_stop
        position["last_sl_move_reason"] = reason
        position["protection_integrity"] = "VERIFIED"
        note_parts.append(f"{reason}: exchange SL verified @ {confirmed_stop:.8f}")
        self.log.warning(
            "TP_PROTECTION_VERIFIED | %s | reason=%s | old_stop=%s | new_stop=%s | verification=%s",
            symbol,
            reason,
            previous_stop,
            confirmed_stop,
            position.get("last_sl_verification"),
        )
        return True

    def _close_unprotected_position(self, position: dict, reason: str) -> bool:
        symbol = str(position.get("symbol") or "")
        direction = str(position.get("direction") or "")
        size = self._position_size(position)

        closer = getattr(self.client, "close_futures_position", None)
        if not symbol or not direction or size <= 0 or not callable(closer):
            self.log.error(
                "Cannot close unprotected position symbol=%s direction=%s size=%s reason=%s",
                symbol,
                direction,
                size,
                reason,
            )
            return False

        try:
            closer(symbol=symbol, direction=direction, size=size, reason=reason)
            self.log.error("Closed unprotected position %s reason=%s", symbol, reason)
            return True
        except TypeError:
            try:
                closer(symbol=symbol, direction=direction, size=size)
                self.log.error("Closed unprotected position %s reason=%s", symbol, reason)
                return True
            except Exception as exc:
                self.log.error("Close unprotected position failed for %s reason=%s error=%s", symbol, reason, exc)
                return False
        except Exception as exc:
            self.log.error("Close unprotected position failed for %s reason=%s error=%s", symbol, reason, exc)
            return False

    def _move_exchange_stop_loss(self, position: dict, new_stop: float, reason: str) -> dict | None:
        """Move the real Bitget stop-loss if the REST client supports it.

        This method is intentionally defensive: until `BitgetRestClient.move_futures_stop_loss`
        exists, it logs a warning and returns False instead of crashing the bot.
        """
        symbol = str(position.get("symbol") or "")
        direction = str(position.get("direction") or "" )
        size = self._position_size(position)

        mover = getattr(self.client, "move_futures_stop_loss", None)
        if size <= 0:
            self.log.error(
                "Exchange SL move blocked for %s: confirmed remaining size unavailable",
                symbol,
            )
            return None
        if not callable(mover):
            self.log.warning("Exchange SL move pending for %s: BitgetRestClient.move_futures_stop_loss missing", symbol)
            return None

        try:
            payload = mover(
                symbol=symbol,
                direction=direction,
                trigger_price=float(new_stop),
                cleanup_existing=False,
                reason=reason,
            )
            self.log.warning(
                "EXCHANGE_SL_REPLACEMENT_SUBMITTED | %s | reason=%s | new_stop=%s | "
                "confirmed_size=%s | old_stop_still_active=True",
                symbol,
                reason,
                new_stop,
                size,
            )
            return payload
        except Exception as exc:
            self.log.error("Exchange SL move failed for %s reason=%s stop=%s error=%s", symbol, reason, new_stop, exc)
            return None

    def _fee_adjusted_break_even(self, direction: str, entry: float) -> float:
        """Legacy non-critical fallback retained for old telemetry/tests only."""
        buffer_pct = float(getattr(self.settings, "break_even_fee_buffer_pct", 0.10) or 0.10)
        buffer = buffer_pct / 100.0
        if direction.upper() == "LONG":
            return entry * (1.0 + buffer)
        return entry * (1.0 - buffer)

    @staticmethod
    def _target_hit(direction: str, current_price: float, target: float) -> bool:
        return current_price >= target if direction == "LONG" else current_price <= target

    @staticmethod
    def _target_hit_range(direction: str, candle_high: float, candle_low: float, target: float) -> bool:
        return candle_high >= target if direction == "LONG" else candle_low <= target

    @staticmethod
    def _stop_hit(direction: str, current_price: float, stop: float) -> bool:
        return current_price <= stop if direction == "LONG" else current_price >= stop

    @staticmethod
    def _stop_hit_range(direction: str, candle_high: float, candle_low: float, stop: float) -> bool:
        return candle_low <= stop if direction == "LONG" else candle_high >= stop

    @staticmethod
    def _is_no_position_to_close_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "22002" in message and "no position to close" in message
