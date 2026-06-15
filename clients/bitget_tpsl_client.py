from __future__ import annotations

import time
from typing import Any


class BitgetTPSLClientMixin:
    """TPSL placement, verification and cleanup only."""

    def get_tpsl_orders(
        self,
        product_type: str | None = None,
        symbol: str | None = None,
        plan_type: str | None = "profit_loss",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "productType": (product_type or self.settings.bitget_product_type).upper(),
        }

        if plan_type:
            params["planType"] = plan_type

        if symbol:
            params["symbol"] = symbol.upper()

        return self._request(
            "GET",
            "/api/v2/mix/order/orders-plan-pending",
            params=params,
            private=True,
        )

    @staticmethod
    def _extract_plan_list(payload: dict[str, Any]) -> list[dict[str, Any]]:
        data = payload.get("data") or []
        candidates: list[Any] = []

        if isinstance(data, list):
            candidates.extend(data)

        elif isinstance(data, dict):
            for key in (
                "orderList",
                "list",
                "orders",
                "entrustedList",
                "planList",
            ):
                value = data.get(key)

                if isinstance(value, list):
                    candidates.extend(value)

            for value in data.values():
                if isinstance(value, list):
                    candidates.extend(value)

                elif isinstance(value, dict):
                    for nested_key in (
                        "orderList",
                        "list",
                        "orders",
                        "entrustedList",
                        "planList",
                    ):
                        nested = value.get(nested_key)

                        if isinstance(nested, list):
                            candidates.extend(nested)

        plans: list[dict[str, Any]] = []

        for item in candidates:
            if isinstance(item, dict):
                plans.append(item)

        return plans

    def _fetch_tpsl_orders_broad(
        self,
        symbol: str,
        product_type: str | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        plan_types = ("profit_loss",)

        all_plans: list[dict[str, Any]] = []
        raw_payloads: dict[str, Any] = {}
        seen_ids: set[str] = set()

        for plan_type in plan_types:
            key = plan_type or "none"

            try:
                payload = self.get_tpsl_orders(
                    product_type=product_type,
                    symbol=symbol,
                    plan_type=plan_type,
                )

                raw_payloads[key] = payload

                for plan in self._extract_plan_list(payload):
                    plan_id = str(
                        plan.get("planOrderId")
                        or plan.get("orderId")
                        or plan.get("id")
                        or id(plan)
                    )

                    if plan_id in seen_ids:
                        continue

                    seen_ids.add(plan_id)
                    all_plans.append(plan)

            except Exception as exc:
                raw_payloads[key] = {"error": str(exc)}

                error_text = str(exc)

                if "40812" in error_text or "400172" in error_text:
                    self.log.info(
                        "TPSL_BROAD_FETCH_SKIPPED_UNSUPPORTED | %s | plan_type=%s | error=%s",
                        symbol,
                        key,
                        exc,
                    )
                else:
                    self.log.warning(
                        "TPSL_BROAD_FETCH_FAILED | %s | plan_type=%s | error=%s",
                        symbol,
                        key,
                        exc,
                    )

        return all_plans, raw_payloads

    def verify_active_stop_loss(
        self,
        symbol: str,
        hold_side: str,
        expected_trigger_price: float,
        tolerance_pct: float = 0.08,
        product_type: str | None = None,
    ) -> dict[str, Any]:
        symbol = symbol.upper()
        hold_side = hold_side.lower()
        tolerance_pct = abs(float(tolerance_pct or 0.08))

        result: dict[str, Any] = {
            "verified": False,
            "symbol": symbol,
            "hold_side": hold_side,
            "expected_trigger_price": expected_trigger_price,
            "matched_order": None,
            "all_loss_orders": [],
            "reason": "not_checked",
        }

        expected_price = float(expected_trigger_price or 0.0)

        attempts = 4
        delay_seconds = 0.45
        last_raw_payloads: dict[str, Any] = {}

        for attempt in range(1, attempts + 1):
            plans, raw_payloads = self._fetch_tpsl_orders_broad(
                symbol=symbol,
                product_type=product_type,
            )

            last_raw_payloads = raw_payloads
            result["all_loss_orders"] = []

            for plan in plans:
                plan_type = str(
                    plan.get("planType")
                    or plan.get("orderType")
                    or ""
                ).lower()

                trigger_price = (
                    plan.get("triggerPrice")
                    or plan.get("planTriggerPrice")
                    or plan.get("stopLossTriggerPrice")
                    or plan.get("stopSurplusTriggerPrice")
                )

                try:
                    trigger_price_float = float(trigger_price)

                except (TypeError, ValueError):
                    continue

                plan_hold_side = str(
                    plan.get("holdSide")
                    or plan.get("posSide")
                    or ""
                ).lower()

                if (
                    hold_side
                    and plan_hold_side
                    and plan_hold_side != hold_side
                ):
                    continue

                looks_like_loss = (
                    "loss" in plan_type
                    or expected_price > 0
                )

                if not looks_like_loss:
                    continue

                order_summary = {
                    "plan_order_id": (
                        plan.get("planOrderId")
                        or plan.get("orderId")
                        or plan.get("id")
                    ),
                    "trigger_price": trigger_price_float,
                    "hold_side": plan_hold_side,
                    "plan_type": plan_type or "unknown",
                    "raw_status": (
                        plan.get("status")
                        or plan.get("state")
                    ),
                }

                result["all_loss_orders"].append(order_summary)

                if expected_price <= 0:
                    continue

                distance_pct = (
                    abs(trigger_price_float - expected_price)
                    / expected_price
                    * 100.0
                )

                if distance_pct <= tolerance_pct:
                    result["verified"] = True

                    result["matched_order"] = {
                        **order_summary,
                        "distance_pct": distance_pct,
                    }

                    result["reason"] = "verified"
                    result["attempt"] = attempt

                    self.log.info(
                        "VERIFY_STOP_LOSS_OK | %s | hold_side=%s | expected=%s | actual=%s | distance_pct=%.5f | attempt=%s",
                        symbol,
                        hold_side,
                        expected_price,
                        trigger_price_float,
                        distance_pct,
                        attempt,
                    )

                    return result

            if attempt < attempts:
                time.sleep(delay_seconds)

        result["reason"] = "no_matching_stop_found"
        result["raw_payload_keys"] = list(last_raw_payloads.keys())

        compact_payloads = {}

        for key, payload in last_raw_payloads.items():
            compact_payloads[key] = (
                payload
                if isinstance(payload, dict)
                else {"payload_type": type(payload).__name__}
            )

        self.log.error(
            "VERIFY_STOP_LOSS_FAILED | %s | hold_side=%s | expected=%s | active_loss_orders=%s | raw_payloads=%s",
            symbol,
            hold_side,
            expected_price,
            result["all_loss_orders"],
            compact_payloads,
        )

        return result

    def cancel_futures_plan_order(
        self,
        symbol: str,
        order_id: str,
        plan_type: str = "loss_plan",
        product_type: str | None = None,
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        """Cancel a Bitget futures plan/TPSL order by order id."""

        body: dict[str, Any] = {
            "symbol": symbol.upper(),
            "productType": (
                product_type
                or self.settings.bitget_product_type
            ),
            "marginCoin": margin_coin.upper(),
            "planType": plan_type,
            "orderId": str(order_id),
        }

        try:
            return self._request(
                method="POST",
                path="/api/v2/mix/order/cancel-plan-order",
                body=body,
                private=True,
            )

        except Exception:
            fallback_body = dict(body)

            fallback_body["planOrderId"] = (
                fallback_body.pop("orderId")
            )

            return self._request(
                method="POST",
                path="/api/v2/mix/order/cancel-plan-order",
                body=fallback_body,
                private=True,
            )

    def cancel_all_futures_tpsl_orders(
        self,
        symbol: str,
        hold_side: str | None = None,
        product_type: str | None = None,
        margin_coin: str = "USDT",
        plan_types: tuple[str, ...] = (
            "loss_plan",
            "profit_plan",
        ),
    ) -> dict[str, Any]:
        """Cancel all active TPSL orders for a symbol."""

        cancelled: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        plans, _ = self._fetch_tpsl_orders_broad(
            symbol=symbol,
            product_type=product_type,
        )

        hold_side_lower = (
            (hold_side or "").lower()
        )

        for plan in plans:
            plan_hold_side = str(
                plan.get("holdSide")
                or plan.get("posSide")
                or ""
            ).lower()

            if (
                hold_side_lower
                and plan_hold_side
                and plan_hold_side != hold_side_lower
            ):
                continue

            detected_plan_type = str(
                plan.get("planType")
                or "loss_plan"
            ).lower()

            if (
                plan_types
                and detected_plan_type not in plan_types
            ):
                continue

            order_id = str(
                plan.get("planOrderId")
                or plan.get("orderId")
                or plan.get("id")
                or ""
            )

            if not order_id:
                continue

            try:
                response = self.cancel_futures_plan_order(
                    symbol=symbol,
                    order_id=order_id,
                    plan_type=detected_plan_type,
                    product_type=product_type,
                    margin_coin=margin_coin,
                )

                cancelled.append({
                    "order_id": order_id,
                    "plan_type": detected_plan_type,
                    "response": response,
                })

            except Exception as exc:
                failed.append({
                    "order_id": order_id,
                    "plan_type": detected_plan_type,
                    "error": str(exc),
                })

                self.log.warning(
                    "TPSL_CANCEL_FAILED | %s | order_id=%s | plan_type=%s | error=%s",
                    symbol,
                    order_id,
                    detected_plan_type,
                    exc,
                )

        return {
            "symbol": symbol.upper(),
            "cancelled": cancelled,
            "failed": failed,
            "cancelled_count": len(cancelled),
            "failed_count": len(failed),
        }

    def sweep_orphan_tpsl_orders(
        self,
        symbols: list[str] | None = None,
        product_type: str | None = None,
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        """Cancel TPSL/plan orders for symbols that have no live Bitget position.

        This is intentionally conservative: if live position sync fails, no cleanup is performed.
        """
        product = (product_type or self.settings.bitget_product_type).upper()

        live_positions_payload = self.get_all_positions()
        live_positions = live_positions_payload.get("data") or []
        live_symbols: set[str] = set()

        for position in live_positions:
            try:
                size = float(
                    position.get("total")
                    or position.get("size")
                    or position.get("available")
                    or position.get("holdVol")
                    or position.get("positionSize")
                    or 0
                )
            except (TypeError, ValueError):
                size = 0.0

            if size > 0:
                symbol = str(position.get("symbol") or "").upper()
                if symbol:
                    live_symbols.add(symbol)

        candidate_symbols = [s.upper() for s in (symbols or []) if s]

        result: dict[str, Any] = {
            "status": "OK",
            "product_type": product,
            "live_symbols": sorted(live_symbols),
            "candidate_symbols": candidate_symbols,
            "swept": [],
            "skipped": [],
            "errors": [],
        }

        for symbol in candidate_symbols:
            if symbol in live_symbols:
                result["skipped"].append({
                    "symbol": symbol,
                    "reason": "live_position_exists",
                })
                continue

            try:
                cleanup = self.cancel_all_futures_tpsl_orders(
                    symbol=symbol,
                    hold_side=None,
                    product_type=product,
                    margin_coin=margin_coin,
                    plan_types=("loss_plan", "profit_plan", "profit_loss"),
                )
                result["swept"].append({
                    "symbol": symbol,
                    "cleanup": cleanup,
                })
                self.log.warning(
                    "ORPHAN_TPSL_SWEEP_DONE | %s | live_position=False | cancelled=%s | failed=%s",
                    symbol,
                    cleanup.get("cancelled_count"),
                    cleanup.get("failed_count"),
                )
            except Exception as exc:
                result["errors"].append({
                    "symbol": symbol,
                    "error": str(exc),
                })
                self.log.error(
                    "ORPHAN_TPSL_SWEEP_FAILED | %s | error=%s",
                    symbol,
                    exc,
                )

        if result["errors"]:
            result["status"] = "PARTIAL_ERROR"

        return result

    @staticmethod
    def extract_tpsl_order_id(
        payload: dict[str, Any] | None,
    ) -> str:
        if not payload:
            return ""

        data = payload.get("data") or {}

        candidates = [
            data.get("orderId"),
            data.get("planOrderId"),
            data.get("id"),
            payload.get("orderId"),
            payload.get("planOrderId"),
            payload.get("id"),
        ]

        for candidate in candidates:
            if candidate:
                return str(candidate)

        return ""

    def place_position_tpsl(
        self,
        symbol: str,
        hold_side: str,
        stop_loss: float,
        take_profit: float,
        margin_mode: str = "isolated",
        product_type: str | None = None,
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        """Place Bitget position-level TP/SL using the V2 place-pos-tpsl endpoint."""
        symbol = symbol.upper()
        hold_side = hold_side.lower()
        product = (product_type or self.settings.bitget_product_type).upper()
        formatted_stop = self._format_trigger_price(symbol, float(stop_loss))
        formatted_tp = self._format_trigger_price(symbol, float(take_profit))

        if formatted_stop <= 0 or formatted_tp <= 0:
            raise ValueError(
                f"Position TPSL requires valid stop_loss and take_profit for {symbol}; "
                f"stop_loss={stop_loss} take_profit={take_profit}"
            )

        body: dict[str, Any] = {
            "symbol": symbol,
            "productType": product,
            "marginCoin": margin_coin.upper(),
            "marginMode": margin_mode,
            "holdSide": hold_side,
            "stopSurplusTriggerPrice": str(formatted_tp),
            "stopSurplusTriggerType": "mark_price",
            "stopLossTriggerPrice": str(formatted_stop),
            "stopLossTriggerType": "mark_price",
        }

        self._validate_futures_order_flags(body)

        response = self._request(
            method="POST",
            path="/api/v2/mix/order/place-pos-tpsl",
            body=body,
            private=True,
        )

        verify: dict[str, Any] = {
            "verified": False,
            "position": None,
            "reason": "not_checked",
        }

        for attempt in range(1, 6):
            try:
                payload = self.get_all_positions()
                for position in payload.get("data") or []:
                    if str(position.get("symbol") or "").upper() != symbol:
                        continue
                    if str(position.get("holdSide") or "").lower() != hold_side:
                        continue

                    take_profit_value = str(position.get("takeProfit") or "")
                    stop_loss_value = str(position.get("stopLoss") or "")
                    take_profit_id = str(position.get("takeProfitId") or "")
                    stop_loss_id = str(position.get("stopLossId") or "")

                    verify["position"] = position
                    verify["attempt"] = attempt

                    if (take_profit_value or take_profit_id) and (stop_loss_value or stop_loss_id):
                        verify["verified"] = True
                        verify["reason"] = "position_tpsl_verified"
                        break

                    verify["reason"] = "position_tpsl_fields_empty"

                if verify.get("verified"):
                    break

            except Exception as exc:
                verify["reason"] = f"verify_error:{exc}"

            time.sleep(0.45)

        result: dict[str, Any] = {
            "symbol": symbol,
            "hold_side": hold_side,
            "stop_loss": {
                "trigger_price": formatted_stop,
                "response": response,
            },
            "take_profits": [
                {
                    "trigger_price": formatted_tp,
                    "size": "position",
                    "response": response,
                }
            ],
            "stop_loss_verified": bool(verify.get("verified")),
            "tp_verified": bool(verify.get("verified")),
            "expected_tp_count": 1,
            "expected_take_profit_count": 1,
            "actual_tp_count": 1 if verify.get("verified") else 0,
            "take_profit_count": 1 if verify.get("verified") else 0,
            "tp_partial_failure": False,
            "tp_full_failure": not bool(verify.get("verified")),
            "protection_verified": bool(verify.get("verified")),
            "protection_integrity": "OK" if verify.get("verified") else "VERIFY_FAILED",
            "position_tpsl_response": response,
            "position_tpsl_verify": verify,
            "route": "place-pos-tpsl",
        }

        self.log.warning(
            "POSITION_TPSL_VERIFY_RESULT | %s | hold_side=%s | stop=%s | tp=%s | verified=%s | integrity=%s | reason=%s",
            symbol,
            hold_side,
            formatted_stop,
            formatted_tp,
            result["protection_verified"],
            result["protection_integrity"],
            verify.get("reason"),
        )

        if not result["protection_verified"]:
            self.log.error(
                "POSITION_TPSL_NOT_VERIFIED | %s | hold_side=%s | response=%s | verify=%s",
                symbol,
                hold_side,
                response,
                verify,
            )

        return result

    def place_futures_protection_orders(
        self,
        symbol: str,
        hold_side: str | None = None,
        stop_loss: float = 0.0,
        take_profits: list[dict[str, float]] | list[float] | None = None,
        size: float | None = None,
        margin_mode: str = "isolated",
        product_type: str | None = None,
        margin_coin: str = "USDT",
        direction: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Place SL and TP protection orders, then return a verified protection payload."""
        if hold_side is None and direction is not None:
            direction_upper = direction.upper()
            if direction_upper == "LONG":
                hold_side = "long"
            elif direction_upper == "SHORT":
                hold_side = "short"

        if hold_side is None:
            raise ValueError("Protection orders require hold_side or direction")
        symbol = symbol.upper()
        hold_side = hold_side.lower()
        product = (product_type or self.settings.bitget_product_type).upper()

        if float(stop_loss or 0) <= 0:
            raise ValueError(
                f"Protection orders require a valid stop_loss for {symbol}"
            )

        if not take_profits:
            raise ValueError(
                f"Protection orders require at least one take_profit for {symbol}"
            )

        first_take_profit = take_profits[0]
        if isinstance(first_take_profit, dict):
            first_take_profit_price = float(
                first_take_profit.get("price")
                or first_take_profit.get("trigger_price")
                or first_take_profit.get("triggerPrice")
                or 0
            )
        else:
            first_take_profit_price = float(first_take_profit or 0)

        if first_take_profit_price <= 0:
            raise ValueError(
                f"Protection orders require a valid first take_profit for {symbol}; take_profits={take_profits}"
            )

        position_tpsl_result = self.place_position_tpsl(
            symbol=symbol,
            hold_side=hold_side,
            stop_loss=float(stop_loss),
            take_profit=first_take_profit_price,
            margin_mode=margin_mode,
            product_type=product,
            margin_coin=margin_coin,
        )

        if not position_tpsl_result.get("protection_verified"):
            raise RuntimeError(
                f"Position TPSL was not verified for {symbol}: {position_tpsl_result}"
            )

        return position_tpsl_result

        formatted_position_size = self._format_size(symbol, float(size or 0))
        if formatted_position_size <= 0:
            live_size = self._live_position_size_for_symbol(symbol, hold_side=hold_side)
            formatted_position_size = self._format_size(symbol, float(live_size or 0))

        if formatted_position_size <= 0:
            raise ValueError(
                f"Protection orders require a valid size for {symbol}; size={size} hold_side={hold_side}"
            )

        formatted_stop = self._format_trigger_price(symbol, float(stop_loss))

        results: dict[str, Any] = {
            "symbol": symbol,
            "hold_side": hold_side,
            "stop_loss": None,
            "take_profits": [],
            "stop_loss_verified": False,
            "tp_verified": False,
            "expected_tp_count": 0,
            "expected_take_profit_count": 0,
            "actual_tp_count": 0,
            "tp_partial_failure": False,
            "tp_full_failure": False,
            "protection_verified": False,
            "protection_integrity": "PENDING",
        }

        sl_body: dict[str, Any] = {
            "symbol": symbol,
            "productType": product,
            "marginCoin": margin_coin.upper(),
            "marginMode": margin_mode,
            "planType": "loss_plan",
            "triggerPrice": str(formatted_stop),
            "triggerType": "mark_price",
            "executePrice": "0",
            "holdSide": hold_side,
        }

        self._validate_futures_order_flags(sl_body)
        results["stop_loss"] = self._request(
            method="POST",
            path="/api/v2/mix/order/place-tpsl-order",
            body=sl_body,
            private=True,
        )

        sl_verify = self.verify_active_stop_loss(
            symbol=symbol,
            hold_side=hold_side,
            expected_trigger_price=formatted_stop,
            product_type=product,
        )
        results["stop_loss_verified"] = bool(sl_verify.get("verified"))
        results["stop_loss_verify"] = sl_verify

        formatted_take_profits: list[dict[str, float]] = []
        invalid_take_profits: list[Any] = []

        for tp in take_profits or []:
            if isinstance(tp, dict):
                trigger = float(
                    tp.get("price")
                    or tp.get("trigger_price")
                    or tp.get("triggerPrice")
                    or 0
                )
                tp_size = float(tp.get("size") or formatted_position_size or 0)
            else:
                trigger = float(tp)
                tp_size = float(formatted_position_size or 0)

            if trigger <= 0:
                invalid_take_profits.append(tp)
                continue

            formatted_take_profits.append({
                "trigger_price": self._format_trigger_price(symbol, trigger),
                "size": self._format_size(symbol, tp_size) if tp_size > 0 else 0.0,
            })

        if not formatted_take_profits:
            raise ValueError(
                f"Protection orders require at least one valid take_profit for {symbol}; "
                f"invalid_take_profits={invalid_take_profits}"
            )

        expected_tp_count = len(formatted_take_profits)
        tp_results: list[dict[str, Any]] = []
        for tp in formatted_take_profits:
            tp_body: dict[str, Any] = {
                "symbol": symbol,
                "productType": product,
                "marginCoin": margin_coin.upper(),
                "marginMode": margin_mode,
                "planType": "profit_plan",
                "triggerPrice": str(tp["trigger_price"]),
                "triggerType": "mark_price",
                "executePrice": "0",
                "holdSide": hold_side,
                "size": str(tp["size"]),
            }

            if float(tp.get("size") or 0) <= 0:
                tp_results.append({
                    "status": "SKIPPED_INVALID_TP_SIZE",
                    "trigger_price": tp.get("trigger_price"),
                    "size": tp.get("size"),
                })
                self.log.error(
                    "TP_PROTECTION_SIZE_INVALID | %s | hold_side=%s | trigger=%s | size=%s | formatted_position_size=%s",
                    symbol,
                    hold_side,
                    tp.get("trigger_price"),
                    tp.get("size"),
                    formatted_position_size,
                )
                continue

            try:
                self._validate_futures_order_flags(tp_body)
                response = self._request(
                    method="POST",
                    path="/api/v2/mix/order/place-tpsl-order",
                    body=tp_body,
                    private=True,
                )
                tp_results.append({
                    "status": "PLACED",
                    "trigger_price": tp.get("trigger_price"),
                    "size": tp.get("size"),
                    "response": response,
                    "order_id": self.extract_tpsl_order_id(response),
                })
            except Exception as exc:
                tp_results.append({
                    "status": "FAILED",
                    "trigger_price": tp.get("trigger_price"),
                    "size": tp.get("size"),
                    "error": str(exc),
                    "body": tp_body,
                })
                self.log.error(
                    "TP_PROTECTION_PLACE_FAILED | %s | hold_side=%s | trigger=%s | size=%s | error=%s | body=%s",
                    symbol,
                    hold_side,
                    tp.get("trigger_price"),
                    tp.get("size"),
                    exc,
                    tp_body,
                )
        actual_tp_count = len(tp_results)
        tp_partial_failure = bool(expected_tp_count and 0 < actual_tp_count < expected_tp_count)
        tp_full_failure = bool(expected_tp_count and actual_tp_count == 0)

        results["take_profits"] = tp_results
        results["expected_tp_count"] = expected_tp_count
        results["expected_take_profit_count"] = expected_tp_count
        results["actual_tp_count"] = actual_tp_count
        results["tp_partial_failure"] = tp_partial_failure
        results["tp_full_failure"] = tp_full_failure
        results["tp_verified"] = bool(not expected_tp_count or actual_tp_count == expected_tp_count)
        results["take_profit_count"] = actual_tp_count
        results["protection_verified"] = bool(results.get("stop_loss_verified") and results.get("tp_verified"))
        results["protection_integrity"] = "OK" if results["protection_verified"] else "VERIFY_FAILED"

        if tp_partial_failure or tp_full_failure:
            self.log.error(
                "ENTRY_TP_PLACEMENT_INCOMPLETE | %s | hold_side=%s | expected_tp=%s | actual_tp=%s | partial=%s | full_failure=%s | stop_verified=%s",
                symbol,
                hold_side,
                expected_tp_count,
                actual_tp_count,
                tp_partial_failure,
                tp_full_failure,
                results.get("stop_loss_verified"),
            )

            try:
                results["failed_protection_cleanup"] = self.cancel_all_futures_tpsl_orders(
                    symbol=symbol,
                    hold_side=hold_side,
                    product_type=product,
                    margin_coin=margin_coin,
                    plan_types=("loss_plan", "profit_plan"),
                )
            except Exception as exc:
                results["failed_protection_cleanup"] = {
                    "status": "CLEANUP_FAILED",
                    "error": str(exc),
                }
                self.log.error(
                    "ENTRY_PROTECTION_CLEANUP_FAILED | %s | hold_side=%s | error=%s",
                    symbol,
                    hold_side,
                    exc,
                )

        self.log.warning(
            "ENTRY_PROTECTION_VERIFY_RESULT | %s | hold_side=%s | stop_verified=%s | tp_verified=%s | tp_count=%s/%s | partial_tp_failure=%s | full_tp_failure=%s | integrity=%s",
            symbol,
            hold_side,
            results.get("stop_loss_verified"),
            results.get("tp_verified"),
            actual_tp_count,
            expected_tp_count,
            tp_partial_failure,
            tp_full_failure,
            results.get("protection_integrity"),
        )

        if not results["protection_verified"]:
            self.log.error(
                "ENTRY_PROTECTION_NOT_VERIFIED | %s | hold_side=%s | stop_verified=%s | tp_verified=%s | integrity=%s",
                symbol,
                hold_side,
                results.get("stop_loss_verified"),
                results.get("tp_verified"),
                results.get("protection_integrity"),
            )

        return results

    def move_futures_stop_loss(
        self,
        symbol: str,
        hold_side: str | None = None,
        trigger_price: float | None = None,
        direction: str | None = None,
        margin_mode: str = "isolated",
        product_type: str | None = None,
        margin_coin: str = "USDT",
        cleanup_existing: bool = True,
        reason: str | None = None,
        **_: Any,
    ) -> dict[str, Any]:
        """Move futures stop loss by optionally cleaning old SL plans and placing a new loss plan."""
        symbol = symbol.upper()

        if trigger_price is None:
            raise ValueError(f"move_futures_stop_loss missing trigger_price for {symbol}")

        if not hold_side:
            direction_normalized = str(direction or "").upper()
            if direction_normalized == "LONG":
                hold_side = "long"
            elif direction_normalized == "SHORT":
                hold_side = "short"
            else:
                raise ValueError(
                    f"move_futures_stop_loss missing hold_side and cannot infer direction={direction!r} for {symbol}"
                )

        hold_side = hold_side.lower()
        product = (product_type or self.settings.bitget_product_type).upper()
        formatted_trigger = self._format_trigger_price(symbol, float(trigger_price))

        # --- PATCH: Ensure valid size for loss_plan SL moves ---
        live_size = self._live_position_size_for_symbol(symbol, hold_side=hold_side)
        formatted_position_size = self._format_size(symbol, float(live_size or 0))

        if formatted_position_size <= 0:
            raise ValueError(
                f"move_futures_stop_loss requires a valid live position size for {symbol}; "
                f"hold_side={hold_side} live_size={live_size}"
            )
        # --- END PATCH ---

        result: dict[str, Any] = {
            "symbol": symbol,
            "hold_side": hold_side,
            "trigger_price": formatted_trigger,
            "cleanup_existing": cleanup_existing,
            "cleanup": None,
            "placed": None,
            "verified": False,
            "verify": None,
            "reason": reason,
            "size": formatted_position_size,
            "live_size": live_size,
        }

        if cleanup_existing:
            try:
                result["cleanup"] = self.cancel_all_futures_tpsl_orders(
                    symbol=symbol,
                    hold_side=hold_side,
                    product_type=product,
                    margin_coin=margin_coin,
                    plan_types=("loss_plan",),
                )
            except Exception as exc:
                result["cleanup"] = {"status": "CLEANUP_FAILED", "error": str(exc)}
                self.log.error(
                    "MOVE_SL_CLEANUP_FAILED | %s | hold_side=%s | error=%s",
                    symbol,
                    hold_side,
                    exc,
                )

        sl_body: dict[str, Any] = {
            "symbol": symbol,
            "productType": product,
            "marginCoin": margin_coin.upper(),
            "marginMode": margin_mode,
            "planType": "loss_plan",
            "triggerPrice": str(formatted_trigger),
            "triggerType": "mark_price",
            "executePrice": "0",
            "holdSide": hold_side,
            "size": str(formatted_position_size),
        }

        self._validate_futures_order_flags(sl_body)
        result["placed"] = self._request(
            method="POST",
            path="/api/v2/mix/order/place-tpsl-order",
            body=sl_body,
            private=True,
        )

        verify = self.verify_active_stop_loss(
            symbol=symbol,
            hold_side=hold_side,
            expected_trigger_price=formatted_trigger,
            product_type=product,
        )
        result["verify"] = verify
        result["verified"] = bool(verify.get("verified"))

        self.log.warning(
            "MOVED_SL_PLACED | %s | hold_side=%s | trigger=%s | size=%s | verified=%s | reason=%s | order_id=%s",
            symbol,
            hold_side,
            formatted_trigger,
            formatted_position_size,
            result["verified"],
            reason,
            self.extract_tpsl_order_id(result.get("placed")),
        )

        return result
