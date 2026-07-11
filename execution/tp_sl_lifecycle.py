"""TP/SL protection lifecycle for PositionManager (extracted, behavior-neutral).

Mixin: SL moves with verify, protection repair, emergency close of unprotected
positions, hit/stop predicates and failed-continuation tightening. Methods are
moved verbatim from position_manager.py.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from clients.schemas import MarketSnapshot


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

    def _fallback_protection_from_execution_log(self, symbol: str) -> dict:
        path = Path("logs/executions.csv")
        if not path.exists():
            return {"stop_loss": 0.0, "take_profits": [], "source": "execution_log_missing"}

        latest: dict | None = None
        try:
            with path.open("r", newline="") as handle:
                reader = csv.reader(handle)
                for row in reader:
                    if len(row) < 9:
                        continue
                    row_symbol = str(row[0] or "").upper()
                    status = str(row[4] or "").upper()
                    if row_symbol != symbol.upper() or status != "EXECUTED":
                        continue

                    stop_loss = self._safe_float(row[7], 0.0)
                    take_profits = []
                    for raw_tp in str(row[8] or "").split("|"):
                        parsed = self._safe_float(raw_tp.strip(), 0.0)
                        if parsed > 0:
                            take_profits.append(parsed)

                    if stop_loss > 0 and take_profits:
                        latest = {
                            "stop_loss": stop_loss,
                            "take_profits": take_profits,
                            "source": "logs/executions.csv",
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

        fallback = self._fallback_protection_from_execution_log(symbol)
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

    def _failed_continuation_target_stop(self, direction: str, entry: float, current_price: float) -> float:
        fee_be = self._fee_adjusted_break_even(direction, entry)
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
        entry: float,
        current_price: float,
        pnl_pct: float,
    ) -> tuple[bool, float, dict[str, object]]:
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
        had_progress = bool(position.get("tp1_hit")) or pnl_pct > 0.10
        pressure_failed, pressure_context = self._directional_pressure_failed(direction, snapshot)

        if not near_tp and not had_progress:
            return False, 0.0, {"reason": "not_near_tp_or_in_profit", **pressure_context}
        if not pressure_failed:
            return False, 0.0, {"reason": "pressure_not_failed", **pressure_context}
        if pnl_pct < self.failed_continuation_min_unrealized_pct:
            return False, 0.0, {"reason": "already_too_negative", "pnl_pct": pnl_pct, **pressure_context}

        new_stop = self._failed_continuation_target_stop(direction, entry, current_price)
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
            "pnl_pct": pnl_pct,
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
        size = self._position_size(position)

        if self._has_local_protection_payload(position):
            self.log.info("Protection repair skipped for %s: local protection payload already present", symbol)
            return True

        if not symbol or not direction or stop_loss <= 0 or not take_profits or size <= 0:
            self.log.warning(
                "Protection repair skipped for %s: missing data stop=%s tps=%s size=%s",
                symbol,
                stop_loss,
                take_profits,
                size,
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
                trigger_price=stop_loss,
                take_profits=take_profits,
                margin_mode="isolated",
            )
            has_sl = bool(payload and payload.get("stop_loss"))
            tps = payload.get("take_profits") if payload else None
            has_tp = bool(tps and isinstance(tps, list) and len(tps) > 0)
            if has_sl and has_tp:
                position["protection_payload"] = payload
                position["exchange_stop_loss"] = stop_loss
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
        data = payload.get("data") or {}
        if isinstance(data, dict):
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

    def _move_exchange_stop_loss_with_retries(self, position: dict, new_stop: float, reason: str) -> bool:

        if not self._cancel_existing_exchange_stop_losses(position, reason):
            self.log.error(
                "EXCHANGE_SL_REPLACE_ABORTED | %s | reason=%s | old_sl_cancel_failed",
                position.get("symbol"),
                reason,
            )
            return False
        for attempt in range(1, self.be_move_retries + 1):
            if self._move_exchange_stop_loss(position, new_stop, reason):
                return True
            self.log.warning(
                "Exchange SL move attempt %s/%s failed for %s reason=%s",
                attempt,
                self.be_move_retries,
                position.get("symbol"),
                reason,
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

        verification = None
        try:
            verifier = getattr(self.client, "verify_active_stop_loss", None)
            if callable(verifier):
                verification = verifier(
                    symbol=symbol,
                    hold_side="long" if str(position.get("direction") or "").upper() == "LONG" else "short",
                    expected_trigger_price=float(target_stop),
                    )
        except Exception as exc:
            verification = {"verified": False, "reason": f"verify_exception:{exc}"}

        verified = bool(verification.get("verified")) if isinstance(verification, dict) else True
        if not verified:
            position["stop_loss"] = previous_stop
            position["exchange_stop_loss"] = previous_stop
            position["last_sl_move_reason"] = f"{reason}_VERIFY_FAILED"
            position["old_stop_loss_removed"] = False
            position["protection_integrity"] = "VERIFY_FAILED"
            position["last_sl_verification"] = verification
            note_parts.append(f"CRITICAL: {reason} SL move not verified; local SL unchanged")
            self.log.error(
                "TP_PROTECTION_VERIFY_FAILED | %s | reason=%s | attempted_stop=%s | local_stop_kept=%s | verification=%s",
                symbol,
                reason,
                target_stop,
                previous_stop,
                verification,
            )
            return False

        position["stop_loss"] = target_stop
        position["exchange_stop_loss"] = target_stop
        position["break_even_active"] = True
        position["tp1_locked_stop_active"] = True
        position["tp1_lock_price"] = target_stop
        position["last_sl_move_reason"] = reason
        position["old_stop_loss_removed"] = True
        position["protection_integrity"] = "VERIFIED"
        position["last_sl_verification"] = verification
        note_parts.append(f"{reason}: exchange SL verified @ {target_stop:.8f}")
        self.log.warning(
            "TP_PROTECTION_VERIFIED | %s | reason=%s | old_stop=%s | new_stop=%s | verification=%s",
            symbol,
            reason,
            previous_stop,
            target_stop,
            verification,
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

    def _move_exchange_stop_loss(self, position: dict, new_stop: float, reason: str) -> bool:
        """Move the real Bitget stop-loss if the REST client supports it.

        This method is intentionally defensive: until `BitgetRestClient.move_futures_stop_loss`
        exists, it logs a warning and returns False instead of crashing the bot.
        """
        symbol = str(position.get("symbol") or "")
        direction = str(position.get("direction") or "" )
        size = self._position_size(position)

        mover = getattr(self.client, "move_futures_stop_loss", None)
        if not callable(mover):
            self.log.warning("Exchange SL move pending for %s: BitgetRestClient.move_futures_stop_loss missing", symbol)
            return False

        try:
            payload = mover(
                symbol=symbol,
                direction=direction,
                trigger_price=float(new_stop),
                reason=reason,
            )
            self._store_new_stop_loss_order_id(position, payload)
            self.log.warning(
                "EXCHANGE_SL_REPLACED | %s | reason=%s | new_stop=%s | order_id=%s",
                symbol,
                reason,
                new_stop,
                position.get("exchange_stop_loss_order_id"),
            )
            return True
        except Exception as exc:
            self.log.error("Exchange SL move failed for %s reason=%s stop=%s error=%s", symbol, reason, new_stop, exc)
            return False

    def _fee_adjusted_break_even(self, direction: str, entry: float) -> float:
        def _sf(name: str, default: float) -> float:
            try:
                return float(getattr(self.settings, name, default))
            except (TypeError, ValueError):
                return default

        # The BE stop must clear the FULL roundtrip fees plus a small margin, or
        # a stop-out at "break-even" still books a net loss (the slow bleed on
        # trades that run up then return to BE). Tie it to the planner's own fee
        # estimate so it self-corrects, and never drop below the configured
        # buffer. e.g. max(0.10, 0.12 + 0.04) = 0.16%.
        configured = _sf("break_even_fee_buffer_pct", 0.10)
        roundtrip_fee_pct = _sf("planner_estimated_roundtrip_fee_bps", 12.0) / 100.0
        extra_margin_pct = _sf("break_even_extra_margin_pct", 0.04)
        buffer_pct = max(configured, roundtrip_fee_pct + extra_margin_pct)
        buffer = buffer_pct / 100.0
        if direction.upper() == "LONG":
            return entry * (1.0 + buffer)
        return entry * (1.0 - buffer)

    @staticmethod
    def _pnl_pct(direction: str, entry: float, current_price: float) -> float:
        if entry <= 0:
            return 0.0
        if direction == "LONG":
            return ((current_price - entry) / entry) * 100
        return ((entry - current_price) / entry) * 100

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
