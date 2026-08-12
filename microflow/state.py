from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite
from typing import Iterable


WINDOWS_MS = (1_000, 5_000, 15_000, 30_000, 60_000)


def normalized_imbalance(positive: float, negative: float) -> float | None:
    """Return (positive-negative)/(positive+negative), or UNKNOWN for zero flow."""
    denominator = float(positive) + float(negative)
    if denominator <= 0:
        return None
    return (float(positive) - float(negative)) / denominator


def _levels(rows: Iterable[Iterable[object]], limit: int = 5) -> list[tuple[float, float]]:
    levels: list[tuple[float, float]] = []
    for raw in rows:
        values = list(raw)
        if len(values) < 2:
            continue
        try:
            price, size = float(values[0]), float(values[1])
        except (TypeError, ValueError):
            continue
        if price > 0 and size >= 0 and isfinite(price) and isfinite(size):
            levels.append((price, size))
        if len(levels) >= limit:
            break
    return levels


@dataclass(frozen=True)
class BookMetrics:
    bid_levels: tuple[tuple[float, float], ...]
    ask_levels: tuple[tuple[float, float], ...]
    best_bid: float
    best_ask: float
    top1_bid_size: float
    top1_ask_size: float
    top5_bid_qty: float
    top5_ask_qty: float
    top5_bid_notional: float
    top5_ask_notional: float
    mid_price: float
    spread_bps: float
    microprice: float
    microprice_edge_bps: float
    book_imbalance_top1: float | None
    book_imbalance_top5: float | None

    @classmethod
    def from_levels(cls, bids_raw: Iterable[Iterable[object]], asks_raw: Iterable[Iterable[object]]) -> "BookMetrics":
        bids, asks = _levels(bids_raw), _levels(asks_raw)
        if not bids or not asks:
            raise ValueError("both orderbook sides require at least one valid level")
        best_bid, bid_size = bids[0]
        best_ask, ask_size = asks[0]
        if best_bid >= best_ask:
            raise ValueError("crossed or locked orderbook")
        mid = (best_bid + best_ask) / 2.0
        touch_size = bid_size + ask_size
        if touch_size <= 0:
            raise ValueError("touch quantities have zero denominator")
        microprice = (best_ask * bid_size + best_bid * ask_size) / touch_size
        bid_qty = sum(size for _, size in bids)
        ask_qty = sum(size for _, size in asks)
        bid_notional = sum(price * size for price, size in bids)
        ask_notional = sum(price * size for price, size in asks)
        return cls(
            bid_levels=tuple(bids),
            ask_levels=tuple(asks),
            best_bid=best_bid,
            best_ask=best_ask,
            top1_bid_size=bid_size,
            top1_ask_size=ask_size,
            top5_bid_qty=bid_qty,
            top5_ask_qty=ask_qty,
            top5_bid_notional=bid_notional,
            top5_ask_notional=ask_notional,
            mid_price=mid,
            spread_bps=(best_ask - best_bid) / mid * 10_000.0,
            microprice=microprice,
            microprice_edge_bps=(microprice - mid) / mid * 10_000.0,
            book_imbalance_top1=normalized_imbalance(bid_size, ask_size),
            book_imbalance_top5=normalized_imbalance(bid_notional, ask_notional),
        )


@dataclass(frozen=True)
class TradeObservation:
    timestamp_ms: int
    price: float
    size: float
    side: str
    trade_id: str


class MicroflowSymbolState:
    """Bounded rolling state for one symbol; all timestamps are exchange milliseconds."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol.upper()
        self.trades: deque[TradeObservation] = deque()
        self.trade_ids: set[str] = set()
        self.last_trade_ts_ms: int | None = None
        self.last_book_ts_ms: int | None = None
        self.last_seq: int | None = None
        self.sequence_valid = True
        self.sequence_errors = 0
        self.duplicate_trades = 0
        self.out_of_order_trades = 0
        self.book: BookMetrics | None = None
        self.previous_book: BookMetrics | None = None

    def add_trade(self, *, timestamp_ms: int, price: float, size: float,
                  side: str, trade_id: str) -> bool:
        normalized_side = str(side).lower()
        if timestamp_ms <= 0 or price <= 0 or size <= 0 or normalized_side not in {"buy", "sell"}:
            raise ValueError("invalid public trade")
        if trade_id and trade_id in self.trade_ids:
            self.duplicate_trades += 1
            return False
        if self.last_trade_ts_ms is not None and timestamp_ms < self.last_trade_ts_ms:
            self.out_of_order_trades += 1
        self.last_trade_ts_ms = max(timestamp_ms, self.last_trade_ts_ms or timestamp_ms)
        observation = TradeObservation(timestamp_ms, price, size, normalized_side, trade_id)
        self.trades.append(observation)
        if trade_id:
            self.trade_ids.add(trade_id)
        self._prune(timestamp_ms)
        return True

    def _prune(self, now_ms: int) -> None:
        cutoff = now_ms - WINDOWS_MS[-1] - 5_000
        while self.trades and self.trades[0].timestamp_ms < cutoff:
            old = self.trades.popleft()
            if old.trade_id:
                self.trade_ids.discard(old.trade_id)

    def update_book(self, *, exchange_ts_ms: int, seq: int | None,
                    pseq: int | None = None,
                    bids: Iterable[Iterable[object]], asks: Iterable[Iterable[object]]) -> BookMetrics:
        if exchange_ts_ms <= 0:
            raise ValueError("invalid orderbook timestamp")
        sequence_broken = (
            seq is not None
            and self.last_seq is not None
            and (seq <= self.last_seq or (pseq is not None and pseq > 0 and pseq != self.last_seq))
        )
        if sequence_broken:
            self.sequence_valid = False
            self.sequence_errors += 1
        else:
            self.sequence_valid = True
        if seq is not None:
            self.last_seq = seq
        metrics = BookMetrics.from_levels(bids, asks)
        self.previous_book, self.book = self.book, metrics
        self.last_book_ts_ms = exchange_ts_ms
        self._prune(exchange_ts_ms)
        return metrics

    def flow(self, now_ms: int, window_ms: int) -> dict[str, float | int | None]:
        cutoff = now_ms - window_ms
        rows = [trade for trade in self.trades if cutoff <= trade.timestamp_ms <= now_ms]
        buy = sum(trade.size for trade in rows if trade.side == "buy")
        sell = sum(trade.size for trade in rows if trade.side == "sell")
        prices = [trade.price for trade in rows]
        return {
            "aggressive_buy_volume": buy,
            "aggressive_sell_volume": sell,
            "trade_count_buy": sum(trade.side == "buy" for trade in rows),
            "trade_count_sell": sum(trade.side == "sell" for trade in rows),
            "total_trade_volume": buy + sell,
            "ofi": normalized_imbalance(buy, sell),
            "realized_range_bps": (
                (max(prices) - min(prices)) / prices[-1] * 10_000.0
                if len(prices) >= 2 and prices[-1] > 0 else None
            ),
        }

    def snapshot(self, *, local_ts_ms: int, connection_id: str) -> dict:
        if self.book is None:
            raise ValueError("orderbook state unavailable")
        book = self.book
        previous = self.previous_book
        flows = {f"{window // 1000}s": self.flow(local_ts_ms, window) for window in WINDOWS_MS}
        return {
            "schema_version": "microflow_state_v1",
            "timestamp_exchange": self.last_book_ts_ms,
            "timestamp_local": local_ts_ms,
            "symbol": self.symbol,
            "trade_flow": flows,
            "book": {
                "bid_levels": [list(level) for level in book.bid_levels],
                "ask_levels": [list(level) for level in book.ask_levels],
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "spread_bps": book.spread_bps,
                "top1_bid_size": book.top1_bid_size,
                "top1_ask_size": book.top1_ask_size,
                "top5_bid_notional": book.top5_bid_notional,
                "top5_ask_notional": book.top5_ask_notional,
                "top5_bid_qty": book.top5_bid_qty,
                "top5_ask_qty": book.top5_ask_qty,
                "book_imbalance_top1": book.book_imbalance_top1,
                "book_imbalance_top5": book.book_imbalance_top5,
            },
            "microprice": {
                "mid_price": book.mid_price,
                "microprice": book.microprice,
                "microprice_vs_mid_bps": book.microprice_edge_bps,
            },
            "book_dynamics": {
                "bid_depth_delta": book.top5_bid_notional - previous.top5_bid_notional if previous else None,
                "ask_depth_delta": book.top5_ask_notional - previous.top5_ask_notional if previous else None,
                "best_bid_change": book.best_bid - previous.best_bid if previous else None,
                "best_ask_change": book.best_ask - previous.best_ask if previous else None,
            },
            "freshness": {
                "trade_stream_age_ms": local_ts_ms - self.last_trade_ts_ms if self.last_trade_ts_ms else None,
                "book_stream_age_ms": local_ts_ms - self.last_book_ts_ms if self.last_book_ts_ms else None,
                "sequence_valid": self.sequence_valid,
                "connection_id": connection_id,
            },
            "quality": {
                "sequence_errors": self.sequence_errors,
                "duplicate_trades": self.duplicate_trades,
                "out_of_order_trades": self.out_of_order_trades,
            },
        }
