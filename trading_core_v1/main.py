"""CLI entrypoint: run one backtest end-to-end and write research_results/<run_id>/.

Usage:
    .venv/bin/python -m trading_core_v1.main --config config/trading_core_v1.yaml [--split TEST]

Telegram lifecycle messages (backtest started / completed / gate PASS-FAIL) are
OPTIONAL and best-effort: if they fail (no token configured, network error,
etc.) the backtest still completes normally and exits 0. No dedicated Telegram
module exists elsewhere in this repo to reuse (audited: no `telegram` sender
found outside secret-name references), so this is a minimal, self-contained,
never-required wrapper reading TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the
same `.env.live` this repo already uses for all secrets.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from trading_core_v1.backtest import BacktestConfig, CostModel, basic_edge_gate, compute_metrics, run_backtest
from trading_core_v1.config import TradingCoreV1Config, apply_env_live_to_environ, load_config
from trading_core_v1.data import chronological_split, load_historical_candles, timeframe_to_ms
from trading_core_v1.journal import write_csv, write_jsonl
from trading_core_v1.strategies_v1.baseline_strategy import BaselineMACrossoverStrategy


def _iso_to_ms(iso: str) -> int:
    import datetime as dt

    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def _notify(message: str) -> None:
    """Best-effort, optional Telegram notification. Never raises."""
    try:
        import os

        import requests

        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5,
        )
    except Exception:
        return  # Telegram must never be required for the core to function.


def build_strategy(cfg: TradingCoreV1Config):
    params = cfg.strategy_params
    return BaselineMACrossoverStrategy(
        fast_period=int(params.get("fast_period", 20)),
        slow_period=int(params.get("slow_period", 50)),
        atr_period=int(params.get("atr_period", 14)),
        stop_atr_multiple=float(params.get("stop_atr_multiple", 2.0)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trading Core V1 backtest runner")
    parser.add_argument("--config", default=str(REPO_ROOT / "config" / "trading_core_v1.yaml"))
    parser.add_argument("--split", default="ALL", choices=["ALL", "TRAIN", "VALIDATION", "TEST"])
    parser.add_argument("--run-id", default=None)
    args = parser.parse_args(argv)

    cfg = load_config(Path(args.config))
    if cfg.telegram_enabled:
        apply_env_live_to_environ()

    run_id = args.run_id or f"{cfg.symbol}_{cfg.timeframe}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    out_dir = REPO_ROOT / "research_results" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    if cfg.telegram_enabled:
        _notify(f"Trading Core V1: backtest started ({run_id}, {cfg.symbol} {cfg.timeframe})")

    start_ms = _iso_to_ms(cfg.start)
    end_ms = _iso_to_ms(cfg.end)

    candles = load_historical_candles(
        symbol=cfg.symbol,
        granularity=cfg.timeframe,
        start_ms=start_ms,
        end_ms=end_ms,
        product_type=cfg.product_type,
        cache_dir=cfg.cache_dir,
    )

    splits = chronological_split(candles, **cfg.split)
    run_candles = candles if args.split == "ALL" else splits[args.split]
    if len(run_candles) < 2:
        print(f"ERROR: split {args.split} has too few candles ({len(run_candles)})", file=sys.stderr)
        return 1

    strategy = build_strategy(cfg)
    backtest_config = BacktestConfig(
        symbol=cfg.symbol,
        risk_pct=cfg.risk_pct,
        initial_equity=cfg.initial_equity,
        min_notional=cfg.min_notional,
        take_profit_r_multiple=cfg.take_profit_r_multiple,
        compounding=cfg.compounding,
        costs=CostModel(
            maker_fee_bps=cfg.maker_fee_bps,
            taker_fee_bps=cfg.taker_fee_bps,
            slippage_bps=cfg.slippage_bps,
        ),
    )

    result = run_backtest(run_candles, strategy, backtest_config)
    metrics = compute_metrics(
        result,
        initial_equity=cfg.initial_equity,
        start_ts=run_candles[0].timestamp,
        end_ts=run_candles[-1].timestamp,
    )
    gate = basic_edge_gate(metrics)
    metrics["BASIC_EDGE_GATE"] = gate

    write_csv(result.trades, out_dir / "trades.csv")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    (out_dir / "config.yaml").write_text(Path(args.config).read_text())
    metadata = {
        "strategy_name": strategy.name,
        "strategy_version": "1.0.0",
        "trading_core_version": "1.0.0",
        "symbol": cfg.symbol,
        "timeframe": cfg.timeframe,
        "split": args.split,
        "data_start_ms": run_candles[0].timestamp,
        "data_end_ms": run_candles[-1].timestamp,
        "bar_count": len(run_candles),
        "cost_assumptions": {
            "maker_fee_bps": cfg.maker_fee_bps,
            "taker_fee_bps": cfg.taker_fee_bps,
            "slippage_bps": cfg.slippage_bps,
        },
        "risk_pct": cfg.risk_pct,
        "initial_equity": cfg.initial_equity,
        "compounding": cfg.compounding,
        "rejections": result.rejections,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))
    write_jsonl(result.trades, out_dir / "trades.jsonl")

    print(json.dumps(metrics, indent=2))
    print(f"BASIC_EDGE_GATE={gate}")
    print(f"artifacts written to: {out_dir}")

    if cfg.telegram_enabled:
        _notify(
            f"Trading Core V1: backtest completed ({run_id}). "
            f"TOTAL_TRADES={metrics['TOTAL_TRADES']} NET_PNL={metrics['NET_PNL']:.2f} "
            f"BASIC_EDGE_GATE={gate}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
