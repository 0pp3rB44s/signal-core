from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from clients.schemas import Candle
from market_features.engine import LiveMarketContext, aggregate_candles, build_market_snapshot
from telemetry.market_context_logger import MarketContextLogger


def _candles(count: int = 320, start: int = 0) -> list[Candle]:
    result = []
    price = 100.0
    for index in range(count):
        close = price + .08 + (index % 7) * .01
        result.append(Candle(start + index * 900_000, price, close + .2, price - .2, close, 100 + index % 13, 10_000 + index))
        price = close
    return result


def _client_orderbook(symbol: str = "BTCUSDT") -> dict[str, Any]:
    """Same shape as BitgetMarketClient.get_orderbook (merge-depth, top levels)."""
    bids = [{"price": 100.0 - i * 0.1, "size": 40.0 if i == 3 else 5.0} for i in range(10)]
    asks = [{"price": 100.1 + i * 0.1, "size": 25.0 if i == 2 else 4.0} for i in range(10)]
    best_bid, best_ask = bids[0]["price"], asks[0]["price"]
    mid = (best_bid + best_ask) / 2
    bid_depth = sum(x["price"] * x["size"] for x in bids)
    ask_depth = sum(x["price"] * x["size"] for x in asks)
    total_depth = bid_depth + ask_depth
    return {
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": best_ask - best_bid,
        "spread_bps": (best_ask - best_bid) / mid * 10000,
        "mid_price": mid,
        "bid_depth_notional": bid_depth,
        "ask_depth_notional": ask_depth,
        "total_depth_notional": total_depth,
        "depth_imbalance": (bid_depth - ask_depth) / total_depth,
        "raw_payload": {},
    }


def _snapshot_with_orderbook():
    source = _candles()
    as_of = source[-1].timestamp_ms + 900_000
    hourly = aggregate_candles(source, "15m", "1h", as_of)
    return build_market_snapshot(
        "BTCUSDT", source, hourly, as_of_timestamp_ms=as_of,
        inputs=LiveMarketContext(orderbook=_client_orderbook()),
    )


def _append_snapshot(path: Path, snapshot, **extra) -> dict[str, str]:
    MarketContextLogger(path).append(
        symbol=snapshot.symbol,
        alignment=snapshot.alignment,
        score_hint=snapshot.score_hint,
        primary_trend=snapshot.primary.trend,
        confirmation_trend=snapshot.confirmation.trend,
        volatility_rank=snapshot.volatility_rank,
        notes=snapshot.notes,
        **extra,
    )
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    return rows[0]


def test_orderbook_columns_are_numeric_when_orderbook_data_is_available(tmp_path: Path) -> None:
    snapshot = _snapshot_with_orderbook()
    orderbook_context = snapshot.context["orderbook"]
    assert isinstance(orderbook_context, dict)

    row = _append_snapshot(
        tmp_path / "market_context.csv", snapshot,
        orderbook_context=orderbook_context,
    )

    assert float(row["spread_bps"]) > 0.0
    assert abs(float(row["orderbook_imbalance"]) - float(orderbook_context["imbalance"])) < 1e-6
    assert row["orderbook_bias"] in {"bullish", "bearish", "neutral"}
    assert float(row["largest_bid_wall_ratio"]) > 1.0
    assert float(row["largest_ask_wall_ratio"]) > 1.0


def test_engine_notes_alone_fill_spread_imbalance_and_bias(tmp_path: Path) -> None:
    snapshot = _snapshot_with_orderbook()

    row = _append_snapshot(tmp_path / "market_context.csv", snapshot)

    assert float(row["spread_bps"]) > 0.0
    assert row["orderbook_imbalance"] != ""
    float(row["orderbook_imbalance"])
    assert row["orderbook_bias"] in {"bullish", "bearish", "neutral"}


def test_missing_orderbook_leaves_columns_empty(tmp_path: Path) -> None:
    source = _candles()
    as_of = source[-1].timestamp_ms + 900_000
    hourly = aggregate_candles(source, "15m", "1h", as_of)
    snapshot = build_market_snapshot("BTCUSDT", source, hourly, as_of_timestamp_ms=as_of)

    row = _append_snapshot(tmp_path / "market_context.csv", snapshot, orderbook_context=None)

    assert row["orderbook_imbalance"] == ""
    assert row["largest_bid_wall_ratio"] == ""
    assert row["largest_ask_wall_ratio"] == ""


def test_runner_forwards_structured_orderbook_context() -> None:
    runner_source = (Path(__file__).parents[1] / "app/runner.py").read_text(encoding="utf-8")
    call_start = runner_source.index("self.market_context_logger.append(")
    assert "orderbook_context=" in runner_source[call_start:call_start + 600]
