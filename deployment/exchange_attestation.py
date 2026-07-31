"""Credentialed, GET-only, fail-closed Bitget pre-deployment attestation."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from app.symbol_allowlist import OWNER_APPROVED_PRODUCTION_SYMBOLS


READ_ONLY_CLIENT_METHODS = frozenset({
    "get_all_positions",
    "get_pending_orders",
    "get_tpsl_orders",
    "get_contracts",
    "get_symbol_account",
    "get_symbol_price",
    "get_orderbook",
})
ACTIVE_SYMBOL_STATES = frozenset({"normal", "listed", "trading"})
RESOLVED_INTENT_STATES = frozenset({"REJECTED", "PROTECTED", "ABANDONED", "NOT_SENT"})


class ExchangeReadAdapter(Protocol):
    def open_positions(self) -> dict[str, Any]: ...
    def pending_orders(self) -> dict[str, Any]: ...
    def protection_orders(self, symbol: str | None = None) -> dict[str, Any]: ...
    def contract_metadata(self, symbol: str) -> dict[str, Any]: ...
    def symbol_account(self, symbol: str) -> dict[str, Any]: ...
    def symbol_price(self, symbol: str) -> dict[str, Any]: ...
    def orderbook(self, symbol: str) -> dict[str, Any]: ...
    def local_order_intents(self) -> list[dict[str, Any]]: ...
    def state_quarantine_count(self) -> int: ...


class BitgetReadOnlyAttestationAdapter:
    """Thin GET/file-read wrapper around the production client."""

    def __init__(
        self,
        client,
        *,
        product_type: str,
        margin_coin: str = "USDT",
        intent_path: str | Path = "state/order_intents.json",
    ) -> None:
        self.client = client
        self.product_type = str(product_type).upper()
        self.margin_coin = str(margin_coin).upper()
        self.intent_path = Path(intent_path)

    def open_positions(self) -> dict[str, Any]:
        return self.client.get_all_positions(
            product_type=self.product_type,
            margin_coin=self.margin_coin,
        )

    def pending_orders(self) -> dict[str, Any]:
        return self.client.get_pending_orders(product_type=self.product_type)

    def protection_orders(self, symbol: str | None = None) -> dict[str, Any]:
        return self.client.get_tpsl_orders(
            product_type=self.product_type,
            symbol=symbol,
        )

    def contract_metadata(self, symbol: str) -> dict[str, Any]:
        return self.client.get_contracts(self.product_type, symbol=symbol.upper())

    def symbol_account(self, symbol: str) -> dict[str, Any]:
        return self.client.get_symbol_account(
            symbol=symbol,
            product_type=self.product_type,
            margin_coin=self.margin_coin,
        )

    def symbol_price(self, symbol: str) -> dict[str, Any]:
        return self.client.get_symbol_price(
            symbol=symbol,
            product_type=self.product_type,
        )

    def orderbook(self, symbol: str) -> dict[str, Any]:
        return self.client.get_orderbook(
            symbol=symbol,
            product_type=self.product_type,
            limit=50,
        )

    def local_order_intents(self) -> list[dict[str, Any]]:
        if not self.intent_path.exists():
            return []
        with self.intent_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and "_state_metadata" in payload:
            payload = payload.get("data")
        if not isinstance(payload, list):
            raise ValueError("order intent state must be a list")
        return [row for row in payload if isinstance(row, dict)]

    def state_quarantine_count(self) -> int:
        if not self.intent_path.parent.exists():
            return 0
        return sum(1 for _ in self.intent_path.parent.glob("*.corrupt_*"))


def _rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("entrustedList", "orderList", "list", "orders", "positions", "planList"):
            if key in data:
                nested = data.get(key)
                return [row for row in nested if isinstance(row, dict)] if isinstance(nested, list) else []
        return [data]
    return []


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return float(number) if number.is_finite() else None


def _direction(row: dict[str, Any]) -> str:
    raw = str(row.get("holdSide") or row.get("posSide") or row.get("side") or "").lower()
    if raw in {"long", "buy"}:
        return "LONG"
    if raw in {"short", "sell"}:
        return "SHORT"
    return "UNKNOWN"


def _position_size(row: dict[str, Any]) -> float:
    for key in ("total", "size", "available", "holdVol", "positionSize"):
        value = _number(row.get(key))
        if value is not None and abs(value) > 0:
            return abs(value)
    return 0.0


def _protection_kind(row: dict[str, Any]) -> str:
    raw = str(row.get("planType") or row.get("orderType") or "").lower()
    if "loss" in raw:
        return "STOP_LOSS"
    if "profit" in raw:
        return "TAKE_PROFIT"
    return "UNKNOWN"


def _trigger(row: dict[str, Any]) -> float | None:
    for key in (
        "triggerPrice",
        "planTriggerPrice",
        "stopLossTriggerPrice",
        "stopSurplusTriggerPrice",
    ):
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _isolated_support(contract: dict[str, Any], account: dict[str, Any]) -> bool | None:
    current = str(account.get("marginMode") or account.get("margin_mode") or "").lower()
    if current == "isolated":
        return True
    explicit = (
        contract.get("supportMarginMode")
        or contract.get("marginModes")
        or contract.get("marginMode")
    )
    if explicit in (None, ""):
        return None
    if isinstance(explicit, list):
        modes = {str(value).lower() for value in explicit}
    else:
        modes = {part.strip().lower() for part in str(explicit).replace("|", ",").split(",")}
    return "isolated" in modes


def _active_symbol_status(value: Any) -> bool:
    return str(value or "").strip().lower() in ACTIVE_SYMBOL_STATES


def _isolated_leverages(account: dict[str, Any]) -> tuple[float | None, float | None]:
    common = _number(account.get("leverage"))
    long_leverage = _number(
        account.get("isolatedLongLever")
        or account.get("isolatedLongLeverage")
        or account.get("longLeverage")
    )
    short_leverage = _number(
        account.get("isolatedShortLever")
        or account.get("isolatedShortLeverage")
        or account.get("shortLeverage")
    )
    return long_leverage or common, short_leverage or common


def _spread_limit_bps(symbol: str) -> float:
    # Exact existing hard execution-cost limits from RiskManager.
    return 7.5 if symbol in {"BTCUSDT", "ETHUSDT", "SOLUSDT"} else 5.0


def _orderbook_metrics(payload: Any) -> tuple[float | None, float | None, float | None]:
    if not isinstance(payload, dict):
        return None, None, None
    bid = _number(payload.get("best_bid"))
    ask = _number(payload.get("best_ask"))
    spread_bps = _number(payload.get("spread_bps"))
    if bid is not None or ask is not None:
        return bid, ask, spread_bps
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return None, None, None
    bids = data.get("bids") or []
    asks = data.get("asks") or []
    bid = _number(bids[0][0]) if bids and isinstance(bids[0], list) and bids[0] else None
    ask = _number(asks[0][0]) if asks and isinstance(asks[0], list) and asks[0] else None
    if bid and ask and ask >= bid:
        mid = (bid + ask) / 2
        spread_bps = ((ask - bid) / mid) * 10_000 if mid > 0 else None
    return bid, ask, spread_bps


def _sanitized_order(row: dict[str, Any]) -> dict[str, str]:
    return {
        "symbol": str(row.get("symbol") or "UNKNOWN").upper(),
        "order_id": str(row.get("orderId") or row.get("planOrderId") or row.get("id") or "UNKNOWN"),
    }


def attest_exchange(
    adapter: ExchangeReadAdapter,
    *,
    symbols: list[str] | tuple[str, ...],
    required_leverage: float,
) -> dict[str, Any]:
    """Collect sanitized exchange truth without any order or account mutation."""
    allowlist = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    errors: list[str] = []

    try:
        raw_positions = _rows(adapter.open_positions())
    except Exception as exc:
        raw_positions = []
        errors.append(f"open_positions:{type(exc).__name__}")
    positions = [
        {
            "symbol": str(row.get("symbol") or "").upper(),
            "direction": _direction(row),
            "size": _position_size(row),
        }
        for row in raw_positions
        if _position_size(row) > 0
    ]

    try:
        pending_rows = _rows(adapter.pending_orders())
    except Exception as exc:
        pending_rows = []
        errors.append(f"pending_orders:{type(exc).__name__}")
    open_orders = [_sanitized_order(row) for row in pending_rows]
    pending_entries = []
    ambiguous_open_orders = []
    for row in pending_rows:
        reduce_only = str(row.get("reduceOnly") or "").strip().lower()
        trade_side = str(row.get("tradeSide") or "").strip().lower()
        if reduce_only in {"yes", "true", "1"} or trade_side == "close":
            continue
        if reduce_only in {"no", "false", "0"} or trade_side == "open":
            pending_entries.append(_sanitized_order(row))
        else:
            ambiguous_open_orders.append(_sanitized_order(row))

    try:
        protection_rows = _rows(adapter.protection_orders())
    except Exception as exc:
        protection_rows = []
        errors.append(f"protection_orders:{type(exc).__name__}")
    protections = [
        {
            "symbol": str(row.get("symbol") or "").upper(),
            "direction": _direction(row),
            "kind": _protection_kind(row),
            "order_id": str(row.get("planOrderId") or row.get("orderId") or row.get("id") or ""),
            "trigger_price": _trigger(row),
        }
        for row in protection_rows
    ]
    open_keys = {(row["symbol"], row["direction"]) for row in positions}
    open_symbols = {row["symbol"] for row in positions}
    orphan_protection = [
        row
        for row in protections
        if (
            (row["symbol"], row["direction"]) not in open_keys
            if row["direction"] != "UNKNOWN"
            else row["symbol"] not in open_symbols
        )
    ]
    orphan_stop = [row for row in orphan_protection if row["kind"] == "STOP_LOSS"]
    orphan_take_profit = [row for row in orphan_protection if row["kind"] == "TAKE_PROFIT"]
    unknown_protection = [row for row in protections if row["kind"] == "UNKNOWN"]

    try:
        local_intents = adapter.local_order_intents()
    except Exception as exc:
        local_intents = []
        errors.append(f"local_order_intents:{type(exc).__name__}")
    unresolved_intents = [
        {
            "symbol": str(row.get("symbol") or "UNKNOWN").upper(),
            "state": str(row.get("state") or "UNKNOWN").upper(),
        }
        for row in local_intents
        if str(row.get("state") or "UNKNOWN").upper() not in RESOLVED_INTENT_STATES
    ]
    try:
        quarantine_count = int(adapter.state_quarantine_count())
    except Exception as exc:
        quarantine_count = -1
        errors.append(f"state_quarantine:{type(exc).__name__}")

    contracts = []
    for symbol in allowlist:
        blockers: list[str] = []
        conditions: list[str] = []
        try:
            contract_rows = _rows(adapter.contract_metadata(symbol))
            contract = next(
                (row for row in contract_rows if str(row.get("symbol") or "").upper() == symbol),
                {},
            )
            if not contract:
                raise ValueError("contract absent")
            account_rows = _rows(adapter.symbol_account(symbol))
            account = account_rows[0] if account_rows else {}
            price_rows = _rows(adapter.symbol_price(symbol))
            price_row = next(
                (row for row in price_rows if str(row.get("symbol") or "").upper() == symbol),
                price_rows[0] if len(price_rows) == 1 else {},
            )
            orderbook = adapter.orderbook(symbol)
            best_bid, best_ask, spread_bps = _orderbook_metrics(orderbook)
            # A successful symbol-scoped GET proves the plan-order read surface
            # is supported without creating, editing, or cancelling an order.
            adapter.protection_orders(symbol)

            status = str(contract.get("symbolStatus") or contract.get("status") or "UNKNOWN")
            price_place = int(contract.get("pricePlace"))
            volume_place = int(contract.get("volumePlace"))
            max_leverage = _number(contract.get("maxLever") or contract.get("maxLeverage"))
            current_long_leverage, current_short_leverage = _isolated_leverages(account)
            isolated = _isolated_support(contract, account)
            current_margin_mode = str(account.get("marginMode") or account.get("margin_mode") or "UNKNOWN")
            row = {
                "symbol": symbol,
                "symbol_status": status,
                "symbol_active": _active_symbol_status(status),
                "isolated_support": isolated,
                "current_margin_mode": current_margin_mode,
                "current_long_leverage": current_long_leverage,
                "current_short_leverage": current_short_leverage,
                "max_leverage": max_leverage,
                "required_leverage_supported": bool(max_leverage is not None and max_leverage >= required_leverage),
                "required_leverage_configured": (
                    current_long_leverage == float(required_leverage)
                    and current_short_leverage == float(required_leverage)
                ),
                "minimum_size": _number(contract.get("minTradeNum")),
                "minimum_notional": _number(contract.get("minTradeUSDT")),
                "tick_size": float(Decimal(1).scaleb(-price_place)),
                "size_multiplier": _number(contract.get("sizeMultiplier")),
                "volume_place": volume_place,
                "mark_price": _number(price_row.get("markPrice")),
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread_bps": spread_bps,
                "spread_limit_bps": _spread_limit_bps(symbol),
                "orderbook_valid": bool(best_bid and best_ask and best_bid < best_ask),
                "plan_order_read_supported": True,
                "adapter_supported": True,
            }
            required_positive = {
                "minimum_size": row["minimum_size"],
                "minimum_notional": row["minimum_notional"],
                "tick_size": row["tick_size"],
                "size_multiplier": row["size_multiplier"],
                "max_leverage": row["max_leverage"],
                "mark_price": row["mark_price"],
            }
            blockers.extend(
                name for name, value in required_positive.items()
                if value is None or value <= 0
            )
            if not row["symbol_active"]:
                blockers.append("symbol_inactive")
            if row["isolated_support"] is not True or current_margin_mode.lower() != "isolated":
                blockers.append("isolated_margin_unverified")
            if not row["required_leverage_supported"]:
                blockers.append("required_leverage_unsupported")
            if not row["required_leverage_configured"]:
                blockers.append("required_leverage_not_configured")
            if not row["orderbook_valid"] or spread_bps is None:
                blockers.append("orderbook_invalid")
            elif spread_bps >= row["spread_limit_bps"]:
                conditions.append("spread_at_or_above_execution_hard_limit")

            classification = "BLOCKED" if blockers else "CONDITIONAL" if conditions else "APPROVED"
            row["classification"] = classification
            row["quarantined"] = classification == "BLOCKED"
            row["reasons"] = sorted(set(blockers + conditions))
            row["attested"] = classification == "APPROVED"
            contracts.append(row)
        except Exception as exc:
            errors.append(f"symbol:{symbol}:{type(exc).__name__}")
            contracts.append({
                "symbol": symbol,
                "classification": "BLOCKED",
                "quarantined": True,
                "adapter_supported": False,
                "plan_order_read_supported": False,
                "attested": False,
                "reasons": [f"adapter_or_metadata:{type(exc).__name__}"],
            })

    account_checks = {
        "flat": not positions,
        "no_pending_entries": not pending_entries,
        "no_open_orders": not open_orders,
        "no_ambiguous_open_orders": not ambiguous_open_orders,
        "no_orphan_stop_loss": not orphan_stop,
        "no_orphan_take_profit": not orphan_take_profit,
        "no_unknown_protection_orders": not unknown_protection,
        "no_unresolved_local_order_intents": not unresolved_intents,
        "no_state_quarantine_artifacts": quarantine_count == 0,
    }
    owner_allowlist_match = allowlist == OWNER_APPROVED_PRODUCTION_SYMBOLS
    all_symbols_approved = bool(
        owner_allowlist_match
        and len(contracts) == len(allowlist)
        and all(row.get("classification") == "APPROVED" for row in contracts)
    )
    ready = bool(not errors and all(account_checks.values()) and all_symbols_approved)
    return {
        "attestation_kind": "CREDENTIALED_PREDEPLOY_EXCHANGE",
        "read_only": True,
        "adapter": type(adapter).__name__,
        "deployment_gate": "PASS" if ready else "FAIL",
        "allowlist": list(allowlist),
        "allowlist_count": len(allowlist),
        "owner_allowlist_match": owner_allowlist_match,
        "required_leverage": float(required_leverage),
        "account_checks": account_checks,
        "open_positions": positions,
        "open_orders": open_orders,
        "pending_entries": pending_entries,
        "ambiguous_open_orders": ambiguous_open_orders,
        "active_stop_orders": [row for row in protections if row["kind"] == "STOP_LOSS"],
        "active_take_profit_orders": [row for row in protections if row["kind"] == "TAKE_PROFIT"],
        "orphan_stop_orders": orphan_stop,
        "orphan_take_profit_orders": orphan_take_profit,
        "unknown_protection_orders": unknown_protection,
        "unresolved_local_order_intents": unresolved_intents,
        "state_quarantine_artifact_count": quarantine_count,
        "all_symbols_approved": all_symbols_approved,
        "contracts": contracts,
        "errors": sorted(set(errors)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the credentialed GET-only pre-deployment exchange gate."
    )
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--required-leverage", required=True, type=float)
    parser.add_argument("--intent-path", default="state/order_intents.json")
    args = parser.parse_args()

    # Credentials are supplied only through the authorised operator's process
    # environment. The tool never opens an env file and never serializes them.
    from app.config import Settings
    from app.symbol_allowlist import parse_symbol_allowlist
    from clients.bitget_rest import BitgetRestClient

    settings = Settings(_env_file=None)
    client = BitgetRestClient(settings=settings)
    adapter = BitgetReadOnlyAttestationAdapter(
        client,
        product_type=settings.bitget_product_type,
        margin_coin=settings.bitget_margin_coin,
        intent_path=args.intent_path,
    )
    result = attest_exchange(
        adapter,
        symbols=parse_symbol_allowlist(args.symbols, required=True),
        required_leverage=args.required_leverage,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["deployment_gate"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BitgetReadOnlyAttestationAdapter",
    "ExchangeReadAdapter",
    "READ_ONLY_CLIENT_METHODS",
    "attest_exchange",
]
