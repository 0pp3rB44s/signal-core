from __future__ import annotations

from typing import Any


class BitgetMarketClientMixin:
    """Market-data endpoints: contracts, candles, multi-timeframe candles and orderbook."""

    def get_contracts(self, product_type: str, symbol: str | None = None) -> dict[str, Any]:
        params = {"productType": product_type}
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request("GET", "/api/v2/mix/market/contracts", params=params)

    def get_candles(
        self,
        symbol: str,
        product_type: str,
        granularity: str = "15m",
        limit: int = 200,
    ) -> dict[str, Any]:
        params = {
            "symbol": symbol.upper(),
            "productType": product_type,
            "granularity": granularity,
            "limit": limit,
        }
        return self._request("GET", "/api/v2/mix/market/candles", params=params)

    def get_multi_timeframe_candles(
        self,
        symbol: str,
        product_type: str | None = None,
        timeframes: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, list[dict[str, Any]]]:
        selected_timeframes = timeframes or ["1m", "5m", "15m", "1h", "4h"]
        timeframe_mapping = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H"}
        product = product_type or self.settings.bitget_product_type
        result: dict[str, list[dict[str, Any]]] = {}

        for timeframe in selected_timeframes:
            try:
                mapped = timeframe_mapping.get(timeframe, timeframe)
                payload = self.get_candles(symbol=symbol, product_type=product, granularity=mapped, limit=limit)
                raw_rows = payload.get("data") or []
                rows: list[dict[str, Any]] = []
                for row in raw_rows:
                    if isinstance(row, list) and len(row) >= 6:
                        rows.append({
                            "timestamp": row[0],
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": float(row[5]),
                        })
                result[timeframe] = rows
            except Exception as exc:
                self.log.warning("MULTI_TF_CANDLE_FETCH_FAILED | %s | timeframe=%s | error=%s", symbol.upper(), timeframe, exc)
                result[timeframe] = []

        return result

    def get_orderbook(self, symbol: str, product_type: str | None = None, limit: int = 50) -> dict[str, Any]:
        params = {
            "symbol": symbol.upper(),
            "productType": product_type or self.settings.bitget_product_type,
            "limit": str(limit),
        }
        payload = self._request("GET", "/api/v2/mix/market/merge-depth", params=params, private=False)
        data = payload.get("data") or {}
        bids_raw = data.get("bids") or []
        asks_raw = data.get("asks") or []

        def norm(rows):
            out = []
            for row in rows:
                if isinstance(row, list) and len(row) >= 2:
                    try:
                        out.append({"price": float(row[0]), "size": float(row[1])})
                    except (TypeError, ValueError):
                        pass
            return out

        bids = norm(bids_raw)
        asks = norm(asks_raw)

        best_bid = bids[0]["price"] if bids else 0.0
        best_ask = asks[0]["price"] if asks else 0.0
        mid = ((best_bid + best_ask) / 2) if best_bid and best_ask else 0.0
        spread = (best_ask - best_bid) if best_bid and best_ask else 0.0
        spread_bps = ((spread / mid) * 10000) if mid else 0.0

        bid_depth = sum(x["price"] * x["size"] for x in bids)
        ask_depth = sum(x["price"] * x["size"] for x in asks)
        total_depth = bid_depth + ask_depth
        imbalance = ((bid_depth - ask_depth) / total_depth) if total_depth else 0.0

        return {
            "symbol": symbol.upper(),
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "spread_bps": spread_bps,
            "mid_price": mid,
            "bid_depth_notional": bid_depth,
            "ask_depth_notional": ask_depth,
            "total_depth_notional": total_depth,
            "depth_imbalance": imbalance,
            "raw_payload": data,
        }
