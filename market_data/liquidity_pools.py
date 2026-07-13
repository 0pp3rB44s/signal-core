"""Liquiditeit-pool detectie (prijs-structuur, ICT/SMC-stijl).

Buy-side liquidity (BSL)  = rustende stops/orders BOVEN swing-highs / equal highs.
Sell-side liquidity (SSL) = rustende stops/orders ONDER swing-lows / equal lows.
Een pool met meerdere touches (equal highs/lows) draagt meer liquiditeit.

'unswept' = de prijs is sinds de vorming niet door het niveau geweest -> nog een
target (de koers wordt ernaartoe getrokken). 'swept' = al opgehaald.

De module werkt op Candle-objecten (clients.schemas.Candle) OF simpele dicts met
o/h/l/c, zodat hij zowel in de backtest als live in de bot bruikbaar is. Puur
read-only detectie; geen trading-beslissingen hier.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(slots=True)
class LiquidityPool:
    price: float
    side: str  # "BSL" (buy-side, boven) of "SSL" (sell-side, onder)
    touches: int
    formed_idx: int  # index van de laatste touch
    swept: bool
    members: list[int] = field(default_factory=list)


def _h(c: Any) -> float:
    return float(c["h"] if isinstance(c, dict) else c.high)


def _l(c: Any) -> float:
    return float(c["l"] if isinstance(c, dict) else c.low)


def _c(c: Any) -> float:
    return float(c["c"] if isinstance(c, dict) else c.close)


def _pivots(candles: Sequence[Any], k: int) -> tuple[list[int], list[int]]:
    """Fractal pivots: high hoger dan k buren aan elke kant (pivot high), idem low."""
    ph: list[int] = []
    pl: list[int] = []
    n = len(candles)
    for i in range(k, n - k):
        window = candles[i - k : i + k + 1]
        if _h(candles[i]) == max(_h(w) for w in window):
            ph.append(i)
        if _l(candles[i]) == min(_l(w) for w in window):
            pl.append(i)
    return ph, pl


def _cluster(candles: Sequence[Any], idxs: list[int], side: str, tol_pct: float) -> list[LiquidityPool]:
    """Groepeer pivots op vergelijkbaar prijsniveau tot pools (equal highs/lows)."""
    price_of = _h if side == "BSL" else _l
    levels = sorted((price_of(candles[i]), i) for i in idxs)
    pools: list[LiquidityPool] = []
    for price, i in levels:
        placed = False
        for p in pools:
            if abs(price - p.price) / p.price <= tol_pct:
                p.members.append(i)
                p.touches += 1
                p.price = (p.price * (p.touches - 1) + price) / p.touches
                placed = True
                break
        if not placed:
            pools.append(LiquidityPool(price=price, side=side, touches=1, formed_idx=i, swept=False, members=[i]))
    return pools


def _mark_swept(candles: Sequence[Any], pools: list[LiquidityPool]) -> list[LiquidityPool]:
    """Markeer swept/unswept: is de pool na de laatste touch doorbroken?"""
    for p in pools:
        p.formed_idx = max(p.members)
        after = candles[p.formed_idx + 1 :]
        if p.side == "BSL":
            p.swept = any(_h(c) > p.price for c in after)
        else:
            p.swept = any(_l(c) < p.price for c in after)
    return pools


def detect_liquidity_pools(
    candles: Sequence[Any],
    pivot_k: int = 3,
    cluster_tol_pct: float = 0.0018,
) -> list[LiquidityPool]:
    """Detecteer alle buy-side + sell-side liquiditeit-pools in de candle-reeks.

    pivot_k: hoeveel buren aan elke kant een pivot moet domineren (hoger = strenger).
    cluster_tol_pct: pivots binnen deze % worden tot één pool (equal high/low) gegroepeerd.
    """
    if len(candles) < 2 * pivot_k + 2:
        return []
    ph, pl = _pivots(candles, pivot_k)
    bsl = _mark_swept(candles, _cluster(candles, ph, "BSL", cluster_tol_pct))
    ssl = _mark_swept(candles, _cluster(candles, pl, "SSL", cluster_tol_pct))
    return bsl + ssl


def nearest_unswept(pools: list[LiquidityPool], price: float, above: bool) -> LiquidityPool | None:
    """Dichtstbijzijnde onaangeroerde pool boven (above=True) of onder (above=False) de prijs."""
    cands = [
        p
        for p in pools
        if not p.swept and ((p.price > price) if above else (p.price < price))
    ]
    if not cands:
        return None
    return min(cands, key=lambda p: abs(p.price - price))
