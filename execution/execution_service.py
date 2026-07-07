from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.equity import resolve_account_equity
from clients.bitget_rest import BitgetRestClient
from clients.schemas import ExecutionReport, TradePlan
from execution.state_store import JsonStateStore
from risk.cooldown_manager import SymbolCooldownManager
from telemetry.trade_logger import LiveTradeJournalLogger, TradeDecisionSnapshotLogger


# --- Analytics helpers ---
def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _deep_get(payload: dict | None, *keys: str):
    if not isinstance(payload, dict):
        return None
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


class ExecutionService:
    # Regime diversification cap: with MAX_OPEN_POSITIONS total slots, no
    # single strategy may hold more than this many at once.
    MAX_OPEN_POSITIONS_PER_STRATEGY = 2

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = logging.getLogger(self.__class__.__name__)
        self.store = JsonStateStore("state/executed_trades.json")
        self.event_store = JsonStateStore("state/execution_events.json")
        self.cooldown_store = JsonStateStore("state/symbol_cooldowns.json")
        self.cooldowns = SymbolCooldownManager(self.cooldown_store)
        self.client = BitgetRestClient(settings=settings)
        self.journal = LiveTradeJournalLogger()
        self.decision_snapshot_logger = TradeDecisionSnapshotLogger()
        self.symbol_cooldown_minutes = int(getattr(settings, "symbol_cooldown_minutes", 30))

    def execute(self, plans: list[TradePlan]) -> list[ExecutionReport]:
        if not self.settings.execution_enabled:
            return []

        reports: list[ExecutionReport] = []
        existing = self.store.load(default=[])
        execution_events = self.event_store.load(default=[])
        pruned_cooldowns = self.cooldowns.prune_expired()
        if pruned_cooldowns:
            self.log.info("SYMBOL_COOLDOWNS_PRUNED | count=%s", pruned_cooldowns)

        local_open_symbols = {
            row.get("symbol")
            for row in existing
            if row.get("status") == "OPEN" and row.get("symbol")
        }
        open_symbols = set(local_open_symbols)

        # Bitget is the source of truth for LIVE exposure.
        # Local state is only memory/logging and must not block new trades after positions are closed on Bitget.
        try:
            bitget_positions_payload = self.client.get_all_positions()
            bitget_positions = bitget_positions_payload.get("data") or []
            bitget_open_symbols = {
                str(position.get("symbol") or "")
                for position in bitget_positions
                if float(position.get("total") or position.get("size") or position.get("available") or 0) > 0
            }

            if self.settings.execution_mode.upper() == "LIVE":
                if local_open_symbols != bitget_open_symbols:
                    self.log.info(
                        "Syncing local open symbols with Bitget truth: local=%s bitget=%s",
                        sorted(local_open_symbols),
                        sorted(bitget_open_symbols),
                    )
                for row in existing:
                    symbol = row.get("symbol")
                    if row.get("status") == "OPEN" and symbol and symbol not in bitget_open_symbols:
                        row["status"] = "CLOSED_SYNCED"
                        row["closed_at"] = datetime.now(timezone.utc).isoformat()
                        row["sync_reason"] = "closed on Bitget; local state synced"
                open_symbols = set(bitget_open_symbols)
            else:
                open_symbols = local_open_symbols.union(bitget_open_symbols)
        except Exception as exc:
            self.log.warning("Bitget position sync failed; using local state fallback: %s", exc)
            open_symbols = set(local_open_symbols)

        max_open_positions = int(self.settings.max_open_positions)
        hard_cap_notional = resolve_account_equity(self.settings)[0] * float(self.settings.max_leverage)

        executable: list[TradePlan] = []
        seen_symbols: set[str] = set()
        for plan in plans:
            if plan.verdict != "EXECUTABLE":
                continue
            if plan.symbol in seen_symbols:
                continue
            seen_symbols.add(plan.symbol)
            executable.append(plan)
            if len(executable) >= self.settings.execution_max_per_cycle:
                break

        for plan in executable:
            # --- Telemetry: log trade decision snapshot before execution logic ---
            decision_snapshot_opened_at = self.decision_snapshot_logger.append_plan(plan)
            if self.settings.execution_mode.upper() == "LIVE":
                try:
                    live_positions_payload = self.client.get_all_positions()
                    live_positions = live_positions_payload.get("data") or []
                    live_open_symbols = {
                        str(position.get("symbol") or "")
                        for position in live_positions
                        if float(position.get("total") or position.get("size") or position.get("available") or position.get("holdVol") or position.get("positionSize") or 0) > 0
                    }
                    open_symbols = set(live_open_symbols)
                    if len(open_symbols) >= int(self.settings.max_open_positions):
                        self.log.critical(
                            "LIVE_ENTRY_BLOCKED_MAX_POSITIONS_EXCHANGE_TRUTH | symbol=%s | open=%s/%s | live_symbols=%s",
                            plan.symbol,
                            len(open_symbols),
                            self.settings.max_open_positions,
                            sorted(open_symbols),
                        )
                        reports.append(
                            self._report(
                                plan=plan,
                                status="SKIPPED",
                                message=f"exchange max open positions reached: {len(open_symbols)}/{self.settings.max_open_positions}",
                                avg_entry=round(sum(plan.entry_prices) / len(plan.entry_prices), 8),
                                notional=min(plan.position_notional_usdt, hard_cap_notional),
                                leverage=plan.leverage,
                            )
                        )
                        continue
                except Exception as exc:
                    self.log.critical(
                        "LIVE_ENTRY_BLOCKED_POSITION_SYNC_FAILED | symbol=%s | error=%s",
                        plan.symbol,
                        exc,
                    )
                    reports.append(
                        self._report(
                            plan=plan,
                            status="SKIPPED",
                            message=f"live order blocked: exchange position sync failed: {exc}",
                            avg_entry=round(sum(plan.entry_prices) / len(plan.entry_prices), 8),
                            notional=min(plan.position_notional_usdt, hard_cap_notional),
                            leverage=plan.leverage,
                        )
                    )
                    continue
            avg_entry = round(sum(plan.entry_prices) / len(plan.entry_prices), 8)
            expected_entry = avg_entry
            actual_entry = avg_entry
            slippage_pct = 0.0
            fees_paid = 0.0
            realized_pnl = 0.0
            exchange_order_id = ""

            plan_strategy_raw = str(plan.strategy or "").strip()
            if plan_strategy_raw.lower() in {"", "unknown", "none", "null", "na", "n/a"}:
                self.log.warning(
                    "EXECUTION_UNKNOWN_STRATEGY_NORMALIZED | %s | original_strategy=%s | fallback=execution_unknown_strategy",
                    plan.symbol,
                    plan.strategy,
                )
                plan.strategy = "execution_unknown_strategy"

            # HYBRID SAFE MODE: live execution gate.
            # Allowed: liquidity sweep reversals + momentum breakout/breakdown + strict trend-continuation entries + low_vol_reclaim/reclaim.
            # Blocked: all unsupported strategies. When ENABLED_STRATEGIES is set
            # in .env it is the explicit allow-list (same rule as risk_manager).
            strategy_name = str(plan.strategy or "").lower()
            is_sweep = "sweep" in strategy_name
            is_momentum = "momentum" in strategy_name or "breakout" in strategy_name or "breakdown" in strategy_name
            is_continuation = "continuation" in strategy_name
            is_low_vol_reclaim = "low_vol_reclaim" in strategy_name or "reclaim" in strategy_name
            enabled_set = self.settings.enabled_strategy_set
            env_allowed = (not enabled_set) or any(name in strategy_name for name in enabled_set)
            if (not is_sweep and not is_momentum and not is_continuation and not is_low_vol_reclaim) or not env_allowed:
                reports.append(
                    self._report(
                        plan=plan,
                        status="SKIPPED",
                        message=f"hybrid gate blocked unsupported strategy: {plan.strategy}",
                        avg_entry=avg_entry,
                        notional=min(plan.position_notional_usdt, hard_cap_notional),
                        leverage=plan.leverage,
                    )
                )
                continue

            if plan.symbol in open_symbols:
                reports.append(
                    self._report(
                        plan=plan,
                        status="SKIPPED",
                        message="position already open for symbol",
                        avg_entry=avg_entry,
                        notional=min(plan.position_notional_usdt, hard_cap_notional),
                        leverage=plan.leverage,
                    )
                )
                continue

            # Regime diversification: one strategy may never occupy the whole
            # book again (low_vol_reclaim previously monopolised both slots and
            # starved the other regimes out of 1000+ executable plans).
            open_for_strategy = sum(
                1
                for row in existing
                if row.get("status") == "OPEN"
                and str(row.get("strategy") or "").lower() == str(plan.strategy or "").lower()
                and row.get("symbol") in open_symbols
            )
            if open_for_strategy >= self.MAX_OPEN_POSITIONS_PER_STRATEGY:
                reports.append(
                    self._report(
                        plan=plan,
                        status="SKIPPED",
                        message=f"max open positions for strategy reached: {open_for_strategy}/{self.MAX_OPEN_POSITIONS_PER_STRATEGY} ({plan.strategy})",
                        avg_entry=avg_entry,
                        notional=min(plan.position_notional_usdt, hard_cap_notional),
                        leverage=plan.leverage,
                    )
                )
                continue

            cooldown_status = self.cooldowns.get(plan.symbol)
            cooldown_active = cooldown_status.active
            cooldown_message = (
                f"symbol cooldown active: {cooldown_status.reason} | remaining={cooldown_status.remaining_minutes}m | until={cooldown_status.until}"
                if cooldown_active
                else ""
            )
            if cooldown_active:
                self.log.info(
                    "SYMBOL_COOLDOWN_ACTIVE | %s | reason=%s | remaining_minutes=%s | until=%s",
                    cooldown_status.symbol,
                    cooldown_status.reason,
                    cooldown_status.remaining_minutes,
                    cooldown_status.until,
                )
                reports.append(
                    self._report(
                        plan=plan,
                        status="SKIPPED",
                        message=cooldown_message,
                        avg_entry=avg_entry,
                        notional=min(plan.position_notional_usdt, hard_cap_notional),
                        leverage=plan.leverage,
                    )
                )
                continue

            if len(open_symbols) >= max_open_positions:
                reports.append(
                    self._report(
                        plan=plan,
                        status="SKIPPED",
                        message=f"max open positions reached: {len(open_symbols)}/{max_open_positions}",
                        avg_entry=avg_entry,
                        notional=min(plan.position_notional_usdt, hard_cap_notional),
                        leverage=plan.leverage,
                    )
                )
                continue

            if plan.position_notional_usdt > hard_cap_notional:
                reports.append(
                    self._report(
                        plan=plan,
                        status="SKIPPED",
                        message=f"hard cap exceeded: notional {plan.position_notional_usdt:.2f} > cap {hard_cap_notional:.2f}",
                        avg_entry=avg_entry,
                        notional=plan.position_notional_usdt,
                        leverage=plan.leverage,
                    )
                )
                continue

            if self.settings.execution_require_confirmation and plan.symbol not in self.settings.execution_confirm_symbol_set:
                reports.append(
                    self._report(
                        plan=plan,
                        status="SKIPPED",
                        message="confirmation missing for symbol",
                        avg_entry=avg_entry,
                        notional=plan.position_notional_usdt,
                        leverage=plan.leverage,
                    )
                )
                continue

            self.log.info(
                "EXECUTABLE_TRADE_CAPS | %s | strategy=%s | notional=%.2f | hard_cap_notional=%.2f | leverage=%.2f",
                plan.symbol,
                plan.strategy,
                min(plan.position_notional_usdt, hard_cap_notional),
                hard_cap_notional,
                plan.leverage,
            )
            live_order_payload = None
            live_order_id = None
            leverage_payload = None
            protection_payload = None
            execution_status = "SIMULATED"
            execution_message = "position stored in state"
            protection_verified = False
            protection_integrity = "NOT_REQUIRED_SIMULATED" if self.settings.execution_mode.upper() != "LIVE" else "PENDING"
            exchange_stop_loss = None
            exchange_take_profit_count = 0
            effective_leverage = plan.leverage

            if self.settings.execution_mode.upper() == "LIVE":
                side = "buy" if plan.direction.upper() == "LONG" else "sell"
                close_side = "sell" if plan.direction.upper() == "LONG" else "buy"
                trade_side = "open"
                hold_side = "long" if plan.direction.upper() == "LONG" else "short"
                default_leverage = float(getattr(self.settings, "default_leverage", 5.0) or 5.0)
                effective_leverage = min(float(plan.leverage), default_leverage, float(self.settings.max_leverage))

                # Conservative live notional cap for small-account protection.
                # Prevent repeated Bitget 40762 "order amount exceeds balance" failures before order-send.
                account_equity, _equity_source = resolve_account_equity(self.settings)
                configured_notional_cap = float(
                    getattr(
                        self.settings,
                        "execution_max_live_notional_per_trade_usdt",
                        0.0,
                    )
                    or 0.0
                )
                fallback_notional_cap = min(50.0, max(10.0, account_equity * 0.75)) if account_equity > 0 else 25.0
                max_live_notional = configured_notional_cap if configured_notional_cap > 0 else fallback_notional_cap
                requested_notional = float(plan.position_notional_usdt or 0.0)
                live_notional = min(requested_notional, hard_cap_notional, max_live_notional)
                min_live_notional_usdt = float(
                    getattr(self.settings, "execution_min_live_notional_usdt", 5.0) or 5.0
                )

                if live_notional <= 0 or live_notional < min_live_notional_usdt:
                    self.log.warning(
                        "BALANCE_PRECHECK_BLOCKED | %s | reason=notional_below_min | requested=%.2f | capped=%.2f | min=%.2f | equity=%.2f",
                        plan.symbol,
                        requested_notional,
                        live_notional,
                        min_live_notional_usdt,
                        account_equity,
                    )
                    reports.append(
                        self._report(
                            plan=plan,
                            status="SKIPPED",
                            message=f"balance precheck blocked: capped notional {live_notional:.2f} below min {min_live_notional_usdt:.2f}",
                            avg_entry=avg_entry,
                            notional=live_notional,
                            leverage=effective_leverage,
                        )
                    )
                    continue

                if requested_notional > live_notional:
                    self.log.warning(
                        "BALANCE_PRECHECK_NOTIONAL_CAPPED | %s | requested=%.2f | capped=%.2f | equity=%.2f | leverage=%s",
                        plan.symbol,
                        requested_notional,
                        live_notional,
                        account_equity,
                        effective_leverage,
                    )

                raw_order_size = live_notional / avg_entry
                order_size = self._format_order_size_for_exchange(plan.symbol, raw_order_size)
                if float(plan.leverage) != effective_leverage:
                    self.log.warning(
                        "LEVERAGE_CAPPED | %s | direction=%s | requested=%sx | effective=%sx | default_cap=%sx | max_cap=%sx",
                        plan.symbol,
                        plan.direction,
                        plan.leverage,
                        effective_leverage,
                        default_leverage,
                        self.settings.max_leverage,
                    )

                valid_take_profits = [
                    float(tp.get("price") or tp.get("trigger_price") or tp.get("triggerPrice") or 0)
                    if isinstance(tp, dict)
                    else float(tp or 0)
                    for tp in (plan.take_profits or [])
                ]
                valid_take_profits = [tp for tp in valid_take_profits if tp > 0]

                if plan.stop_loss <= 0 or not valid_take_profits:
                    self.log.critical(
                        "LIVE_ENTRY_BLOCKED_MISSING_PROTECTION | %s | direction=%s | stop_loss=%s | take_profits=%s",
                        plan.symbol,
                        plan.direction,
                        plan.stop_loss,
                        plan.take_profits,
                    )
                    reports.append(
                        self._report(
                            plan=plan,
                            status="SKIPPED",
                            message="live order blocked: invalid or missing SL/TP",
                            avg_entry=avg_entry,
                            notional=live_notional,
                            leverage=effective_leverage,
                        )
                    )
                    continue

                if order_size <= 0:
                    self.log.error(
                        "ORDER_SIZE_INVALID | %s | raw_size=%s | formatted_size=%s | notional=%s | avg_entry=%s",
                        plan.symbol,
                        raw_order_size,
                        order_size,
                        plan.position_notional_usdt,
                        avg_entry,
                    )
                    reports.append(
                        self._report(
                            plan=plan,
                            status="SKIPPED",
                            message="live order blocked: invalid order size",
                            avg_entry=avg_entry,
                            notional=live_notional,
                            leverage=effective_leverage,
                        )
                    )
                    continue

                if not hasattr(self.client, "place_futures_protection_orders"):
                    reports.append(
                        self._report(
                            plan=plan,
                            status="SKIPPED",
                            message="live order blocked: exchange SL/TP protection is not implemented yet",
                            avg_entry=avg_entry,
                            notional=live_notional,
                            leverage=effective_leverage,
                        )
                    )
                    continue

                try:
                    leverage_payload = self.client.set_futures_leverage(
                        symbol=plan.symbol,
                        leverage=effective_leverage,
                        margin_mode="isolated",
                        hold_side=hold_side,
                    )

                    self.log.warning(
                        "LIVE_ENTRY_START | %s | direction=%s | side=%s | hold_side=%s | size=%s | notional=%.2f | requested_notional=%.2f | sl=%s | tp_count=%s",
                        plan.symbol,
                        plan.direction,
                        side,
                        hold_side,
                        order_size,
                        live_notional,
                        requested_notional,
                        plan.stop_loss,
                        len(plan.take_profits or []),
                    )

                    live_order_payload = self.client.place_futures_market_order(
                        symbol=plan.symbol,
                        size=order_size,
                        side=side,
                        trade_side=trade_side,
                        margin_mode="isolated",
                    )

                    live_order_id = self.client.extract_order_id(live_order_payload)

                    if not live_order_id:
                        raise RuntimeError(
                            f"LIVE_ENTRY_NO_ORDER_ID | {plan.symbol} | payload={live_order_payload}"
                        )

                    self.log.warning(
                        "LIVE_ENTRY_FILLED | %s | order_id=%s | side=%s | hold_side=%s | size=%s",
                        plan.symbol,
                        live_order_id,
                        side,
                        hold_side,
                        order_size,
                    )

                    verification_payload = self.client.get_all_positions()
                    verification_positions = verification_payload.get("data") or []

                    exchange_position_found = False

                    for position in verification_positions:
                        if str(position.get("symbol") or "") != plan.symbol:
                            continue

                        try:
                            live_size = float(
                                position.get("total")
                                or position.get("size")
                                or position.get("available")
                                or position.get("holdVol")
                                or position.get("positionSize")
                                or 0
                            )
                        except Exception:
                            live_size = 0.0

                        if live_size > 0:
                            exchange_position_found = True
                            break

                    if not exchange_position_found:
                        self.log.critical(
                            "FALSE_FILL_DETECTED | %s | order_id=%s | order acknowledged but no exchange position found",
                            plan.symbol,
                            live_order_id,
                        )
                        raise RuntimeError(
                            f"FALSE_FILL_DETECTED | {plan.symbol} | order acknowledged but no exchange position found"
                        )

                    self.log.warning(
                        "EXCHANGE_POSITION_CONFIRMED | %s | order_id=%s",
                        plan.symbol,
                        live_order_id,
                    )


                    # Place protection with stronger validation and retry.
                    protection_payload = None
                    has_sl = False
                    has_tp = False
                    entry_protection_verified = False

                    for protection_attempt in range(1, 4):
                        try:
                            protection_payload = self.client.place_futures_protection_orders(
                                symbol=plan.symbol,
                                direction=plan.direction,
                                hold_side=hold_side,
                                size=order_size,
                                stop_loss=plan.stop_loss,
                                take_profits=plan.take_profits,
                                margin_mode="isolated",
                            )
                        except Exception as protection_exc:
                            protection_payload = {
                                "status": "PROTECTION_PLACEMENT_EXCEPTION",
                                "error": str(protection_exc),
                                "attempt": protection_attempt,
                            }
                            self.log.critical(
                                "ENTRY_PROTECTION_PLACEMENT_EXCEPTION | %s | attempt=%s/3 | error=%s",
                                plan.symbol,
                                protection_attempt,
                                protection_exc,
                            )

                        has_sl = bool(protection_payload and protection_payload.get("stop_loss_verified"))
                        actual_tp_count = int(protection_payload.get("take_profit_count") or 0) if protection_payload else 0
                        expected_tp_count = int(
                            protection_payload.get("expected_take_profit_count") or len(valid_take_profits)
                        ) if protection_payload else len(valid_take_profits)
                        has_tp = actual_tp_count >= expected_tp_count and expected_tp_count > 0
                        entry_protection_verified = bool(
                            protection_payload and protection_payload.get("protection_verified")
                        )
                        protection_integrity = str(
                            protection_payload.get("protection_integrity") if protection_payload else "MISSING_PAYLOAD"
                        )

                        self.log.warning(
                            "ENTRY_PROTECTION_ATTEMPT | %s | attempt=%s/3 | has_sl=%s | has_tp=%s | tp_count=%s/%s | verified=%s | integrity=%s",
                            plan.symbol,
                            protection_attempt,
                            has_sl,
                            has_tp,
                            actual_tp_count,
                            expected_tp_count,
                            entry_protection_verified,
                            protection_integrity,
                        )

                        if has_sl and has_tp and entry_protection_verified:
                            break

                    if not has_sl or not has_tp or not entry_protection_verified:
                        self.log.critical(
                            "ENTRY_PROTECTION_VERIFY_FAILED | %s | order_id=%s | has_sl=%s | has_tp=%s | tp_count=%s/%s | protection_verified=%s | integrity=%s | payload=%s",
                            plan.symbol,
                            live_order_id,
                            has_sl,
                            has_tp,
                            actual_tp_count,
                            expected_tp_count,
                            entry_protection_verified,
                            protection_integrity,
                            protection_payload,
                        )

                        self.log.critical(
                            "UNPROTECTED_POSITION_DETECTED | %s | order_id=%s | invoking_fail_safe_close=True",
                            plan.symbol,
                            live_order_id,
                        )

                        self._fail_safe_close(
                            symbol=plan.symbol,
                            size=order_size,
                            close_side=close_side,
                            direction=plan.direction,
                            reason="entry_protection_failed",
                        )

                        self._verify_no_live_position_after_fail_safe(
                            symbol=plan.symbol,
                            direction=plan.direction,
                            reason="entry_protection_failed",
                        )

                        reports.append(
                            self._report(
                                plan=plan,
                                status="ERROR",
                                message="FAIL-SAFE TRIGGERED: SL/TP NOT VERIFIED -> emergency close invoked",
                                avg_entry=avg_entry,
                                notional=live_notional,
                                leverage=effective_leverage,
                            )
                        )
                        continue
                    self.log.warning(
                        "ENTRY_PROTECTION_CONFIRMED | %s | order_id=%s | verified=%s | integrity=%s | sl_verified=%s | tp_count=%s",
                        plan.symbol,
                        live_order_id,
                        entry_protection_verified,
                        protection_integrity,
                        bool(protection_payload.get("stop_loss_verified")) if protection_payload else False,
                        int(protection_payload.get("take_profit_count") or 0) if protection_payload else 0,
                    )

                    protection_verified = bool(protection_payload.get("protection_verified")) if protection_payload else False
                    protection_integrity = str(protection_payload.get("protection_integrity") or "UNKNOWN") if protection_payload else "MISSING_PAYLOAD"
                    exchange_stop_loss = protection_payload.get("stop_loss") if protection_payload else None
                    exchange_take_profit_count = int(protection_payload.get("take_profit_count") or len(protection_payload.get("take_profits") or [])) if protection_payload else 0
                    self.log.warning(
                        "ENTRY_PROTECTION_CONFIRMED | %s | direction=%s | stop_loss=%s | take_profits=%s | size=%s",
                        plan.symbol,
                        plan.direction,
                        exchange_stop_loss,
                        exchange_take_profit_count,
                        order_size,
                    )

                    exchange_order_id = str(live_order_id or "")
                    order_detail_payload = None
                    detailed_fill_metrics: dict[str, object] = {}

                    # NB: extract_fill_metrics levert de canonieke sleutels
                    # avg_price/fee/pnl/state (zoals de reconciler ze ook leest).
                    # Deze laag las jarenlang niet-bestaande aliassen, waardoor
                    # elke fill terugviel op het plan-gemiddelde en slippage
                    # altijd 0.0000 was (N8, roadmap 2026-07-07).
                    fill_metrics = self.client.extract_fill_metrics(live_order_payload)

                    extracted_actual_entry = _safe_float(fill_metrics.get("avg_price"), 0.0)
                    if extracted_actual_entry > 0:
                        actual_entry = round(extracted_actual_entry, 8)
                    else:
                        actual_entry = avg_entry

                    fees_paid = abs(_safe_float(fill_metrics.get("fee"), 0.0))
                    realized_pnl = _safe_float(fill_metrics.get("pnl"), 0.0)

                    if exchange_order_id:
                        try:
                            detailed_fill_metrics = {}
                            # Marktorders registreren hun fill soms pas een
                            # fractie later; probeer kort opnieuw tot er een
                            # echte fill-prijs staat.
                            for detail_attempt in range(3):
                                order_detail_payload = self.client.get_order_detail(
                                    symbol=plan.symbol,
                                    order_id=exchange_order_id,
                                )
                                detailed_fill_metrics = self.client.extract_fill_metrics(order_detail_payload)
                                if _safe_float(detailed_fill_metrics.get("avg_price"), 0.0) > 0:
                                    break
                                time.sleep(0.5)

                            detailed_actual_entry = _safe_float(
                                detailed_fill_metrics.get("avg_price"),
                                0.0,
                            )
                            if detailed_actual_entry > 0:
                                actual_entry = round(detailed_actual_entry, 8)
                                extracted_actual_entry = detailed_actual_entry

                            detailed_fees = abs(_safe_float(detailed_fill_metrics.get("fee"), 0.0))
                            if detailed_fees > 0:
                                fees_paid = detailed_fees

                            detailed_realized_pnl = _safe_float(
                                detailed_fill_metrics.get("pnl"),
                                0.0,
                            )
                            if detailed_realized_pnl != 0:
                                realized_pnl = detailed_realized_pnl

                            self.log.info(
                                "ORDER_DETAIL_ANALYTICS | %s | order_id=%s | actual_entry=%s | fees=%s | realized_pnl=%s | state=%s",
                                plan.symbol,
                                exchange_order_id,
                                actual_entry,
                                fees_paid,
                                realized_pnl,
                                detailed_fill_metrics.get("state"),
                            )
                        except Exception as detail_exc:
                            self.log.warning(
                                "ORDER_DETAIL_LOOKUP_FAILED | %s | order_id=%s | error=%s",
                                plan.symbol,
                                exchange_order_id,
                                detail_exc,
                            )

                    if expected_entry > 0 and actual_entry > 0:
                        if plan.direction.upper() == "LONG":
                            slippage_pct = round(((actual_entry - expected_entry) / expected_entry) * 100, 5)
                        else:
                            slippage_pct = round(((expected_entry - actual_entry) / expected_entry) * 100, 5)

                    if extracted_actual_entry <= 0:
                        self.log.warning(
                            "FILL_ANALYTICS_FALLBACK | %s | order_id=%s | reason=no_fill_price_in_order_payload | expected_entry=%s",
                            plan.symbol,
                            exchange_order_id,
                            expected_entry,
                        )
                    else:
                        self.log.info(
                            "FILL_ANALYTICS | %s | order_id=%s | expected=%s | actual=%s | slippage_pct=%s | fees=%s",
                            plan.symbol,
                            exchange_order_id,
                            expected_entry,
                            actual_entry,
                            slippage_pct,
                            fees_paid,
                        )

                    execution_status = "EXECUTED"
                    execution_message = f"live market order placed | size={order_size} | order_id={live_order_id}"

                except Exception as exc:
                    # --- Balance guard block for insufficient margin errors (Bitget 40762) ---
                    if (
                        not live_order_payload
                        and hasattr(self.client, "is_insufficient_balance_error")
                        and self.client.is_insufficient_balance_error(exc)
                    ):
                        self.log.warning(
                            "BALANCE_GUARD_BLOCKED | %s | error=%s",
                            plan.symbol,
                            exc,
                        )

                        reports.append(
                            self._report(
                                plan=plan,
                                status="SKIPPED",
                                message=f"balance guard blocked order: {exc}",
                                avg_entry=avg_entry,
                                notional=live_notional,
                                leverage=effective_leverage,
                            )
                        )
                        continue
                    if live_order_payload:
                        self.log.critical(
                            "LIVE_ENTRY_EXCEPTION_AFTER_ORDER | %s | order_id=%s | invoking_fail_safe_close=True | error=%s",
                            plan.symbol,
                            live_order_id,
                            exc,
                        )
                        self._fail_safe_close(
                            symbol=plan.symbol,
                            size=order_size,
                            close_side=close_side,
                            direction=plan.direction,
                            reason="entry_protection_failed",
                        )
                        self._verify_no_live_position_after_fail_safe(
                            symbol=plan.symbol,
                            direction=plan.direction,
                            reason="entry_protection_failed_exception",
                        )
                        reports.append(
                            self._report(
                                plan=plan,
                                status="ERROR",
                                message=f"FAIL-SAFE TRIGGERED: protection/order flow failed after entry -> position closed | error={exc}",
                                avg_entry=avg_entry,
                                notional=live_notional,
                                leverage=effective_leverage,
                            )
                        )
                    else:
                        reports.append(
                            self._report(
                                plan=plan,
                                status="SKIPPED",
                                message=f"live order failed before entry: {exc}",
                                avg_entry=avg_entry,
                                notional=live_notional,
                                leverage=effective_leverage,
                            )
                        )
                    continue

            position = {
                "symbol": plan.symbol,
                "strategy": plan.strategy,
                "direction": plan.direction,
                "mode": self.settings.execution_mode,
                "status": "OPEN",
                "avg_entry": avg_entry,
                "expected_entry": expected_entry,
                "actual_entry": actual_entry,
                "slippage_pct": slippage_pct,
                "fees_paid": fees_paid,
                "entry_prices": plan.entry_prices,
                "stop_loss": plan.stop_loss,
                "initial_stop_loss": plan.stop_loss,
                "take_profits": plan.take_profits,
                "tp1_hit": False,
                "tp2_hit": False,
                "tp3_hit": False,
                "break_even_active": False,
                "remaining_size_pct": 100.0,
                "position_notional_usdt": plan.position_notional_usdt,
                "leverage": effective_leverage,
                "requested_leverage": plan.leverage,
                "opened_at": datetime.now(timezone.utc).isoformat(),
                "live_order_id": live_order_id,
                "live_order_payload": live_order_payload,
                "order_detail_payload": order_detail_payload if self.settings.execution_mode.upper() == "LIVE" else None,
                "net_pnl": realized_pnl - fees_paid,
                "leverage_payload": leverage_payload if self.settings.execution_mode.upper() == "LIVE" else None,
                "protection_payload": protection_payload if self.settings.execution_mode.upper() == "LIVE" else None,
                "protection_verified": protection_verified,
                "protection_integrity": protection_integrity,
                "exchange_stop_loss": exchange_stop_loss,
                "exchange_take_profit_count": exchange_take_profit_count,
                "entry_protection_verified": protection_verified,
                "entry_protection_integrity": protection_integrity,
                "entry_stop_loss_verified": bool(protection_payload.get("stop_loss_verified")) if protection_payload else False,
                "entry_expected_take_profit_count": int(protection_payload.get("expected_take_profit_count") or 0) if protection_payload else 0,
                "last_price": avg_entry,
                "notes": plan.notes,
                "reasons": plan.reasons,
            }
            existing.append(position)
            open_symbols.add(plan.symbol)

            report = self._report(
                plan=plan,
                status=execution_status,
                message=execution_message,
                avg_entry=actual_entry or avg_entry,
                notional=live_notional,
                leverage=effective_leverage,
                expected_entry=expected_entry,
                actual_entry=actual_entry,
                slippage_pct=slippage_pct,
                fees_paid=fees_paid,
                realized_pnl=realized_pnl,
                exchange_order_id=exchange_order_id,
            )
            reports.append(report)

            if report.status == "EXECUTED":
                try:
                    self.journal.log_open(report)
                except Exception as exc:
                    self.log.warning("Live journal log_open failed for %s: %s", report.symbol, exc)

        for report in reports:
            event = asdict(report)
            event["timestamp"] = datetime.now(timezone.utc).isoformat()
            execution_events.append(event)

            if report.status == "EXECUTED":
                cooldown_status = self.cooldowns.set(
                    report.symbol,
                    minutes=self.symbol_cooldown_minutes,
                    reason="post_execution_lockout",
                )
                self.log.info(
                    "SYMBOL_COOLDOWN_SET | %s | reason=%s | minutes=%s | until=%s",
                    cooldown_status.symbol,
                    cooldown_status.reason,
                    self.symbol_cooldown_minutes,
                    cooldown_status.until,
                )

        execution_events = execution_events[-500:]
        self.store.save(existing)
        self.event_store.save(execution_events)
        return reports

    def _format_order_size_for_exchange(self, symbol: str, raw_size: float) -> float:
        try:
            if hasattr(self.client, "_format_size"):
                formatted = self.client._format_size(symbol, raw_size)
                formatted_float = float(formatted)
                self.log.info(
                    "ORDER_SIZE_FORMATTED | %s | raw_size=%s | formatted_size=%s | source=bitget_contract_precision",
                    symbol,
                    raw_size,
                    formatted,
                )
                return formatted_float
        except Exception as exc:
            self.log.warning(
                "ORDER_SIZE_FORMAT_FAILED | %s | raw_size=%s | fallback=round_6 | error=%s",
                symbol,
                raw_size,
                exc,
            )

        return round(float(raw_size or 0.0), 6)

    def _symbol_cooldown_active(self, symbol: str, execution_events: list[dict]) -> tuple[bool, str]:
        if self.symbol_cooldown_minutes <= 0:
            return False, ""

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=self.symbol_cooldown_minutes)

        for event in reversed(execution_events):
            if event.get("symbol") != symbol:
                continue
            if event.get("status") != "EXECUTED":
                continue

            raw_ts = event.get("timestamp")
            if not raw_ts:
                continue

            try:
                ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if ts >= cutoff:
                age_minutes = (datetime.now(timezone.utc) - ts).total_seconds() / 60
                remaining = max(0, self.symbol_cooldown_minutes - int(age_minutes))
                return True, f"symbol cooldown active: {symbol} ({remaining}m remaining)"

            break

        return False, ""

    def _fail_safe_close(
        self,
        *,
        symbol: str,
        size: float,
        close_side: str,
        direction: str = "",
        reason: str = "fail_safe_close",
    ) -> None:
        close_errors: list[str] = []

        try:
            response = self.client.place_futures_market_order(
                symbol=symbol,
                size=size,
                side=close_side,
                trade_side="close",
                margin_mode="isolated",
            )
            self.log.critical(
                "FAIL_SAFE_CLOSE_DIRECT_SENT | %s | close_side=%s | size=%s | reason=%s | response=%s",
                symbol,
                close_side,
                size,
                reason,
                response,
            )
            return
        except Exception as exc:
            close_errors.append(f"direct_close={exc}")
            self.log.critical(
                "FAIL_SAFE_CLOSE_DIRECT_FAILED | %s | close_side=%s | size=%s | reason=%s | error=%s",
                symbol,
                close_side,
                size,
                reason,
                exc,
            )

        try:
            if hasattr(self.client, "close_futures_position_full"):
                response = self.client.close_futures_position_full(
                    symbol=symbol,
                    direction=direction,
                    size=size,
                    reason=reason,
                )
                self.log.critical(
                    "FAIL_SAFE_CLOSE_FULL_SENT | %s | direction=%s | size=%s | reason=%s | response=%s",
                    symbol,
                    direction,
                    size,
                    reason,
                    response,
                )
                return
        except Exception as exc:
            close_errors.append(f"close_full={exc}")
            self.log.critical(
                "FAIL_SAFE_CLOSE_FULL_FAILED | %s | direction=%s | size=%s | reason=%s | error=%s",
                symbol,
                direction,
                size,
                reason,
                exc,
            )

        self.log.critical(
            "FAIL_SAFE_CLOSE_FAILED | %s | direction=%s | close_side=%s | size=%s | reason=%s | manual_intervention_required=True | errors=%s",
            symbol,
            direction,
            close_side,
            size,
            reason,
            " | ".join(close_errors),
        )

    def _verify_no_live_position_after_fail_safe(
        self,
        *,
        symbol: str,
        direction: str,
        reason: str,
    ) -> None:
        try:
            payload = self.client.get_all_positions()
            positions = payload.get("data") or []
            symbol_upper = symbol.upper()
            live_matches = []

            for position in positions:
                if str(position.get("symbol") or "").upper() != symbol_upper:
                    continue

                size = _safe_float(
                    position.get("total")
                    or position.get("size")
                    or position.get("available")
                    or 0.0,
                    0.0,
                )

                if size > 0:
                    live_matches.append(position)

            if live_matches:
                self.log.critical(
                    "FAIL_SAFE_POSITION_STILL_OPEN | %s | direction=%s | reason=%s | manual_intervention_required=True | positions=%s",
                    symbol_upper,
                    direction,
                    reason,
                    live_matches,
                )
            else:
                self.log.critical(
                    "FAIL_SAFE_POSITION_CLOSED_CONFIRMED | %s | direction=%s | reason=%s",
                    symbol_upper,
                    direction,
                    reason,
                )
        except Exception as exc:
            self.log.critical(
                "FAIL_SAFE_POSITION_VERIFY_FAILED | %s | direction=%s | reason=%s | manual_intervention_required=True | error=%s",
                symbol,
                direction,
                reason,
                exc,
            )

    def _report(
        self,
        plan: TradePlan,
        status: str,
        message: str,
        avg_entry: float,
        notional: float,
        leverage: float,
        expected_entry: float = 0.0,
        actual_entry: float = 0.0,
        slippage_pct: float = 0.0,
        fees_paid: float = 0.0,
        realized_pnl: float = 0.0,
        exchange_order_id: str = "",
    ) -> ExecutionReport:
        return ExecutionReport(
            symbol=plan.symbol,
            direction=plan.direction,
            strategy=plan.strategy,
            mode=self.settings.execution_mode,
            status=status,
            message=message,
            avg_entry=avg_entry,
            stop_loss=plan.stop_loss,
            take_profits=plan.take_profits,
            position_notional_usdt=notional,
            leverage=leverage,
            expected_entry=expected_entry or avg_entry,
            actual_entry=actual_entry or avg_entry,
            slippage_pct=slippage_pct,
            fees_paid=fees_paid,
            realized_pnl=realized_pnl,
            exchange_order_id=exchange_order_id,
        )