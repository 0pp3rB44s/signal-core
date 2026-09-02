"""Concrete Bitget ExchangePort for the isolated funding pilot."""
from __future__ import annotations

import time

from funding_pilot.core import ExchangeTruth, FailClosed, OID_PREFIX, PilotLedger
from funding_pilot.funding_bills import FundingBillSchemaError, fetch_funding_bills


def _rows(payload):
    data = (payload or {}).get("data")
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("entrustedList", "orderList", "list", "orders", "planList"):
            if isinstance(data.get(key), list):
                return [x for x in data[key] if isinstance(x, dict)]
    return []


def _number(row, *keys):
    for key in keys:
        if row.get(key) not in (None, ""):
            try: return float(row[key])
            except (TypeError, ValueError): raise FailClosed(f"invalid exchange number: {key}")
    return None


class BitgetPilotExchangePort:
    """Read-authoritative adapter; writes require an explicit in-memory arm."""
    def __init__(self, client, ledger: PilotLedger, *, armed_live: bool = False) -> None:
        self.client, self.ledger, self.armed_live = client, ledger, bool(armed_live)

    def _owned(self) -> dict[str, dict]:
        relevant = [row for row in self.ledger.events()
                    if row["kind"] in {"ENTRY_INTENT", "ENTRY_TERMINAL", "STOP_ACK", "CANONICAL_OPEN", "CANONICAL_TIME_EXIT"}]
        lifecycles = {}
        for event in sorted(relevant, key=lambda row: int(row["id"])):
            payload = event["payload"]
            oid = str(payload.get("entry_client_oid") or "")
            if not oid.startswith(OID_PREFIX):
                continue
            lifecycle = lifecycles.setdefault(oid, {"entry_client_oid": oid})
            lifecycle.setdefault("ownership_started_at_ms", int(event["timestamp_ms"]))
            if event["kind"] in {"ENTRY_TERMINAL", "CANONICAL_TIME_EXIT"}:
                state = str(payload.get("state") or "CLOSED").upper()
                lifecycle["terminal_state"] = state
                # Post-execution uncertainty is deliberately non-terminal.  The
                # exchange identity remains owned until zero exposure and zero
                # pilot orders/protection have been proved.
                lifecycle["closed"] = state in {
                    "REJECTED", "CANCELLED", "SKIPPED_CONFLICT", "SAFE_CLOSED", "CLOSED"
                }
                continue
            lifecycle.update(payload)
            lifecycle["symbol"] = str(event.get("symbol") or payload.get("symbol") or "").upper()
            lifecycle["last_event_id"] = int(event["id"])
        active = [row for row in lifecycles.values() if not row.get("closed")]
        by_symbol = {}
        for lifecycle in active:
            symbol = lifecycle["symbol"]
            if symbol in by_symbol:
                raise FailClosed(f"multiple active pilot lifecycles: {symbol}")
            by_symbol[symbol] = lifecycle
        return by_symbol

    @staticmethod
    def _oid(row) -> str:
        return str(row.get("clientOid") or row.get("client_oid") or row.get("entryClientOid") or "")

    def _accounts(self):
        rows = _rows(self.client.get_accounts())
        usdt = next((r for r in rows if str(r.get("marginCoin") or r.get("coin") or "USDT").upper() == "USDT"), None)
        if not usdt:
            raise FailClosed("USDT account truth unavailable")
        equity = _number(usdt, "accountEquity", "equity", "usdtEquity")
        available = _number(usdt, "available", "availableBalance", "crossedMaxAvailable")
        if equity is None or available is None:
            raise FailClosed("account equity or available balance unknown")
        return equity, available

    def _ingest_closed_economics(self, owned: dict[str, dict]) -> None:
        for symbol, identity in owned.items():
            expected_position_id = str(identity.get("exchange_position_id") or "")
            if not expected_position_id:
                continue
            for row in _rows(self.client.get_position_history(symbol=symbol, limit=100)):
                row_id = str(row.get("positionId") or row.get("posId") or "")
                if not row_id or row_id != expected_position_id:
                    continue
                close_event_id = str(row.get("closeOrderId") or row.get("orderId") or row.get("tradeId") or row.get("id") or row.get("closeId") or row.get("uTime") or "")
                if not close_event_id:
                    raise FailClosed(f"partial-close identity unavailable: {symbol}")
                pnl = _number(row, "pnl", "realizedPnl", "netProfit")
                open_fee = _number(row, "openFee", "open_fee")
                close_fee = _number(row, "closeFee", "close_fee")
                if None in (pnl, open_fee, close_fee):
                    raise FailClosed(f"closed fees/PnL incomplete: {symbol}")
                items = (
                    (f"opening_fee:{identity.get('entry_client_oid')}", 0.0, abs(open_fee)),
                    (f"closing_fee:{close_event_id}", 0.0, abs(close_fee)),
                    (f"realized_pnl:{close_event_id}", pnl, 0.0),
                )
                for key, realized, fee in items:
                    self.ledger.append_economic_once(key, {
                        "realized_pnl": realized, "fees": fee, "funding": 0.0,
                        "other_costs": 0.0, "exchange_position_id": row_id,
                    }, symbol=symbol)

    def _ingest_open_funding(self, owned: dict[str, dict]) -> None:
        if not owned:
            return
        try:
            product_type = getattr(getattr(self.client, "settings", None),
                                   "bitget_product_type", "USDT-FUTURES")
            rows = fetch_funding_bills(self.client, product_type=product_type)
        except FundingBillSchemaError as exc:
            raise FailClosed("funding bill schema unavailable") from exc
        for symbol, identity in owned.items():
            position_id = str(identity.get("exchange_position_id") or "")
            if not position_id:
                continue
            for row in rows:
                row_position = str(row.get("positionId") or row.get("posId") or "")
                row_symbol = str(row.get("symbol") or "").upper()
                try:
                    created_ms = int(row.get("cTime"))
                except (TypeError, ValueError):
                    raise FailClosed(f"attributable funding timestamp invalid: {symbol}") from None
                exact_position = (bool(row_position) and row_position == position_id
                                  and row_symbol == symbol)
                lifecycle_window = (not row_position and row_symbol == symbol and
                    created_ms >= int(identity.get("ownership_started_at_ms") or 0))
                if not (exact_position or lifecycle_window):
                    continue
                bill_id = str(row.get("billId") or "")
                amount = _number(row, "amount", "funding", "fundingFee")
                if not bill_id or amount is None:
                    raise FailClosed(f"attributable funding row incomplete: {symbol}")
                key = f"funding:{bill_id}"
                self.ledger.append_economic_once(key, {"realized_pnl":0.0, "fees":0.0,
                    "funding":-amount, "other_costs":0.0, "exchange_position_id":position_id}, symbol=symbol)

    def truth(self) -> ExchangeTruth:
        equity, available = self._accounts()
        owned = self._owned()
        self._ingest_closed_economics(owned)
        self._ingest_open_funding(owned)
        all_positions = _rows(self.client.get_all_positions())
        pending = _rows(self.client.get_pending_orders(limit=100))
        plans = []
        seen_plan_ids = set()
        for plan_type in ("profit_loss", "normal_plan", "track_plan"):
            for row in _rows(self.client.get_tpsl_orders(plan_type=plan_type)):
                plan_id = str(row.get("orderId") or row.get("planOrderId") or "")
                if plan_id and plan_id in seen_plan_ids:
                    continue
                if plan_id:
                    seen_plan_ids.add(plan_id)
                plans.append(row)
        pilot_orders = tuple({**row, "client_oid": self._oid(row),
                              "order_id": row.get("orderId") or row.get("order_id")}
                             for row in pending if self._oid(row).startswith(OID_PREFIX))
        pilot_stops = tuple(
            row for row in plans
            if self._oid(row).startswith(OID_PREFIX)
            or str(row.get("orderId") or row.get("planOrderId") or "")
            in {str(v.get("stop_order_id") or "") for v in owned.values()}
        )
        for symbol, identity in owned.items():
            for row in pending:
                if str(row.get("symbol") or "").upper() == symbol and not self._oid(row).startswith(OID_PREFIX):
                    raise FailClosed(f"SKIP_SIGNAL_SHARED_EXPOSURE_CONFLICT: foreign order {symbol}")
            known_stop_id = str(identity.get("stop_order_id") or "")
            for row in plans:
                if str(row.get("symbol") or "").upper() != symbol:
                    continue
                order_id = str(row.get("orderId") or row.get("planOrderId") or "")
                if not self._oid(row).startswith(OID_PREFIX) and order_id != known_stop_id:
                    raise FailClosed(f"SKIP_SIGNAL_SHARED_EXPOSURE_CONFLICT: foreign plan {symbol}")
        pilot_positions = []
        for row in all_positions:
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in owned:
                continue
            identity = owned[symbol]
            oid = str(identity.get("entry_client_oid") or "")
            if not oid.startswith(OID_PREFIX):
                raise FailClosed(f"owned position identity ambiguous: {symbol}")
            expected_position_id = str(identity.get("exchange_position_id") or "")
            actual_position_id = str(row.get("positionId") or row.get("posId") or row.get("id") or "")
            if not expected_position_id:
                raise FailClosed(f"SKIP_SIGNAL_SHARED_EXPOSURE_CONFLICT: {symbol}")
            if not actual_position_id or actual_position_id != expected_position_id:
                raise FailClosed(f"pilot position exchange identity mismatch: {symbol}")
            size = _number(row, "total", "size", "available")
            entry = _number(row, "openPriceAvg", "averageOpenPrice", "entryPrice")
            unrealized = _number(row, "unrealizedPL", "unrealizedPnl", "upl")
            mark = _number(row, "markPrice", "marketPrice")
            if None in (size, entry, unrealized, mark):
                raise FailClosed(f"position economics unknown: {symbol}")
            pilot_positions.append({
                **row, "symbol": symbol, "client_oid": oid, "size": size,
                "entry_price": entry, "unrealized_pnl": unrealized,
                "notional": abs(float(size) * float(mark)),
            })
        # Any owned working/stop row must carry or resolve to deterministic identity.
        if any(not self._oid(row).startswith(OID_PREFIX) for row in pilot_orders):
            raise FailClosed("pilot working-order ownership ambiguous")
        normalized_stops = []
        for row in pilot_stops:
            symbol = str(row.get("symbol") or "").upper()
            identity = owned.get(symbol) or {}
            oid = self._oid(row) or str(identity.get("stop_client_oid") or identity.get("entry_client_oid") or "")
            if not oid.startswith(OID_PREFIX):
                raise FailClosed("pilot stop ownership ambiguous")
            normalized_stops.append({**row, "client_oid": oid, "symbol": symbol,
                                     "order_id": row.get("orderId") or row.get("planOrderId")})
        protected_margin = 0.0
        for row in all_positions:
            symbol = str(row.get("symbol") or "").upper()
            if symbol in owned:
                continue
            margin = _number(row, "marginSize", "positionMargin", "margin")
            if margin is None:
                size = _number(row, "total", "size", "available")
                mark = _number(row, "markPrice", "marketPrice")
                leverage = _number(row, "leverage")
                if None in (size, mark, leverage) or leverage <= 0:
                    raise FailClosed(f"protected production margin unknown: {symbol}")
                margin = abs(size * mark / leverage)
            protected_margin += abs(margin)
        return ExchangeTruth(True, equity, available, tuple(pilot_positions), pilot_orders,
                             tuple(normalized_stops), protected_margin)

    def min_notional(self, symbol):
        value = self.client._min_notional(symbol)
        return None if value is None else float(value)

    def decision_book(self, symbol):
        data = (self.client.get_orderbook(symbol=symbol, limit=5) or {}).get("data") or {}
        bids, asks = data.get("bids") or [], data.get("asks") or []
        if not bids or not asks: raise FailClosed("decision book unavailable")
        return {"bid": float(bids[0][0]), "ask": float(asks[0][0]), "timestamp_ms": int(time.time()*1000)}

    def _write(self):
        if not self.armed_live: raise FailClosed("REAL_ORDER_ARMED=FALSE")

    def submit_entry(self, **_): self._write(); raise FailClosed("entry owned by ExecutionService")
    def place_native_stop(self, **_): self._write(); raise FailClosed("stop owned by ExecutionService")
    def verify_native_stop(self, *, symbol, side, stop_price):
        result = self.client.verify_active_stop_loss(symbol=symbol, hold_side="long" if side.upper()=="LONG" else "short", expected_trigger_price=stop_price)
        return {"verified": bool(result.get("verified")), **result}
    def cancel_working_order(self, order):
        self._write(); return self.client.cancel_futures_order(symbol=order["symbol"], order_id=order.get("orderId") or order.get("order_id"))
    def close_reduce_only(self, position, reason):
        self._write(); return self.client.close_futures_position_full(symbol=position["symbol"], direction=position.get("side") or position.get("direction"))
    def cancel_stop(self, stop):
        self._write(); return self.client.cancel_futures_plan_order(symbol=stop["symbol"], order_id=stop.get("order_id") or stop.get("orderId"), plan_type="loss_plan")
