"""Credentialed, read-only Bitget attestation for a later deployment window."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol


READ_ONLY_CLIENT_METHODS = frozenset({
    "get_all_positions",
    "get_pending_orders",
    "get_tpsl_orders",
    "get_contracts",
    "get_symbol_account",
})


class ExchangeReadAdapter(Protocol):
    def open_positions(self) -> dict[str, Any]: ...
    def pending_orders(self) -> dict[str, Any]: ...
    def protection_orders(self) -> dict[str, Any]: ...
    def contract_metadata(self, symbol: str) -> dict[str, Any]: ...
    def symbol_account(self, symbol: str) -> dict[str, Any]: ...


class BitgetReadOnlyAttestationAdapter:
    """Thin GET-only wrapper around the production client."""

    def __init__(self, client, *, product_type: str, margin_coin: str = "USDT") -> None:
        self.client = client
        self.product_type = str(product_type).upper()
        self.margin_coin = str(margin_coin).upper()

    def open_positions(self) -> dict[str, Any]:
        return self.client.get_all_positions(
            product_type=self.product_type,
            margin_coin=self.margin_coin,
        )

    def pending_orders(self) -> dict[str, Any]:
        return self.client.get_pending_orders(product_type=self.product_type)

    def protection_orders(self) -> dict[str, Any]:
        return self.client.get_tpsl_orders(product_type=self.product_type)

    def contract_metadata(self, symbol: str) -> dict[str, Any]:
        return self.client.get_contracts(self.product_type, symbol=symbol.upper())

    def symbol_account(self, symbol: str) -> dict[str, Any]:
        return self.client.get_symbol_account(
            symbol=symbol,
            product_type=self.product_type,
            margin_coin=self.margin_coin,
        )


def _rows(payload: Any) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    if isinstance(data, dict):
        for key in ("entrustedList", "orderList", "list", "orders", "positions"):
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


def attest_exchange(
    adapter: ExchangeReadAdapter,
    *,
    symbols: list[str] | tuple[str, ...],
    required_leverage: float,
) -> dict[str, Any]:
    """Collect sanitized exchange truth without placing, changing, or cancelling orders."""
    allowlist = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()))
    errors: list[str] = []

    try:
        raw_positions = _rows(adapter.open_positions())
    except Exception as exc:
        raw_positions = []
        errors.append(f"open_positions:{type(exc).__name__}")
    positions = [
        {"symbol": str(row.get("symbol") or "").upper(), "direction": _direction(row),
         "size": _position_size(row)}
        for row in raw_positions if _position_size(row) > 0
    ]

    try:
        pending_rows = _rows(adapter.pending_orders())
    except Exception as exc:
        pending_rows = []
        errors.append(f"pending_orders:{type(exc).__name__}")
    pending_entries = []
    for row in pending_rows:
        reduce_only = str(row.get("reduceOnly") or "").lower()
        trade_side = str(row.get("tradeSide") or "").lower()
        if reduce_only in {"yes", "true", "1"} or trade_side == "close":
            continue
        pending_entries.append({
            "symbol": str(row.get("symbol") or "UNKNOWN").upper(),
            "order_id": str(row.get("orderId") or row.get("id") or "UNKNOWN"),
        })

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
        row for row in protections
        if (
            (row["symbol"], row["direction"]) not in open_keys
            if row["direction"] != "UNKNOWN"
            else row["symbol"] not in open_symbols
        )
    ]

    contracts = []
    for symbol in allowlist:
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
            price_place = int(contract.get("pricePlace"))
            max_leverage = _number(contract.get("maxLever") or contract.get("maxLeverage"))
            isolated = _isolated_support(contract, account)
            row = {
                "symbol": symbol,
                "symbol_status": str(contract.get("symbolStatus") or contract.get("status") or "UNKNOWN"),
                "isolated_support": isolated,
                "current_margin_mode": str(account.get("marginMode") or "UNKNOWN"),
                "current_leverage": _number(account.get("leverage")),
                "max_leverage": max_leverage,
                "required_leverage_supported": bool(
                    max_leverage is not None and max_leverage >= required_leverage
                ),
                "minimum_size": _number(contract.get("minTradeNum")),
                "minimum_notional": _number(contract.get("minTradeUSDT")),
                "tick_size": float(Decimal(1).scaleb(-price_place)),
                "size_multiplier": _number(contract.get("sizeMultiplier")),
                "volume_place": int(contract.get("volumePlace")),
            }
            required_values = (
                row["minimum_size"], row["minimum_notional"], row["tick_size"],
                row["size_multiplier"], row["max_leverage"],
            )
            row["attested"] = bool(
                all(value is not None and value > 0 for value in required_values)
                and row["isolated_support"] is True
                and row["required_leverage_supported"]
            )
            contracts.append(row)
        except Exception as exc:
            errors.append(f"contract:{symbol}:{type(exc).__name__}")
            contracts.append({"symbol": symbol, "attested": False})

    ready = bool(
        allowlist
        and not errors
        and not positions
        and not pending_entries
        and not orphan_protection
        and len(contracts) == len(allowlist)
        and all(row.get("attested") for row in contracts)
    )
    return {
        "attestation_kind": "CREDENTIALED_PREDEPLOY_EXCHANGE",
        "read_only": True,
        "code_level_readiness": True,
        "credentialed_attestation_complete": not errors,
        "deployment_gate": "PASS" if ready else "BLOCKED",
        "allowlist": list(allowlist),
        "required_leverage": float(required_leverage),
        "open_positions": positions,
        "pending_entries": pending_entries,
        "active_stop_orders": [row for row in protections if row["kind"] == "STOP_LOSS"],
        "active_take_profit_orders": [row for row in protections if row["kind"] == "TAKE_PROFIT"],
        "orphan_protection_orders": orphan_protection,
        "contracts": contracts,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the credentialed GET-only pre-deployment exchange gate."
    )
    parser.add_argument("--symbols", required=True)
    parser.add_argument("--required-leverage", required=True, type=float)
    args = parser.parse_args()

    # Environment must be supplied explicitly by the authorised operator.  The
    # tool never opens an env file itself and never serializes credentials.
    from app.config import Settings
    from app.symbol_allowlist import parse_symbol_allowlist
    from clients.bitget_rest import BitgetRestClient

    settings = Settings(_env_file=None)
    client = BitgetRestClient(settings=settings)
    adapter = BitgetReadOnlyAttestationAdapter(
        client,
        product_type=settings.bitget_product_type,
        margin_coin=settings.bitget_margin_coin,
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
