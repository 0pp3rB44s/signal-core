from typing import Any

from clients.schemas import Candle, ContractSpec


def _to_float(value: Any) -> float | None:
    if value in (None, "", "None"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> int | None:
    if value in (None, "", "None"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_candles(raw_rows: list[list[Any]]) -> list[Candle]:
    candles_by_timestamp: dict[int, Candle] = {}

    for row in raw_rows:
        if not isinstance(row, list) or len(row) < 6:
            continue

        try:
            timestamp_ms = int(row[0])
            open_price = float(row[1])
            high_price = float(row[2])
            low_price = float(row[3])
            close_price = float(row[4])
            volume_base = float(row[5])
            volume_quote = float(row[6]) if len(row) > 6 and row[6] not in (None, "") else None
        except (TypeError, ValueError):
            continue

        if timestamp_ms <= 0:
            continue
        if min(open_price, high_price, low_price, close_price) <= 0:
            continue
        if high_price < low_price:
            continue
        if high_price < max(open_price, close_price):
            continue
        if low_price > min(open_price, close_price):
            continue
        if volume_base < 0:
            continue

        candles_by_timestamp[timestamp_ms] = Candle(
            timestamp_ms=timestamp_ms,
            open=open_price,
            high=high_price,
            low=low_price,
            close=close_price,
            volume_base=volume_base,
            volume_quote=volume_quote,
        )

    return sorted(candles_by_timestamp.values(), key=lambda c: c.timestamp_ms)


def normalize_contracts(raw_contracts: list[dict[str, Any]], product_type: str) -> list[ContractSpec]:
    specs: list[ContractSpec] = []
    for row in raw_contracts:
        symbol = str(row.get("symbol") or row.get("symbolName") or "").upper()
        if not symbol:
            continue
        quote_coin = str(row.get("quoteCoin") or row.get("quote_currency") or "").upper()
        base_coin = str(row.get("baseCoin") or row.get("base_currency") or "").upper()
        volume_24h_usdt = (
            _to_float(row.get("usdtVolume"))
            or _to_float(row.get("quoteVolume"))
            or _to_float(row.get("turnover24h"))
            or _to_float(row.get("volumeUsd24h"))
        )
        change_pct_24h = _to_float(row.get("changeUtc24h"))
        if change_pct_24h is None:
            raw_pct = _to_float(row.get("change24h"))
            if raw_pct is not None:
                change_pct_24h = raw_pct * 100 if abs(raw_pct) <= 2 else raw_pct
        specs.append(
            ContractSpec(
                symbol=symbol,
                product_type=product_type,
                quote_coin=quote_coin,
                base_coin=base_coin,
                status=str(row.get("symbolStatus") or row.get("status") or "").lower(),
                min_trade_num=_to_float(row.get("minTradeNum") or row.get("minTradeUSDT")),
                size_multiplier=_to_float(row.get("sizeMultiplier") or row.get("sizeIncrement")),
                price_place=_to_int(row.get("pricePlace") or row.get("pricePrecision")),
                volume_24h_usdt=volume_24h_usdt,
                change_pct_24h=change_pct_24h,
                raw=row,
            )
        )
    return specs
