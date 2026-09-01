"""Concrete Bitget ExchangePort for the isolated funding pilot."""
from __future__ import annotations

import time

from funding_pilot.core import ExchangeTruth, FailClosed, OID_PREFIX, PilotLedger


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
        owned = {}
        for kind in ("ENTRY_INTENT", "STOP_ACK", "CANONICAL_OPEN"):
            for event in self.ledger.events(kind):
                payload = event["payload"]
                symbol = str(event.get("symbol") or payload.get("symbol") or "").upper()
                if symbol:
                    owned[symbol] = {**owned.get(symbol, {}), **payload}
        return owned

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
                row_id = str(row.get("positionId") or row.get("id") or row.get("closeId") or "")
                if not row_id or row_id != expected_position_id:
                    continue
                dedupe = f"economics:{symbol}:{row_id or row.get('utime') or row.get('cTime')}"
                if self.ledger.get(dedupe) == "INGESTED":
                    continue
                pnl = _number(row, "pnl", "realizedPnl", "netProfit")
                open_fee = _number(row, "openFee", "open_fee")
                close_fee = _number(row, "closeFee", "close_fee")
                funding = _number(row, "totalFunding", "funding", "fundingFee")
                if None in (pnl, open_fee, close_fee, funding):
                    raise FailClosed(f"closed fees/funding/PnL incomplete: {symbol}")
                self.ledger.append("ECONOMICS", {
                    "realized_pnl": pnl, "fees": abs(open_fee) + abs(close_fee),
                    "funding": funding, "other_costs": 0.0,
                    "exchange_position_id": row_id,
                }, symbol=symbol)
                self.ledger.set(dedupe, "INGESTED")

    def truth(self) -> ExchangeTruth:
        equity, available = self._accounts()
        owned = self._owned()
        self._ingest_closed_economics(owned)
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
