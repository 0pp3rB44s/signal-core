"""Exchange truth: account and open positions. STRICTLY READ-ONLY.

Only GET endpoints are ever called from here. This module must never import the
execution service, the entry submitter, or any order-mutating client method.
A guard test enforces that.

Unreachable exchange => UNKNOWN. We never fall back to a local snapshot and
present it as live, which is what the v2 dashboard did.
"""

from __future__ import annotations

from typing import Any

from dashboard_v3.core import sources as src
from dashboard_v3.core.status import Signal, SignalSet, Status

#: The only client methods this dashboard is permitted to call.
ALLOWED_CLIENT_METHODS = frozenset({
    "get_accounts", "get_all_positions", "get_tpsl_orders",
    "get_order_history", "get_pending_orders",
})


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_f(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if row.get(key) not in (None, ""):
            try:
                return float(row[key])
            except (TypeError, ValueError):
                continue
    return None


def _direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"long", "buy"}:
        return "LONG"
    if text in {"short", "sell"}:
        return "SHORT"
    return "UNKNOWN"


def _matching_local_state(
    states: list[dict[str, Any]],
    *,
    symbol: str,
    direction: str,
    exchange_entry: float | None,
) -> dict[str, Any] | None:
    """Attach lifecycle metadata only after an unambiguous exchange-entry match."""
    candidates = [
        row
        for row in states
        if str(row.get("status") or "").upper() == "OPEN"
        and str(row.get("symbol") or "").upper() == symbol
        and _direction(row.get("direction")) == direction
    ]
    if len(candidates) != 1 or exchange_entry is None or exchange_entry <= 0:
        return None
    local_entry = _first_f(candidates[0], ("exchange_avg_entry",))
    if local_entry is None or local_entry <= 0:
        return None
    tolerance = max(abs(exchange_entry) * 1e-8, 1e-8)
    return candidates[0] if abs(local_entry - exchange_entry) <= tolerance else None


def _plan_trigger(row: dict[str, Any]) -> float | None:
    return _first_f(
        row,
        ("triggerPrice", "planTriggerPrice", "stopLossTriggerPrice", "stopSurplusTriggerPrice"),
    )


def _plan_kind(row: dict[str, Any]) -> str:
    text = str(row.get("planType") or row.get("orderType") or "").lower()
    if "loss" in text:
        return "STOP_LOSS"
    if "profit" in text:
        return "TAKE_PROFIT"
    return "UNKNOWN"


def build(client_factory=None) -> dict[str, Any]:
    """Fetch account + positions. ``client_factory`` is injectable for tests."""
    signals = SignalSet()
    errors: list[str] = []
    reachable = False
    account: dict[str, Any] = {}
    positions: list[dict[str, Any]] = []
    open_orders: list[dict[str, Any]] = []
    local_loaded = src.load_json("state/executed_trades.json", default=[])
    local_states = [
        row for row in (local_loaded.value or []) if isinstance(row, dict)
    ] if isinstance(local_loaded.value, list) else []

    try:
        if client_factory is None:
            from app.config import Settings
            from clients.bitget_rest import BitgetRestClient

            settings = Settings()
            client = BitgetRestClient(settings=settings)
            product = settings.bitget_product_type
        else:
            client, product = client_factory()

        acct = client.get_accounts(product_type=product)
        rows = (acct or {}).get("data") or []
        account = rows[0] if isinstance(rows, list) and rows else {}

        pos_payload = client.get_all_positions(product_type=product)
        raw_positions = (pos_payload or {}).get("data") or []

        tpsl_by_symbol: dict[str, list[dict[str, Any]]] = {}
        try:
            tpsl = client.get_tpsl_orders(product_type=product)
            for row in (tpsl or {}).get("data") or []:
                if isinstance(row, dict):
                    tpsl_by_symbol.setdefault(str(row.get("symbol", "")).upper(), []).append(row)
        except Exception as exc:
            errors.append(f"TP/SL lookup unavailable: {type(exc).__name__}")

        try:
            pending = client.get_pending_orders(product_type=product)
            rows = client._order_rows(pending) if hasattr(client, "_order_rows") else []
            open_orders = [r for r in rows if isinstance(r, dict) and r.get("orderId")]
        except Exception as exc:
            errors.append(f"Open orders unavailable: {type(exc).__name__}")

        for row in raw_positions:
            if not isinstance(row, dict):
                continue
            size = _first_f(row, ("total", "size", "available", "holdVol", "positionSize")) or 0.0
            if size <= 0:
                continue
            symbol = str(row.get("symbol") or "").upper()
            entry = _first_f(row, ("openPriceAvg", "averageOpenPrice", "avgOpenPrice", "openPrice"))
            mark = _first_f(row, ("markPrice", "marketPrice", "lastPrice"))
            hold = str(row.get("holdSide") or "").lower()
            direction = _direction(hold)
            local_state = _matching_local_state(
                local_states,
                symbol=symbol,
                direction=direction,
                exchange_entry=entry,
            )
            symbol_plans = [
                plan
                for plan in tpsl_by_symbol.get(symbol, [])
                if _direction(plan.get("holdSide") or plan.get("posSide")) in {direction, "UNKNOWN"}
            ]
            stop_triggers = [
                trigger
                for plan in symbol_plans
                if _plan_kind(plan) == "STOP_LOSS"
                for trigger in [_plan_trigger(plan)]
                if trigger is not None and trigger > 0
            ]
            tp_triggers = [
                trigger
                for plan in symbol_plans
                if _plan_kind(plan) == "TAKE_PROFIT"
                for trigger in [_plan_trigger(plan)]
                if trigger is not None and trigger > 0
            ]
            row_stop = _first_f(row, ("stopLoss",))
            if row_stop is not None and row_stop > 0:
                stop_triggers.append(row_stop)
            row_tp = _first_f(row, ("takeProfit",))
            if row_tp is not None and row_tp > 0:
                tp_triggers.append(row_tp)
            active_stop = (
                max(stop_triggers)
                if direction == "LONG" and stop_triggers
                else min(stop_triggers)
                if direction == "SHORT" and stop_triggers
                else None
            )
            sl_id = str(row.get("stopLossId") or "")
            tp_id = str(row.get("takeProfitId") or "")
            has_sl = bool(active_stop is not None or sl_id)
            has_tp = bool(tp_triggers or tp_id)

            if has_sl and has_tp:
                protection, prot_status = "PROTECTED", Status.HEALTHY
            elif has_sl or has_tp:
                protection, prot_status = "PARTIAL", Status.DEGRADED
            else:
                protection, prot_status = "UNPROTECTED", Status.BLOCKED

            unrealised_pnl = _first_f(row, ("unrealizedPL", "unrealisedPnl"))
            margin = _first_f(row, ("marginSize", "margin"))
            price_return_pct = None
            if entry is not None and entry > 0 and mark is not None and direction != "UNKNOWN":
                move = mark - entry if direction == "LONG" else entry - mark
                price_return_pct = move / entry * 100.0
            margin_roi_pct = (
                unrealised_pnl / margin * 100.0
                if unrealised_pnl is not None and margin is not None and margin > 0
                else None
            )

            positions.append({
                "symbol": symbol,
                "active_symbol": True,
                "side": direction,
                "size": size,
                # Exchange open-position data is the only displayed current fill.
                "entry": entry,
                "exchange_entry": entry,
                "planned_entry": _first_f(local_state, ("planned_avg_entry",)) if local_state else None,
                "entry_provenance": "BITGET_OPEN_POSITION_API",
                "position_entry_provenance": (
                    str(local_state.get("exchange_avg_entry_source") or "UNKNOWN")
                    if local_state else "LOCAL_STATE_UNMATCHED"
                ),
                "lifecycle_id": (
                    str(local_state.get("position_lifecycle_id") or "")
                    if local_state else ""
                ),
                "local_state_match": bool(local_state),
                "mark": mark,
                "leverage": row.get("leverage"),
                "margin_mode": row.get("marginMode"),
                "margin": margin,
                "unrealised_pnl": unrealised_pnl,
                "monetary_pnl": unrealised_pnl,
                "price_return_pct": price_return_pct,
                "margin_roi_pct": margin_roi_pct,
                "liquidation": _first_f(row, ("liquidationPrice",)),
                "stop_loss": active_stop or ("id:" + sl_id if sl_id else None),
                "active_stop": active_stop,
                "take_profit": tp_triggers or (["id:" + tp_id] if tp_id else []),
                "protection": protection,
                "protection_status": prot_status,
                "protection_state": (
                    str(local_state.get("protection_state") or protection)
                    if local_state else protection
                ),
                "opened_epoch": _f(row.get("cTime"), 0) / 1000.0 or None,
                # These are not carried on the exchange payload. Never guessed.
                "strategy": "UNKNOWN",
                "score": "UNKNOWN",
                "tpsl_orders": symbol_plans,
            })
        reachable = True
    except Exception as exc:
        errors.append(f"Bitget unreachable: {type(exc).__name__}: {str(exc)[:160]}")

    if not reachable:
        signals.add(Signal("exchange", "Bitget API", Status.UNKNOWN,
                           errors[0] if errors else "no response",
                           "Position and balance state cannot be verified."))
    else:
        signals.add(Signal("exchange", "Bitget API", Status.HEALTHY, "reachable (read-only)"))
        unprotected = [p for p in positions if p["protection"] != "PROTECTED"]
        if unprotected:
            signals.add(Signal(
                "protection", "Position protection", Status.BLOCKED,
                f"{len(unprotected)} of {len(positions)} without full exchange-side SL+TP",
                "An unprotected position must block new entries and be resolved first.",
            ))
        elif positions:
            signals.add(Signal("protection", "Position protection", Status.HEALTHY,
                               f"all {len(positions)} protected"))
        else:
            signals.add(Signal("protection", "Position protection", Status.HEALTHY,
                               "flat — nothing to protect"))

    equity = _f(account.get("accountEquity")) if account else None
    return {
        "reachable": reachable,
        "errors": errors,
        "signals": signals,
        "status": signals.status,
        "account": {
            "equity": equity,
            "available": _f(account.get("available")) if account else None,
            "locked": _f(account.get("locked")) if account else None,
            "unrealised": _f(account.get("unrealizedPL")) if account else None,
            "margin_coin": account.get("marginCoin") or "USDT",
        },
        "positions": positions,
        "active_symbols": [p["symbol"] for p in positions],
        "local_state_provenance": local_loaded.provenance,
        "open_orders": open_orders,
        "position_count": len(positions),
        "unprotected_count": sum(1 for p in positions if p["protection"] != "PROTECTED"),
    }


__all__ = ["ALLOWED_CLIENT_METHODS", "build"]
