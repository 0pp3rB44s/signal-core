"""Public-data speculative-market classifier and bounded event pilots.

Research only: public Bitget GET endpoints, no credentials, no orders, no imports
from execution or strategy packages. Historical candle findings use the current
listed universe and current spread/depth snapshots; the report labels those
limitations instead of pretending they are point-in-time execution truth.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import requests

BASE = "https://api.bitget.com"
PRODUCT = "USDT-FUTURES"
SIZES = (10, 25, 50, 100, 250, 500, 1000)
TAKER_FEE_BPS_SIDE = 6.0


class PublicClient:
    def __init__(self, cache: Path) -> None:
        self.cache = cache
        self.cache.mkdir(parents=True, exist_ok=True)

    def get(self, path: str, params: dict, retries: int = 4):
        last = None
        for attempt in range(retries):
            try:
                response = requests.get(BASE + path, params=params, timeout=20)
                response.raise_for_status()
                payload = response.json()
                if payload.get("code") != "00000":
                    raise RuntimeError(str(payload)[:300])
                return payload["data"]
            except Exception as exc:  # public endpoint; bounded retry only
                last = exc
                time.sleep(0.5 * (2**attempt))
        raise RuntimeError(f"public GET failed: {path}: {last}")

    def candles(self, symbol: str, granularity: str = "1H", limit: int = 1000) -> list:
        target = self.cache / f"{symbol}_{granularity}_{limit}.json"
        if target.exists():
            return json.loads(target.read_text())
        rows = self.get("/api/v2/mix/market/candles", {
            "symbol": symbol, "productType": PRODUCT,
            "granularity": granularity, "limit": str(limit),
        })
        target.write_text(json.dumps(rows, separators=(",", ":")))
        return rows

    def candle_history(self, symbol: str, days: int) -> list:
        target = self.cache / f"{symbol}_1H_{days}d.json"
        if target.exists():
            return json.loads(target.read_text())
        rows = self.candles(symbol)
        target_bars = days * 24
        while len(rows) < target_bars:
            earliest = min(int(row[0]) for row in rows)
            older = self.get("/api/v2/mix/market/history-candles", {
                "symbol": symbol, "productType": PRODUCT, "granularity": "1H",
                # Bitget history-candles rejects values above 200 with 40053.
                "endTime": str(earliest - 1), "limit": "200",
            })
            unseen = [row for row in older if int(row[0]) < earliest]
            if not unseen:
                break
            rows = unseen + rows
            time.sleep(.05)
        rows = sorted({int(row[0]): row for row in rows}.values(), key=lambda row: int(row[0]))[-target_bars:]
        target.write_text(json.dumps(rows, separators=(",", ":")))
        return rows


def frame(rows: list) -> pd.DataFrame:
    cols = ["ts", "open", "high", "low", "close", "base_volume", "quote_volume"]
    if not rows:
        return pd.DataFrame(columns=cols)
    out = pd.DataFrame(rows, columns=cols)
    for col in cols:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values("ts").drop_duplicates("ts").reset_index(drop=True)


def percentile(series: pd.Series) -> pd.Series:
    return series.rank(pct=True, method="average")


def slippage_bps(levels: list, side: str, notional: float) -> float | None:
    remaining = notional
    spent = 0.0
    qty = 0.0
    for raw_price, raw_qty, *_ in levels:
        price, available = float(raw_price), float(raw_qty)
        take = min(remaining, price * available)
        if take <= 0:
            continue
        spent += take
        qty += take / price
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-6 or qty <= 0:
        return None
    avg = spent / qty
    best = float(levels[0][0])
    return (avg / best - 1) * 1e4 if side == "buy" else (1 - avg / best) * 1e4


def symbol_features(symbol: str, candles: pd.DataFrame, ticker: dict) -> dict:
    close = candles.close
    returns = np.log(close / close.shift(1))
    spread = np.nan
    bid, ask = float(ticker.get("bidPr") or 0), float(ticker.get("askPr") or 0)
    if bid > 0 and ask >= bid:
        spread = (ask - bid) / ((ask + bid) / 2) * 1e4
    recent = candles.tail(24 * 7)
    prior = candles.iloc[-24 * 14:-24 * 7]
    return {
        "symbol": symbol,
        "bars": len(candles),
        "history_days_observed": len(candles) / 24,
        "realized_vol_daily": float(returns.tail(24 * 7).std() * math.sqrt(24)),
        "median_range_bps": float(((recent.high - recent.low) / recent.close * 1e4).median()),
        "median_hourly_quote_volume": float(recent.quote_volume.median()),
        "turnover_24h": float(ticker.get("usdtVolume") or 0),
        "spread_bps": spread,
        "funding_rate_now": float(ticker.get("fundingRate") or np.nan),
        "price_acceleration_24h": float(abs(close.iloc[-1] / close.iloc[-25] - 1)) if len(close) > 25 else np.nan,
        "volume_acceleration": float(recent.quote_volume.tail(24).median() / prior.quote_volume.median())
            if len(prior) and prior.quote_volume.median() > 0 else np.nan,
    }


def classify(features: pd.DataFrame) -> pd.DataFrame:
    f = features.copy()
    f["vol_pct"] = percentile(f.realized_vol_daily)
    f["liq_pct"] = percentile(np.log1p(f.turnover_24h))
    f["spread_pct"] = percentile(f.spread_bps.fillna(np.inf))
    conditions = [
        (f.vol_pct >= .70) & (f.liq_pct >= .60) & (f.spread_pct <= .50),
        (f.vol_pct >= .60) & (f.liq_pct >= .30) & (f.spread_pct <= .75),
        (f.vol_pct >= .80) & (f.spread_bps <= 100) & (f.turnover_24h >= 100_000),
    ]
    f["tier"] = np.select(conditions, ["HV_A", "HV_B", "HV_C"], default="HV_D")
    return f


def event_rows(symbol: str, d: pd.DataFrame) -> list[dict]:
    x = d.copy()
    x["ret"] = x.close / x.close.shift(1) - 1
    x["trail_vol"] = x.ret.rolling(72, min_periods=48).std().shift(1)
    x["zret"] = x.ret / x.trail_vol
    x["vmed"] = x.quote_volume.rolling(72, min_periods=48).median().shift(1)
    x["vratio"] = x.quote_volume / x.vmed
    x["range"] = (x.high - x.low) / x.close
    x["range_med"] = x["range"].rolling(72, min_periods=48).median().shift(1)
    x["range_ratio"] = x["range"] / x.range_med
    for h in (1, 4, 12, 24):
        x[f"fwd_{h}"] = x.close.shift(-h) / x.close - 1
    specs = {
        "attention_continuation": (x.zret.abs() >= 2.5) & (x.vratio >= 2.0),
        "pump_exhaustion_reversal": (x.zret.abs() >= 4.0) & (x.vratio >= 3.0),
        "vol_expansion": (x.zret.abs() >= 1.5) & (x.range_ratio >= 2.5),
    }
    out = []
    for name, mask in specs.items():
        for idx in x.index[mask]:
            direction = np.sign(x.at[idx, "ret"])
            if name == "pump_exhaustion_reversal":
                direction *= -1
            for horizon in (1, 4, 12, 24):
                raw = x.at[idx, f"fwd_{horizon}"]
                if pd.isna(raw):
                    continue
                out.append({
                    "architecture": name, "symbol": symbol,
                    "event_ts": int(x.at[idx, "ts"]), "horizon_h": horizon,
                    "signed_return_bps": float(direction * raw * 1e4),
                    "absolute_forward_move_bps": float(abs(raw) * 1e4),
                    "zret": float(x.at[idx, "zret"]), "vratio": float(x.at[idx, "vratio"]),
                    "range_ratio": float(x.at[idx, "range_ratio"]),
                })
    return out


def robust_stats(group: pd.DataFrame, cost_bps: float) -> dict:
    values = group.signed_return_bps.replace([np.inf, -np.inf], np.nan).dropna()
    if values.empty:
        return {}
    lo, hi = values.quantile([.01, .99])
    wins = values.clip(lo, hi)
    trim = values[(values >= lo) & (values <= hi)]
    ex_top = group.loc[group.absolute_forward_move_bps <= group.absolute_forward_move_bps.quantile(.99), "signed_return_bps"]
    by_symbol = group.groupby("symbol").signed_return_bps.sum()
    positive_symbol = by_symbol.clip(lower=0)
    by_week = group.assign(week=pd.to_datetime(group.event_ts, unit="ms").dt.to_period("W").astype(str)).groupby("week").signed_return_bps.sum()
    positive_week = by_week.clip(lower=0)
    top_symbol = by_symbol.idxmax()
    top_week = by_week.idxmax()
    without_top_symbol = group.loc[group.symbol != top_symbol, "signed_return_bps"]
    weeks = pd.to_datetime(group.event_ts, unit="ms").dt.to_period("W").astype(str)
    without_top_week = group.loc[weeks != top_week, "signed_return_bps"]
    cutoff = group.event_ts.median()
    first = group.loc[group.event_ts <= cutoff, "signed_return_bps"]
    second = group.loc[group.event_ts > cutoff, "signed_return_bps"]
    span_days = max(1.0, (group.event_ts.max() - group.event_ts.min()) / 86_400_000)
    total_pos_symbol = positive_symbol.sum()
    total_pos_week = positive_week.sum()
    return {
        "events": int(len(values)), "symbols": int(group.symbol.nunique()),
        "mean_return_bps": float(values.mean()), "median_return_bps": float(values.median()),
        "trimmed_mean_bps": float(trim.mean()), "winsorized_mean_bps": float(wins.mean()),
        "return_ex_top1pct_bps": float(ex_top.mean()),
        "return_ex_top_symbol_bps": float(without_top_symbol.mean()),
        "return_ex_top_week_bps": float(without_top_week.mean()),
        "first_half_mean_bps": float(first.mean()), "second_half_mean_bps": float(second.mean()),
        "raw_events_day": float(len(values) / span_days),
        "net_edge_bps": float(values.mean() - cost_bps),
        "move_to_cost": float(group.absolute_forward_move_bps.mean() / cost_bps) if cost_bps > 0 else np.nan,
        "top_symbol_share": float(positive_symbol.max() / total_pos_symbol) if total_pos_symbol > 0 else np.nan,
        "top5_symbol_share": float(positive_symbol.nlargest(5).sum() / total_pos_symbol) if total_pos_symbol > 0 else np.nan,
        "top_week_share": float(positive_week.max() / total_pos_week) if total_pos_week > 0 else np.nan,
    }


def nonoverlapping(group: pd.DataFrame, horizon_h: int) -> pd.DataFrame:
    """Keep chronological events separated by the holding horizon per symbol."""
    kept = []
    gap_ms = horizon_h * 3_600_000
    for _, symbol_group in group.sort_values("event_ts").groupby("symbol"):
        last = -10**30
        for idx, row in symbol_group.iterrows():
            if row.event_ts - last >= gap_ms:
                kept.append(idx)
                last = row.event_ts
    return group.loc[kept]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, default=Path("research/data/speculative_v1"))
    parser.add_argument("--output", type=Path, default=Path("research/results/speculative_v1"))
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--history-days", type=int, default=180)
    parser.add_argument("--validation-tiers", default="HV_A")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    client = PublicClient(args.cache)
    contracts = client.get("/api/v2/mix/market/contracts", {"productType": PRODUCT})
    tickers = client.get("/api/v2/mix/market/tickers", {"productType": PRODUCT})
    ticker_by_symbol = {x["symbol"]: x for x in tickers}
    symbols = sorted({x["symbol"] for x in contracts if x.get("symbolStatus") == "normal" and x["symbol"] in ticker_by_symbol})

    def acquire(symbol: str):
        try:
            d = frame(client.candles(symbol))
            return symbol, d, None
        except Exception as exc:
            return symbol, pd.DataFrame(), str(exc)

    frames, errors = {}, {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for symbol, d, error in pool.map(acquire, symbols):
            if error or len(d) < 100:
                errors[symbol] = error or f"only {len(d)} bars"
            else:
                frames[symbol] = d

    features = classify(pd.DataFrame([symbol_features(s, d, ticker_by_symbol[s]) for s, d in frames.items()]))

    # Extend only the classified speculative tiers. Classification uses the
    # recent window above; the longer sample is reserved for event validation.
    validation_tiers = {value.strip() for value in args.validation_tiers.split(",") if value.strip()}
    validation_symbols = features.loc[features.tier.isin(validation_tiers), "symbol"].tolist()

    def extend(symbol: str):
        try:
            return symbol, frame(client.candle_history(symbol, args.history_days)), None
        except Exception as exc:
            return symbol, frames[symbol], str(exc)

    if args.history_days > 42:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for symbol, d, error in pool.map(extend, validation_symbols):
                frames[symbol] = d
                if error:
                    errors[f"history:{symbol}"] = error

    # Current depth is collected for likely executable high-volatility names only.
    eligible = features[features.tier != "HV_D"].sort_values(["tier", "turnover_24h"], ascending=[True, False])
    depth_symbols = eligible.symbol.head(200).tolist()
    depth_rows = []
    for symbol in depth_symbols:
        try:
            book = client.get("/api/v2/mix/market/merge-depth", {
                "symbol": symbol, "productType": PRODUCT, "precision": "scale0", "limit": "50"})
            row = {"symbol": symbol}
            for size in SIZES:
                buy = slippage_bps(book.get("asks") or [], "buy", size)
                sell = slippage_bps(book.get("bids") or [], "sell", size)
                row[f"roundtrip_slippage_bps_{size}"] = None if buy is None or sell is None else buy + sell
            depth_rows.append(row)
        except Exception as exc:
            errors[f"depth:{symbol}"] = str(exc)
        time.sleep(.06)
    depth = pd.DataFrame(depth_rows)
    features = features.merge(depth, on="symbol", how="left")
    features["cost_bps_100"] = 2 * TAKER_FEE_BPS_SIDE + features.spread_bps + features.roundtrip_slippage_bps_100

    all_events = []
    tiers = features.set_index("symbol").tier.to_dict()
    costs = features.set_index("symbol").cost_bps_100.to_dict()
    for symbol, d in frames.items():
        if tiers.get(symbol) != "HV_D":
            all_events.extend(event_rows(symbol, d))
    events = pd.DataFrame(all_events)
    if not events.empty:
        events["tier"] = events.symbol.map(tiers)
        events["estimated_cost_bps_100"] = events.symbol.map(costs)

    summaries = []
    if not events.empty:
        for (architecture, tier, horizon), group in events.groupby(["architecture", "tier", "horizon_h"]):
            group = group.dropna(subset=["estimated_cost_bps_100"])
            group = nonoverlapping(group, int(horizon))
            if group.empty:
                continue
            cost = float(group.estimated_cost_bps_100.median())
            summaries.append({"architecture": architecture, "tier": tier, "horizon_h": int(horizon),
                              "median_estimated_cost_bps_100": cost, **robust_stats(group, cost)})
    summary = pd.DataFrame(summaries).sort_values("net_edge_bps", ascending=False) if summaries else pd.DataFrame()
    features.to_csv(args.output / "universe_classifier.csv", index=False)
    events.to_csv(args.output / "events.csv", index=False)
    summary.to_csv(args.output / "pilot_summary.csv", index=False)
    metadata = {
        "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "exchange": "Bitget", "product": PRODUCT, "granularity": "1H",
        "markets_scanned": len(symbols), "markets_with_history": len(frames),
        "requested_validation_days": args.history_days,
        "extended_validation_tiers": sorted(validation_tiers),
        "eligible_markets": int((features.tier != "HV_D").sum()),
        "tier_counts": features.tier.value_counts().to_dict(), "errors": errors,
        "fee_assumption_bps_per_side": TAKER_FEE_BPS_SIDE,
        "limitations": [
            "current-listed universe creates survivorship bias",
            "current spread/depth snapshots are not historical event-time books",
            "classification uses the recent 1000 bars; eligible tiers use the requested extended history when available",
            "current tier assignment applied backward creates classification look-ahead",
            "funding/OI, liquidation, listing and cross-venue mechanisms require separate datasets",
        ],
        "orders_allowed": False, "public_data_only": True,
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True))
    print(json.dumps({**metadata, "errors": len(errors)}, indent=2, default=str))
    if not summary.empty:
        print(summary.head(20).to_string(index=False))


if __name__ == "__main__":
    main()
