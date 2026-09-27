from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from backtesting.optimizer import optimize
from scripts.run_backtest import load_market_data


def main() -> None:
    print("Starting optimizer...\n")

    settings = get_settings()
    market_data = load_market_data()

    if not market_data:
        print("No backtest data found.")
        print("Run download_backtest_data.py first.")
        return

    report = optimize(settings=settings, market_data=market_data, top_n=10)

    print("\n=== OPTIMIZER TOP RESULTS ===\n")

    for i, row in enumerate(report["top"], start=1):
        params = row["params"]
        result = row["result"]

        print(f"#{i}")
        print(f"Score: {row['rank_score']}")
        print(f"Params: {params}")
        print(
            f"Trades: {result.get('trades')} | "
            f"Winrate: {result.get('winrate')} | "
            f"PnL: {result.get('pnl')}"
        )
        print("-" * 40)

    report_path = Path("reports/backtests/optimizer_report.json")
    print(f"\nReport saved to: {report_path.resolve()}\n")


if __name__ == "__main__":
    main()
