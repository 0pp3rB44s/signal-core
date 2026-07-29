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


def build(client_factory=None) -> dict[str, Any]:
    """Fetch account + positions. ``client_factory`` is injectable for tests."""
    signals = SignalSet()
    errors: list[str] = []
    reachable = False
    account: dict[str, Any] = {}
    positions: list[dict[str, Any]] = []
    open_orders: list[dict[str, Any]] = []

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
            sl_value = str(row.get("stopLoss") or "")
            sl_id = str(row.get("stopLossId") or "")
            tp_value = str(row.get("takeProfit") or "")
            tp_id = str(row.get("takeProfitId") or "")
            has_sl = bool(sl_value or sl_id)
            has_tp = bool(tp_value or tp_id)

            if has_sl and has_tp:
                protection, prot_status = "PROTECTED", Status.HEALTHY
            elif has_sl or has_tp:
                protection, prot_status = "PARTIAL", Status.DEGRADED
            else:
                protection, prot_status = "UNPROTECTED", Status.BLOCKED

            positions.append({
                "symbol": symbol,
                "side": "LONG" if hold == "long" else ("SHORT" if hold == "short" else "UNKNOWN"),
                "size": size,
                "entry": entry,
                "mark": mark,
                "leverage": row.get("leverage"),
                "margin_mode": row.get("marginMode"),
                "margin": _first_f(row, ("marginSize", "margin")),
                "unrealised_pnl": _first_f(row, ("unrealizedPL", "unrealisedPnl")),
                "liquidation": _first_f(row, ("liquidationPrice",)),
                "stop_loss": sl_value or ("id:" + sl_id if sl_id else None),
                "take_profit": tp_value or ("id:" + tp_id if tp_id else None),
                "protection": protection,
                "protection_status": prot_status,
                "opened_epoch": _f(row.get("cTime"), 0) / 1000.0 or None,
                # These are not carried on the exchange payload. Never guessed.
                "strategy": "UNKNOWN",
                "score": "UNKNOWN",
                "tpsl_orders": tpsl_by_symbol.get(symbol, []),
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
        "open_orders": open_orders,
        "position_count": len(positions),
        "unprotected_count": sum(1 for p in positions if p["protection"] != "PROTECTED"),
    }


__all__ = ["ALLOWED_CLIENT_METHODS", "build"]
