"""Exchange-truth reconciliation for PositionManager (extracted, behavior-neutral).

Mixin: recovery of untracked exchange positions, live-payload parsing and
close-truth resolution from Bitget position/order history. Methods are moved
verbatim from position_manager.py.
"""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path


class PositionReconcilerMixin:
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
