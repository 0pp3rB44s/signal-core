from __future__ import annotations

from typing import Any

from app.runtime_diagnostics import runtime_heartbeat


class UnsupportedGranularityError(ValueError):
    """Raised for a timeframe Bitget would reject, before any network call."""


# Bitget accepts minutes lowercase but hours/days/weeks/months UPPERCASE. Verified
# against the live public endpoint on 2026-07-25: "1h", "4h", "1d" and "1w" all
# return HTTP 400 code 400171, while "1H", "4H", "1D", "1W" return 200. Sending the
# internal lowercase "1h" made every confirmation-timeframe fetch fail for a whole
# validation run while the process stayed alive and reported healthy.
#
# Keys are the lowercase internal vocabulary; values are the exact API strings.
_API_GRANULARITY: dict[str, str] = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "1w": "1W",
    "6hutc": "6Hutc", "12hutc": "12Hutc", "1dutc": "1Dutc",
    "3dutc": "3Dutc", "1wutc": "1Wutc", "1mutc": "1Mutc",
}

# "1M" is one month and "1m" is one minute, so the month form can only be
# recognised case-sensitively and must never be lowercased into a minute.
_MONTH_GRANULARITY = "1M"

SUPPORTED_GRANULARITIES = tuple(sorted(set(_API_GRANULARITY.values()) | {_MONTH_GRANULARITY}))


def api_granularity(granularity: str) -> str:
    """Translate an internal timeframe into the exact value Bitget accepts.

    This is the single canonical normalisation boundary: every candle request goes
    through get_candles, so callers may pass either the internal lowercase form
    ("1h") or the API form ("1H") and still reach the exchange correctly.
    """
    raw = str(granularity or "").strip()
    if not raw:
        raise UnsupportedGranularityError("granularity is required")
    if raw == _MONTH_GRANULARITY:
        return _MONTH_GRANULARITY
    try:
        return _API_GRANULARITY[raw.lower()]
    except KeyError:
        raise UnsupportedGranularityError(
            f"unsupported granularity {granularity!r}; "
            f"supported: {', '.join(SUPPORTED_GRANULARITIES)}"
        ) from None


class BitgetMarketClientMixin:
    """Market-data endpoints: contracts, candles, multi-timeframe candles and orderbook."""

    def get_contracts(self, product_type: str, symbol: str | None = None) -> dict[str, Any]:
        params = {"productType": product_type}
        if symbol:
            params["symbol"] = symbol.upper()
        return self._request("GET", "/api/v2/mix/market/contracts", params=params)

    def get_symbol_price(
        self,
        symbol: str,
        product_type: str | None = None,
    ) -> dict[str, Any]:
        """Return exchange, index, and mark prices for one futures symbol."""
        return self._request(
            "GET",
            "/api/v2/mix/market/symbol-price",
            params={
                "symbol": symbol.upper(),
                "productType": product_type or self.settings.bitget_product_type,
            },
            private=False,
        )

    def get_candles(
        self,
        symbol: str,
        product_type: str,
        granularity: str = "15m",
        limit: int = 200,
    ) -> dict[str, Any]:
        # Normalise before anything else so an unsupported timeframe fails here
        # rather than as an opaque HTTP 400 on every scan.
        granularity = api_granularity(granularity)
        params = {
            "symbol": symbol.upper(),
            "productType": product_type,
            "granularity": granularity,
            "limit": limit,
        }
        if getattr(self.settings, "forward_paper_only", False):
            runtime_heartbeat(
                "candle_request",
                symbol=symbol.upper(),
                granularity=granularity,
                limit=limit,
            )
        payload = self._request("GET", "/api/v2/mix/market/candles", params=params)
        if getattr(self.settings, "forward_paper_only", False):
            runtime_heartbeat(
                "candle_response",
                symbol=symbol.upper(),
                granularity=granularity,
                row_count=len(payload.get("data") or []),
            )
        return payload

    def get_multi_timeframe_candles(
        self,
        symbol: str,
        product_type: str | None = None,
        timeframes: list[str] | None = None,
        limit: int = 200,
    ) -> dict[str, list[dict[str, Any]]]:
        selected_timeframes = timeframes or ["1m", "5m", "15m", "1h", "4h"]
        # No local mapping here: get_candles owns normalisation. The partial map this
        # replaced covered only 1m/5m/15m/1h/4h, so every other timeframe silently
        # reached the API in a form it rejects.
        product = product_type or self.settings.bitget_product_type
        result: dict[str, list[dict[str, Any]]] = {}

        for timeframe in selected_timeframes:
            try:
                payload = self.get_candles(symbol=symbol, product_type=product, granularity=timeframe, limit=limit)
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
