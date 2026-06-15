from __future__ import annotations

from typing import Any

from app.config import Settings
from clients.bitget_account_client import BitgetAccountClientMixin
from clients.bitget_base_client import BitgetAPIError, BitgetBaseClient, BitgetRetryableError
from clients.bitget_market_client import BitgetMarketClientMixin
from clients.bitget_order_client import BitgetOrderClientMixin
from clients.bitget_precision import BitgetPrecisionMixin
from clients.bitget_tpsl_client import BitgetTPSLClientMixin


class BitgetRestClient(
    BitgetBaseClient,
    BitgetMarketClientMixin,
    BitgetPrecisionMixin,
    BitgetAccountClientMixin,
    BitgetOrderClientMixin,
    BitgetTPSLClientMixin,
):
    """Compatibility wrapper for the split Bitget REST client."""

    def __init__(self, settings: Settings, timeout: int = 15) -> None:
        super().__init__(settings=settings, timeout=timeout)


    def place_futures_order(self, *args, **kwargs):
        return self.place_futures_market_order(*args, **kwargs)

    def is_insufficient_balance_error(self, exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "40762" in text
            or "insufficient" in text
            or "balance" in text
            or "margin not enough" in text
        )

    def emergency_flatten_all(self) -> dict[str, Any]:
        """Emergency kill-switch: close every live position and cleanup TPSL."""
        results: dict[str, Any] = {
            "status": "OK",
            "positions_found": 0,
            "closed": [],
            "errors": [],
        }

        payload = self.get_all_positions()
        positions = payload.get("data") or []

        live_positions = []
        for position in positions:
            try:
                size = float(position.get("total") or position.get("size") or 0)
            except (TypeError, ValueError):
                size = 0.0

            if size > 0:
                live_positions.append(position)

        results["positions_found"] = len(live_positions)

        for position in live_positions:
            symbol = str(position.get("symbol") or "").upper()
            hold_side = str(position.get("holdSide") or "").lower()
            direction = "LONG" if hold_side == "long" else "SHORT"

            try:
                close_result = self.close_futures_position_full(
                    symbol=symbol,
                    direction=direction,
                    reason="emergency_flatten_all",
                    cleanup_tpsl=True,
                )

                results["closed"].append(
                    {
                        "symbol": symbol,
                        "direction": direction,
                        "result": close_result,
                    }
                )

                self.log.critical(
                    "EMERGENCY_FLATTEN_ALL_CLOSE | %s | direction=%s",
                    symbol,
                    direction,
                )

            except Exception as exc:
                results["errors"].append(
                    {
                        "symbol": symbol,
                        "error": str(exc),
                    }
                )

                self.log.critical(
                    "EMERGENCY_FLATTEN_ALL_FAILED | %s | error=%s",
                    symbol,
                    exc,
                )

        if results["errors"]:
            results["status"] = "PARTIAL_ERROR"

        return results

    def extract_order_id(self, payload: dict[str, Any] | None) -> str:
        if not isinstance(payload, dict):
            return ""

        data = payload.get("data")

        if isinstance(data, dict):
            order_id = data.get("orderId") or data.get("order_id")
            if order_id:
                return str(order_id)

            order_ids = data.get("orderIdList") or data.get("orderIds") or data.get("order_id_list")
            if isinstance(order_ids, list) and order_ids:
                first = order_ids[0]
                if isinstance(first, dict):
                    return str(first.get("orderId") or first.get("order_id") or first.get("id") or "")
                return str(first or "")

            client_oid = data.get("clientOid") or data.get("client_oid")
            if client_oid:
                return str(client_oid)

        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                return str(first.get("orderId") or first.get("order_id") or first.get("id") or "")
            return str(first or "")

        return str(payload.get("orderId") or payload.get("order_id") or payload.get("id") or "")


__all__ = [
    "BitgetAPIError",
    "BitgetRetryableError",
    "BitgetRestClient",
]
