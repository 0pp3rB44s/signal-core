from __future__ import annotations

import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from enum import Enum

from app.config import Settings
from app.equity import resolve_account_equity
from clients.bitget_order_client import BitgetOrderClientMixin
from clients.bitget_rest import BitgetRestClient
from clients.schemas import ExecutionReport, TradePlan
from execution.entry_submitter import (
    EntryOrderSubmitter,
    RESULT_ADOPTED,
    RESULT_BLOCKED_UNKNOWN,
)
from execution.entry_routing import (
    STAGE_FALLBACK_ACK,
    STAGE_FALLBACK_FILL,
    STAGE_FALLBACK_SUBMIT,
    STAGE_MAKER_END,
    STAGE_MAKER_FILL,
    STAGE_MAKER_SUBMIT,
    STAGE_PLAN,
    STAGE_POSITION_CONFIRMED,
    EntryRoutingRecorder,
    Quote,
    capture_quote,
)
from execution.entry_snapshot import (
    VOLATILITY_RANK_LEGACY_SEMANTICS,
    atr_bps_from_percent,
    economic_hurdle_observability,
    missingness,
)
from execution.order_identity import ENTRY_LEG_MAKER, ENTRY_LEG_MARKET
from execution.order_intent_store import OrderIntentStore, new_session_id
from execution.position_model import (
    EXCHANGE_ACTUAL,
    decimal_value,
    position_lifecycle_id,
    stop_is_legal,
)
from execution.portfolio_selector import select_execution_winner
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


def is_low_vol_reclaim_v2(strategy: object) -> bool:
    return str(strategy or "").strip().lower() == "low_vol_reclaim_v2"


def normal_entry_policy(settings: object, strategy: object) -> tuple[bool, bool]:
    """Return (maker_enabled, market_fallback_enabled) for a normal entry."""
    if is_low_vol_reclaim_v2(strategy):
        return True, False
    return (
        bool(getattr(settings, "maker_entry_enabled", False)),
        bool(getattr(settings, "maker_entry_fallback_market", True)),
    )


def fee_feasibility(
    *, planned_gross_move_bps: float, maker_fee_rate: float,
    normal_exit_fee_rate: float, net_edge_buffer_bps: float,
) -> dict[str, float | bool]:
    """Cost-only gate; rates are decimal account rates from the exchange."""
    expected_fee_bps = (max(maker_fee_rate, 0.0) + max(normal_exit_fee_rate, 0.0)) * 10_000.0
    minimum_required = expected_fee_bps + max(net_edge_buffer_bps, 0.0)
    return {
        "planned_gross_move_bps": round(planned_gross_move_bps, 4),
        "expected_fee_bps": round(expected_fee_bps, 4),
        "minimum_required_price_movement_bps": round(minimum_required, 4),
        "fee_gate_pass": planned_gross_move_bps >= minimum_required,
    }


class FailSafeFlatness(str, Enum):
    """What a post-fail-safe readback actually established.

    `FLAT` is the only verdict that may retire an entry intent, and it means the
    exchange answered and the answer said there is no position. `REMAINS` and
    `UNKNOWN` are both refusals: the first knows the position is alive, the
    second knows nothing at all, and neither is grounds for CLOSED_OUT.
    """

    FLAT = "FLAT"
    REMAINS = "REMAINS"
    UNKNOWN = "UNKNOWN"


def _fail_safe_position_size(position: dict) -> float | None:
    """Size of one position row, or None when it cannot be read.

    Deliberately not `_safe_float`, whose 0.0 default turns an unreadable size
    into a flat position — the one mistake this verification exists to prevent.
    """
    for field in ("total", "size", "available"):
        value = position.get(field)
        if value in (None, ""):
            continue
        try:
            return abs(float(value))
        except (TypeError, ValueError):
            return None
    return None


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
        # Idempotent live-entry path: one deterministic clientOid per logical
        # entry, persisted before submit and reconciled after any ambiguity.
        self.session_id = new_session_id()
        self.intent_store = OrderIntentStore()
        self.entry_submitter = EntryOrderSubmitter(
            client=self.client,
            intent_store=self.intent_store,
            session_id=self.session_id,
            execution_mode=str(settings.execution_mode),
            log=self.log,
        )
        self._entry_recovery_done = False
        self._account_fee_rates: dict[str, tuple[float, float]] = {}
        #: Set by `_ensure_entry_intent_recovery`, consumed once by
        #: `_entry_guard_reason`. Recovery may now run on a cycle that never
        #: reaches the guard, so its verdict has to wait somewhere.
        self._entry_recovery_block_reason = ""

    def _v2_fee_gate(self, plan: TradePlan, planned_entry: float) -> dict[str, float | bool]:
        """Load actual account fees and evaluate the frozen v2 cost hurdle."""
        symbol = str(plan.symbol).upper()
        rates = self._account_fee_rates.get(symbol)
        if rates is None:
            getter = getattr(self.client, "get_trade_fee_rate", None)
            if getter is None:
                raise RuntimeError("authenticated fee endpoint unavailable")
            payload = getter(symbol=symbol, business_type="mix")
            data = payload.get("data") or {}
            maker_rate = float(data.get("makerFeeRate"))
            taker_rate = float(data.get("takerFeeRate"))
            if maker_rate < 0 or taker_rate <= 0:
                raise RuntimeError("invalid authenticated account fee rates")
            rates = (maker_rate, taker_rate)
            self._account_fee_rates[symbol] = rates

        target = _safe_float((plan.take_profits or [0.0])[0], 0.0)
        anchor = _safe_float(getattr(plan, "geometry_entry", 0.0), 0.0) or planned_entry
        if anchor <= 0 or target <= 0:
            raise RuntimeError("planned movement unavailable")
        planned_move = abs(target - anchor) / anchor * 10_000.0
        result = fee_feasibility(
            planned_gross_move_bps=planned_move,
            maker_fee_rate=rates[0],
            normal_exit_fee_rate=rates[1],
            net_edge_buffer_bps=float(
                getattr(self.settings, "planner_minimum_net_edge_buffer_bps", 4.0) or 0.0
            ),
        )
        result["maker_fee_rate"] = rates[0]
        result["normal_exit_fee_rate"] = rates[1]
        return result

    def _ensure_entry_intent_recovery(self) -> None:
        """Reconcile persisted order intents once per process. Housekeeping only.

        This used to live inside `_entry_guard_reason`, which `execute` reaches
        only *after* it has a portfolio winner. With no winner — a weekly freeze,
        a quiet market, every plan risk-blocked — `execute` returns early and the
        reconciliation never ran at all. Unfilled maker legs then accumulated as
        SUBMITTED forever: 64 of them by the time the exchange attestation caught
        it. The one job that cleans up after a bot that could not trade was gated
        behind the bot trading.

        It creates no order, cancels nothing, and cannot lift a risk gate. A
        blocking verdict is stored rather than returned, so the entry guard still
        sees it on the first cycle that asks — exactly as before.
        """
        if self.settings.execution_mode.upper() != "LIVE":
            return
        if self._entry_recovery_done:
            return
        try:
            recovery = self.entry_submitter.recover_pending_intents()
        except Exception as exc:
            # Deliberately not latched: a failed reconciliation must be retried,
            # and until it succeeds no new entry may be created.
            self.log.critical(
                "STARTUP_RECOVERY_FAILED | new_entries_blocked=True | error=%s", exc
            )
            self._entry_recovery_block_reason = f"order-intent recovery failed: {exc}"
            return
        self._entry_recovery_done = True
        if recovery.get("blocked"):
            self._entry_recovery_block_reason = "; ".join(
                recovery.get("reasons") or ["unreconciled order intent"]
            )

    def execute(self, plans: list[TradePlan]) -> list[ExecutionReport]:
        if not self.settings.execution_enabled:
            return []

        # Housekeeping before selection. Everything below this line can return
        # early; reconciliation must not depend on whether this cycle trades.
        self._ensure_entry_intent_recovery()

        selection = select_execution_winner(
            plans,
            allowed_symbols=(
                self.settings.production_symbol_set
                if self.settings.is_live_execution
                else None
            ),
        )
        winner = selection.winner
        if winner is None:
            if plans:
                self.log.warning(
                    "PORTFOLIO_SELECTION_EMPTY | plans=%s | rejected=%s",
                    len(plans),
                    len(selection.rejected),
                )
            return []
        plans = [winner]

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
                # Detect, never retire. This used to set CLOSED_SYNCED and save
                # it, which finished a close without economics, without dedup,
                # without a provisional row and without any recovery hook -- and
                # because both services share `state/executed_trades.json`, it
                # took the position away from `PositionManager.sync` before the
                # one path that *is* wired to the shared recorder could see it.
                # Whichever ran first won, so a close's economics could be lost
                # for good.
                #
                # Capacity does not depend on this: `open_symbols` is taken from
                # exchange truth on the next line, and the per-strategy count is
                # already filtered by `symbol in open_symbols`. Leaving the row
                # OPEN costs nothing here and lets the recorder decide its fate.
                stale_local_opens = sorted({
                    str(row.get("symbol"))
                    for row in existing
                    if row.get("status") == "OPEN"
                    and row.get("symbol")
                    and row.get("symbol") not in bitget_open_symbols
                })
                if stale_local_opens:
                    self.log.warning(
                        "LOCAL_OPEN_NOT_ON_EXCHANGE_DEFERRED | symbols=%s | "
                        "close-out left to PositionManager so economics, dedup and "
                        "recovery all run",
                        stale_local_opens,
                    )
                open_symbols = set(bitget_open_symbols)
            else:
                open_symbols = local_open_symbols.union(bitget_open_symbols)
        except Exception as exc:
            self.log.warning("Bitget position sync failed; using local state fallback: %s", exc)
            open_symbols = set(local_open_symbols)

        max_open_positions = int(self.settings.max_open_positions)
        hard_cap_notional = resolve_account_equity(self.settings)[0] * float(self.settings.max_leverage)

        # Unfinished order intents are reconciled against Bitget before any new
        # entry may be created, and an unresolvable one blocks entries entirely.
        entry_block_reason = self._entry_guard_reason()

        for plan in plans:
            # --- Telemetry: log trade decision snapshot before execution logic ---
            decision_snapshot_opened_at = self.decision_snapshot_logger.append_plan(plan)
            if entry_block_reason:
                self.log.critical(
                    "NEW_ENTRIES_BLOCKED | symbol=%s | plan_id=%s | reason=%s",
                    plan.symbol,
                    plan.plan_id,
                    entry_block_reason,
                )
                reports.append(
                    self._report(
                        plan=plan,
                        status="SKIPPED",
                        message=f"new entries blocked: {entry_block_reason}",
                        planned_avg_entry=round(sum(plan.entry_prices) / len(plan.entry_prices), 8),
                        notional=min(plan.position_notional_usdt, hard_cap_notional),
                        leverage=plan.leverage,
                    )
                )
                continue
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
                                planned_avg_entry=round(sum(plan.entry_prices) / len(plan.entry_prices), 8),
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
                            planned_avg_entry=round(sum(plan.entry_prices) / len(plan.entry_prices), 8),
                            notional=min(plan.position_notional_usdt, hard_cap_notional),
                            leverage=plan.leverage,
                        )
                    )
                    continue
            planned_avg_entry = round(sum(plan.entry_prices) / len(plan.entry_prices), 8)
            expected_entry = planned_avg_entry
            actual_entry = planned_avg_entry
            exchange_avg_entry = 0.0
            exchange_avg_entry_source = ""
            exchange_avg_entry_confirmed_at = ""
            confirmed_fill_quantity = 0.0
            exchange_tick_size = 0.0
            # Standaard = plan-niveaus; de live-tak herankert deze op de echte
            # fill (bug 2026-07-08) en overschrijft ze vóór opslag.
            protect_stop_loss = plan.stop_loss
            protect_take_profits = list(plan.take_profits or [])
            entry_via = "market"
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
                        planned_avg_entry=planned_avg_entry,
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
                        planned_avg_entry=planned_avg_entry,
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
                        planned_avg_entry=planned_avg_entry,
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
                        planned_avg_entry=planned_avg_entry,
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
                        planned_avg_entry=planned_avg_entry,
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
                        planned_avg_entry=planned_avg_entry,
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
                        planned_avg_entry=planned_avg_entry,
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
            entry_client_oid = ""
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
                # --- defence in depth: short-side invariant ---------------
                # RiskManager rejects SHORT candidates before the planner runs,
                # so this must be unreachable in normal operation. It exists so
                # that no future change to the risk layer, and no directly
                # injected plan, can put a forbidden SHORT on the exchange.
                # Reached before any client call: zero exchange mutation.
                if plan.direction.upper() == "SHORT" and not self._shorts_permitted():
                    self.log.critical(
                        "SHORTS_DISABLED_EXECUTION_INVARIANT | %s | strategy=%s | side=SHORT | "
                        "stage=execution_service | mode=LIVE | submission_refused=True",
                        plan.symbol,
                        plan.strategy,
                    )
                    reports.append(
                        self._report(
                            plan=plan,
                            status="SKIPPED",
                            message="blocked: shorts disabled by configuration (ENABLE_SHORTS=false)",
                            planned_avg_entry=planned_avg_entry,
                            notional=min(plan.position_notional_usdt, hard_cap_notional),
                            leverage=plan.leverage,
                        )
                    )
                    continue

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
                            planned_avg_entry=planned_avg_entry,
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

                raw_order_size = live_notional / planned_avg_entry
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
                            planned_avg_entry=planned_avg_entry,
                            notional=live_notional,
                            leverage=effective_leverage,
                        )
                    )
                    continue

                if order_size <= 0:
                    self.log.error(
                        "ORDER_SIZE_INVALID | %s | raw_size=%s | formatted_size=%s | notional=%s | planned_avg_entry=%s",
                        plan.symbol,
                        raw_order_size,
                        order_size,
                        plan.position_notional_usdt,
                        planned_avg_entry,
                    )
                    reports.append(
                        self._report(
                            plan=plan,
                            status="SKIPPED",
                            message="live order blocked: invalid order size",
                            planned_avg_entry=planned_avg_entry,
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
                            planned_avg_entry=planned_avg_entry,
                            notional=live_notional,
                            leverage=effective_leverage,
                        )
                    )
                    continue

                if is_low_vol_reclaim_v2(plan.strategy):
                    try:
                        fee_gate = self._v2_fee_gate(plan, planned_avg_entry)
                    except Exception as exc:
                        self.log.error(
                            "LOW_VOL_RECLAIM_V2_FEE_GATE | %s | strategy_id=low_vol_reclaim_v2 | "
                            "planned_gross_move_bps=unknown | expected_fee_bps=unknown | "
                            "fee_gate_pass=False | error=%s",
                            plan.symbol,
                            exc,
                        )
                        reports.append(
                            self._report(
                                plan=plan,
                                status="SKIPPED",
                                message=f"v2 fee gate blocked: {exc}",
                                planned_avg_entry=planned_avg_entry,
                                notional=live_notional,
                                leverage=effective_leverage,
                            )
                        )
                        continue
                    plan.notes.extend(
                        [
                            f"planned_gross_move_bps={fee_gate['planned_gross_move_bps']}",
                            f"expected_fee_bps={fee_gate['expected_fee_bps']}",
                            f"minimum_required_price_movement_bps={fee_gate['minimum_required_price_movement_bps']}",
                            f"fee_gate_pass={str(fee_gate['fee_gate_pass']).lower()}",
                            f"maker_fee_rate={fee_gate['maker_fee_rate']}",
                            f"normal_exit_fee_rate={fee_gate['normal_exit_fee_rate']}",
                        ]
                    )
                    self.log.info(
                        "LOW_VOL_RECLAIM_V2_FEE_GATE | %s | strategy_id=low_vol_reclaim_v2 | "
                        "planned_gross_move_bps=%.4f | expected_fee_bps=%.4f | "
                        "minimum_required_price_movement_bps=%.4f | fee_gate_pass=%s",
                        plan.symbol,
                        fee_gate["planned_gross_move_bps"],
                        fee_gate["expected_fee_bps"],
                        fee_gate["minimum_required_price_movement_bps"],
                        fee_gate["fee_gate_pass"],
                    )
                    if not bool(fee_gate["fee_gate_pass"]):
                        reports.append(
                            self._report(
                                plan=plan,
                                status="SKIPPED",
                                message="v2 fee gate blocked: planned movement does not clear actual fees",
                                planned_avg_entry=planned_avg_entry,
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

                    # v2 normal entries are maker-only. Other strategies retain
                    # their configured routing policy.
                    live_order_payload = None
                    live_order_id = None
                    entry_via = "market"
                    maker_enabled, market_fallback_enabled = normal_entry_policy(
                        self.settings, plan.strategy
                    )

                    # Observability only. Records what the routing did; changes
                    # no price, size, timeout or order. Quote capture is opt-in
                    # because two GETs before a submit would move the fill.
                    routing = EntryRoutingRecorder(
                        lifecycle_id=f"entry-{plan.plan_id}",
                        plan_id=str(plan.plan_id or ""),
                        candidate_id=str(getattr(plan, "candidate_id", "") or ""),
                        symbol=plan.symbol,
                        direction=plan.direction,
                        planned_entry=planned_avg_entry,
                        intended_route=(
                            "maker_only"
                            if is_low_vol_reclaim_v2(plan.strategy)
                            else "maker_then_market_fallback"
                            if maker_enabled
                            else "market"
                        ),
                        size_requested=order_size,
                        log=self.log,
                    )
                    pre_entry_features = self._pre_entry_features(plan)
                    if is_low_vol_reclaim_v2(plan.strategy):
                        pre_entry_features.update(
                            strategy_id="low_vol_reclaim_v2",
                            maker_order_submitted=False,
                            maker_filled=False,
                            maker_timeout=False,
                            trade_skipped_no_fill=False,
                        )
                    routing.safe_set_pre_entry_features(pre_entry_features)
                    routing.record(
                        STAGE_PLAN,
                        quote=self._routing_quote(plan.symbol),
                        order_price=planned_avg_entry,
                        size_requested=order_size,
                    )

                    if maker_enabled:
                        if is_low_vol_reclaim_v2(plan.strategy):
                            routing.pre_entry_features["maker_order_submitted"] = True
                        from execution.maker_entry import attempt_maker_entry
                        maker_anchor = _safe_float(getattr(plan, "geometry_entry", 0.0), 0.0) or planned_avg_entry
                        routing.record(
                            STAGE_MAKER_SUBMIT,
                            quote=self._routing_quote(plan.symbol),
                            order_price=maker_anchor,
                            size_requested=order_size,
                        )
                        maker_started_at = time.monotonic()
                        maker_result = attempt_maker_entry(
                            client=self.client, settings=self.settings, symbol=plan.symbol,
                            direction=plan.direction, size=order_size, anchor_price=maker_anchor,
                            hold_side=hold_side, log=self.log,
                            submit=lambda place, _plan=plan, _size=order_size, _side=side: (
                                self.entry_submitter.submit_entry(
                                    plan=_plan, size=_size, side=_side, order_type="limit",
                                    leg=ENTRY_LEG_MAKER, place=place, notional_usdt=live_notional,
                                )
                            ),
                        )
                        maker_wait_ms = round((time.monotonic() - maker_started_at) * 1000, 1)
                        maker_qty = _safe_float(maker_result.get("filled_qty"), 0.0)
                        if maker_qty > 0:
                            routing.record(
                                STAGE_MAKER_FILL,
                                order_id=str(maker_result.get("order_id") or ""),
                                client_oid=str(maker_result.get("client_oid") or ""),
                                fill_price=_safe_float(maker_result.get("fill_entry"), 0.0) or None,
                                size_filled=maker_qty,
                                maker_wait_elapsed_ms=maker_wait_ms,
                                exchange_order_status=str(maker_result.get("status") or ""),
                                reason=(
                                    "filled_during_cancel"
                                    if "FILLED_DURING_CANCEL" in str(maker_result.get("message") or "").upper()
                                    else None
                                ),
                            )
                        routing.record(
                            STAGE_MAKER_END,
                            quote=self._routing_quote(plan.symbol),
                            order_id=str(maker_result.get("order_id") or ""),
                            client_oid=str(maker_result.get("client_oid") or ""),
                            size_filled=maker_qty or None,
                            remaining_size=max(order_size - maker_qty, 0.0),
                            maker_wait_elapsed_ms=maker_wait_ms,
                            exchange_order_status=str(maker_result.get("status") or ""),
                            reason=str(maker_result.get("message") or "") or None,
                        )

                        if maker_result["status"] == "BLOCKED_UNKNOWN":
                            # Maker leg left the exchange state unknown: entering
                            # again (market fallback) could duplicate the position.
                            entry_block_reason = maker_result.get("message") or "maker entry state unknown"
                            self.log.critical(
                                "LIVE_ENTRY_BLOCKED_MAKER_STATE_UNKNOWN | %s | plan_id=%s | reason=%s",
                                plan.symbol, plan.plan_id, entry_block_reason,
                            )
                            if is_low_vol_reclaim_v2(plan.strategy):
                                routing.write()
                            reports.append(
                                self._report(
                                    plan=plan,
                                    status="SKIPPED",
                                    message=f"new entries blocked: {entry_block_reason}",
                                    planned_avg_entry=planned_avg_entry,
                                    notional=live_notional,
                                    leverage=effective_leverage,
                                )
                            )
                            continue
                        if maker_result["status"] == "FILLED":
                            live_order_payload = maker_result["payload"]
                            live_order_id = maker_result["order_id"]
                            entry_client_oid = maker_result.get("client_oid") or ""
                            entry_via = "maker"
                            if is_low_vol_reclaim_v2(plan.strategy):
                                routing.pre_entry_features["maker_filled"] = True
                        elif not market_fallback_enabled:
                            # Pure maker-modus: niet gevuld -> skippen, geen taker.
                            if is_low_vol_reclaim_v2(plan.strategy):
                                routing.pre_entry_features["maker_timeout"] = (
                                    maker_result["status"] == "UNFILLED_CANCELLED"
                                )
                                routing.pre_entry_features["trade_skipped_no_fill"] = True
                                routing.write()
                            reports.append(
                                self._report(
                                    plan=plan,
                                    status="SKIPPED",
                                    message=f"maker entry {maker_result['status'].lower()} (geen taker-fallback)",
                                    planned_avg_entry=planned_avg_entry,
                                    notional=live_notional,
                                    leverage=effective_leverage,
                                )
                            )
                            continue
                        else:
                            entry_via = "maker_then_market_fallback"

                    if live_order_id is None:
                        def _place_market_entry(client_oid: str, _plan=plan, _size=order_size,
                                                _side=side, _ref=planned_avg_entry):
                            return self.client.place_futures_market_order(
                                symbol=_plan.symbol,
                                size=_size,
                                side=_side,
                                trade_side=trade_side,
                                margin_mode="isolated",
                                client_oid=client_oid,
                                # Planned entry, so the exchange minimum notional
                                # is validated before transport on the market leg
                                # too — a market order has no price of its own.
                                reference_price=_ref,
                            )

                        routing.record(
                            STAGE_FALLBACK_SUBMIT,
                            quote=self._routing_quote(plan.symbol),
                            order_price=planned_avg_entry,
                            size_requested=order_size,
                            reason="maker_unfilled" if entry_via != "market" else "market_only_route",
                        )
                        submission = self.entry_submitter.submit_entry(
                            plan=plan,
                            size=order_size,
                            side=side,
                            order_type="market",
                            leg=ENTRY_LEG_MARKET,
                            place=_place_market_entry,
                            notional_usdt=live_notional,
                        )
                        entry_client_oid = submission.client_oid
                        routing.record(
                            STAGE_FALLBACK_ACK,
                            order_id=str(submission.order_id or ""),
                            client_oid=str(submission.client_oid or ""),
                            exchange_order_status=str(submission.status or ""),
                            reason=str(submission.classification or "") or None,
                        )

                        if submission.status == RESULT_BLOCKED_UNKNOWN:
                            entry_block_reason = submission.message
                            reports.append(
                                self._report(
                                    plan=plan,
                                    status="SKIPPED",
                                    message=f"new entries blocked: {submission.message}",
                                    planned_avg_entry=planned_avg_entry,
                                    notional=live_notional,
                                    leverage=effective_leverage,
                                )
                            )
                            continue

                        if not submission.has_live_order:
                            self.log.warning(
                                "LIVE_ENTRY_NOT_CREATED | %s | plan_id=%s | client_oid=%s | status=%s | classification=%s | submissions=%s",
                                plan.symbol, plan.plan_id, submission.client_oid,
                                submission.status, submission.classification, submission.submissions,
                            )
                            reports.append(
                                self._report(
                                    plan=plan,
                                    status="SKIPPED",
                                    message=f"live entry not created ({submission.status}): {submission.message}",
                                    planned_avg_entry=planned_avg_entry,
                                    notional=live_notional,
                                    leverage=effective_leverage,
                                )
                            )
                            continue

                        live_order_payload = submission.payload
                        live_order_id = submission.order_id
                        if submission.status == RESULT_ADOPTED:
                            entry_via = f"{entry_via}_adopted_after_reconciliation"

                    self.log.warning("ENTRY_VIA | %s | %s", plan.symbol, entry_via)

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

                    exchange_position_identity = None

                    exchange_position_found = False
                    live_fill_entry = 0.0
                    live_mark_price = 0.0

                    for position in verification_positions:
                        if str(position.get("symbol") or "") != plan.symbol:
                            continue

                        live_hold_side = str(
                            position.get("holdSide")
                            or position.get("posSide")
                            or ""
                        ).lower()
                        if live_hold_side and live_hold_side != hold_side:
                            self.log.critical(
                                "ENTRY_POSITION_SIDE_MISMATCH | %s | expected=%s | live=%s | "
                                "order_id=%s",
                                plan.symbol,
                                hold_side,
                                live_hold_side,
                                live_order_id,
                            )
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
                            live_fill_entry = _safe_float(
                                position.get("openPriceAvg")
                                or position.get("averageOpenPrice")
                                or position.get("openAvgPrice")
                                or position.get("avgOpenPrice")
                                or position.get("openPrice")
                                or 0.0,
                                0.0,
                            )
                            live_mark_price = _safe_float(
                                position.get("markPrice")
                                or position.get("lastPrice")
                                or position.get("marketPrice")
                                or 0.0,
                                0.0,
                            )
                            # The one moment the exchange tells us when this
                            # position opened. Captured here because a fail-safe
                            # close later in this flow has no other identity.
                            try:
                                _opened_ms = int(position.get("cTime") or 0) or None
                            except (TypeError, ValueError):
                                _opened_ms = None
                            exchange_position_identity = {
                                "symbol": str(plan.symbol).upper(),
                                "direction": str(plan.direction).upper(),
                                "hold_side": live_hold_side,
                                "opened_at_ms": _opened_ms,
                                "confirmed_position_size": live_size,
                                "exchange_avg_entry": live_fill_entry,
                                "exchange_position_id": str(position.get("positionId") or ""),
                                "entry_order_id": live_order_id,
                            }
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
                        "EXCHANGE_POSITION_CONFIRMED | %s | order_id=%s | client_oid=%s",
                        plan.symbol,
                        live_order_id,
                        entry_client_oid or "-",
                    )

                    if entry_client_oid:
                        self.entry_submitter.mark_filled(
                            entry_client_oid,
                            filled_qty=_safe_float(live_size, 0.0),
                            avg_price=live_fill_entry,
                        )
                    confirmed_fill_quantity = _safe_float(live_size, 0.0)
                    if live_fill_entry > 0:
                        exchange_avg_entry = round(live_fill_entry, 8)
                        exchange_avg_entry_source = "BITGET_OPEN_POSITION"
                        exchange_avg_entry_confirmed_at = datetime.now(timezone.utc).isoformat()


                    # --- SL/TP herankeren op de ECHTE fill (bug 2026-07-08) ---
                    # De planner berekent stop/TP vanaf latest_close, maar de
                    # market-order vult op de live prijs (structureel 0,1-0,4%
                    # verderop). Zonder herankering verschrompelt de stopafstand
                    # met 30-90% -> mini-stops -> uitgestopt vóór TP. We behouden
                    # de ontworpen prijs-RATIO's t.o.v. de echte fill.
                    ref_entry = _safe_float(getattr(plan, "geometry_entry", 0.0), 0.0)
                    if live_fill_entry > 0 and ref_entry > 0:
                        scale = live_fill_entry / ref_entry
                        reanchored_stop = round(plan.stop_loss * scale, 8)
                        reanchored_tps = [round(tp * scale, 8) for tp in protect_take_profits]
                        old_stop_bps = abs(plan.stop_loss - ref_entry) / ref_entry * 10000
                        new_stop_bps = abs(reanchored_stop - live_fill_entry) / live_fill_entry * 10000
                        self.log.warning(
                            "SLTP_REANCHORED | %s | ref_entry=%.8f | fill=%.8f | drift_bps=%.1f | "
                            "stop %.8f->%.8f | intended_stop_bps=%.1f | actual_now_stop_bps=%.1f",
                            plan.symbol,
                            ref_entry,
                            live_fill_entry,
                            (live_fill_entry - ref_entry) / ref_entry * 10000,
                            plan.stop_loss,
                            reanchored_stop,
                            old_stop_bps,
                            new_stop_bps,
                        )
                        protect_stop_loss = reanchored_stop
                        protect_take_profits = reanchored_tps

                    price_scale = self.client._contract_price_scale(plan.symbol)
                    if not isinstance(price_scale, int) or price_scale < 0:
                        raise RuntimeError(
                            f"EXCHANGE_TICK_SIZE_UNAVAILABLE | {plan.symbol}"
                        )
                    exchange_tick_size = float(10 ** (-price_scale))
                    if not stop_is_legal(
                        direction=plan.direction,
                        target=decimal_value(protect_stop_loss),
                        current_mark=decimal_value(live_mark_price),
                        tick_size=decimal_value(exchange_tick_size),
                        safety_ticks=int(self.settings.break_even_mark_safety_ticks),
                    ):
                        raise RuntimeError(
                            f"INITIAL_STOP_NOT_LEGAL | {plan.symbol} | mark={live_mark_price} "
                            f"| stop={protect_stop_loss} | tick={exchange_tick_size}"
                        )

                    # Place protection with stronger validation and retry.
                    protection_payload = None
                    has_sl = False
                    has_tp = False
                    entry_protection_verified = False
                    actual_tp_count = 0
                    expected_tp_count = len(valid_take_profits)

                    # Protection uses Bitget's position-level TP/SL endpoint, which
                    # SETS the position's protection rather than stacking orders, so
                    # a re-attempt reconciles instead of duplicating. Placement is
                    # skipped entirely when this intent is already PROTECTED.
                    intent_record = (
                        self.intent_store.get(entry_client_oid) if entry_client_oid else None
                    )
                    if intent_record and intent_record.get("protection_state") == "CONFIRMED":
                        self.log.critical(
                            "ENTRY_PROTECTION_ALREADY_CONFIRMED | %s | client_oid=%s | placement_skipped=True",
                            plan.symbol,
                            entry_client_oid,
                        )
                        has_sl = True
                        has_tp = True
                        entry_protection_verified = True
                        protection_payload = {
                            "protection_verified": True,
                            "protection_integrity": "RECONCILED_ALREADY_PROTECTED",
                            "stop_loss_verified": True,
                            "take_profit_count": expected_tp_count,
                            "expected_take_profit_count": expected_tp_count,
                        }
                        protection_integrity = "RECONCILED_ALREADY_PROTECTED"
                        actual_tp_count = expected_tp_count

                    protection_attempts_allowed = 0 if entry_protection_verified else 3

                    for protection_attempt in range(1, protection_attempts_allowed + 1):
                        try:
                            protection_payload = self.client.place_futures_protection_orders(
                                symbol=plan.symbol,
                                direction=plan.direction,
                                hold_side=hold_side,
                                size=live_size,
                                stop_loss=protect_stop_loss,
                                take_profits=protect_take_profits,
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
                            lifecycle_identity=exchange_position_identity,
                            size=live_size,
                            close_side=close_side,
                            direction=plan.direction,
                            reason="entry_protection_failed",
                        )

                        flatness = self._verify_no_live_position_after_fail_safe(
                            symbol=plan.symbol,
                            direction=plan.direction,
                            reason="entry_protection_failed",
                        )

                        self._close_out_entry_intent_if_flat(
                            client_oid=entry_client_oid,
                            flatness=flatness,
                            symbol=plan.symbol,
                            reason="entry_protection_failed",
                        )

                        reports.append(
                            self._report(
                                plan=plan,
                                status="ERROR",
                                message="FAIL-SAFE TRIGGERED: SL/TP NOT VERIFIED -> emergency close invoked",
                                planned_avg_entry=planned_avg_entry,
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

                    if entry_client_oid and protection_verified:
                        self.entry_submitter.mark_protected(
                            entry_client_oid, integrity=protection_integrity
                        )
                        self.log.critical(
                            "ENTRY_PROTECTION_RECONCILED | %s | plan_id=%s | client_oid=%s | order_id=%s | integrity=%s",
                            plan.symbol,
                            plan.plan_id,
                            entry_client_oid,
                            live_order_id,
                            protection_integrity,
                        )
                    exchange_stop_loss = protection_payload.get("stop_loss") if protection_payload else None
                    exchange_take_profit_count = int(protection_payload.get("take_profit_count") or len(protection_payload.get("take_profits") or [])) if protection_payload else 0
                    self.log.warning(
                        "ENTRY_PROTECTION_CONFIRMED | %s | direction=%s | stop_loss=%s | take_profits=%s | size=%s",
                        plan.symbol,
                        plan.direction,
                        exchange_stop_loss,
                        exchange_take_profit_count,
                        live_size,
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
                        actual_entry = planned_avg_entry

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
                                if exchange_avg_entry <= 0:
                                    exchange_avg_entry = round(
                                        detailed_actual_entry,
                                        8,
                                    )
                                    exchange_avg_entry_source = "BITGET_ORDER_DETAIL"
                                    exchange_avg_entry_confirmed_at = datetime.now(timezone.utc).isoformat()

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

                    # Routing record is closed here, where the fill price is
                    # finally known. Failure to write is logged and swallowed:
                    # observability must never abort an entry that already
                    # exists on the exchange.
                    if routing.stage(STAGE_MAKER_FILL) is None:
                        routing.record(
                            STAGE_FALLBACK_FILL,
                            order_id=str(exchange_order_id or ""),
                            client_oid=str(entry_client_oid or ""),
                            fill_price=actual_entry or None,
                            size_filled=_safe_float(fill_metrics.get("filled_qty"), 0.0) or order_size,
                            exchange_order_status=str(fill_metrics.get("state") or "") or None,
                        )
                    routing.record(
                        STAGE_POSITION_CONFIRMED,
                        order_id=str(exchange_order_id or ""),
                        fill_price=actual_entry or None,
                        exchange_order_status=exchange_avg_entry_source,
                    )
                    routing.write()

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
                    execution_message = (
                        f"live maker order filled | size={order_size} | order_id={live_order_id}"
                        if entry_via == "maker"
                        else f"live market order placed | size={order_size} | order_id={live_order_id}"
                    )

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
                                planned_avg_entry=planned_avg_entry,
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
                            lifecycle_identity=exchange_position_identity,
                            size=order_size,
                            close_side=close_side,
                            direction=plan.direction,
                            reason="entry_protection_failed",
                        )
                        flatness = self._verify_no_live_position_after_fail_safe(
                            symbol=plan.symbol,
                            direction=plan.direction,
                            reason="entry_protection_failed_exception",
                        )
                        self._close_out_entry_intent_if_flat(
                            client_oid=entry_client_oid,
                            flatness=flatness,
                            symbol=plan.symbol,
                            reason="entry_protection_failed_exception",
                        )
                        reports.append(
                            self._report(
                                plan=plan,
                                status="ERROR",
                                message=f"FAIL-SAFE TRIGGERED: protection/order flow failed after entry -> position closed | error={exc}",
                                planned_avg_entry=planned_avg_entry,
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
                                planned_avg_entry=planned_avg_entry,
                                notional=live_notional,
                                leverage=effective_leverage,
                            )
                        )
                    continue

            lifecycle_id = position_lifecycle_id(
                plan_id=plan.plan_id,
                symbol=plan.symbol,
                direction=plan.direction,
                client_oid=entry_client_oid,
                order_id=str(live_order_id or ""),
            )
            if self.settings.execution_mode.upper() == "LIVE" and exchange_tick_size <= 0:
                try:
                    price_scale = self.client._contract_price_scale(plan.symbol)
                    if isinstance(price_scale, int) and price_scale >= 0:
                        exchange_tick_size = float(10 ** (-price_scale))
                except Exception as tick_exc:
                    self.log.warning(
                        "EXCHANGE_TICK_SIZE_UNAVAILABLE | %s | lifecycle_id=%s | error=%s",
                        plan.symbol,
                        lifecycle_id,
                        tick_exc,
                    )

            position = {
                "symbol": plan.symbol,
                "strategy": plan.strategy,
                "direction": plan.direction,
                "mode": self.settings.execution_mode,
                "status": "OPEN",
                # Migration contract: avg_entry remains a planning alias only.
                "avg_entry": planned_avg_entry,
                "planned_avg_entry": planned_avg_entry,
                "expected_entry": expected_entry,
                "actual_entry": actual_entry,
                "exchange_avg_entry": exchange_avg_entry or None,
                "exchange_avg_entry_source": exchange_avg_entry_source,
                "exchange_avg_entry_confirmed_at": exchange_avg_entry_confirmed_at,
                "exchange_entry_order_id": str(live_order_id or ""),
                "exchange_entry_client_oid": entry_client_oid,
                "position_lifecycle_id": lifecycle_id,
                # Exchange-side identity, captured at EXCHANGE_POSITION_CONFIRMED
                # above and now carried for the whole life of the position.
                #
                # It previously lived only in `exchange_position_identity`, which
                # the emergency-flatten path reads and nothing else. Every close
                # route therefore wrote its CLOSE_PROVISIONAL row with no
                # positionId and no exchange open time, so startup recovery had no
                # strong identity and fell through to the composite fallback —
                # where it compares Bitget's `ctime` against our *observation*
                # clock and refused two real lifecycles whose maker entries
                # confirmed 5.7 s and 20.8 s after the exchange opened them.
                #
                # Both values are exchange truth copied verbatim (`positionId`,
                # `cTime`) or empty. `position_lifecycle_id` above is our own id
                # and is deliberately never reused as an exchange identifier.
                "exchange_position_id": str(
                    (exchange_position_identity or {}).get("exchange_position_id") or ""
                ),
                "exchange_open_time": (exchange_position_identity or {}).get("opened_at_ms") or "",
                "opened_at_ms": (exchange_position_identity or {}).get("opened_at_ms") or "",
                "confirmed_fill_quantity": confirmed_fill_quantity or None,
                "confirmed_position_size": confirmed_fill_quantity or None,
                "confirmed_remaining_size": confirmed_fill_quantity or None,
                "confirmed_remaining_size_source": (
                    "BITGET_OPEN_POSITION" if confirmed_fill_quantity > 0 else ""
                ),
                "confirmed_opening_fee_usdt": fees_paid if fees_paid > 0 else None,
                "confirmed_opening_fee_source": (
                    EXCHANGE_ACTUAL if fees_paid > 0 else ""
                ),
                "exchange_opening_fee_usdt": fees_paid if fees_paid > 0 else None,
                "exchange_opening_fee_source": EXCHANGE_ACTUAL if fees_paid > 0 else "",
                "exchange_tick_size": exchange_tick_size or None,
                "plan_id": plan.plan_id,
                "candidate_id": plan.candidate_id,
                "slippage_pct": slippage_pct,
                "fees_paid": fees_paid,
                "entry_prices": plan.entry_prices,
                "entry_via": entry_via,
                "stop_loss": protect_stop_loss,
                "initial_stop_loss": protect_stop_loss,
                "take_profits": protect_take_profits,
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
                "protection_state": (
                    "INITIAL_PROTECTION_CONFIRMED"
                    if protection_verified
                    else "PROTECTION_UPDATE_FAILED"
                ),
                "confirmed_stop": protect_stop_loss if protection_verified else None,
                "entry_stop_loss_verified": bool(protection_payload.get("stop_loss_verified")) if protection_payload else False,
                "entry_expected_take_profit_count": int(protection_payload.get("expected_take_profit_count") or 0) if protection_payload else 0,
                "last_price": exchange_avg_entry or planned_avg_entry,
                "notes": plan.notes,
                "reasons": plan.reasons,
            }
            existing.append(position)
            open_symbols.add(plan.symbol)

            report = self._report(
                plan=plan,
                status=execution_status,
                message=execution_message,
                planned_avg_entry=planned_avg_entry,
                notional=live_notional,
                leverage=effective_leverage,
                exchange_avg_entry=exchange_avg_entry,
                exchange_avg_entry_source=exchange_avg_entry_source,
                position_lifecycle_id=lifecycle_id,
                exchange_entry_order_id=str(live_order_id or ""),
                exchange_entry_client_oid=entry_client_oid,
                confirmed_position_size=confirmed_fill_quantity,
                confirmed_opening_fee_usdt=fees_paid,
                expected_entry=expected_entry,
                actual_entry=actual_entry,
                slippage_pct=slippage_pct,
                fees_paid=fees_paid,
                realized_pnl=realized_pnl,
                exchange_order_id=exchange_order_id,
                stop_loss=protect_stop_loss,
                take_profits=protect_take_profits,
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

    def _shorts_permitted(self) -> bool:
        """Mirror of RiskManager.shorts_permitted for the execution invariant.

        Deliberately duplicated rather than imported: this is a defence-in-depth
        check, and it must not stop working because the risk layer was changed,
        refactored or bypassed.
        """
        value = getattr(self.settings, "enable_shorts", None)
        if isinstance(value, bool):
            return value
        live = str(getattr(self.settings, "execution_mode", "")).strip().upper() == "LIVE"
        return not live

    def _entry_guard_reason(self) -> str:
        """Return a non-empty reason when no new live entry may be created.

        The reconciliation itself runs in `_ensure_entry_intent_recovery`, which
        `execute` calls before it decides whether it has anything to trade. This
        still runs it, for the case where the guard is reached first, and then
        consumes its verdict once — the same single delivery the inline version
        produced on the cycle it reconciled.
        """
        if self.settings.execution_mode.upper() != "LIVE":
            return ""

        self._ensure_entry_intent_recovery()
        recovery_reason = self._entry_recovery_block_reason
        self._entry_recovery_block_reason = ""
        if recovery_reason:
            return recovery_reason

        blocking = self.intent_store.blocking()
        if blocking:
            details = ", ".join(
                f"{row.get('symbol')}:{row.get('client_oid')}" for row in blocking
            )
            return f"unreconciled order intent(s) in UNKNOWN state: {details}"

        return self._exchange_pending_entry_guard_reason()

    def _exchange_pending_entry_guard_reason(self) -> str:
        """Block while any non-reduce-only exchange entry is still pending."""
        try:
            payload = self.client.get_pending_orders(
                product_type=self.settings.bitget_product_type,
            )
            rows = BitgetOrderClientMixin._order_rows(payload)
        except Exception as exc:
            self.log.critical(
                "LIVE_ENTRY_BLOCKED_PENDING_ORDER_SYNC_FAILED | error=%s",
                exc,
            )
            return f"exchange pending-entry check failed: {exc}"

        pending_entries = []
        for row in rows:
            reduce_only = str(row.get("reduceOnly") or "").strip().lower()
            trade_side = str(row.get("tradeSide") or "").strip().lower()
            if reduce_only in {"yes", "true", "1"} or trade_side == "close":
                continue
            pending_entries.append(
                {
                    "symbol": str(row.get("symbol") or "UNKNOWN").upper(),
                    "order_id": str(row.get("orderId") or row.get("id") or "UNKNOWN"),
                }
            )

        if not pending_entries:
            return ""
        details = ", ".join(
            f"{row['symbol']}:{row['order_id']}" for row in pending_entries
        )
        self.log.critical(
            "LIVE_ENTRY_BLOCKED_PENDING_EXCHANGE_ENTRY | pending=%s",
            details,
        )
        return f"pending exchange entry order(s): {details}"

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
        lifecycle_identity: dict | None = None,
    ) -> None:
        """Close a position this flow opened but could not protect.

        ``lifecycle_identity`` holds what the exchange told us at
        EXCHANGE_POSITION_CONFIRMED -- above all ``opened_at_ms`` from cTime.
        This close happens inside the entry flow, before PositionManager has
        ever seen the position, so there is no lifecycle id yet. Without the
        captured open time a later reconciliation could only match on symbol and
        side, which is a guess: two lifecycles on one symbol and side can close
        in the same second.

        When it is None -- the position was never confirmed -- the close still
        runs, but no economics are claimed for it.
        """
        close_errors: list[str] = []
        hold_side = "long" if str(direction).upper() == "LONG" else "short"

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
                # The client only reports CLOSED after re-reading the position
                # and finding remaining size 0; `reconcile_fail_safe_close`
                # re-checks that itself, so a non-flat response records nothing.
                # Orchestration only: the reconciliation and dataset rules live
                # in the shared recorder, not here.
                self._record_fail_safe_close_economics(response, lifecycle_identity)
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

    def _record_fail_safe_close_economics(self, close_result, lifecycle_identity) -> str:
        """Hand a completed fail-safe close to the shared close-economics recorder.

        Orchestration only. Every rule about what counts as flat, how a
        lifecycle is matched, and what a provisional row looks like lives in
        `execution.closed_lifecycle_recorder`, shared with the three
        PositionManager close paths — nothing about it is re-implemented here.

        Never raises: bookkeeping must not turn a completed close into an
        exception inside the entry flow.
        """
        try:
            from execution.close_dedup import (
                DedupOutcome,
                economic_close_status,
                provisional_close_status,
            )
            from execution.closed_lifecycle_recorder import (
                exchange_confirmed_flat,
                reconcile_fail_safe_close,
            )
            from telemetry.trade_logger import TradeDatasetV2Logger

            dataset_path = "logs/trade_dataset_v2.csv"
            # NOT_FOUND is the only outcome that permits a marker: FOUND means
            # one already exists, BLOCKED_UNREADABLE means we cannot tell and a
            # duplicate provisional row would later drive a duplicate recovery.
            if (
                lifecycle_identity
                and exchange_confirmed_flat(close_result)
                and economic_close_status(dataset_path, lifecycle_identity) is DedupOutcome.NOT_FOUND
                and provisional_close_status(dataset_path, lifecycle_identity) is DedupOutcome.NOT_FOUND
            ):
                provisional = dict(lifecycle_identity)
                provisional.update({
                    "closed_reason": "fail_safe_close",
                    "close_reason": "fail_safe_close",
                    "sync_source": "execution_service",
                })
                TradeDatasetV2Logger(dataset_path).append_close(
                    trade=provisional,
                    result="fail_safe_close",
                    pnl=None,
                    quality={},
                )

            def _write(identity: dict, econ: dict) -> None:
                from telemetry.trade_logger import append_exchange_truth_close
                append_exchange_truth_close(
                    position=identity,
                    economics=econ,
                    close_reason="fail_safe_close",
                    dataset_path="logs/trade_dataset_v2.csv",
                )

            return reconcile_fail_safe_close(
                lifecycle_identity=lifecycle_identity,
                close_result=close_result,
                dataset_path=dataset_path,
                fetch_history=self._fetch_closed_position_history,
                write_economic_close=_write,
                log_=self.log,
            )
        except Exception as exc:
            self.log.critical(
                "FAIL_SAFE_CLOSE_ECONOMICS_FAILED | %s | error=%s",
                (lifecycle_identity or {}).get("symbol"),
                exc,
            )
            return "ERROR"

    def _fetch_closed_position_history(self) -> list:
        """Recent closed lifecycles from Bitget. Read-only."""
        payload = self.client._request(
            "GET",
            "/api/v2/mix/position/history-position",
            {"productType": getattr(self.settings, "bitget_product_type", "USDT-FUTURES"),
             "limit": "50"},
            private=True,
        )
        return (payload.get("data") or {}).get("list") or []

    def _verify_no_live_position_after_fail_safe(
        self,
        *,
        symbol: str,
        direction: str,
        reason: str,
    ) -> FailSafeFlatness:
        """Ask the exchange, fresh, whether the position is really gone.

        Returns a verdict rather than logging one. `FLAT` is a claim that the
        exchange answered, the answer parsed, and no position for this symbol
        carries size. Everything else — transport failure, a payload that is not
        the documented shape, a row whose size cannot be read — is `UNKNOWN`,
        because "we could not tell" and "there is nothing there" are different
        facts and only one of them may retire an intent.

        The old version returned None in all four branches and answered every
        one of them by writing a log line, so the caller closed the intent out
        whatever happened.
        """
        symbol_upper = symbol.upper()
        try:
            payload = self.client.get_all_positions()
        except Exception as exc:
            self.log.critical(
                "FAIL_SAFE_POSITION_VERIFY_FAILED | %s | direction=%s | reason=%s | "
                "flatness=UNKNOWN | manual_intervention_required=True | error=%s",
                symbol, direction, reason, exc,
            )
            return FailSafeFlatness.UNKNOWN

        # An absent or non-list `data` is not an empty position book. Reading it
        # as one is how a malformed response used to prove the position closed.
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            self.log.critical(
                "FAIL_SAFE_POSITION_VERIFY_MALFORMED | %s | direction=%s | reason=%s | "
                "flatness=UNKNOWN | manual_intervention_required=True | payload_type=%s",
                symbol_upper, direction, reason, type(payload).__name__,
            )
            return FailSafeFlatness.UNKNOWN

        live_matches = []
        for position in payload["data"]:
            if not isinstance(position, dict):
                self.log.critical(
                    "FAIL_SAFE_POSITION_VERIFY_MALFORMED | %s | direction=%s | reason=%s | "
                    "flatness=UNKNOWN | manual_intervention_required=True | row_type=%s",
                    symbol_upper, direction, reason, type(position).__name__,
                )
                return FailSafeFlatness.UNKNOWN
            if str(position.get("symbol") or "").upper() != symbol_upper:
                continue

            size = _fail_safe_position_size(position)
            if size is None:
                # A row for our symbol whose size will not parse cannot be
                # rounded down to zero: that is exactly the position we are
                # trying to prove gone.
                self.log.critical(
                    "FAIL_SAFE_POSITION_VERIFY_UNPARSEABLE_SIZE | %s | direction=%s | "
                    "reason=%s | flatness=UNKNOWN | manual_intervention_required=True",
                    symbol_upper, direction, reason,
                )
                return FailSafeFlatness.UNKNOWN
            if size > 0:
                live_matches.append(position)

        if live_matches:
            self.log.critical(
                "FAIL_SAFE_POSITION_STILL_OPEN | %s | direction=%s | reason=%s | "
                "flatness=REMAINS | manual_intervention_required=True | positions=%s",
                symbol_upper, direction, reason, live_matches,
            )
            return FailSafeFlatness.REMAINS

        self.log.critical(
            "FAIL_SAFE_POSITION_CLOSED_CONFIRMED | %s | direction=%s | reason=%s | flatness=FLAT",
            symbol_upper, direction, reason,
        )
        return FailSafeFlatness.FLAT

    def _close_out_entry_intent_if_flat(
        self,
        *,
        client_oid: str,
        flatness: FailSafeFlatness,
        symbol: str,
        reason: str,
    ) -> bool:
        """Retire the entry intent only against a proven-flat exchange.

        CLOSED_OUT moves the intent to STATE_ABANDONED, which is terminal: it
        leaves `recoverable()` and `_prune` may drop it. Claiming it while the
        position may still be live would delete the one record that startup
        recovery uses to find an orphaned, unprotected position. So anything
        short of FLAT leaves the intent at FILLED — recoverable on purpose.
        """
        if not client_oid:
            return False
        intents = getattr(self.entry_submitter, "intents", None)
        existing = intents.get(client_oid) if intents is not None else None
        if isinstance(existing, dict) and existing.get("protection_state") == "CLOSED_OUT":
            # Already retired. A second transition would add another ABANDONED
            # entry to the intent history for one lifecycle.
            return False
        if flatness is not FailSafeFlatness.FLAT:
            self.log.critical(
                "FAIL_SAFE_CLOSE_OUT_REFUSED | %s | client_oid=%s | reason=%s | "
                "flatness=%s | intent stays FILLED and recoverable | "
                "manual_intervention_required=True",
                str(symbol).upper(), client_oid, reason, flatness.value,
            )
            return False
        self.entry_submitter.mark_closed_out(client_oid, reason=reason)
        return True

    def _routing_quote(self, symbol: str) -> Quote:
        """Market snapshot for the routing record, or an explicit absence.

        Off by default, and that default is load-bearing. Capturing a quote
        means two GETs of roughly 300ms each; placed before a submit that would
        move the fill, which is the exact quantity being measured. Enabling
        ENTRY_ROUTING_QUOTE_CAPTURE buys mid-referenced metrics at the price of
        a slower entry, so it is a measurement decision, not a default.
        """
        if not bool(getattr(self.settings, "entry_routing_quote_capture", False)):
            return Quote.unavailable("quote_capture_disabled")
        return capture_quote(self.client, symbol, self.log)

    @staticmethod
    def _pre_entry_features(plan: TradePlan) -> dict[str, object]:
        """Everything the planner knew, as it knew it. Absent stays absent.

        Values are parsed out of the plan's own notes and reasons rather than
        recomputed, so this records what the decision actually saw. A feature
        that was not present is omitted, never defaulted to zero: zero is a
        legitimate score and would be indistinguishable from silence.
        """
        features: dict[str, object] = {
            "strategy": getattr(plan, "strategy", None),
            "direction": getattr(plan, "direction", None),
            "symbol": getattr(plan, "symbol", None),
            "score": getattr(plan, "score", None),
            "risk_reward_ratio": getattr(plan, "risk_reward_ratio", None),
        }
        for raw in [
            *(getattr(plan, "notes", None) or []),
            *(getattr(plan, "reasons", None) or []),
        ]:
            for part in str(raw).split("|"):
                token = part.strip()
                if "=" not in token:
                    continue
                key, _, value = token.partition("=")
                key = key.strip()
                if not key or len(key) > 60 or key in features:
                    continue
                text = value.strip()
                try:
                    features[key] = float(text)
                except ValueError:
                    features[key] = text or None

        # Research annotations. Wrapped because this runs inside the entry path:
        # a malformed note must degrade the snapshot, never the trade.
        try:
            features.update(ExecutionService._research_annotations(features))
        except Exception as exc:  # noqa: BLE001 - observability must not raise
            features["research_annotation_error"] = str(exc)
        return features

    @staticmethod
    def _research_annotations(features: dict[str, object]) -> dict[str, object]:
        annotations: dict[str, object] = {}
        atr_percent = features.get("atr_percent")
        annotations["atr_percent_raw"] = atr_percent if isinstance(atr_percent, float) else None
        annotations["atr_bps"] = atr_bps_from_percent(atr_percent)
        annotations["volatility_rank_legacy"] = (
            features.get("volatility_rank")
            if isinstance(features.get("volatility_rank"), float) else None
        )
        annotations["volatility_rank_legacy_semantics"] = VOLATILITY_RANK_LEGACY_SEMANTICS
        annotations.update(economic_hurdle_observability(features))
        annotations["field_availability_version"] = 1
        annotations["missingness"] = missingness({**features, **annotations})
        return annotations

    def _report(
        self,
        plan: TradePlan,
        status: str,
        message: str,
        planned_avg_entry: float,
        notional: float,
        leverage: float,
        exchange_avg_entry: float = 0.0,
        exchange_avg_entry_source: str = "",
        position_lifecycle_id: str = "",
        exchange_entry_order_id: str = "",
        exchange_entry_client_oid: str = "",
        confirmed_position_size: float = 0.0,
        confirmed_opening_fee_usdt: float = 0.0,
        expected_entry: float = 0.0,
        actual_entry: float = 0.0,
        slippage_pct: float = 0.0,
        fees_paid: float = 0.0,
        realized_pnl: float = 0.0,
        exchange_order_id: str = "",
        stop_loss: float | None = None,
        take_profits: list[float] | None = None,
    ) -> ExecutionReport:
        return ExecutionReport(
            candidate_id=plan.candidate_id,
            plan_id=plan.plan_id,
            symbol=plan.symbol,
            direction=plan.direction,
            strategy=plan.strategy,
            mode=self.settings.execution_mode,
            status=status,
            message=message,
            avg_entry=planned_avg_entry,
            planned_avg_entry=planned_avg_entry,
            exchange_avg_entry=exchange_avg_entry,
            exchange_avg_entry_source=exchange_avg_entry_source,
            position_lifecycle_id=position_lifecycle_id,
            exchange_entry_order_id=exchange_entry_order_id,
            exchange_entry_client_oid=exchange_entry_client_oid,
            confirmed_position_size=confirmed_position_size,
            confirmed_opening_fee_usdt=confirmed_opening_fee_usdt,
            stop_loss=plan.stop_loss if stop_loss is None else stop_loss,
            take_profits=plan.take_profits if take_profits is None else take_profits,
            position_notional_usdt=notional,
            leverage=leverage,
            expected_entry=expected_entry or planned_avg_entry,
            actual_entry=actual_entry or planned_avg_entry,
            slippage_pct=slippage_pct,
            fees_paid=fees_paid,
            realized_pnl=realized_pnl,
            exchange_order_id=exchange_order_id,
        )
