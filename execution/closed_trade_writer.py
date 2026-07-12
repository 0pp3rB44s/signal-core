"""Closed-trade dataset/journal writes for PositionManager (extracted, behavior-neutral).

Mixin: methods are moved verbatim from position_manager.py and still run on the
PositionManager instance (self.log/self.settings/self.journal/self.cooldowns).
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from risk.cooldown_manager import SymbolCooldownManager
from telemetry.trade_logger import append_closed_trade_row


class ClosedTradeWriterMixin:
    def _ensure_close_dataset_context(self, position: dict) -> None:
        """Telemetry-only: ensure CLOSE dataset rows carry full autopsy context."""
        if not isinstance(position, dict):
            return

        self._hydrate_position_from_open_dataset_row(position)
        self._hydrate_close_position_size(position)

        avg_entry = self._safe_float(position.get("avg_entry"), 0.0)
        entry_price = self._safe_float(position.get("entry_price"), 0.0)
        if avg_entry <= 0 and entry_price > 0:
            position["avg_entry"] = entry_price
            avg_entry = entry_price
        if entry_price <= 0 and avg_entry > 0:
            position["entry_price"] = avg_entry

        raw_tps = position.get("take_profits") or []
        normalized_tps = []
        if isinstance(raw_tps, list):
            for tp in raw_tps:
                parsed = self._safe_float(tp, 0.0)
                if parsed > 0:
                    normalized_tps.append(parsed)
        elif isinstance(raw_tps, str):
            for raw_tp in raw_tps.replace(",", "|").split("|"):
                parsed = self._safe_float(raw_tp.strip(), 0.0)
                if parsed > 0:
                    normalized_tps.append(parsed)

        if normalized_tps:
            position["take_profits"] = normalized_tps
            position["tp1"] = normalized_tps[0]
            if len(normalized_tps) >= 2:
                position["tp2"] = normalized_tps[1]
            if len(normalized_tps) >= 3:
                position["tp3"] = normalized_tps[2]

        position["near_tp_seen"] = bool(position.get("near_tp_seen", False))
        position["near_tp_distance_pct"] = self._safe_float(position.get("near_tp_distance_pct"), 999.0)
        position["min_distance_to_tp_pct"] = self._safe_float(position.get("min_distance_to_tp_pct"), 999.0)
        position["max_favorable_excursion_pct"] = self._safe_float(position.get("max_favorable_excursion_pct"), 0.0)
        position["max_adverse_excursion_pct"] = self._safe_float(position.get("max_adverse_excursion_pct"), 0.0)

        near_tp_seen_at = str(position.get("near_tp_seen_at") or "")
        closed_at = str(position.get("closed_at") or "")
        if near_tp_seen_at and closed_at and not position.get("time_from_near_tp_to_exit_seconds"):
            try:
                near_tp_dt = datetime.fromisoformat(near_tp_seen_at.replace("Z", "+00:00"))
                closed_dt = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
                position["time_from_near_tp_to_exit_seconds"] = round(
                    max(0.0, (closed_dt - near_tp_dt).total_seconds()),
                    3,
                )
            except Exception:
                pass

    def _ensure_closed_trade_dataset_row(self, position: dict) -> None:
        status = str(position.get("status") or "")
        if status not in {"CLOSED", "CLOSED_SYNCED"}:
            return

        symbol = str(position.get("symbol") or "").upper()
        closed_at = str(position.get("closed_at") or "")
        if not symbol or not closed_at:
            return

        if self._closed_trade_dataset_row_exists(symbol=symbol, closed_at=closed_at):
            return

        data_confidence = str(position.get("data_confidence") or "").upper()
        process_verdict = str(position.get("process_verdict") or "").upper()
        close_source = str(position.get("close_source") or position.get("sync_source") or "").lower()
        is_exchange_truth = (
            data_confidence == "EXCHANGE_TRUTH"
            or process_verdict == "EXCHANGE_TRUTH_CLOSE"
            or close_source == "bitget_order_history"
        )

        if not is_exchange_truth:
            close_reason_raw = str(position.get("closed_reason") or status.lower()).lower()
            direction = str(position.get("direction") or "").upper()
            entry_price = float(position.get("avg_entry") or position.get("entry_price") or 0.0)
            exit_price_for_gate = float(
                position.get("exchange_truth_exit_price")
                or position.get("last_price")
                or position.get("exit_price")
                or entry_price
                or 0.0
            )
            take_profits_for_gate = [
                float(tp)
                for tp in (position.get("take_profits") or [])
                if float(tp or 0) > 0
            ]
            stop_loss_for_gate = float(position.get("stop_loss") or 0.0)

            def near_gate(a: float, b: float, tolerance_pct: float = 0.0035) -> bool:
                if a <= 0 or b <= 0:
                    return False
                return abs(a - b) / b <= tolerance_pct

            tp_truth = False
            if close_reason_raw.startswith("tp") or close_reason_raw in {"tp_synced", "tp1_synced", "tp2_synced", "tp3_synced"}:
                tp_truth = True
            if take_profits_for_gate:
                if direction == "LONG" and exit_price_for_gate >= min(take_profits_for_gate):
                    tp_truth = True
                if direction == "SHORT" and exit_price_for_gate <= max(take_profits_for_gate):
                    tp_truth = True
                if any(near_gate(exit_price_for_gate, target) for target in take_profits_for_gate):
                    tp_truth = True

            sl_truth = False
            if close_reason_raw in {"stop_loss", "break_even_stop"}:
                sl_truth = True
            if stop_loss_for_gate > 0 and near_gate(exit_price_for_gate, stop_loss_for_gate):
                sl_truth = True

            allow_strategy_truth_close = bool(
                status in {"CLOSED", "CLOSED_SYNCED"}
                and entry_price > 0
                and exit_price_for_gate > 0
                and (tp_truth or sl_truth)
            )

            if not allow_strategy_truth_close:
                self.log.warning(
                    "LOW_CONFIDENCE_BACKFILL_SKIPPED_MAIN_DATASET | %s | status=%s | closed_at=%s | reason=%s | data_confidence=%s | process_verdict=%s | close_source=%s",
                    symbol,
                    status,
                    closed_at,
                    position.get("closed_reason") or status.lower(),
                    data_confidence or "UNKNOWN",
                    process_verdict or "UNKNOWN",
                    close_source or "UNKNOWN",
                )
                return

            position["data_confidence"] = "STRATEGY_TRUTH_VALIDATED"
            position["process_verdict"] = "VALIDATED_POSITION_CLOSE"
            position["close_source"] = close_source or "validated_exchange_position_closed_sync"
            self.log.warning(
                "STRATEGY_TRUTH_BACKFILL_ALLOWED_MAIN_DATASET | %s | status=%s | closed_at=%s | reason=%s | exit=%s | entry=%s | tp_truth=%s | sl_truth=%s | close_source=%s",
                symbol,
                status,
                closed_at,
                position.get("closed_reason") or status.lower(),
                exit_price_for_gate,
                entry_price,
                tp_truth,
                sl_truth,
                position.get("close_source"),
            )

        exchange_close_truth = {}
        if position.get("exchange_truth_pnl") in (None, ""):
            exchange_close_truth = self._exchange_close_truth_from_position_history(position)
            if str(exchange_close_truth.get("close_source") or "") == "bitget_position_history" and exchange_close_truth.get("pnl") not in (None, ""):
                position["close_source"] = "bitget_position_history"
                position["data_confidence"] = "EXCHANGE_TRUTH"
                position["process_verdict"] = "EXCHANGE_TRUTH_CLOSE"
                position["exchange_truth_order_id"] = exchange_close_truth.get("order_id", "")
                position["exchange_truth_exit_price"] = exchange_close_truth.get("exit_price", position.get("exchange_truth_exit_price", ""))
                position["exchange_truth_size"] = exchange_close_truth.get("size", position.get("exchange_truth_size", ""))
                position["exchange_truth_pnl"] = exchange_close_truth.get("pnl", "")
                position["exchange_truth_fee"] = exchange_close_truth.get("fee", "")
                position["realized_pnl"] = exchange_close_truth.get("pnl", position.get("realized_pnl", ""))
                # netProfit is al netto: overschrijf ook net_pnl, anders blijft
                # de open-time placeholder (-entry fee) in het record staan.
                position["net_pnl"] = exchange_close_truth.get("pnl", position.get("net_pnl", ""))
                position["fees_paid"] = exchange_close_truth.get("fee", position.get("fees_paid", ""))

        data_confidence = str(position.get("data_confidence") or data_confidence or "")
        process_verdict = str(position.get("process_verdict") or process_verdict or "")
        close_source = str(position.get("close_source") or close_source or "")

        if position.get("exchange_truth_pnl") in (None, ""):
            exchange_close_truth = self._exchange_close_truth_from_position_history(position)
            if str(exchange_close_truth.get("close_source") or "") == "bitget_position_history" and exchange_close_truth.get("pnl") not in (None, ""):
                position["close_source"] = "bitget_position_history"
                position["data_confidence"] = "EXCHANGE_TRUTH"
                position["process_verdict"] = "EXCHANGE_TRUTH_CLOSE"
                position["exchange_truth_order_id"] = exchange_close_truth.get("order_id", "")
                position["exchange_truth_exit_price"] = exchange_close_truth.get("exit_price", position.get("exchange_truth_exit_price", ""))
                position["exchange_truth_size"] = exchange_close_truth.get("size", position.get("exchange_truth_size", ""))
                position["exchange_truth_pnl"] = exchange_close_truth.get("pnl", "")
                position["exchange_truth_fee"] = exchange_close_truth.get("fee", "")
                position["realized_pnl"] = exchange_close_truth.get("pnl", position.get("realized_pnl", ""))
                # netProfit is al netto: overschrijf ook net_pnl, anders blijft
                # de open-time placeholder (-entry fee) in het record staan.
                position["net_pnl"] = exchange_close_truth.get("pnl", position.get("net_pnl", ""))
                position["fees_paid"] = exchange_close_truth.get("fee", position.get("fees_paid", ""))

        data_confidence = str(position.get("data_confidence") or data_confidence or "")
        process_verdict = str(position.get("process_verdict") or process_verdict or "")
        close_source = str(position.get("close_source") or close_source or "")

        self.log.warning(
            "EXCHANGE_TRUTH_BACKFILL_ALLOWED_MAIN_DATASET | %s | status=%s | closed_at=%s | reason=%s | data_confidence=%s | process_verdict=%s | close_source=%s",
            symbol,
            status,
            closed_at,
            position.get("closed_reason") or status.lower(),
            data_confidence,
            process_verdict,
            close_source,
        )

        # Write EXCHANGE_TRUTH closed state to trade_dataset_v2.csv
        self._ensure_close_dataset_context(position)

        direction = str(position.get("direction") or "").upper()
        entry_price = float(position.get("avg_entry") or position.get("entry_price") or 0.0)
        exit_price = float(
            position.get("exchange_truth_exit_price")
            or position.get("last_price")
            or position.get("exit_price")
            or entry_price
            or 0.0
        )
        pnl_pct = float(
            position.get("realized_pnl_pct")
            or position.get("pnl_pct")
            or self._pnl_pct(direction, entry_price, exit_price)
        )
        close_reason = str(position.get("closed_reason") or status.lower())

        self._append_closed_trade_dataset_row(
            position=position,
            close_reason=close_reason,
            exit_price=exit_price,
            pnl_pct=pnl_pct,
            extra={
                "close_source": position.get("close_source") or position.get("sync_source") or "bitget_order_history",
                "data_confidence": "EXCHANGE_TRUTH",
                "process_verdict": "EXCHANGE_TRUTH_CLOSE",
                "exchange_truth_order_id": position.get("exchange_truth_order_id", ""),
                "exchange_truth_exit_price": position.get("exchange_truth_exit_price", exit_price),
                "exchange_truth_size": position.get("exchange_truth_size", position.get("size", "")),
                "exchange_truth_pnl": position.get("exchange_truth_pnl", position.get("realized_pnl", "")),
                "exchange_truth_fee": position.get("exchange_truth_fee", position.get("fees_paid", "")),
            },
        )

        self.log.warning(
            "EXCHANGE_TRUTH_BACKFILL_APPENDED_MAIN_DATASET | %s | exit=%s | pnl_pct=%.4f | closed_at=%s",
            symbol,
            exit_price,
            pnl_pct,
            closed_at,
        )

    def _closed_trade_dataset_row_exists(self, symbol: str, closed_at: str) -> bool:
        path = Path("logs/trade_dataset_v2.csv")
        if not path.exists():
            return False

        target_symbol = str(symbol or "").upper()
        target_closed_at = str(closed_at or "")
        target_prefix = target_closed_at[:19]

        if not target_symbol or not target_closed_at:
            return False

        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line or line.startswith("event_type,"):
                        continue
                    if "POSITION_CLOSED" not in line and "CLOSE" not in line and "CLOSED" not in line:
                        continue
                    if target_symbol not in line:
                        continue
                    if target_closed_at in line:
                        return True
                    if target_prefix and target_prefix in line:
                        return True
        except Exception as exc:
            self.log.warning(
                "CLOSED_TRADE_DATASET_EXISTS_CHECK_FAILED | %s | closed_at=%s | error=%s",
                symbol,
                closed_at,
                exc,
            )
            return False

        return False

    def _hydrate_close_position_size(self, position: dict) -> None:
        size = self._position_size(position)
        if size > 0:
            position["size"] = size
            position["order_size"] = size
            position["position_size"] = size
            return

        for key in (
            "entry_size",
            "filled_size",
            "filled_qty",
            "filled_quantity",
            "base_size",
            "qty",
            "quantity",
            "exchange_live_size",
            "last_live_size",
        ):
            try:
                value = float(position.get(key) or 0.0)
            except (TypeError, ValueError):
                value = 0.0
            if value > 0:
                position["size"] = value
                position["order_size"] = value
                position["position_size"] = value
                return

        notional = float(position.get("position_notional_usdt") or position.get("notional") or 0.0)
        entry = float(position.get("avg_entry") or position.get("entry_price") or position.get("last_price") or 0.0)
        if notional > 0 and entry > 0:
            value = round(notional / entry, 8)
            position["size"] = value
            position["order_size"] = value
            position["position_size"] = value

    def _register_symbol_cooldown(self, symbol: str, reason: str, pnl_pct: float) -> None:
        minutes = int(getattr(self.settings, "symbol_cooldown_minutes", 30) or 30)
        normalized_reason = SymbolCooldownManager.normalize_reason(reason)
        if pnl_pct < 0:
            minutes = max(minutes, int(minutes * 1.5))
            normalized_reason = f"loss_{normalized_reason}"

        status = self.cooldowns.set_cooldown(
            symbol,
            minutes=minutes,
            reason=normalized_reason,
        )
        self.log.warning(
            "SYMBOL_COOLDOWN_SET | %s | reason=%s | minutes=%s | until=%s | pnl_pct=%.4f",
            status.symbol,
            status.reason,
            minutes,
            status.until,
            pnl_pct,
        )

        try:
            self.cooldowns.set_cooldown(
                f"recent_close::{symbol}",
                minutes=15,
                reason=f"recent_close_{normalized_reason}",
            )
            self.log.info(
                "RECENT_CLOSE_COOLDOWN_SET | %s | minutes=%s | reason=%s",
                symbol,
                15,
                normalized_reason,
            )
        except Exception as exc:
            self.log.warning(
                "RECENT_CLOSE_COOLDOWN_FAILED | %s | error=%s",
                symbol,
                exc,
            )

    def _sync_journal_close(self, symbol: str, reason: str, pnl_pct: float) -> None:
        try:
            self.journal.log_close(symbol=symbol, result=reason, pnl=round(pnl_pct, 4))
        except Exception as exc:
            self.log.warning("Live journal log_close failed for %s: %s", symbol, exc)

    def _strategy_label_for_close(self, position: dict, close_reason: str, extra: dict | None = None) -> str:
        raw_strategy = str(position.get("strategy") or "").strip()
        if raw_strategy and raw_strategy.lower() not in {"unknown", "none", "null", "na", "n/a"}:
            return raw_strategy

        extra = extra or {}
        source = str(extra.get("close_source") or extra.get("source") or close_reason or "").lower()

        if "exchange_position_closed" in source or "sync" in source or "reconcile" in source:
            return "reconciliation_close"
        if "residual" in source:
            return "residual_cleanup_close"
        if "protection" in source or "unprotected" in source:
            return "protection_repair_close"
        if "tp3" in source:
            return "tp3_close"
        if "stop" in source or "break_even" in source:
            return "stop_close"
        if "manual" in source:
            return "manual_close"

        return "recovered_unlinked_close"

    @staticmethod
    def _snapshot_link_key(position: dict, closed_at: str | None = None) -> str:
        symbol = str(position.get("symbol") or "").upper()
        opened_at = str(position.get("opened_at") or position.get("created_at") or closed_at or "")
        if not symbol or not opened_at:
            return ""
        return f"{symbol}|{opened_at[:19]}"

    def _append_closed_trade_dataset_row(
        self,
        position: dict,
        close_reason: str,
        exit_price: float,
        pnl_pct: float,
        extra: dict | None = None,
    ) -> None:
        symbol = str(position.get("symbol") or "").upper()
        direction = str(position.get("direction") or "").upper()
        entry_price = float(position.get("avg_entry") or 0.0)
        size = self._position_size(position)
        pnl = 0.0

        if entry_price > 0 and exit_price > 0 and size > 0:
            if direction == "LONG":
                pnl = (exit_price - entry_price) * size
            else:
                pnl = (entry_price - exit_price) * size

        payload_extra = {
            "source": "position_manager_guaranteed_close",
            "remaining_size_pct": position.get("remaining_size_pct", ""),
            "tp1_hit": position.get("tp1_hit", False),
            "tp2_hit": position.get("tp2_hit", False),
            "tp3_hit": position.get("tp3_hit", False),
            "break_even_active": position.get("break_even_active", False),
            "tp1_locked_stop_active": position.get("tp1_locked_stop_active", False),
            "last_sl_move_reason": position.get("last_sl_move_reason", ""),
            "protection_integrity": position.get("protection_integrity", ""),
            "max_favorable_excursion_pct": position.get("max_favorable_excursion_pct", ""),
            "max_adverse_excursion_pct": position.get("max_adverse_excursion_pct", ""),
            "near_tp_seen": position.get("near_tp_seen", False),
            "near_tp_seen_at": position.get("near_tp_seen_at", ""),
            "near_tp_distance_pct": position.get("near_tp_distance_pct", ""),
            "min_distance_to_tp_pct": position.get("min_distance_to_tp_pct", ""),
            "current_distance_to_tp_pct": position.get("current_distance_to_tp_pct", ""),
            "near_tp_first_price": position.get("near_tp_first_price", ""),
            "near_tp_first_target": position.get("near_tp_first_target", ""),
            "near_tp_latest_price": position.get("near_tp_latest_price", ""),
            "near_tp_latest_target": position.get("near_tp_latest_target", ""),
            "mfe_at_near_tp_seen_pct": position.get("mfe_at_near_tp_seen_pct", ""),
            "profit_giveback_pct": round(max(0.0, float(position.get("max_favorable_excursion_pct") or 0.0) - float(pnl_pct or 0.0)), 4),
            "reversed_after_near_tp": bool(position.get("near_tp_seen", False)) and float(pnl_pct or 0.0) < float(position.get("max_favorable_excursion_pct") or 0.0),
            "close_source": position.get("close_source", ""),
            "data_confidence": position.get("data_confidence", ""),
            "process_verdict": position.get("process_verdict", ""),
            "exchange_truth_order_id": position.get("exchange_truth_order_id", ""),
            "exchange_truth_exit_price": position.get("exchange_truth_exit_price", ""),
            "exchange_truth_size": position.get("exchange_truth_size", ""),
            "exchange_truth_pnl": position.get("exchange_truth_pnl", ""),
            "exchange_truth_fee": position.get("exchange_truth_fee", ""),
        }

        if position.get("exchange_truth_pnl") not in (None, ""):
            try:
                pnl = float(position.get("exchange_truth_pnl") or pnl)
            except (TypeError, ValueError):
                pass

        if position.get("exchange_truth_pnl") not in (None, ""):
            try:
                pnl = float(position.get("exchange_truth_pnl") or pnl)
            except (TypeError, ValueError):
                pass

        if extra:
            payload_extra.update(extra)

        payload_extra.update({
            "snapshot_link_key": self._snapshot_link_key(position, closed_at=str(position.get("closed_at") or "")),
            "opened_at": str(position.get("opened_at") or ""),
            "closed_at": str(position.get("closed_at") or ""),
            "position_size": self._position_size(position),
        })

        strategy_label = self._strategy_label_for_close(
            position=position,
            close_reason=close_reason,
            extra=payload_extra,
        )

        if str(position.get("strategy") or "").strip().lower() in {"", "unknown", "none", "null", "na", "n/a"}:
            position["strategy"] = strategy_label
            self.log.warning(
                "UNKNOWN_STRATEGY_CLOSE_NORMALIZED | %s | close_reason=%s | strategy_label=%s | source=%s",
                symbol,
                close_reason,
                strategy_label,
                payload_extra.get("close_source") or payload_extra.get("source") or "",
            )

        try:
            append_closed_trade_row(
                symbol=symbol,
                strategy=strategy_label,
                direction=direction,
                entry_price=entry_price,
                exit_price=float(exit_price or 0.0),
                size=size,
                pnl=round(pnl, 8),
                pnl_pct=round(float(pnl_pct or 0.0), 6),
                close_reason=close_reason,
                opened_at=str(position.get("opened_at") or ""),
                closed_at=str(position.get("closed_at") or datetime.now(timezone.utc).isoformat()),
                extra=payload_extra,
            )
            self.log.warning(
                "TRADE_DATASET_CLOSED_ROW_APPENDED | %s | reason=%s | exit=%s | pnl_pct=%.4f | size=%s",
                symbol,
                close_reason,
                exit_price,
                pnl_pct,
                size,
            )
        except Exception as exc:
            self.log.error(
                "TRADE_DATASET_CLOSED_ROW_APPEND_FAILED | %s | reason=%s | error=%s",
                symbol,
                close_reason,
                exc,
            )

    def _realized_roi_pct_from_exchange_truth(
        self,
        position: dict,
        exchange_close_truth: dict,
        entry_price: float,
        exit_price: float,
        direction: str,
    ) -> float:
        realized_pnl = exchange_close_truth.get("pnl")
        size = float(exchange_close_truth.get("size") or self._position_size(position) or 0.0)
        leverage = float(position.get("leverage") or self.settings.default_leverage or 1.0)

        try:
            realized_pnl_float = float(realized_pnl)
        except (TypeError, ValueError):
            realized_pnl_float = 0.0

        notional = entry_price * size if entry_price > 0 and size > 0 else 0.0
        margin = notional / leverage if notional > 0 and leverage > 0 else 0.0
        if margin > 0 and realized_pnl not in (None, ""):
            return round((realized_pnl_float / margin) * 100.0, 6)

        return self._pnl_pct(direction, entry_price, exit_price)
