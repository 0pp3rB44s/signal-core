from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings
from clients.bitget_rest import BitgetRestClient
from clients.schemas import MarketSnapshot, PositionUpdate
from execution.state_store import JsonStateStore
from risk.cooldown_manager import SymbolCooldownManager
from telemetry.trade_logger import LiveTradeJournalLogger, append_closed_trade_row


class PositionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = logging.getLogger(self.__class__.__name__)
        self.store = JsonStateStore("state/executed_trades.json")
        self.event_store = JsonStateStore("state/position_events.json")
        self.cooldown_store = JsonStateStore("state/symbol_cooldowns.json")
        self.cooldowns = SymbolCooldownManager(self.cooldown_store)
        self.journal = LiveTradeJournalLogger()
        self.client = BitgetRestClient(settings=settings)
        self.protection_repair_retries = 3
        self.be_move_retries = 3
        self.residual_position_pct_threshold = 5.0
        self.residual_position_abs_threshold = 0.000001
        self.tp_miss_near_pct = 0.18
        self.near_tp_protection_trigger_pct = 85.0
        self.near_tp_be_fee_buffer_pct = 0.08
        self.failed_continuation_min_age_minutes = 10.0
        self.failed_continuation_sl_buffer_pct = 0.06
        self.failed_continuation_min_unrealized_pct = -0.35

    def sync(self, snapshots: list[MarketSnapshot]) -> list[PositionUpdate]:
        positions = self.store.load(default=[])

        price_map = {snapshot.symbol: snapshot.primary.latest_close for snapshot in snapshots}
        snapshot_map = {snapshot.symbol: snapshot for snapshot in snapshots}
        candle_range_map = {}
        for snapshot in snapshots:
            candles = getattr(snapshot.primary, "candles", []) or []
            if candles:
                last_candle = candles[-1]
                candle_range_map[snapshot.symbol] = {
                    "high": float(getattr(last_candle, "high", snapshot.primary.latest_close)),
                    "low": float(getattr(last_candle, "low", snapshot.primary.latest_close)),
                }
            else:
                candle_range_map[snapshot.symbol] = {
                    "high": float(snapshot.primary.latest_close),
                    "low": float(snapshot.primary.latest_close),
                }
        updates: list[PositionUpdate] = []
        events = self.event_store.load(default=[])
        bitget_open_symbols: set[str] = set()
        positions_live: list[dict] = []
        bitget_sync_ok = False
        try:
            payload = self.client.get_all_positions()
            positions_live = payload.get("data") or []
            bitget_open_symbols = {
                str(p.get("symbol") or "")
                for p in positions_live
                if float(p.get("total") or p.get("size") or 0) > 0
            }
            bitget_sync_ok = True
        except Exception as exc:
            self.log.warning("Bitget position sync failed in PositionManager: %s", exc)

        local_open_symbols = {
            str(position.get("symbol") or "")
            for position in positions
            if position.get("status") == "OPEN"
        }
        missing_local_symbols = sorted(symbol for symbol in bitget_open_symbols if symbol and symbol not in local_open_symbols)
        if missing_local_symbols:
            self.log.error(
                "STATE_MISMATCH | Bitget has open positions not tracked locally: %s",
                ",".join(missing_local_symbols),
            )
            now = datetime.now(timezone.utc).isoformat()
            for symbol in missing_local_symbols:
                events.append(
                    {
                        "timestamp": now,
                        "symbol": symbol,
                        "status": "STATE_MISMATCH",
                        "current_price": float(price_map.get(symbol, 0.0)),
                        "unrealized_pnl_pct": 0,
                        "stop_loss": 0,
                        "break_even_active": False,
                        "tp1_locked_stop_active": False,
                        "tp1_hit": False,
                        "tp2_hit": False,
                        "tp3_hit": False,
                        "note": "Bitget open position exists but executed_trades.json has no matching OPEN record",
                    }
                )

            recovered = self._recover_missing_local_positions(missing_local_symbols, positions_live, price_map)
            if recovered:
                positions.extend(recovered)
                for recovered_position in recovered:
                    events.append(
                        {
                            "timestamp": now,
                            "symbol": recovered_position["symbol"],
                            "status": "STATE_RECOVERED",
                            "current_price": float(recovered_position.get("last_price", 0.0)),
                            "unrealized_pnl_pct": 0,
                            "stop_loss": float(recovered_position.get("stop_loss", 0.0)),
                            "break_even_active": bool(recovered_position.get("break_even_active", False)),
                            "tp1_locked_stop_active": bool(recovered_position.get("tp1_locked_stop_active", False)),
                            "tp1_hit": bool(recovered_position.get("tp1_hit", False)),
                            "tp2_hit": bool(recovered_position.get("tp2_hit", False)),
                            "tp3_hit": bool(recovered_position.get("tp3_hit", False)),
                            "note": "recovered missing local state from Bitget open position",
                        }
                    )
                self.store.save(positions)

            self.event_store.save(events[-500:])

        if not positions:
            return updates

        for position in positions:
            if position.get("status") != "OPEN":
                if not bool(position.get("dataset_close_written", False)):
                    self._ensure_closed_trade_dataset_row(position)
                    position["dataset_close_written"] = True
                continue
            symbol = position["symbol"]
            if bitget_sync_ok and symbol in bitget_open_symbols:
                self._heal_missing_protection_from_fallback(position)
            if not bitget_sync_ok:
                current_price = float(price_map.get(symbol, position.get("last_price", position["avg_entry"])))
                position["last_price"] = current_price
                note = "exchange sync failed; preserving OPEN state"
                updates.append(
                    PositionUpdate(
                        symbol=symbol,
                        status=position["status"],
                        current_price=current_price,
                        unrealized_pnl_pct=round(
                            self._pnl_pct(str(position.get("direction") or ""), float(position.get("avg_entry") or 0), current_price),
                            3,
                        ),
                        stop_loss=float(position.get("stop_loss", 0)),
                        break_even_active=bool(position.get("break_even_active", False)),
                        tp1_hit=bool(position.get("tp1_hit", False)),
                        tp2_hit=bool(position.get("tp2_hit", False)),
                        tp3_hit=bool(position.get("tp3_hit", False)),
                        note=note,
                    )
                )
                events.append(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "symbol": symbol,
                        "status": position["status"],
                        "current_price": current_price,
                        "unrealized_pnl_pct": round(
                            self._pnl_pct(str(position.get("direction") or ""), float(position.get("avg_entry") or 0), current_price),
                            3,
                        ),
                        "stop_loss": float(position.get("stop_loss", 0)),
                        "break_even_active": bool(position.get("break_even_active", False)),
                        "tp1_locked_stop_active": bool(position.get("tp1_locked_stop_active", False)),
                        "tp1_hit": bool(position.get("tp1_hit", False)),
                        "tp2_hit": bool(position.get("tp2_hit", False)),
                        "tp3_hit": bool(position.get("tp3_hit", False)),
                        "note": note,
                    }
                )
                self.log.warning("POSITION_SYNC_UNCERTAIN | %s | preserving OPEN state because Bitget sync failed", symbol)
                continue
            if symbol not in bitget_open_symbols:
                entry_price = float(position.get("avg_entry") or 0)
                direction = str(position.get("direction") or "")
                exchange_close_truth = self._exchange_close_truth_from_position_history(position)
                position_history_source = str(exchange_close_truth.get("close_source") or "") == "bitget_position_history"
                position_history_has_pnl = exchange_close_truth.get("pnl") not in (None, "")
                if not position_history_source or not position_history_has_pnl:
                    fallback_close_truth = self._exchange_close_truth_from_order_history(position)
                    if fallback_close_truth.get("exit_price") or fallback_close_truth.get("size") or fallback_close_truth.get("pnl"):
                        if position_history_has_pnl:
                            fallback_close_truth["pnl"] = exchange_close_truth.get("pnl")
                            fallback_close_truth["fee"] = exchange_close_truth.get("fee")
                            fallback_close_truth["gross_pnl"] = exchange_close_truth.get("gross_pnl")
                            fallback_close_truth["open_fee"] = exchange_close_truth.get("open_fee")
                            fallback_close_truth["close_fee"] = exchange_close_truth.get("close_fee")
                            fallback_close_truth["close_source"] = "bitget_position_history_with_order_fallback"
                        exchange_close_truth = fallback_close_truth

                closed_price = float(
                    exchange_close_truth.get("exit_price")
                    or position.get("last_price")
                    or position["avg_entry"]
                )

                if exchange_close_truth.get("size", 0.0) > 0:
                    position["size"] = float(exchange_close_truth.get("size") or 0.0)
                    position["order_size"] = float(exchange_close_truth.get("size") or 0.0)
                    position["position_size"] = float(exchange_close_truth.get("size") or 0.0)

                if exchange_close_truth.get("pnl") is not None:
                    position["realized_pnl"] = float(exchange_close_truth.get("pnl") or 0.0)
                if exchange_close_truth.get("fee") is not None:
                    position["fees_paid"] = float(exchange_close_truth.get("fee") or 0.0)
                if exchange_close_truth.get("order_id"):
                    position["close_order_id"] = exchange_close_truth.get("order_id")

                pnl_pct = self._realized_roi_pct_from_exchange_truth(
                    position=position,
                    exchange_close_truth=exchange_close_truth,
                    entry_price=entry_price,
                    exit_price=closed_price,
                    direction=direction,
                )

                inferred_close_reason = self._infer_exchange_closed_reason(
                    position=position,
                    exit_price=closed_price,
                    direction=direction,
                )
                self._hydrate_close_position_size(position)

                position["status"] = "CLOSED_SYNCED"
                position["closed_reason"] = inferred_close_reason
                position["closed_at"] = datetime.now(timezone.utc).isoformat()
                position["realized_pnl_pct"] = round(pnl_pct, 4)
                position["sync_reason"] = "Local OPEN was not present in Bitget open positions; marked CLOSED_SYNCED"
                self.log.warning(
                    "LOCAL_OPEN_NOT_ON_EXCHANGE_SYNCED | %s | bitget_open_symbols=%s | exit=%s | pnl_pct=%.4f | inferred_reason=%s | size=%s",
                    symbol,
                    sorted(bitget_open_symbols),
                    closed_price,
                    pnl_pct,
                    inferred_close_reason,
                    self._position_size(position),
                )
                try:
                    cleanup_result = self.client.cancel_all_futures_tpsl_orders(
                        symbol=symbol,
                        hold_side="long" if direction == "LONG" else "short",
                    )
                    position["exchange_closed_tpsl_cleanup"] = cleanup_result
                    position["stale_tpsl_cleanup_done"] = True
                    self.log.warning(
                        "EXCHANGE_CLOSED_TPSL_CLEANUP | %s | reason=%s | result=%s",
                        symbol,
                        position.get("closed_reason"),
                        cleanup_result,
                    )
                except Exception as exc:
                    position["stale_tpsl_cleanup_done"] = False
                    self.log.error(
                        "EXCHANGE_CLOSED_TPSL_CLEANUP_FAILED | %s | error=%s",
                        symbol,
                        exc,
                    )
                self._sync_journal_close(symbol, position["closed_reason"], pnl_pct)
                position["close_source"] = exchange_close_truth.get("close_source") or "exchange_position_closed_sync"
                exchange_truth_pnl_available = exchange_close_truth.get("pnl") not in (None, "")
                close_source = str(exchange_close_truth.get("close_source") or "")
                exchange_source = close_source in {"bitget_order_history", "bitget_position_history"}
                if exchange_source and exchange_truth_pnl_available:
                    position["data_confidence"] = "EXCHANGE_TRUTH"
                    position["process_verdict"] = "EXCHANGE_TRUTH_CLOSE"
                elif exchange_source:
                    position["data_confidence"] = "LOW_CONFIDENCE"
                    position["process_verdict"] = "EXCHANGE_TRUTH_MISSING_PNL"
                else:
                    position["data_confidence"] = "STRATEGY_TRUTH"
                    position["process_verdict"] = "POSITION_CLOSED_SYNCED"
                position["exchange_truth_order_id"] = exchange_close_truth.get("order_id", "")
                position["exchange_truth_exit_price"] = exchange_close_truth.get("exit_price", closed_price)
                position["exchange_truth_size"] = exchange_close_truth.get("size", "")
                position["exchange_truth_pnl"] = exchange_close_truth.get("pnl", "")
                position["exchange_truth_fee"] = exchange_close_truth.get("fee", "")
                self._append_closed_trade_dataset_row(
                    position=position,
                    close_reason=position["closed_reason"],
                    exit_price=closed_price,
                    pnl_pct=pnl_pct,
                    extra={
                        "close_source": position.get("close_source"),
                        "data_confidence": position.get("data_confidence"),
                        "process_verdict": position.get("process_verdict"),
                        "exchange_truth_order_id": position.get("exchange_truth_order_id", ""),
                        "exchange_truth_exit_price": position.get("exchange_truth_exit_price", closed_price),
                        "exchange_truth_size": position.get("exchange_truth_size", ""),
                        "exchange_truth_pnl": position.get("exchange_truth_pnl", ""),
                        "exchange_truth_fee": position.get("exchange_truth_fee", ""),
                    },
                )
                self._register_symbol_cooldown(symbol, position["closed_reason"], pnl_pct)

                self.log.warning(
                    "POSITION_CLOSED_CLEAN | %s | reason=%s | exit=%s | pnl_pct=%.4f | stale_tpsl_cleanup_done=%s | close_source=%s | data_confidence=%s | process_verdict=%s",
                    symbol,
                    position.get("closed_reason"),
                    closed_price,
                    pnl_pct,
                    position.get("stale_tpsl_cleanup_done"),
                    position.get("close_source"),
                    position.get("data_confidence"),
                    position.get("process_verdict"),
                )

                events.append(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "symbol": symbol,
                        "status": "CLOSED_SYNCED",
                        "current_price": position.get("last_price", position["avg_entry"]),
                        "unrealized_pnl_pct": 0,
                        "stop_loss": float(position.get("stop_loss", 0)),
                        "break_even_active": bool(position.get("break_even_active", False)),
                        "tp1_locked_stop_active": bool(position.get("tp1_locked_stop_active", False)),
                        "tp1_hit": bool(position.get("tp1_hit", False)),
                        "tp2_hit": bool(position.get("tp2_hit", False)),
                        "tp3_hit": bool(position.get("tp3_hit", False)),
                        "note": "local OPEN not present on Bitget; marked CLOSED_SYNCED",
                    }
                )
                continue
            current_price = float(price_map.get(symbol, position.get("last_price", position["avg_entry"])))
            candle_range = candle_range_map.get(symbol, {"high": current_price, "low": current_price})
            current_high = float(candle_range.get("high", current_price))
            current_low = float(candle_range.get("low", current_price))
            live_position = self._find_live_position(symbol, positions_live)
            live_size = self._live_position_size(live_position or {}) if live_position else 0.0
            original_size = float(position.get("size") or position.get("order_size") or live_size or 0.0)
            current_remaining_pct = (
                (live_size / original_size) * 100.0
                if original_size > 0 and live_size > 0
                else float(position.get("remaining_size_pct", 100.0))
            )
            position["exchange_live_size"] = live_size
            position["exchange_remaining_pct"] = round(current_remaining_pct, 6)
            position["last_price"] = current_price
            entry = float(position["avg_entry"])
            stop = float(position["stop_loss"])
            direction = position["direction"]

            pnl_pct = self._pnl_pct(direction, entry, current_price)

            # MFE/MAE tracking for post-trade autopsy.
            # max_favorable_excursion_pct = best unrealized profit seen while open.
            # max_adverse_excursion_pct = worst unrealized drawdown seen while open.
            previous_mfe = float(position.get("max_favorable_excursion_pct") or 0.0)
            previous_mae = float(position.get("max_adverse_excursion_pct") or 0.0)
            position["max_favorable_excursion_pct"] = round(max(previous_mfe, pnl_pct), 4)
            position["max_adverse_excursion_pct"] = round(min(previous_mae, pnl_pct), 4)

            # --- Telemetry: trade duration/timing fields ---
            now_iso = datetime.now(timezone.utc).isoformat()
            opened_at = str(position.get("opened_at") or position.get("created_at") or "")

            if opened_at:
                try:
                    opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                    now_dt = datetime.now(timezone.utc)
                    age_seconds = max(0.0, (now_dt - opened_dt).total_seconds())
                    position["trade_duration_seconds"] = round(age_seconds, 3)

                    if pnl_pct > 0 and not position.get("time_to_first_green_seconds"):
                        position["time_to_first_green_seconds"] = round(age_seconds, 3)

                    if pnl_pct < 0 and not position.get("time_to_first_red_seconds"):
                        position["time_to_first_red_seconds"] = round(age_seconds, 3)

                    if position["max_favorable_excursion_pct"] > previous_mfe and not position.get("time_to_mfe_seconds"):
                        position["time_to_mfe_seconds"] = round(age_seconds, 3)

                    if position["max_adverse_excursion_pct"] < previous_mae and not position.get("time_to_mae_seconds"):
                        position["time_to_mae_seconds"] = round(age_seconds, 3)

                    # --- P0.5A: entry quality autopsy telemetry ---
                    # These are observation-based fields; they do not affect execution.
                    if pnl_pct < 0 and not position.get("immediate_adverse_move_pct"):
                        position["immediate_adverse_move_pct"] = round(pnl_pct, 4)

                    if age_seconds >= 300 and not position.get("first_5m_pnl"):
                        position["first_5m_pnl"] = round(pnl_pct, 4)

                    first_three_samples = position.get("first_3_candles_samples")
                    if not isinstance(first_three_samples, list):
                        first_three_samples = []

                    if len(first_three_samples) < 3:
                        first_three_samples.append(round(pnl_pct, 4))
                        position["first_3_candles_samples"] = first_three_samples

                    if len(first_three_samples) >= 3 and not position.get("first_3_candles_result"):
                        positive_count = sum(1 for sample in first_three_samples[:3] if sample > 0)
                        negative_count = sum(1 for sample in first_three_samples[:3] if sample < 0)

                        if positive_count == 3:
                            first_three_result = "GREEN_START"
                        elif negative_count == 3:
                            first_three_result = "RED_START"
                        elif positive_count > 0 and negative_count > 0:
                            first_three_result = "MIXED_START"
                        else:
                            first_three_result = "FLAT_START"

                        position["first_3_candles_result"] = first_three_result
                        position["first_3_candles_source"] = "sync_observations"
                except Exception:
                    pass

            # Near-TP protection: if a trade reaches most of TP1 but fails to fill,
            # protect it at BE + fee buffer instead of letting it round-trip to red.
            take_profits = position.get("take_profits") or []
            tp1 = self._safe_float(take_profits[0], 0.0) if isinstance(take_profits, list) and take_profits else 0.0
            if tp1 > 0 and entry > 0 and not bool(position.get("tp1_hit", False)):
                tp_distance_pct = abs(tp1 - entry) / max(entry, 1e-9) * 100.0
                distance_to_tp_pct = abs(tp1 - current_price) / max(current_price, 1e-9) * 100.0
                tp_reach_pct = max(0.0, min(999.0, (position["max_favorable_excursion_pct"] / tp_distance_pct) * 100.0)) if tp_distance_pct > 0 else 0.0
                previous_min_distance = self._safe_float(position.get("min_distance_to_tp_pct"), 999.0)
                position["near_tp_distance_pct"] = round(distance_to_tp_pct, 5)
                position["min_distance_to_tp_pct"] = round(min(previous_min_distance, distance_to_tp_pct), 5)
                position["tp1_reach_pct"] = round(tp_reach_pct, 2)

                if tp_reach_pct >= self.near_tp_protection_trigger_pct and not bool(position.get("near_tp_protection_active", False)):
                    if direction == "LONG":
                        protective_stop = round(entry * (1.0 + self.near_tp_be_fee_buffer_pct / 100.0), 8)
                        should_move_stop = protective_stop > stop and protective_stop < current_price
                    else:
                        protective_stop = round(entry * (1.0 - self.near_tp_be_fee_buffer_pct / 100.0), 8)
                        should_move_stop = protective_stop < stop and protective_stop > current_price

                    position["near_tp_seen"] = True
                    if not position.get("near_tp_seen_at"):
                        position["near_tp_seen_at"] = datetime.now(timezone.utc).isoformat()
                    # --- Telemetry: time to near TP ---
                    if not position.get("time_to_near_tp_seconds"):
                        try:
                            opened_at = str(position.get("opened_at") or position.get("created_at") or "")
                            if opened_at:
                                opened_dt = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
                                position["time_to_near_tp_seconds"] = round(
                                    (datetime.now(timezone.utc) - opened_dt).total_seconds(),
                                    3,
                                )
                        except Exception:
                            pass
                    position["near_tp_protection_candidate_stop"] = protective_stop
                    position["near_tp_protection_reach_pct"] = round(tp_reach_pct, 2)

                    if should_move_stop:
                        self.log.warning(
                            "PROTECTION_ACTION_STARTED | %s | action=move_stop_loss | reason=NEAR_TP_PROTECTION | direction=%s | old_stop=%s | new_stop=%s | entry=%s | current=%s | tp1=%s | reach=%.2f",
                            symbol,
                            direction,
                            stop,
                            protective_stop,
                            entry,
                            current_price,
                            tp1,
                            tp_reach_pct,
                        )
                        moved = self._move_exchange_stop_loss_with_retries(
                            position,
                            protective_stop,
                            "NEAR_TP_PROTECTION",
                        )
                        if moved:
                            position["stop_loss"] = protective_stop
                            stop = protective_stop
                            position["near_tp_protection_active"] = True
                            position["break_even_active"] = True
                            position["tp1_locked_stop_active"] = True
                            position["near_tp_protection_reason"] = "tp1_reached_85pct_be_fee_lock"
                            self.log.warning(
                                "PROTECTION_ACTION_SUCCESS | %s | action=move_stop_loss | reason=NEAR_TP_PROTECTION | new_stop=%s | entry=%s | current=%s | tp1=%s | reach=%.2f",
                                symbol,
                                protective_stop,
                                entry,
                                current_price,
                                tp1,
                                tp_reach_pct,
                            )
                            self.log.warning(
                                "NEAR_TP_PROTECTION_ARMED | %s | direction=%s | reach=%.2f | stop=%s | entry=%s | current=%s | tp1=%s",
                                symbol,
                                direction,
                                tp_reach_pct,
                                protective_stop,
                                entry,
                                current_price,
                                tp1,
                            )
                        else:
                            position["near_tp_protection_failed"] = "move_exchange_stop_loss_with_retries_returned_false"
                            self.log.error(
                                "PROTECTION_ACTION_FAILED | %s | action=move_stop_loss | reason=NEAR_TP_PROTECTION | new_stop=%s | entry=%s | current=%s | tp1=%s | reach=%.2f | error=%s",
                                symbol,
                                protective_stop,
                                entry,
                                current_price,
                                tp1,
                                tp_reach_pct,
                                "move_exchange_stop_loss_with_retries_returned_false",
                            )
                            self.log.error(
                                "NEAR_TP_PROTECTION_FAILED | %s | direction=%s | reach=%.2f | stop=%s | error=%s",
                                symbol,
                                direction,
                                tp_reach_pct,
                                protective_stop,
                                "move_exchange_stop_loss_with_retries_returned_false",
                            )

            note_parts: list[str] = []

            exchange_tpsl_verified = self._exchange_position_has_tpsl(live_position or {})
            if exchange_tpsl_verified:
                position["protection_verified"] = True
                position["protection_integrity"] = "EXCHANGE_POSITION_TPSL_VERIFIED"
                position["exchange_take_profit"] = live_position.get("takeProfit") if live_position else position.get("exchange_take_profit")
                position["exchange_stop_loss"] = live_position.get("stopLoss") if live_position else position.get("exchange_stop_loss")
                position["exchange_take_profit_id"] = live_position.get("takeProfitId") if live_position else position.get("exchange_take_profit_id")
                position["exchange_stop_loss_id"] = live_position.get("stopLossId") if live_position else position.get("exchange_stop_loss_id")

            local_marked_done = (
                bool(position.get("tp3_hit", False))
                or float(position.get("remaining_size_pct", 100.0)) <= self.residual_position_pct_threshold
                or position.get("closed_reason") in {"tp3", "break_even_stop", "stop_loss", "exchange_position_closed"}
            )
            residual_live_position = (
                live_size > self.residual_position_abs_threshold
                and original_size > 0
                and current_remaining_pct <= self.residual_position_pct_threshold
            )

            if position.get("status") == "OPEN" and local_marked_done and residual_live_position:
                self.log.warning(
                    "RESIDUAL_POSITION_DETECTED | %s | live_size=%s | original_size=%s | remaining_pct=%.4f | tp1=%s | tp2=%s | tp3=%s | reason=%s",
                    symbol,
                    live_size,
                    original_size,
                    current_remaining_pct,
                    position.get("tp1_hit"),
                    position.get("tp2_hit"),
                    position.get("tp3_hit"),
                    position.get("closed_reason"),
                )
                try:
                    residual_close_result = self.client.close_futures_position_full(
                        symbol=symbol,
                        direction=direction,
                        reason="residual_position_cleanup",
                        cleanup_tpsl=True,
                    )
                    position["residual_cleanup_result"] = residual_close_result
                    position["remaining_size_pct"] = 0.0
                    position["status"] = "CLOSED"
                    position["closed_reason"] = "residual_position_cleanup"
                    position["closed_at"] = datetime.now(timezone.utc).isoformat()
                    position["stale_tpsl_cleanup_done"] = True
                    note_parts.append("RESIDUAL_POSITION_CLEANUP_SENT")
                    self.log.warning(
                        "RESIDUAL_POSITION_CLEANUP_SENT | %s | live_size=%s | remaining_pct=%.4f | result=%s",
                        symbol,
                        live_size,
                        current_remaining_pct,
                        residual_close_result,
                    )
                    self._sync_journal_close(symbol, "residual_position_cleanup", pnl_pct)
                    self._append_closed_trade_dataset_row(
                        position=position,
                        close_reason="residual_position_cleanup",
                        exit_price=current_price,
                        pnl_pct=pnl_pct,
                        extra={"close_source": "residual_position_cleanup", "live_size": live_size},
                    )
                    self._register_symbol_cooldown(symbol, "residual_position_cleanup", pnl_pct)
                except Exception as exc:
                    position["stale_tpsl_cleanup_done"] = False
                    position["residual_cleanup_error"] = str(exc)

                    if self._is_no_position_to_close_error(exc):
                        position["status"] = "CLOSED_SYNCED"
                        position["closed_reason"] = "residual_position_exchange_no_position_to_close"
                        position["closed_at"] = datetime.now(timezone.utc).isoformat()
                        position["remaining_size_pct"] = 0.0
                        position["exchange_live_size"] = 0.0
                        position["exchange_remaining_pct"] = 0.0
                        position["sync_reason"] = "Bitget returned 22002 No position to close during residual cleanup; local OPEN resolved as CLOSED_SYNCED"
                        note_parts.append("RESIDUAL_DESYNC_RESOLVED_NO_POSITION_TO_CLOSE")
                        self.log.warning(
                            "RESIDUAL_DESYNC_RESOLVED_NO_POSITION_TO_CLOSE | %s | attempted_live_size=%s | remaining_pct=%.4f | error=%s",
                            symbol,
                            live_size,
                            current_remaining_pct,
                            exc,
                        )
                        self._sync_journal_close(symbol, position["closed_reason"], pnl_pct)
                        self._append_closed_trade_dataset_row(
                            position=position,
                            close_reason=position["closed_reason"],
                            exit_price=current_price,
                            pnl_pct=pnl_pct,
                            extra={
                                "close_source": "residual_exchange_no_position_to_close_desync_resolved",
                                "attempted_live_size": live_size,
                                "remaining_pct": current_remaining_pct,
                            },
                        )
                        self._register_symbol_cooldown(symbol, position["closed_reason"], pnl_pct)
                    else:
                        note_parts.append("CRITICAL: residual position cleanup failed")
                        self.log.error(
                            "RESIDUAL_POSITION_CLEANUP_FAILED | %s | live_size=%s | remaining_pct=%.4f | error=%s",
                            symbol,
                            live_size,
                            current_remaining_pct,
                            exc,
                        )
            tp2_debug_mismatch = (
                bool(position.get("tp2_hit", False))
                and (
                    not bool(position.get("tp1_locked_stop_active", False))
                    or not bool(position.get("break_even_active", False))
                    or abs(float(position.get("stop_loss") or 0.0) - float(position.get("exchange_stop_loss") or 0.0)) > 1e-9
                    or not bool(position.get("old_stop_loss_removed", False))
                )
            )

            if tp2_debug_mismatch:
                self.log.warning(
                    "TP2_RECOVERY_MISMATCH | %s | status=%s | tp1=%s | tp2=%s | tp3=%s | tp1_lock=%s | be=%s | sl=%s | exchange_sl=%s | old_sl_removed=%s | last_reason=%s | tps=%s",
                    symbol,
                    position.get("status"),
                    position.get("tp1_hit"),
                    position.get("tp2_hit"),
                    position.get("tp3_hit"),
                    position.get("tp1_locked_stop_active", False),
                    position.get("break_even_active", False),
                    position.get("stop_loss"),
                    position.get("exchange_stop_loss"),
                    position.get("old_stop_loss_removed", False),
                    position.get("last_sl_move_reason"),
                    len(position.get("take_profits", [])),
                )
            elif bool(position.get("tp2_hit", False)):
                self.log.info(
                    "TP2_RECOVERY_HEALTHY | %s | tp1_lock=%s | be=%s | sl=%s | exchange_sl=%s | old_sl_removed=%s | last_reason=%s",
                    symbol,
                    position.get("tp1_locked_stop_active", False),
                    position.get("break_even_active", False),
                    position.get("stop_loss"),
                    position.get("exchange_stop_loss"),
                    position.get("old_stop_loss_removed", False),
                    position.get("last_sl_move_reason"),
                )

            # Recovery path: TP2 already hit previously but TP1-lock SL was never confirmed on exchange.
            if (
                position.get("status") == "OPEN"
                and bool(position.get("tp2_hit", False))
                and not bool(position.get("tp1_locked_stop_active", False))
                and len(position.get("take_profits", [])) >= 1
            ):
                tp1_lock_stop = float(position.get("take_profits", [0])[0])
                previous_stop = float(position.get("stop_loss") or 0.0)

                self.log.warning(
                    "TP2_LOCK_RECOVERY_ATTEMPT | %s | current_stop=%s | target_stop=%s",
                    symbol,
                    previous_stop,
                    tp1_lock_stop,
                )

                self._protect_after_tp_fill(
                    position=position,
                    target_stop=tp1_lock_stop,
                    reason="TP2_LOCK_RECOVERY",
                    note_parts=note_parts,
                )

            if not position.get("protection_verified") and not exchange_tpsl_verified:
                position["protection_integrity"] = "VERIFY_REQUIRED"
                self.log.warning(
                    "PROTECTION_VERIFY_REQUIRED | %s | local_payload=%s | exchange_trigger_price=%s | tps=%s",
                    symbol,
                    self._has_local_protection_payload(position),
                    position.get("exchange_stop_loss"),
                    len(position.get("take_profits", [])),
                )

                if self._ensure_exchange_protection_with_retries(position):
                    position["protection_verified"] = True
                    position["protection_integrity"] = "VERIFIED_OR_REPAIRED"
                    note_parts.append("Exchange protection verified/repaired")
                    self.log.warning(
                        "PROTECTION_VERIFIED_OR_REPAIRED | %s | trigger_price=%s | exchange_trigger_price=%s | tps=%s",
                        symbol,
                        position.get("stop_loss"),
                        position.get("exchange_stop_loss"),
                        len(position.get("take_profits", [])),
                    )
                else:
                    position["protection_verified"] = False
                    position["protection_integrity"] = "FAILED_CLOSE_REQUIRED"
                    note_parts.append("CRITICAL: protection repair failed; closing position")
                    self.log.error(
                        "UNPROTECTED_POSITION_CLOSE_REQUIRED | %s | reason=protection_repair_failed | local_payload=%s | trigger_price=%s | tps=%s",
                        symbol,
                        self._has_local_protection_payload(position),
                        position.get("stop_loss"),
                        len(position.get("take_profits", [])),
                    )
                    if self._close_unprotected_position(position, "protection_repair_failed"):
                        position["status"] = "CLOSED"
                        position["closed_reason"] = "protection_repair_failed"
                        position["closed_at"] = datetime.now(timezone.utc).isoformat()
                        position["remaining_size_pct"] = 0.0
                        position["protection_integrity"] = "FAILED_CLOSED"
                        self._sync_journal_close(symbol, "protection_repair_failed", pnl_pct)
                        self._append_closed_trade_dataset_row(
                            position=position,
                            close_reason="protection_repair_failed",
                            exit_price=current_price,
                            pnl_pct=pnl_pct,
                            extra={
                                "close_source": "unprotected_position_emergency_close",
                                "failure_type": "PROTECTION_FAILURE",
                                "process_verdict": "EXECUTION_FAILURE",
                            },
                        )
                        self._register_symbol_cooldown(symbol, "protection_repair_failed", pnl_pct)
                        position["failure_type"] = "PROTECTION_FAILURE"
                        position["process_verdict"] = "EXECUTION_FAILURE"
                        self.log.error(
                            "UNPROTECTED_POSITION_CLOSED | %s | reason=protection_repair_failed | exit=%s | pnl_pct=%.4f",
                            symbol,
                            current_price,
                            pnl_pct,
                        )
                    else:
                        note_parts.append("CRITICAL: close attempt failed")
                        self.log.critical(
                            "UNPROTECTED_POSITION_CLOSE_FAILED | %s | manual_intervention_required=True",
                            symbol,
                        )

            tps = [float(x) for x in position.get("take_profits", [])]

            # Near-TP / profit-giveback tracking for trade autopsy.
            next_target = self._next_unhit_target(position)
            distance_to_next_tp_pct = self._distance_to_target_pct(direction, current_price, next_target)
            previous_min_near_tp_distance = float(position.get("min_distance_to_tp_pct") or 999.0)
            if next_target > 0:
                position["current_distance_to_tp_pct"] = round(distance_to_next_tp_pct, 4)
                position["min_distance_to_tp_pct"] = round(min(previous_min_near_tp_distance, distance_to_next_tp_pct), 4)
                if distance_to_next_tp_pct <= self.tp_miss_near_pct:
                    if not bool(position.get("near_tp_seen", False)):
                        position["near_tp_seen_at"] = datetime.now(timezone.utc).isoformat()
                        position["near_tp_first_price"] = current_price
                        position["near_tp_first_target"] = next_target
                        position["mfe_at_near_tp_seen_pct"] = position.get("max_favorable_excursion_pct", pnl_pct)
                    position["near_tp_seen"] = True
                    position["near_tp_latest_price"] = current_price
                    position["near_tp_latest_target"] = next_target
                    position["near_tp_distance_pct"] = round(distance_to_next_tp_pct, 4)
                    note_parts.append(f"NEAR_TP_SEEN distance={distance_to_next_tp_pct:.4f}% target={next_target:.8f}")
            tp1_size_inferred = (
                not position.get("tp1_hit")
                and current_remaining_pct <= (100.0 - float(self.settings.tp1_close_pct) + 5.0)
                and current_remaining_pct < 99.0
            )

            if len(tps) >= 1 and not position.get("tp1_hit") and (
                self._target_hit_range(direction, current_high, current_low, tps[0]) or tp1_size_inferred
            ):
                position["tp1_hit"] = True
                position["remaining_size_pct"] = max(0.0, float(position.get("remaining_size_pct", 100.0)) - self.settings.tp1_close_pct)
                if tp1_size_inferred:
                    note_parts.append("TP1_FEE_BE_SIZE_INFERRED")
                    self.log.warning(
                        "TP1_FEE_BE_SIZE_INFERRED | %s | live_remaining_pct=%.2f",
                        symbol,
                        current_remaining_pct,
                    )
                else:
                    note_parts.append("TP1 hit")
                single_tp_mode = len(tps) == 1

                if self.settings.move_stop_to_be_after_tp1 and not single_tp_mode:
                    fee_adjusted_be = self._fee_adjusted_break_even(direction, entry)
                    self._protect_after_tp_fill(
                        position=position,
                        target_stop=fee_adjusted_be,
                        reason="TP1_FEE_BE",
                        note_parts=note_parts,
                    )
                    self.log.warning(
                        "TP1_FILLED_MOVE_SL_BE | %s | target_stop=%s | protection_integrity=%s | exchange_trigger_price=%s",
                        symbol,
                        fee_adjusted_be,
                        position.get("protection_integrity"),
                        position.get("exchange_stop_loss"),
                    )
                elif single_tp_mode:
                    note_parts.append("SINGLE_TP_MODE_NO_BE_MOVE")
                    self.log.info(
                        "SINGLE_TP_MODE_NO_BE_MOVE | %s | tp_count=%s",
                        symbol,
                        len(tps),
                    )

            tp2_size_inferred = (
                not position.get("tp2_hit")
                and current_remaining_pct <= (
                    100.0 - float(self.settings.tp1_close_pct) - float(self.settings.tp2_close_pct) + 5.0
                )
            )

            if len(tps) >= 2 and not position.get("tp2_hit") and (
                self._target_hit_range(direction, current_high, current_low, tps[1]) or tp2_size_inferred
            ):
                position["tp2_hit"] = True
                position["remaining_size_pct"] = max(0.0, float(position.get("remaining_size_pct", 100.0)) - self.settings.tp2_close_pct)
                if tp2_size_inferred:
                    note_parts.append("TP2_LOCK_TP1_SIZE_INFERRED")
                    self.log.warning(
                        "TP2_LOCK_TP1_SIZE_INFERRED | %s | live_remaining_pct=%.2f",
                        symbol,
                        current_remaining_pct,
                    )
                else:
                    note_parts.append("TP2 hit")
                if len(tps) >= 1:
                    tp1_lock_stop = float(tps[0])
                    self._protect_after_tp_fill(
                        position=position,
                        target_stop=tp1_lock_stop,
                        reason="TP2_LOCK_TP1",
                        note_parts=note_parts,
                    )
                    self.log.warning(
                        "TP2_FILLED_MOVE_SL_TP1 | %s | target_stop=%s | protection_integrity=%s | exchange_trigger_price=%s",
                        symbol,
                        tp1_lock_stop,
                        position.get("protection_integrity"),
                        position.get("exchange_stop_loss"),
                    )

            tp3_size_inferred = (
                not position.get("tp3_hit")
                and current_remaining_pct <= 5.0
                and current_remaining_pct > 0.0
            )
            if len(tps) >= 3 and not position.get("tp3_hit") and (
                self._target_hit_range(direction, current_high, current_low, tps[2]) or tp3_size_inferred
            ):
                position["tp3_hit"] = True
                if tp3_size_inferred:
                    note_parts.append("TP3_CLOSE_ALL_SIZE_INFERRED")
                    self.log.warning(
                        "TP3_CLOSE_ALL_SIZE_INFERRED | %s | live_remaining_pct=%.2f | live_size=%.6f",
                        symbol,
                        current_remaining_pct,
                        live_size,
                    )
                if bool(getattr(self.settings, "tp3_close_all_remainder", True)):
                    close_all_result = self.client.close_futures_position_full(
                        symbol=symbol,
                        direction=direction,
                        reason="tp3_close_all_remainder",
                        cleanup_tpsl=True,
                    )
                    position["tp3_close_all_result"] = close_all_result
                    position["remaining_size_pct"] = 0.0
                    position["break_even_active"] = True
                    position["tp1_locked_stop_active"] = True
                    position["status"] = "CLOSED"
                    position["closed_reason"] = "tp3"
                    position["closed_at"] = datetime.now(timezone.utc).isoformat()
                    position["stale_tpsl_cleanup_done"] = True
                    note_parts.append("TP3_CLOSE_ALL_REMAINDER_EXCHANGE_SENT")
                    note_parts.append("TP3 hit; full live remainder close-all requested on Bitget")
                    self.log.warning(
                        "TP3_CLOSE_ALL_REMAINDER_EXCHANGE_SENT | %s | direction=%s | live_size=%s | result=%s",
                        symbol,
                        direction,
                        live_size,
                        close_all_result,
                    )
                    self.log.warning(
                        "POSITION_CLOSED_CLEAN | %s | remaining_size_pct=%s | live_size_before_close=%s | tpsl_cleanup_done=%s",
                        symbol,
                        position.get("remaining_size_pct"),
                        live_size,
                        position.get("stale_tpsl_cleanup_done"),
                    )
                    self._sync_journal_close(symbol, "tp3", pnl_pct)
                    self._append_closed_trade_dataset_row(
                        position=position,
                        close_reason="tp3",
                        exit_price=current_price,
                        pnl_pct=pnl_pct,
                        extra={"close_source": "tp3_close_all_remainder", "live_size_before_close": live_size},
                    )
                    self._register_symbol_cooldown(symbol, "tp3", pnl_pct)
                else:
                    position["remaining_size_pct"] = max(
                        0.0,
                        float(position.get("remaining_size_pct", 100.0)) - self.settings.tp3_close_pct,
                    )
                    position["break_even_active"] = True
                    position["tp1_locked_stop_active"] = True
                    if float(position.get("remaining_size_pct", 0.0)) <= 0.0:
                        position["status"] = "CLOSED"
                        position["closed_reason"] = "tp3"
                        position["closed_at"] = datetime.now(timezone.utc).isoformat()
                        note_parts.append("TP3 hit; position closed by configured pct remainder")
                        self._sync_journal_close(symbol, "tp3", pnl_pct)
                        self._append_closed_trade_dataset_row(
                            position=position,
                            close_reason="tp3",
                            exit_price=current_price,
                            pnl_pct=pnl_pct,
                            extra={"close_source": "tp3_configured_pct_remainder"},
                        )
                        self._register_symbol_cooldown(symbol, "tp3", pnl_pct)
                    else:
                        note_parts.append("TP3 hit; configured partial TP3 close applied")

            should_tighten_failed, failed_continuation_stop, failed_continuation_context = self._should_tighten_failed_continuation(
                position=position,
                snapshot=snapshot_map.get(symbol),
                direction=direction,
                entry=entry,
                current_price=current_price,
                pnl_pct=pnl_pct,
            )
            if should_tighten_failed:
                previous_stop = float(position.get("stop_loss") or 0.0)
                self.log.warning(
                    "FAILED_CONTINUATION_DETECTED | %s | direction=%s | current_price=%s | pnl_pct=%.4f | old_stop=%s | new_stop=%s | context=%s",
                    symbol,
                    direction,
                    current_price,
                    pnl_pct,
                    previous_stop,
                    failed_continuation_stop,
                    failed_continuation_context,
                )
                self.log.warning(
                    "PROTECTION_ACTION_STARTED | %s | action=move_stop_loss | reason=FAILED_CONTINUATION_PROTECTION | direction=%s | old_stop=%s | new_stop=%s | current=%s | pnl_pct=%.4f",
                    symbol,
                    direction,
                    previous_stop,
                    failed_continuation_stop,
                    current_price,
                    pnl_pct,
                )
                if self._move_exchange_stop_loss_with_retries(position, failed_continuation_stop, "FAILED_CONTINUATION_PROTECTION"):
                    self.log.warning(
                        "PROTECTION_ACTION_SUCCESS | %s | action=move_stop_loss | reason=FAILED_CONTINUATION_PROTECTION | old_stop=%s | new_stop=%s | current=%s | pnl_pct=%.4f",
                        symbol,
                        previous_stop,
                        failed_continuation_stop,
                        current_price,
                        pnl_pct,
                    )
                    position["stop_loss"] = failed_continuation_stop
                    position["exchange_stop_loss"] = failed_continuation_stop
                    position["break_even_active"] = True
                    position["tp1_locked_stop_active"] = True
                    position["failed_continuation_protection_active"] = True
                    position["failed_continuation_context"] = failed_continuation_context
                    position["last_sl_move_reason"] = "FAILED_CONTINUATION_PROTECTION"
                    position["old_stop_loss_removed"] = True
                    note_parts.append(f"FAILED_CONTINUATION_SL_TIGHTENED @ {failed_continuation_stop:.8f}")
                    self.log.warning(
                        "FAILED_CONTINUATION_SL_TIGHTENED | %s | old_stop=%s | new_stop=%s | context=%s",
                        symbol,
                        previous_stop,
                        failed_continuation_stop,
                        failed_continuation_context,
                    )
                else:
                    position["failed_continuation_protection_failed"] = True
                    position["failed_continuation_context"] = failed_continuation_context
                    note_parts.append("WARNING: failed continuation detected but SL tighten failed")
                    self.log.error(
                        "PROTECTION_ACTION_FAILED | %s | action=move_stop_loss | reason=FAILED_CONTINUATION_PROTECTION | old_stop=%s | new_stop=%s | current=%s | pnl_pct=%.4f | context=%s",
                        symbol,
                        previous_stop,
                        failed_continuation_stop,
                        current_price,
                        pnl_pct,
                        failed_continuation_context,
                    )
                    self.log.error(
                        "FAILED_CONTINUATION_SL_TIGHTEN_FAILED | %s | attempted_stop=%s | context=%s",
                        symbol,
                        failed_continuation_stop,
                        failed_continuation_context,
                    )
            current_stop = float(position["stop_loss"])
            stop_hit = self._stop_hit_range(direction, current_high, current_low, current_stop)
            exchange_live_position_open_after_stop = (
                bitget_sync_ok
                and symbol in bitget_open_symbols
                and live_size > 0
            )

            if position.get("status") == "OPEN" and stop_hit and exchange_live_position_open_after_stop:
                closed_reason = "break_even_stop" if position.get("break_even_active") else "stop_loss"
                position["exchange_position_still_open_after_local_stop"] = True

                if exchange_tpsl_verified:
                    note_parts.append("LOCAL_STOP_TOUCHED_BUT_EXCHANGE_TPSL_ACTIVE_NO_CLOSE")
                    position["local_stop_touched_exchange_tpsl_active"] = True
                    position["last_recovery_hold_reason"] = "exchange_tpsl_active_local_stop_close_blocked"
                    self.log.warning(
                        "LOCAL_STOP_TOUCHED_EXCHANGE_TPSL_ACTIVE_NO_CLOSE | %s | stop=%s | current_low=%s | current_high=%s | live_size=%s | protection_integrity=%s",
                        symbol,
                        current_stop,
                        current_low,
                        current_high,
                        live_size,
                        position.get("protection_integrity"),
                    )
                    stop_hit = False
                else:
                    note_parts.append("LOCAL_STOP_TOUCHED_EXCHANGE_OPEN_NO_AUTOCLOSE_SAFE_MODE")
                    position["last_recovery_hold_reason"] = "exchange_open_after_local_stop_autoclose_disabled_safe_mode"
                    position["local_stop_touched_exchange_open_no_autoclose"] = True
                    self.log.error(
                        "LOCAL_STOP_TOUCHED_EXCHANGE_OPEN_NO_AUTOCLOSE_SAFE_MODE | %s | stop=%s | current_low=%s | current_high=%s | live_size=%s",
                        symbol,
                        current_stop,
                        current_low,
                        current_high,
                        live_size,
                    )
                    stop_hit = False

            elif position.get("status") == "OPEN" and stop_hit:
                closed_reason = "break_even_stop" if position.get("break_even_active") else "stop_loss"
                position["remaining_size_pct"] = 0.0
                position["status"] = "CLOSED"
                position["closed_reason"] = closed_reason
                position["closed_at"] = datetime.now(timezone.utc).isoformat()
                note_parts.append(f"local stop hit @ {current_stop:.8f}")
                self.log.warning(
                    "STOP_HIT_LOCAL_CLOSE | %s | reason=%s | stop=%s | current_low=%s | current_high=%s",
                    symbol,
                    closed_reason,
                    current_stop,
                    current_low,
                    current_high,
                )
                self._sync_journal_close(symbol, closed_reason, pnl_pct)
                self._append_closed_trade_dataset_row(
                    position=position,
                    close_reason=closed_reason,
                    exit_price=current_price,
                    pnl_pct=pnl_pct,
                    extra={"close_source": "local_stop_hit"},
                )
                self._register_symbol_cooldown(symbol, closed_reason, pnl_pct)

            note = " | ".join(note_parts) if note_parts else "position synced"

            updates.append(
                PositionUpdate(
                    symbol=symbol,
                    status=position["status"],
                    current_price=current_price,
                    unrealized_pnl_pct=round(pnl_pct, 3),
                    stop_loss=float(position["stop_loss"]),
                    break_even_active=bool(position.get("break_even_active", False)),
                    tp1_hit=bool(position.get("tp1_hit", False)),
                    tp2_hit=bool(position.get("tp2_hit", False)),
                    tp3_hit=bool(position.get("tp3_hit", False)),
                    note=note,
                    )
            )
            events.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "symbol": symbol,
                    "status": position["status"],
                    "current_price": current_price,
                    "unrealized_pnl_pct": round(pnl_pct, 3),
                    "stop_loss": float(position["stop_loss"]),
                    "break_even_active": bool(position.get("break_even_active", False)),
                    "tp1_locked_stop_active": bool(position.get("tp1_locked_stop_active", False)),
                    "tp1_hit": bool(position.get("tp1_hit", False)),
                    "tp2_hit": bool(position.get("tp2_hit", False)),
                    "tp3_hit": bool(position.get("tp3_hit", False)),
                    "note": note,
                }
            )

        self.store.save(positions)
        self.event_store.save(events[-500:])
        return updates


    def _recover_missing_local_positions(
        self,
        missing_symbols: list[str],
        positions_live: list[dict],
        price_map: dict[str, float],
    ) -> list[dict]:
        recovered: list[dict] = []
        now = datetime.now(timezone.utc).isoformat()

        self.log.warning(
            "STATE_RECOVERY_SCAN | missing=%s | live_keys=%s",
            ",".join(missing_symbols),
            [sorted(list(p.keys()))[:12] for p in positions_live[:3]],
        )

        for symbol in missing_symbols:
            live_position = self._find_live_position(symbol, positions_live)
            if not live_position:
                self.log.warning(
                    "STATE_RECOVERY_SKIPPED | %s | reason=live_position_not_found | live_symbols=%s",
                        symbol,
                    [self._live_symbol(p) for p in positions_live],
                    )
                continue

            size = self._live_position_size(live_position)
            entry = self._live_entry_price(live_position)
            direction = self._live_direction(live_position)
            current_price = self._live_mark_price(live_position) or float(price_map.get(symbol, entry or 0.0))

            if not symbol or size <= 0 or entry <= 0 or direction not in {"LONG", "SHORT"}:
                self.log.warning(
                    "STATE_RECOVERY_SKIPPED | %s | size=%s entry=%s direction=%s raw=%s",
                        symbol,
                    size,
                    entry,
                    direction,
                    live_position,
                    )
                continue

            protection = self._extract_live_protection_payload(live_position)
            stop_loss = float(protection.get("stop_loss") or 0.0)
            take_profits = [float(x) for x in protection.get("take_profits", []) if float(x) > 0]

            if stop_loss <= 0 or not take_profits:
                fallback_protection = self._fallback_protection_from_execution_log(symbol)
                fallback_stop = float(fallback_protection.get("stop_loss") or 0.0)
                fallback_tps = [float(x) for x in fallback_protection.get("take_profits", []) if float(x) > 0]
                if fallback_stop > 0 and fallback_tps:
                    stop_loss = fallback_stop
                    take_profits = fallback_tps
                    protection = fallback_protection
                    self.log.warning(
                        "STATE_RECOVERY_PROTECTION_FALLBACK | %s | stop=%s tps=%s source=%s",
                        symbol,
                        stop_loss,
                        take_profits,
                        fallback_protection.get("source"),
                    )

            recovered_position = {
                "symbol": symbol,
                "direction": direction,
                "strategy": "recovered_exchange_position",
                "status": "OPEN",
                "avg_entry": entry,
                "last_price": current_price,
                "size": size,
                "order_size": size,
                "position_size": size,
                "position_notional_usdt": round(size * current_price, 6),
                "leverage": float(live_position.get("leverage") or getattr(self.settings, "default_leverage", 1.0) or 1.0),
                "stop_loss": stop_loss,
                "take_profits": take_profits,
                "remaining_size_pct": 100.0,
                "break_even_active": False,
                "tp1_lock_price": 0.0,
                "last_sl_move_reason": "STATE_RECOVERY",
                "tp1_locked_stop_active": False,
                "tp1_hit": False,
                "tp2_hit": False,
                "tp3_hit": False,
                "protection_verified": bool(stop_loss > 0 and take_profits),
                "protection_payload": protection if stop_loss > 0 and take_profits else {},
                "recovered_from_exchange": True,
                "exchange_position_still_open_after_local_stop": False,
                "last_recovery_hold_reason": "",
                "opened_at": now,
                "recovered_at": now,
                "notes": ["STATE_RECOVERED_FROM_BITGET", "exchange was source of truth"],
            }

            self.log.warning(
                "STATE_RECOVERED | %s | direction=%s size=%s entry=%s stop=%s tps=%s protection_verified=%s",
                symbol,
                direction,
                size,
                entry,
                stop_loss,
                take_profits,
                recovered_position["protection_verified"],
            )
            recovered.append(recovered_position)

        return recovered

    @staticmethod
    def _live_symbol(position: dict) -> str:
        for key in ("symbol", "instId", "symbolName", "contractSymbol"):
            value = str(position.get(key) or "")
            if value:
                return value.upper()
        return ""

    def _hydrate_position_from_open_dataset_row(self, position: dict) -> None:
        symbol = str(position.get("symbol") or "").upper()
        opened_at = str(position.get("opened_at") or "")
        if not symbol or not opened_at:
            return

        path = Path("logs/trade_dataset_v2.csv")
        if not path.exists():
            return

        try:
            with path.open("r", newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                matching_rows = []
                for row in reader:
                    if str(row.get("event_type") or "").upper() != "OPEN":
                        continue
                    if str(row.get("symbol") or "").upper() != symbol:
                        continue
                    row_opened_at = str(row.get("opened_at") or row.get("timestamp") or "")
                    if row_opened_at == opened_at or row_opened_at[:19] == opened_at[:19]:
                        matching_rows.append(row)

                if not matching_rows:
                    return

                row = matching_rows[-1]

                def row_float(key: str) -> float:
                    try:
                        return float(row.get(key) or 0.0)
                    except (TypeError, ValueError):
                        return 0.0

                entry = row_float("entry") or row_float("actual_entry") or row_float("expected_entry")
                notional = row_float("notional")
                leverage = row_float("leverage")
                fees = row_float("fees")
                slippage_pct = row_float("slippage_pct")
                stop_loss = row_float("stop_loss")

                raw_take_profits = str(row.get("take_profits") or "")
                take_profits: list[float] = []
                for raw_tp in raw_take_profits.replace(",", "|").split("|"):
                    parsed_tp = self._safe_float(raw_tp.strip(), 0.0)
                    if parsed_tp > 0:
                        take_profits.append(parsed_tp)

                if not position.get("avg_entry") and entry > 0:
                    position["avg_entry"] = entry
                if not position.get("entry_price") and entry > 0:
                    position["entry_price"] = entry
                if not position.get("notional") and notional > 0:
                    position["notional"] = notional
                if not position.get("position_notional_usdt") and notional > 0:
                    position["position_notional_usdt"] = notional
                if not position.get("leverage") and leverage > 0:
                    position["leverage"] = leverage
                if not position.get("fees_paid") and fees > 0:
                    position["fees_paid"] = fees
                if not position.get("slippage_pct") and slippage_pct:
                    position["slippage_pct"] = slippage_pct

                current_stop_loss = self._safe_float(position.get("stop_loss"), 0.0)
                if current_stop_loss <= 0 and stop_loss > 0:
                    position["stop_loss"] = stop_loss
                    position["hydrated_stop_loss_from_open_dataset"] = True

                current_take_profits = position.get("take_profits") or []
                has_current_take_profits = isinstance(current_take_profits, list) and any(
                    self._safe_float(tp, 0.0) > 0 for tp in current_take_profits
                )
                if not has_current_take_profits and take_profits:
                    position["take_profits"] = take_profits
                    position["hydrated_take_profits_from_open_dataset"] = True

                if (current_stop_loss <= 0 and stop_loss > 0) or (not has_current_take_profits and take_profits):
                    self.log.warning(
                        "OPEN_DATASET_PROTECTION_HYDRATED | %s | stop=%s | tps=%s",
                        symbol,
                        position.get("stop_loss"),
                        position.get("take_profits"),
                    )

                current_size = self._position_size(position)
                if current_size <= 0 and notional > 0 and entry > 0:
                    inferred_size = round(notional / entry, 8)
                    position["size"] = inferred_size
                    position["order_size"] = inferred_size
                    position["position_size"] = inferred_size
                    position["inferred_size_from_open_dataset"] = True

        except Exception as exc:
            self.log.warning(
                "OPEN_DATASET_CONTEXT_HYDRATE_FAILED | %s | opened_at=%s | error=%s",
                symbol,
                opened_at,
                exc,
            )

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


    def _exchange_position_has_tpsl(self, live_position: dict) -> bool:
        if not isinstance(live_position, dict) or not live_position:
            return False

        take_profit_value = str(live_position.get("takeProfit") or "").strip()
        stop_loss_value = str(live_position.get("stopLoss") or "").strip()
        take_profit_id = str(live_position.get("takeProfitId") or "").strip()
        stop_loss_id = str(live_position.get("stopLossId") or "").strip()

        return bool((take_profit_value or take_profit_id) and (stop_loss_value or stop_loss_id))

    def _find_live_position(cls, symbol: str, positions_live: list[dict]) -> dict | None:
        wanted = symbol.upper()
        for position in positions_live:
            live_symbol = cls._live_symbol(position)
            if live_symbol == wanted:
                return position
        return None

    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _live_position_size(self, position: dict) -> float:
        for key in ("total", "size", "available", "holdVol", "positionSize", "availableSize"):
            value = self._safe_float(position.get(key), 0.0)
            if value > 0:
                return value
        return 0.0

    def _live_entry_price(self, position: dict) -> float:
        for key in ("averageOpenPrice", "avgOpenPrice", "openPriceAvg", "entryPrice", "avgEntryPrice"):
            value = self._safe_float(position.get(key), 0.0)
            if value > 0:
                return value
        return 0.0

    def _live_mark_price(self, position: dict) -> float:
        for key in ("markPrice", "lastPrice", "marketPrice"):
            value = self._safe_float(position.get(key), 0.0)
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _live_direction(position: dict) -> str:
        raw = str(
            position.get("holdSide")
            or position.get("posSide")
            or position.get("side")
            or position.get("direction")
            or ""
        ).lower()
        if "long" in raw or raw == "buy":
            return "LONG"
        if "short" in raw or raw == "sell":
            return "SHORT"
        return ""

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

    def _infer_exchange_closed_reason(self, position: dict, exit_price: float, direction: str) -> str:
        direction = str(direction or "").upper()
        stop_loss = float(position.get("stop_loss") or 0.0)
        take_profits = [float(tp) for tp in (position.get("take_profits") or []) if float(tp or 0) > 0]

        if exit_price <= 0:
            return "closed_synced"

        tolerance_pct = 0.0015

        def near(a: float, b: float) -> bool:
            if a <= 0 or b <= 0:
                return False
            return abs(a - b) / b <= tolerance_pct

        if stop_loss > 0 and near(exit_price, stop_loss):
            return "stop_loss"

        for idx, target in enumerate(take_profits, start=1):
            if near(exit_price, target):
                return f"tp{idx}"

        if position.get("tp3_hit"):
            return "tp3"
        if position.get("tp2_hit"):
            return "tp2_synced"
        if position.get("tp1_hit"):
            return "tp1_synced"

        if direction == "LONG" and stop_loss > 0 and exit_price <= stop_loss:
            return "stop_loss"
        if direction == "SHORT" and stop_loss > 0 and exit_price >= stop_loss:
            return "stop_loss"

        if take_profits:
            first_tp = take_profits[0]
            if direction == "LONG" and exit_price >= first_tp:
                return "tp_synced"
            if direction == "SHORT" and exit_price <= first_tp:
                return "tp_synced"

        return "closed_synced"

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


    @staticmethod
    def _position_size(position: dict) -> float:
        size = float(position.get("size") or position.get("order_size") or position.get("position_size") or 0)
        if size > 0:
            return size

        notional = float(position.get("position_notional_usdt") or 0)
        avg_entry = float(position.get("avg_entry") or position.get("last_price") or 0)
        if notional > 0 and avg_entry > 0:
            return round(notional / avg_entry, 6)

        return 0.0

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
                stop_loss=stop_loss,
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
        buffer_pct = float(getattr(self.settings, "break_even_fee_buffer_pct", 0.10) or 0.10)
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

    def _exchange_close_truth_from_position_history(self, position: dict) -> dict:
        """Fetch realized close truth from Bitget position history."""
        symbol = str(position.get("symbol") or "").upper()
        direction = str(position.get("direction") or "").upper()
        if not symbol:
            return {"close_source": "bitget_position_history_unavailable"}

        hold_side = "long" if direction == "LONG" else "short" if direction == "SHORT" else ""

        try:
            payload = self.client.get_position_history(symbol=symbol, limit=20)
        except Exception as exc:
            self.log.warning("EXCHANGE_CLOSE_TRUTH_POSITION_HISTORY_FAILED | %s | error=%s", symbol, exc)
            return {"close_source": "bitget_position_history_error"}

        data = payload.get("data") if isinstance(payload, dict) else None
        rows = []
        if isinstance(data, dict):
            raw_rows = data.get("list") or data.get("positions") or data.get("data") or []
            if isinstance(raw_rows, list):
                rows = raw_rows
        elif isinstance(data, list):
            rows = data

        if not rows:
            return {"close_source": "bitget_position_history_empty"}

        def row_time(row: dict) -> int:
            for key in ("utime", "ctime", "updatedTime", "createdTime", "closeTime"):
                try:
                    value = int(float(row.get(key) or 0))
                except (TypeError, ValueError):
                    value = 0
                if value > 0:
                    return value
            return 0

        candidates = []
        for row in rows:
            row_symbol = str(row.get("symbol") or row.get("instId") or "").upper()
            row_hold_side = str(row.get("holdSide") or row.get("posSide") or row.get("side") or "").lower()
            if row_symbol and row_symbol != symbol:
                continue
            if hold_side and row_hold_side and row_hold_side != hold_side:
                continue
            candidates.append(row)

        if not candidates:
            return {"close_source": "bitget_position_history_no_match"}

        selected = sorted(candidates, key=row_time, reverse=True)[0]

        def pick_float(row: dict, keys: tuple[str, ...], default: float = 0.0) -> float:
            for key in keys:
                value = row.get(key)
                if value not in (None, ""):
                    return self._safe_float(value, default)
            return default

        net_profit = pick_float(selected, ("netProfit", "net_profit", "realizedPnl", "realizedPNL", "realizedPnlAfterFee", "achievedProfits", "totalProfits", "profit"), 0.0)
        pnl = pick_float(selected, ("pnl", "positionPnl", "positionPNL", "grossPnl", "grossPNL", "realizedPnl", "realizedPNL"), net_profit)
        open_fee = abs(pick_float(selected, ("openFee", "open_fee", "openingFee", "entryFee"), 0.0))
        close_fee = abs(pick_float(selected, ("closeFee", "close_fee", "closingFee", "exitFee"), 0.0))
        total_fee = open_fee + close_fee
        exit_price = self._safe_float(
            selected.get("closeAvgPrice")
            or selected.get("closePrice")
            or selected.get("averageClosePrice")
            or selected.get("avgClosePrice"),
            0.0,
        )
        size = self._safe_float(
            selected.get("closeTotalPos")
            or selected.get("closeSize")
            or selected.get("total")
            or selected.get("size"),
            0.0,
        )

        self.log.warning(
            "EXCHANGE_CLOSE_TRUTH_SELECTED | %s | source=bitget_position_history | exit=%s | size=%s | net_profit=%s | pnl=%s | fee=%s | raw_time=%s | keys=%s",
            symbol,
            exit_price,
            size,
            net_profit,
            pnl,
            total_fee,
            row_time(selected),
            sorted(selected.keys()),
        )

        return {
            "close_source": "bitget_position_history",
            "order_id": selected.get("orderId") or selected.get("id") or "",
            "exit_price": exit_price,
            "size": size,
            "pnl": net_profit,
            "gross_pnl": pnl,
            "fee": total_fee,
            "open_fee": open_fee,
            "close_fee": close_fee,
            "raw": selected,
        }

    def _exchange_close_truth_from_order_history(self, position: dict) -> dict:
        symbol = str(position.get("symbol") or "").upper()
        direction = str(position.get("direction") or "").upper()
        if not symbol:
            return {"close_source": "exchange_position_closed_sync"}

        try:
            payload = self.client.get_order_history(symbol=symbol, limit=30)
        except Exception as exc:
            self.log.warning(
                "EXCHANGE_CLOSE_TRUTH_HISTORY_FAILED | %s | error=%s",
                symbol,
                exc,
            )
            return {"close_source": "exchange_position_closed_sync_history_failed"}

        orders = payload.get("data") or []
        if isinstance(orders, dict):
            orders = orders.get("entrustedList") or orders.get("orderList") or orders.get("list") or []
        if not isinstance(orders, list):
            return {"close_source": "exchange_position_closed_sync_no_history"}

        close_candidates = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            order_symbol = str(order.get("symbol") or "").upper()
            if order_symbol and order_symbol != symbol:
                continue

            trade_side = str(order.get("tradeSide") or order.get("reduceOnly") or "").lower()
            side = str(order.get("side") or "").lower()
            hold_side = str(order.get("holdSide") or "").lower()
            state = str(order.get("state") or order.get("status") or "").lower()

            looks_closed = (
                trade_side == "close"
                or str(order.get("reduceOnly") or "").upper() == "YES"
                or "close" in trade_side
            )
            if not looks_closed:
                if direction == "LONG" and side == "sell" and hold_side in {"long", ""}:
                    looks_closed = True
                elif direction == "SHORT" and side == "buy" and hold_side in {"short", ""}:
                    looks_closed = True

            if not looks_closed:
                continue
            if state and state not in {"filled", "full-fill", "full_fill", "success", "closed", "done"}:
                continue

            close_candidates.append(order)

        if not close_candidates:
            return {"close_source": "exchange_position_closed_sync_no_close_order"}

        def order_timestamp(order: dict) -> int:
            for key in ("uTime", "cTime", "updatedTime", "createdTime", "ctime", "utime"):
                try:
                    return int(float(order.get(key) or 0))
                except (TypeError, ValueError):
                    continue
            return 0

        close_candidates.sort(key=order_timestamp, reverse=True)
        order = close_candidates[0]
        metrics = self.client.extract_fill_metrics({"data": order})
        metrics["close_source"] = "bitget_order_history"

        self.log.warning(
            "EXCHANGE_CLOSE_TRUTH_SELECTED | %s | order_id=%s | exit=%s | size=%s | pnl=%s | fee=%s | state=%s",
            symbol,
            metrics.get("order_id"),
            metrics.get("avg_price"),
            metrics.get("filled_qty"),
            metrics.get("pnl"),
            metrics.get("fee"),
            metrics.get("state"),
        )

        raw_order = metrics.get("raw") if isinstance(metrics.get("raw"), dict) else order

        pnl_keys = (
            "pnl",
            "profit",
            "realizedPnl",
            "realisedPnl",
            "totalProfits",
            "totalProfit",
            "netProfit",
            "netPnl",
            "closedPnl",
            "closePnl",
            "posPnl",
        )

        realized_pnl = None
        for key in pnl_keys:
            value = raw_order.get(key)
            if value not in (None, ""):
                try:
                    realized_pnl = float(value)
                    break
                except Exception:
                    pass

        fee_keys = (
            "fee",
            "fees",
            "transactionFee",
            "tradeFee",
            "totalFee",
            "deductedFee",
        )

        realized_fee = None
        for key in fee_keys:
            value = raw_order.get(key)
            if value not in (None, ""):
                try:
                    realized_fee = float(value)
                    break
                except Exception:
                    pass

        if realized_pnl is None:
            self.log.warning(
                "EXCHANGE_CLOSE_TRUTH_PNL_UNAVAILABLE | %s | order_id=%s",
                symbol,
                metrics.get("order_id"),
            )

        return {
            "close_source": metrics.get("close_source"),
            "order_id": metrics.get("order_id", ""),
            "exit_price": float(metrics.get("avg_price") or 0.0),
            "size": float(metrics.get("filled_qty") or 0.0),
            "pnl": realized_pnl if realized_pnl is not None else "",
            "fee": realized_fee if realized_fee is not None else "",
            "raw": metrics.get("raw", {}),
        }

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
