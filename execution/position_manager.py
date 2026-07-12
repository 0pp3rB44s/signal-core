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
from execution.closed_trade_writer import ClosedTradeWriterMixin
from execution.position_reconciler import PositionReconcilerMixin
from execution.tp_sl_lifecycle import TpSlLifecycleMixin
from telemetry.trade_logger import LiveTradeJournalLogger


class PositionManager(ClosedTradeWriterMixin, PositionReconcilerMixin, TpSlLifecycleMixin):
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
                    # netProfit uit de Bitget-positiehistorie is al netto (na
                    # fees). Zonder deze regel houdt het record voor eeuwig de
                    # open-time placeholder net_pnl (= -entry fee) vast en leert
                    # elke consument van executed_trades op fees-only cijfers.
                    position["net_pnl"] = float(exchange_close_truth.get("pnl") or 0.0)
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

            # Reconcile avg_entry to the REAL average fill. It was historically the
            # planned ladder average, not the actual fill, so the fee-adjusted
            # break-even (computed from avg_entry) could land BELOW the true
            # break-even -> a "protected" stop-out still booked a loss. Prefer
            # exchange truth (openPriceAvg), then the recorded actual fill.
            real_entry = 0.0
            if live_position:
                for _k in ("openPriceAvg", "averageOpenPrice", "avgOpenPrice", "openAvgPrice", "openPrice"):
                    real_entry = self._safe_float(live_position.get(_k), 0.0)
                    if real_entry > 0:
                        break
            if real_entry <= 0:
                real_entry = self._safe_float(position.get("actual_entry"), 0.0)
            recorded_avg = self._safe_float(position.get("avg_entry"), 0.0)
            if real_entry > 0 and recorded_avg > 0 and abs(real_entry - recorded_avg) / recorded_avg > 0.0005:
                self.log.warning(
                    "AVG_ENTRY_RECONCILED | %s | recorded=%s -> real_fill=%s | re-arming BE to true break-even",
                    symbol, recorded_avg, real_entry,
                )
                position["avg_entry"] = round(real_entry, 8)
                # Let the BE protection re-evaluate against the corrected entry so
                # a stop parked below the true break-even gets raised (raise-only,
                # guarded by be_is_tighter downstream).
                position["profit_lock_active"] = False

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
                    # Lock at least at the fee-adjusted break-even, not the older
                    # standalone near_tp_be_fee_buffer_pct (0.08%) which sits BELOW
                    # the ~0.12% roundtrip fee -> a "protected" stop-out still books
                    # a net loss. Floor at _fee_adjusted_break_even so a near-TP
                    # lock is flat-to-green like every other BE path.
                    fee_be = self._fee_adjusted_break_even(direction, entry)
                    if direction == "LONG":
                        protective_stop = round(max(entry * (1.0 + self.near_tp_be_fee_buffer_pct / 100.0), fee_be), 8)
                        should_move_stop = protective_stop > stop and protective_stop < current_price
                    else:
                        protective_stop = round(min(entry * (1.0 - self.near_tp_be_fee_buffer_pct / 100.0), fee_be), 8)
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

            # Profit-lock (P1.1A): between entry and ~82% of TP1 there used to be
            # zero profit protection; live data (2026-07-07, 15 closes) shows the
            # median trade peaks at 50-64% of TP1 with near-zero MAE and then
            # reverses into a loss. Once MFE covers the configured fraction of
            # the TP1 distance, lock the trade at fee-adjusted break-even.
            profit_lock_fraction = float(getattr(self.settings, "profit_lock_tp1_fraction", 0.60) or 0.0)
            # NOTE: intentionally NOT gated on `not break_even_active`. If an
            # earlier protection (e.g. NEAR_TP) set the BE flag with a stop below
            # the fee-adjusted break-even, this still raises the stop up to it.
            # The be_is_tighter check below guarantees it only ever tightens, so
            # it can never loosen a legitimately tighter stop.
            if (
                profit_lock_fraction > 0
                and tps
                and entry > 0
                and position.get("status") == "OPEN"
                and not position.get("tp1_hit")
                and not position.get("profit_lock_active")
            ):
                tp1_distance_pct = abs(float(tps[0]) - entry) / entry * 100.0
                mfe_pct = float(position.get("max_favorable_excursion_pct") or 0.0)
                if tp1_distance_pct > 0 and mfe_pct >= profit_lock_fraction * tp1_distance_pct:
                    fee_adjusted_be = self._fee_adjusted_break_even(direction, entry)
                    current_stop_value = float(position.get("stop_loss") or 0.0)
                    be_is_tighter = (
                        fee_adjusted_be > current_stop_value
                        if direction == "LONG"
                        else (fee_adjusted_be < current_stop_value or current_stop_value <= 0)
                    )
                    # Bitget weigert een stop aan de verkeerde kant van de
                    # mark-prijs (code 40917). Zodra de prijs terugzakt onder de
                    # fee-adjusted BE is de move onmogelijk; de MFE-trigger
                    # blijft echter waar (high-water mark), dus zonder deze
                    # check herhaalt de bot de gedoemde API-call elke cyclus
                    # (197 errors op één BNB-nacht). Wacht stil tot de prijs
                    # weer boven BE staat; 5bps marge tegen mark/last-verschil.
                    be_is_placeable = (
                        fee_adjusted_be < current_price * (1 - 0.0005)
                        if direction == "LONG"
                        else fee_adjusted_be > current_price * (1 + 0.0005)
                    )
                    if be_is_tighter and not be_is_placeable:
                        note_parts.append(
                            f"PROFIT_LOCK_BE_WAITING stop={fee_adjusted_be:.8f} px={current_price:.8f}"
                        )
                    elif be_is_tighter and self._protect_after_tp_fill(
                        position=position,
                        target_stop=fee_adjusted_be,
                        reason="PROFIT_LOCK_BE",
                        note_parts=note_parts,
                    ):
                        position["profit_lock_active"] = True
                        position["profit_lock_mfe_pct"] = round(mfe_pct, 4)
                        self.log.warning(
                            "PROFIT_LOCK_BE_ARMED | %s | mfe_pct=%.4f | tp1_distance_pct=%.4f | fraction=%.2f | new_stop=%s",
                            symbol,
                            mfe_pct,
                            tp1_distance_pct,
                            profit_lock_fraction,
                            fee_adjusted_be,
                        )

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

            # A failed tighten leaves a pending intent that retries every cycle
            # until the exchange accepts it. Without this, retries only happened
            # when the detection conditions happened to re-align — observed live
            # as a 28-minute unprotected gap on FILUSDT (2026-07-07).
            if (
                position.get("failed_continuation_tighten_pending")
                and not position.get("failed_continuation_protection_active")
            ):
                pending_stop = self._failed_continuation_target_stop(direction, entry, current_price)
                pending_current_stop = float(position.get("stop_loss") or 0.0)
                pending_tighter = (
                    (direction == "LONG" and pending_stop > pending_current_stop)
                    or (direction == "SHORT" and (pending_current_stop <= 0 or pending_stop < pending_current_stop))
                )
                if pending_tighter and self._move_exchange_stop_loss_with_retries(
                    position, pending_stop, "FAILED_CONTINUATION_PROTECTION_RETRY"
                ):
                    position["stop_loss"] = pending_stop
                    position["exchange_stop_loss"] = pending_stop
                    position["break_even_active"] = True
                    position["tp1_locked_stop_active"] = True
                    position["failed_continuation_protection_active"] = True
                    position["failed_continuation_tighten_pending"] = False
                    position["last_sl_move_reason"] = "FAILED_CONTINUATION_PROTECTION"
                    position["old_stop_loss_removed"] = True
                    note_parts.append(f"FAILED_CONTINUATION_SL_TIGHTENED_RETRY @ {pending_stop:.8f}")
                    self.log.warning(
                        "FAILED_CONTINUATION_SL_TIGHTENED_RETRY | %s | new_stop=%s",
                        symbol,
                        pending_stop,
                    )

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
                    position["failed_continuation_tighten_pending"] = False
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
                    position["failed_continuation_tighten_pending"] = True
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
            # Dead-trade timeout (P0.5): a flat trade past its window occupies
            # one of the 4 slots a fresh setup could use. Fail-safe conditions:
            # only with verified live exchange state, never on trades that hit
            # TP1 or are meaningfully in profit/loss (protections manage those).
            if position.get("status") == "OPEN":
                dead_timeout_minutes = float(
                    getattr(self.settings, "dead_trade_timeout_reclaim_minutes", 90.0)
                    if "reclaim" in str(position.get("strategy") or "").lower()
                    else getattr(self.settings, "dead_trade_timeout_default_minutes", 240.0)
                    or 0.0
                )
                dead_max_abs_pnl = float(getattr(self.settings, "dead_trade_max_abs_pnl_pct", 0.20) or 0.0)
                position_age_minutes = self._position_age_minutes(position)
                if (
                    dead_timeout_minutes > 0
                    and position_age_minutes >= dead_timeout_minutes
                    and not position.get("tp1_hit")
                    and abs(pnl_pct) < dead_max_abs_pnl
                    and bitget_sync_ok
                    and symbol in bitget_open_symbols
                    and live_size > 0
                ):
                    try:
                        dead_close_result = self.client.close_futures_position_full(
                            symbol=symbol,
                            direction=direction,
                            reason="dead_trade_timeout",
                            cleanup_tpsl=True,
                        )
                    except Exception as exc:
                        dead_close_result = {"status": "CLOSE_FAILED", "error": str(exc)}
                    if str(dead_close_result.get("status") or "").upper() not in {"CLOSE_FAILED"}:
                        position["dead_trade_close_result"] = dead_close_result
                        position["remaining_size_pct"] = 0.0
                        position["status"] = "CLOSED"
                        position["closed_reason"] = "dead_trade_timeout"
                        position["closed_at"] = datetime.now(timezone.utc).isoformat()
                        position["stale_tpsl_cleanup_done"] = True
                        note_parts.append(
                            f"DEAD_TRADE_TIMEOUT close after {position_age_minutes:.0f}min flat"
                        )
                        self.log.warning(
                            "DEAD_TRADE_TIMEOUT_CLOSED | %s | strategy=%s | age_min=%.0f | pnl_pct=%.4f | timeout_min=%.0f",
                            symbol,
                            position.get("strategy"),
                            position_age_minutes,
                            pnl_pct,
                            dead_timeout_minutes,
                        )
                        self._sync_journal_close(symbol, "dead_trade_timeout", pnl_pct)
                        self._append_closed_trade_dataset_row(
                            position=position,
                            close_reason="dead_trade_timeout",
                            exit_price=current_price,
                            pnl_pct=pnl_pct,
                            extra={"close_source": "dead_trade_timeout", "age_minutes": round(position_age_minutes, 1)},
                        )
                        self._register_symbol_cooldown(symbol, "dead_trade_timeout", pnl_pct)
                    else:
                        self.log.error(
                            "DEAD_TRADE_TIMEOUT_CLOSE_FAILED | %s | result=%s",
                            symbol,
                            dead_close_result,
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


    @staticmethod
    def _safe_float(value, default: float = 0.0) -> float:
        try:
            if value in (None, ""):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

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

