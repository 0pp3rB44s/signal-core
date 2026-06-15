from __future__ import annotations

import time
from typing import Any


class BitgetOrderClientMixin:
    """Market order, order detail/history, fill metrics, leverage, and close logic only."""

    def get_order_history(
        self,
        product_type: str | None = None,
        symbol: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "limit": str(limit),
        }
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request("GET", "/api/v2/mix/order/orders-history", params=params, private=True)

    def get_order_detail(
        self,
        symbol: str,
        order_id: str,
        product_type: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "orderId": str(order_id),
        }
        return self._request("GET", "/api/v2/mix/order/detail", params=params, private=True)

    def extract_fill_metrics(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data") or {}
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = {}

        def _first_float(*keys: str) -> float:
            for key in keys:
                value = data.get(key)
                if value is None:
                    continue
                try:
                    return float(value)
                except (TypeError, ValueError):
                    continue
            return 0.0

        def _first_str(*keys: str) -> str:
            for key in keys:
                value = data.get(key)
                if value is not None:
                    return str(value)
            return ""

        return {
            "order_id": _first_str("orderId", "clientOid", "id"),
            "avg_price": _first_float("avgPrice", "priceAvg", "fillPrice", "price"),
            "filled_qty": _first_float("baseVolume", "filledQty", "sizeQty", "fillSize", "size"),
            "fee": _first_float("fee", "totalFee", "fillFee"),
            "pnl": _first_float("pnl", "profit", "totalProfits"),
            "state": _first_str("state", "status"),
            "raw": data,
        }

    def set_futures_leverage(
        self,
        symbol: str,
        leverage: int,
        hold_side: str,
        margin_mode: str = "isolated",
        product_type: str | None = None,
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "symbol": symbol.upper(),
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "marginCoin": margin_coin.upper(),
            "marginMode": margin_mode,
            "leverage": str(int(leverage)),
            "holdSide": hold_side.lower(),
        }

        return self._request(
            method="POST",
            path="/api/v2/mix/account/set-leverage",
            body=body,
            private=True,
        )

    def place_futures_market_order(
        self,
        symbol: str,
        direction: str | None = None,
        size: float = 0.0,
        margin_mode: str = "isolated",
        product_type: str | None = None,
        margin_coin: str = "USDT",
        client_oid: str | None = None,
        side: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Place a Bitget futures market entry order."""
        if direction is None and side is not None:
            side_lower = side.lower()
            if side_lower in {"buy", "long"}:
                direction = "LONG"
            elif side_lower in {"sell", "short"}:
                direction = "SHORT"

        if direction is None:
            raise ValueError("Futures market order requires direction or side")
        direction_upper = direction.upper()
        if direction_upper not in {"LONG", "SHORT"}:
            raise ValueError(f"Unsupported futures direction: {direction}")

        side = "buy" if direction_upper == "LONG" else "sell"
        hold_side = "long" if direction_upper == "LONG" else "short"
        formatted_size = self._format_size(symbol, float(size))

        if formatted_size < self._min_size(symbol):
            raise ValueError(
                f"Order size below minimum for {symbol}: size={formatted_size} min={self._min_size(symbol)}"
            )

        body: dict[str, Any] = {
            "symbol": symbol.upper(),
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "marginCoin": margin_coin.upper(),
            "marginMode": margin_mode,
            "size": str(formatted_size),
            "side": side,
            "tradeSide": "open",
            "orderType": "market",
            "holdSide": hold_side,
        }

        if client_oid:
            body["clientOid"] = client_oid

        self._validate_futures_order_flags(body)

        self.log.warning(
            "BITGET_PLACE_MARKET_ORDER | %s | direction=%s | side=%s | hold_side=%s | size=%s | margin_mode=%s",
            symbol.upper(),
            direction_upper,
            side,
            hold_side,
            formatted_size,
            margin_mode,
        )

        return self._request(
            method="POST",
            path="/api/v2/mix/order/place-order",
            body=body,
            private=True,
        )

    def _verify_reduce_only_close_body(
        self,
        body: dict[str, Any],
        symbol: str,
        hold_side: str,
    ) -> None:
        trade_side = str(body.get("tradeSide") or "").lower()
        reduce_only = str(body.get("reduceOnly") or "").upper()
        order_type = str(body.get("orderType") or "").lower()
        size = str(body.get("size") or "").strip()

        if trade_side != "close" or reduce_only != "YES" or order_type != "market" or not size:
            self.log.critical(
                "REDUCE_ONLY_VERIFY_FAILED | %s | hold_side=%s | tradeSide=%s | reduceOnly=%s | orderType=%s | size=%s | body=%s",
                symbol.upper(),
                hold_side.lower(),
                body.get("tradeSide"),
                body.get("reduceOnly"),
                body.get("orderType"),
                body.get("size"),
                body,
            )
            raise ValueError(
                f"Close order must be reduce-only market close for {symbol}; body={body}"
            )

        self.log.warning(
            "REDUCE_ONLY_VERIFY_OK | %s | hold_side=%s | tradeSide=%s | reduceOnly=%s | orderType=%s | size=%s",
            symbol.upper(),
            hold_side.lower(),
            body.get("tradeSide"),
            body.get("reduceOnly"),
            body.get("orderType"),
            body.get("size"),
        )

    def close_futures_position(
        self,
        symbol: str,
        hold_side: str,
        size: float,
        margin_mode: str = "isolated",
        product_type: str | None = None,
        margin_coin: str = "USDT",
        client_oid: str | None = None,
    ) -> dict[str, Any]:
        """Close/reduce an existing Bitget futures position."""
        hold_side_lower = hold_side.lower()

        if hold_side_lower not in {"long", "short"}:
            raise ValueError(f"Unsupported hold side: {hold_side}")

        side = "sell" if hold_side_lower == "long" else "buy"
        formatted_size = self._format_size(symbol, float(size))

        if formatted_size <= 0:
            raise ValueError(f"Close size invalid for {symbol}: {formatted_size}")

        body: dict[str, Any] = {
            "symbol": symbol.upper(),
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "marginCoin": margin_coin.upper(),
            "marginMode": margin_mode,
            "size": str(formatted_size),
            "side": side,
            "tradeSide": "close",
            "orderType": "market",
            "holdSide": hold_side_lower,
            "reduceOnly": "YES",
        }

        if client_oid:
            body["clientOid"] = client_oid

        self._validate_futures_order_flags(body)
        self._verify_reduce_only_close_body(
            body=body,
            symbol=symbol,
            hold_side=hold_side_lower,
        )

        self.log.warning(
            "BITGET_CLOSE_POSITION | %s | hold_side=%s | side=%s | size=%s",
            symbol.upper(),
            hold_side_lower,
            side,
            formatted_size,
        )

        return self._request(
            method="POST",
            path="/api/v2/mix/order/place-order",
            body=body,
            private=True,
        )

    def close_futures_position_full(
        self,
        symbol: str,
        direction: str,
        margin_mode: str = "isolated",
        reason: str | None = None,
        cleanup_tpsl: bool = True,
        size: float | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Close the full live remaining position and optionally cleanup stale TP/SL orders."""
        symbol = symbol.upper()
        direction_upper = direction.upper()
        hold_side = "long" if direction_upper == "LONG" else "short"
        close_reason = reason or "close_all"
        provided_size = float(size or 0.0)
        live_size = self._live_position_size_for_symbol(symbol, hold_side=hold_side)
        if live_size <= 0 and provided_size > 0:
            live_size = provided_size
            self.log.critical(
                "CLOSE_FULL_USING_PROVIDED_SIZE_FALLBACK | %s | hold_side=%s | provided_size=%s | reason=%s",
                symbol,
                hold_side,
                provided_size,
                reason,
            )

        results: dict[str, Any] = {
            "status": "PENDING",
            "symbol": symbol,
            "direction": direction_upper,
            "hold_side": hold_side,
            "live_size": live_size,
            "reason": close_reason,
            "cleanup_tpsl": bool(cleanup_tpsl),
            "cleanup_before": None,
            "close": None,
            "cleanup_after": None,
        }

        if cleanup_tpsl:
            try:
                results["cleanup_before"] = self.cancel_all_futures_tpsl_orders(
                    symbol=symbol,
                    hold_side=hold_side,
                )
            except Exception as exc:
                results["cleanup_before"] = {
                    "status": "CLEANUP_FAILED",
                    "error": str(exc),
                }

        if live_size <= 0:
            results["status"] = "NO_POSITION"
            self.log.warning(
                "CLOSE_FULL_NO_POSITION | %s | direction=%s | hold_side=%s | reason=%s | cleanup_tpsl=%s",
                symbol,
                direction_upper,
                hold_side,
                close_reason,
                cleanup_tpsl,
            )
            return results

        results["close"] = self.close_futures_position(
            symbol=symbol,
            hold_side=hold_side,
            size=live_size,
            margin_mode=margin_mode,
            client_oid=f"close-full-{symbol.lower()}-{int(time.time())}",
        )

        results["status"] = "CLOSED"

        if cleanup_tpsl:
            try:
                results["cleanup_after"] = self.cancel_all_futures_tpsl_orders(
                    symbol=symbol,
                    hold_side=hold_side,
                )
            except Exception as exc:
                results["cleanup_after"] = {
                    "status": "CLEANUP_FAILED",
                    "error": str(exc),
                }

        self.log.warning(
            "CLOSE_FULL_POSITION_DONE | %s | status=%s | direction=%s | live_size=%s | cleanup_tpsl=%s | reason=%s",
            symbol,
            results.get("status"),
            direction_upper,
            live_size,
            cleanup_tpsl,
            close_reason,
        )

        return results

