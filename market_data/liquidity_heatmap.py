"""Read-only liquidity heatmap uit bestaande Bitget orderbook-snapshots.

BELANGRIJK — SCOPE (eigenaar, 2026-07-07):
Dit is een pure analyse/confluence-laag. Geen entries op basis van de
heatmap alleen, geen strategy/planner/risk-gedragswijziging zonder
backtest-bewijs. De outputs worden alleen gelogd (snapshot-notes),
gepersisteerd (state/liquidity_heatmap.json) en read-only getoond in het
dashboard. Toekomstige toepassing (score-penalty bij entry in opposing
wall, bonus bij TP richting liquidity pocket, warning bij SL vlak achter
obvious liquidity) mag pas ná backtest en expliciete goedkeuring.

Input is het genormaliseerde orderbook van BitgetMarketClient.get_orderbook:
{"bids": [{"price": float, "size": float}, ...], "asks": [...],
 "mid_price": float, "spread_bps": float, ...}
"""

from __future__ import annotations

from typing import Any

# Een level is een "wall" als zijn notional >= WALL_RATIO x het gemiddelde
# level-notional van dezelfde kant.
WALL_RATIO = 3.0
# Liquiditeit binnen dit venster rond mid telt mee voor de above/below scores.
SCORE_WINDOW_PCT = 1.0
# Boven deze spread is de markt niet veilig genoeg voor scalps: risk_off.
MAX_SPREAD_BPS = 8.0
# Magneet-richting pas uitspreken als één kant duidelijk zwaarder is.
MAGNET_DOMINANCE = 1.25
# Entry binnen deze afstand van een grote opposing wall = risk zone.
RISK_ZONE_WALL_BPS = 15.0


def _neutral(symbol: str = "", reason: str = "no_orderbook_data") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "data_ok": False,
        "neutral_reason": reason,
        "nearest_bid_wall_price": 0.0,
        "nearest_ask_wall_price": 0.0,
        "bid_wall_strength": 0.0,
        "ask_wall_strength": 0.0,
        "liquidity_above_score": 50.0,
        "liquidity_below_score": 50.0,
        "spread_bps": 0.0,
        "imbalance": 0.0,
        "liquidity_magnet_direction": "NEUTRAL",
        "liquidity_risk_zone": False,
        "risk_off": False,
    }


def _walls(levels: list[dict], mid: float) -> tuple[float, float]:
    """(nearest_wall_price, wall_strength) — dichtstbijzijnde wall t.o.v. mid."""
    notionals = [float(l.get("price") or 0.0) * float(l.get("size") or 0.0) for l in levels]
    notionals = [n for n in notionals if n > 0]
    if not notionals:
        return 0.0, 0.0
    mean_notional = sum(notionals) / len(notionals)
    if mean_notional <= 0:
        return 0.0, 0.0

    for level in levels:  # levels staan gesorteerd vanaf best bid/ask
        notional = float(level.get("price") or 0.0) * float(level.get("size") or 0.0)
        if notional >= WALL_RATIO * mean_notional:
            return float(level.get("price") or 0.0), round(notional / mean_notional, 2)
    return 0.0, 0.0


def build_liquidity_heatmap(orderbook: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(orderbook, dict):
        return _neutral()

    symbol = str(orderbook.get("symbol") or "")
    bids = [l for l in (orderbook.get("bids") or []) if isinstance(l, dict)]
    asks = [l for l in (orderbook.get("asks") or []) if isinstance(l, dict)]
    mid = float(orderbook.get("mid_price") or 0.0)

    if not bids or not asks or mid <= 0:
        return _neutral(symbol)

    spread_bps = float(orderbook.get("spread_bps") or 0.0)

    window_low = mid * (1 - SCORE_WINDOW_PCT / 100)
    window_high = mid * (1 + SCORE_WINDOW_PCT / 100)
    below_notional = sum(
        float(l["price"]) * float(l["size"]) for l in bids
        if window_low <= float(l.get("price") or 0.0) <= mid
    )
    above_notional = sum(
        float(l["price"]) * float(l["size"]) for l in asks
        if mid <= float(l.get("price") or 0.0) <= window_high
    )
    total_window = above_notional + below_notional
    if total_window > 0:
        above_score = round(above_notional / total_window * 100, 2)
        below_score = round(below_notional / total_window * 100, 2)
    else:
        above_score = below_score = 50.0

    nearest_bid_wall, bid_wall_strength = _walls(bids, mid)
    nearest_ask_wall, ask_wall_strength = _walls(asks, mid)

    bid_depth = float(orderbook.get("bid_depth_notional") or 0.0)
    ask_depth = float(orderbook.get("ask_depth_notional") or 0.0)
    total_depth = bid_depth + ask_depth
    imbalance = round((bid_depth - ask_depth) / total_depth, 4) if total_depth > 0 else 0.0

    # Magneet: prijs wordt aangetrokken tot de kant met de zwaarste resting
    # liquidity binnen het venster.
    if above_notional > below_notional * MAGNET_DOMINANCE:
        magnet = "UP"
    elif below_notional > above_notional * MAGNET_DOMINANCE:
        magnet = "DOWN"
    else:
        magnet = "NEUTRAL"

    risk_off = spread_bps > MAX_SPREAD_BPS

    # Risk zone: mid zit vrijwel tegen een grote wall aan (beide kanten
    # gecheckt — een wall vlak boven raakt longs, vlak onder raakt shorts).
    def _near_wall(wall_price: float) -> bool:
        if wall_price <= 0:
            return False
        return abs(wall_price - mid) / mid * 10000 <= RISK_ZONE_WALL_BPS

    liquidity_risk_zone = (
        (_near_wall(nearest_ask_wall) and ask_wall_strength >= WALL_RATIO)
        or (_near_wall(nearest_bid_wall) and bid_wall_strength >= WALL_RATIO)
    )

    return {
        "symbol": symbol,
        "data_ok": True,
        "neutral_reason": "",
        "nearest_bid_wall_price": nearest_bid_wall,
        "nearest_ask_wall_price": nearest_ask_wall,
        "bid_wall_strength": bid_wall_strength,
        "ask_wall_strength": ask_wall_strength,
        "liquidity_above_score": above_score,
        "liquidity_below_score": below_score,
        "spread_bps": round(spread_bps, 3),
        "imbalance": imbalance,
        "liquidity_magnet_direction": magnet,
        "liquidity_risk_zone": liquidity_risk_zone,
        "risk_off": risk_off,
    }
