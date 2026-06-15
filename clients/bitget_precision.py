from __future__ import annotations


class BitgetPrecisionMixin:
    """Precision helpers: contract scales, size formatting, trigger price formatting and min size."""

    def _contract_volume_scale(self, symbol: str) -> int | None:
        try:
            contracts = self.get_contracts(self.settings.bitget_product_type, symbol=symbol)
            data = contracts.get("data") or []
            if not data:
                return None

            contract = data[0]

            for key in ["volumePlace", "sizePlace", "quantityScale"]:
                value = contract.get(key)
                if value is not None:
                    return int(value)

        except Exception as exc:
            self.log.warning("Failed to fetch contract precision for %s: %s", symbol, exc)

        return None

    def _format_size(self, symbol: str, size: float) -> float:
        symbol_upper = symbol.upper()
        scale = self._contract_volume_scale(symbol_upper)

        if scale is not None:
            if scale <= 0:
                return float(int(size))
            return round(size, scale)

        if symbol_upper.endswith("USDT"):
            if any(x in symbol_upper for x in ["DOGE", "LINK", "WIF", "ADA"]):
                return float(int(size))
            if any(x in symbol_upper for x in ["SUI", "AVAX", "SOL", "INJ", "NEAR", "ARB"]):
                return round(size, 1)
            if "ETH" in symbol_upper:
                return round(size, 2)
            return round(size, 3)

        return round(size, 3)

    def _contract_price_scale(self, symbol: str) -> int | None:
        try:
            contracts = self.get_contracts(self.settings.bitget_product_type, symbol=symbol)
            data = contracts.get("data") or []
            if not data:
                return None

            contract = data[0]

            for key in ["pricePlace", "priceScale"]:
                value = contract.get(key)
                if value is not None:
                    return int(value)

        except Exception as exc:
            self.log.warning("Failed to fetch contract price precision for %s: %s", symbol, exc)

        return None

    def _format_trigger_price(self, symbol: str, price: float) -> float:
        symbol_upper = symbol.upper()
        scale = self._contract_price_scale(symbol_upper)

        if scale is not None:
            if scale <= 0:
                return float(int(price))
            return round(price, scale)

        if symbol_upper.endswith("USDT"):
            if "BTC" in symbol_upper:
                return round(price, 1)
            if any(x in symbol_upper for x in ["ETH", "BNB", "AAVE", "BCH", "LTC"]):
                return round(price, 2)
            if any(x in symbol_upper for x in ["DOT", "SOL", "AVAX", "LINK", "UNI", "ICP", "ATOM", "ETC", "FIL", "NEAR"]):
                return round(price, 3)
            if any(x in symbol_upper for x in ["ADA", "XRP", "DOGE", "TRX", "XLM", "SUI", "ARB", "WIF"]):
                return round(price, 4)

        return round(price, 4)

    def _min_size(self, symbol: str) -> float:
        s = symbol.upper()
        if "BTC" in s:
            return 0.001
        if "ETH" in s:
            return 0.01
        return 0.001