from __future__ import annotations

import time
from typing import Any


#: Bitget business codes / messages that definitively mean "no such order".
#: Anything outside this set leaves the exchange state UNKNOWN, which must never
#: be treated as "safe to submit again".
_ORDER_ABSENT_CODES = frozenset({"22001", "43001", "40109", "40768"})
_ORDER_ABSENT_MARKERS = (
    "does not exist",
    "not exist",
    "no order",
    "order not found",
)

#: Bitget v2 hedge mode: when ``tradeSide`` is ``close``, ``side`` names the
#: *position being closed*, not the direction of the closing fill. Proven
#: against this account's own filled closes:
#:
#:     posSide=long   side=buy    tradeSide=close   filled
#:     posSide=short  side=sell   tradeSide=close   filled
#:
#: The inverted pair is well-formed -- every field is individually valid, so
#: ``_validate_futures_order_flags`` passes it -- and Bitget rejects the whole
#: order with 400 ``22002 No position to close``, because there is no position
#: on the side it was asked to close.
_CLOSE_SIDE_BY_HOLD_SIDE = {"long": "buy", "short": "sell"}

#: Bitget's answer to "close this side" when that side holds nothing. It says
#: something about the *exchange*, never about whether our close was well
#: formed, so it must trigger a re-read rather than a retry.
_NO_POSITION_TO_CLOSE_CODES = frozenset({"22002"})
_NO_POSITION_TO_CLOSE_MARKERS = ("no position to close",)


#: The only ``close_futures_position_full`` statuses that mean the exchange
#: holds nothing on this side any more. A caller may record a local close on
#: these and on nothing else -- PARTIALLY_CLOSED and CLOSE_NOT_REFLECTED both
#: leave a real, protected position behind.
CLOSE_CONFIRMED_FLAT_STATUSES = frozenset({"CLOSED", "NO_POSITION"})


def close_side_for_hold_side(hold_side: str) -> str:
    """The ``side`` a hedge-mode reduce-only close must carry for ``hold_side``.

    Single source of truth: every close path routes through here so they cannot
    drift apart again, and the payload verifier checks against this same table
    rather than restating the rule.
    """
    normalised = str(hold_side or "").lower()
    try:
        return _CLOSE_SIDE_BY_HOLD_SIDE[normalised]
    except KeyError:
        raise ValueError(
            f"Unsupported hold side for close: {hold_side!r}; expected long or short"
        ) from None


def is_no_position_to_close_error(exc: Exception) -> bool:
    """True when Bitget says the side we asked to close holds no position."""
    text = str(exc).lower()
    if any(marker in text for marker in _NO_POSITION_TO_CLOSE_MARKERS):
        return True
    return any(f"code={code}" in text for code in _NO_POSITION_TO_CLOSE_CODES)


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

    def get_order_detail_by_client_oid(
        self,
        symbol: str,
        client_oid: str,
        product_type: str | None = None,
    ) -> dict[str, Any]:
        """Look up one order by the clientOid we chose before submitting it."""
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "clientOid": str(client_oid),
        }
        return self._request("GET", "/api/v2/mix/order/detail", params=params, private=True)

    def get_pending_orders(
        self,
        symbol: str | None = None,
        product_type: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "limit": str(limit),
        }
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request(
            "GET", "/api/v2/mix/order/orders-pending", params=params, private=True
        )

    @staticmethod
    def _order_rows(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
        data = (payload or {}).get("data")
        if isinstance(data, dict):
            # Bitget returns a list-envelope ({"entrustedList": null, "endId": null})
            # when there are no orders. Treating that envelope as an order row
            # invents a phantom order in every readout, so detect the envelope by
            # its keys rather than by whether the list happens to be populated.
            for key in ("entrustedList", "orderList", "list"):
                if key in data:
                    entrust = data.get(key)
                    return [row for row in entrust if isinstance(row, dict)] if isinstance(entrust, list) else []
            return [data]
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []

    @classmethod
    def _is_definitive_absent_error(cls, exc: Exception) -> bool:
        """True only when the exchange explicitly said the order does not exist."""
        text = str(exc).lower()
        if any(marker in text for marker in _ORDER_ABSENT_MARKERS):
            return True
        return any(f"code={code}" in text for code in _ORDER_ABSENT_CODES)

    def find_order_by_client_oid(
        self,
        symbol: str,
        client_oid: str,
        product_type: str | None = None,
    ) -> dict[str, Any]:
        """Resolve what the exchange knows about ``client_oid``.

        Returns ``{"status": FOUND|ABSENT|UNKNOWN, "order": dict|None,
        "source": str, "errors": list[str]}``.

        ABSENT is only returned when every lookup route explicitly reported "no
        such order". Any inconclusive route yields UNKNOWN, because guessing
        "there is no order" is exactly how a duplicate position gets created.
        """
        symbol_upper = symbol.upper()
        client_oid = str(client_oid)
        errors: list[str] = []
        absent_votes = 0
        routes = 0

        lookups = (
            ("order_detail", lambda: self.get_order_detail_by_client_oid(
                symbol=symbol_upper, client_oid=client_oid, product_type=product_type)),
            ("orders_pending", lambda: self.get_pending_orders(
                symbol=symbol_upper, product_type=product_type)),
            ("orders_history", lambda: self.get_order_history(
                symbol=symbol_upper, product_type=product_type, limit=100)),
        )

        for source, lookup in lookups:
            routes += 1
            try:
                payload = lookup()
            except Exception as exc:
                if self._is_definitive_absent_error(exc):
                    absent_votes += 1
                    continue
                errors.append(f"{source}={exc}")
                self.log.warning(
                    "ORDER_LOOKUP_INCONCLUSIVE | %s | source=%s | client_oid=%s | error=%s",
                    symbol_upper,
                    source,
                    client_oid,
                    exc,
                )
                continue

            for row in self._order_rows(payload):
                row_oid = str(row.get("clientOid") or row.get("client_oid") or "")
                if row_oid and row_oid == client_oid:
                    self.log.critical(
                        "ORDER_LOOKUP_FOUND | %s | source=%s | client_oid=%s | order_id=%s | state=%s",
                        symbol_upper,
                        source,
                        client_oid,
                        row.get("orderId") or row.get("order_id") or "",
                        row.get("state") or row.get("status") or "",
                    )
                    return {
                        "status": "FOUND",
                        "order": row,
                        "source": source,
                        "errors": errors,
                    }

            absent_votes += 1

        if absent_votes == routes and not errors:
            self.log.critical(
                "ORDER_LOOKUP_ABSENT | %s | client_oid=%s | routes=%s",
                symbol_upper,
                client_oid,
                routes,
            )
            return {"status": "ABSENT", "order": None, "source": "all_routes", "errors": []}

        self.log.critical(
            "ORDER_LOOKUP_UNKNOWN | %s | client_oid=%s | absent_votes=%s/%s | errors=%s",
            symbol_upper,
            client_oid,
            absent_votes,
            routes,
            " | ".join(errors) or "-",
        )
        return {"status": "UNKNOWN", "order": None, "source": "inconclusive", "errors": errors}

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
        reference_price: float | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Place a Bitget futures market entry order.

        ``reference_price`` is the planned entry used to validate the exchange
        minimum notional. A market order carries no price of its own, so without
        it the minTradeUSDT floor cannot be checked locally.
        """
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

        # Refuse before touching the network at all when the runtime is pinned to
        # forward paper. Contract metadata is a *public* endpoint, so validating
        # size first would issue an HTTP request on a path that can never place
        # an order, and would surface a size error instead of the real reason.
        self._assert_order_transport_allowed()

        side = "buy" if direction_upper == "LONG" else "sell"
        hold_side = "long" if direction_upper == "LONG" else "short"

        # Exchange-derived validation, quantized DOWN. A market order carries no
        # price, so the min-notional floor cannot be evaluated here; the exchange
        # enforces its own minTradeUSDT, so a miss surfaces as a rejection rather
        # than as an unnoticed risk breach.
        normalized, reason = self.validate_entry_size(
            symbol, float(size), reference_price=reference_price)
        if reason is not None:
            raise ValueError(
                f"Order size rejected for {symbol}: reason={reason} "
                f"requested={size} normalized={normalized} "
                f"reference_price={reference_price}"
            )
        formatted_size = float(normalized)

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
            "BITGET_PLACE_MARKET_ORDER | %s | direction=%s | side=%s | hold_side=%s | size=%s | margin_mode=%s | client_oid=%s",
            symbol.upper(),
            direction_upper,
            side,
            hold_side,
            formatted_size,
            margin_mode,
            client_oid or "-",
        )

        # Entry creation is not idempotent at the transport layer: never retry
        # blindly. Failures are classified for clientOid reconciliation instead.
        return self._request(
            method="POST",
            path="/api/v2/mix/order/place-order",
            body=body,
            private=True,
            allow_blind_retry=False,
            client_oid=client_oid or "",
        )

    def place_futures_limit_order(
        self,
        symbol: str,
        direction: str,
        size: float,
        price: float,
        margin_mode: str = "isolated",
        product_type: str | None = None,
        margin_coin: str = "USDT",
        client_oid: str | None = None,
        post_only: bool = True,
        **_: Any,
    ) -> dict[str, Any]:
        """Place a futures LIMIT entry. post_only=True forces maker-only:
        Bitget rejects/adjusts the order if it would cross the spread, so we
        never pay taker fees (maker-entry experiment, 2026-07-08)."""
        direction_upper = str(direction).upper()
        if direction_upper not in {"LONG", "SHORT"}:
            raise ValueError(f"Unsupported futures direction: {direction}")

        # Refuse before touching the network at all when the runtime is pinned to
        # forward paper. Contract metadata is a *public* endpoint, so validating
        # size first would issue an HTTP request on a path that can never place
        # an order, and would surface a size error instead of the real reason.
        self._assert_order_transport_allowed()

        side = "buy" if direction_upper == "LONG" else "sell"
        hold_side = "long" if direction_upper == "LONG" else "short"
        formatted_price = self._format_trigger_price(symbol, float(price))

        if formatted_price <= 0:
            raise ValueError(f"Invalid limit price for {symbol}: {formatted_price}")

        # A limit order has a price, so the min-notional floor is checked too.
        normalized, reason = self.validate_entry_size(
            symbol, float(size), reference_price=formatted_price)
        if reason is not None:
            raise ValueError(
                f"Order size rejected for {symbol}: reason={reason} "
                f"requested={size} normalized={normalized} price={formatted_price}"
            )
        formatted_size = float(normalized)

        body: dict[str, Any] = {
            "symbol": symbol.upper(),
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "marginCoin": margin_coin.upper(),
            "marginMode": margin_mode,
            "size": str(formatted_size),
            "price": str(formatted_price),
            "side": side,
            "tradeSide": "open",
            "orderType": "limit",
            "force": "post_only" if post_only else "gtc",
            "holdSide": hold_side,
        }
        if client_oid:
            body["clientOid"] = client_oid

        self._validate_futures_order_flags(body)
        self.log.warning(
            "BITGET_PLACE_LIMIT_ORDER | %s | direction=%s | side=%s | size=%s | price=%s | post_only=%s | client_oid=%s",
            symbol.upper(), direction_upper, side, formatted_size, formatted_price, post_only,
            client_oid or "-",
        )
        # Same rule as the market entry: one attempt, classified outcome.
        return self._request(
            method="POST",
            path="/api/v2/mix/order/place-order",
            body=body,
            private=True,
            allow_blind_retry=False,
            client_oid=client_oid or "",
        )

    def cancel_futures_order(
        self,
        symbol: str,
        order_id: str | None = None,
        client_oid: str | None = None,
        product_type: str | None = None,
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        """Cancel an open futures order (used when a maker limit doesn't fill in time)."""
        body: dict[str, Any] = {
            "symbol": symbol.upper(),
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "marginCoin": margin_coin.upper(),
        }
        if order_id:
            body["orderId"] = str(order_id)
        if client_oid:
            body["clientOid"] = str(client_oid)
        return self._request(
            method="POST",
            path="/api/v2/mix/order/cancel-order",
            body=body,
            private=True,
        )

    def _verify_reduce_only_close_body(
        self,
        body: dict[str, Any],
        symbol: str,
        hold_side: str,
    ) -> None:
        symbol_upper = symbol.upper()
        hold_side_lower = str(hold_side or "").lower()
        trade_side = str(body.get("tradeSide") or "").lower()
        reduce_only = str(body.get("reduceOnly") or "").upper()
        order_type = str(body.get("orderType") or "").lower()
        body_side = str(body.get("side") or "").lower()
        body_hold_side = str(body.get("holdSide") or "").lower()
        try:
            size_value = float(str(body.get("size") or "").strip())
        except (TypeError, ValueError):
            size_value = 0.0

        expected_product = str(self.settings.bitget_product_type or "").upper()
        expected_margin_coin = str(self.settings.bitget_margin_coin or "USDT").upper()

        failures: list[str] = []

        if trade_side != "close":
            failures.append(f"tradeSide={body.get('tradeSide')!r} must be 'close'")
        if reduce_only != "YES":
            failures.append(f"reduceOnly={body.get('reduceOnly')!r} must be 'YES'")
        if order_type != "market":
            failures.append(f"orderType={body.get('orderType')!r} must be 'market'")
        if size_value <= 0:
            failures.append(f"size={body.get('size')!r} must be a positive number")

        # side <-> holdSide is the relation Bitget actually enforces and the one
        # _validate_futures_order_flags cannot see: it checks each field against
        # its own vocabulary, so an inverted-but-well-formed pair sails through.
        if hold_side_lower not in _CLOSE_SIDE_BY_HOLD_SIDE:
            failures.append(f"hold_side={hold_side!r} is neither long nor short")
        else:
            if body_hold_side != hold_side_lower:
                failures.append(
                    f"holdSide={body.get('holdSide')!r} does not match the "
                    f"position being closed ({hold_side_lower})"
                )
            expected_side = _CLOSE_SIDE_BY_HOLD_SIDE[hold_side_lower]
            if body_side != expected_side:
                failures.append(
                    f"side={body.get('side')!r} cannot close holdSide="
                    f"{hold_side_lower!r}; hedge mode requires side={expected_side!r}"
                )

        if str(body.get("symbol") or "").upper() != symbol_upper:
            failures.append(f"symbol={body.get('symbol')!r} must be {symbol_upper}")
        if str(body.get("productType") or "").upper() != expected_product:
            failures.append(
                f"productType={body.get('productType')!r} must be {expected_product}"
            )
        if str(body.get("marginCoin") or "").upper() != expected_margin_coin:
            failures.append(
                f"marginCoin={body.get('marginCoin')!r} must be {expected_margin_coin}"
            )

        # Nothing that only makes sense when opening may ride along on a close.
        for opening_key in ("presetStopLossPrice", "presetStopSurplusPrice", "price"):
            if body.get(opening_key) is not None:
                failures.append(f"{opening_key} has no place in a close payload")

        if failures:
            self.log.critical(
                "REDUCE_ONLY_VERIFY_FAILED | %s | hold_side=%s | failures=%s | body=%s",
                symbol_upper,
                hold_side_lower,
                "; ".join(failures),
                body,
            )
            raise ValueError(
                f"Refusing to send close for {symbol_upper}: {'; '.join(failures)}"
            )

        self.log.warning(
            "REDUCE_ONLY_VERIFY_OK | %s | hold_side=%s | side=%s | tradeSide=%s | reduceOnly=%s | orderType=%s | size=%s",
            symbol_upper,
            hold_side_lower,
            body.get("side"),
            body.get("tradeSide"),
            body.get("reduceOnly"),
            body.get("orderType"),
            body.get("size"),
        )

    def close_futures_position(
        self,
        symbol: str,
        hold_side: str | None = None,
        size: float = 0.0,
        margin_mode: str = "isolated",
        product_type: str | None = None,
        margin_coin: str = "USDT",
        client_oid: str | None = None,
        direction: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Close/reduce an existing Bitget futures position."""
        if hold_side is None and direction is not None:
            direction_upper = str(direction).upper()
            if direction_upper == "LONG":
                hold_side = "long"
            elif direction_upper == "SHORT":
                hold_side = "short"

        if hold_side is None:
            raise ValueError(f"Close position requires hold_side or direction for {symbol}")

        hold_side_lower = hold_side.lower()

        if hold_side_lower not in {"long", "short"}:
            raise ValueError(f"Unsupported hold side: {hold_side}")

        side = close_side_for_hold_side(hold_side_lower)
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

        # Protection is deliberately NOT cancelled up front. Bitget closes a
        # protected position without complaint, so cancelling first buys
        # nothing -- and a close that then fails would leave the position
        # naked. Stops come off only once the exchange confirms there is
        # nothing left to protect.
        results["cleanup_before"] = {"status": "SKIPPED_PROTECTION_HELD_UNTIL_CLOSE_CONFIRMED"}

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

        try:
            results["close"] = self.close_futures_position(
                symbol=symbol,
                hold_side=hold_side,
                size=live_size,
                margin_mode=margin_mode,
                client_oid=f"close-full-{symbol.lower()}-{int(time.time())}",
            )
        except Exception as exc:
            # Re-read rather than assume: the only question that matters is
            # whether the position is still there, and the error text cannot
            # answer it on its own.
            remaining = self._live_position_size_for_symbol(symbol, hold_side=hold_side)
            results["remaining_size"] = remaining
            results["error"] = str(exc)

            if is_no_position_to_close_error(exc) and remaining <= 0:
                # Already flat -- somebody else (a triggered TP/SL) got there
                # first. Nothing failed, and retrying would only repeat this.
                results["status"] = "NO_POSITION"
                self.log.warning(
                    "CLOSE_FULL_ALREADY_FLAT | %s | hold_side=%s | reason=%s | detail=%s",
                    symbol,
                    hold_side,
                    close_reason,
                    exc,
                )
                return results

            self.log.error(
                "CLOSE_FULL_REJECTED_POSITION_RETAINED | %s | hold_side=%s | reason=%s | "
                "remaining_size=%s | protection_untouched=True | error=%s",
                symbol,
                hold_side,
                close_reason,
                remaining,
                exc,
            )
            raise

        # An accepted order is not a closed position. Ask the exchange.
        remaining = self._live_position_size_for_symbol(symbol, hold_side=hold_side)
        results["remaining_size"] = remaining

        if remaining > 0:
            # Partial fill or an accepted order that did not reduce anything.
            # Either way the position still exists, still needs its stop, and
            # must not be recorded as closed.
            results["status"] = (
                "PARTIALLY_CLOSED" if remaining < live_size else "CLOSE_NOT_REFLECTED"
            )
            self.log.error(
                "CLOSE_FULL_POSITION_REMAINS | %s | status=%s | hold_side=%s | "
                "live_size=%s | remaining_size=%s | protection_untouched=True | reason=%s",
                symbol,
                results["status"],
                hold_side,
                live_size,
                remaining,
                close_reason,
            )
            return results

        results["status"] = "CLOSED"

        if cleanup_tpsl:
            # Safe now, and only now: nothing is left to protect. Note this
            # cancels loss_plan/profit_plan only -- position-level
            # pos_loss/pos_profit are released by the exchange with the
            # position itself.
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
