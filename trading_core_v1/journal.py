"""Trade journal: one record per completed trade, CSV/JSONL exportable."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class TradeRecord:
    trade_id: int
    strategy: str
    symbol: str
    side: str
    signal_time: int
    entry_time: int
    entry_price: float
    stop_price: float
    exit_time: int
    exit_price: float
    exit_reason: str  # "STOP" | "TAKE_PROFIT" | "END_OF_DATA"
    position_size: float
    gross_pnl: float
    fees: float
    slippage: float
    net_pnl: float
    equity_before: float
    equity_after: float
    risk_amount: float
    r_multiple: float


def write_csv(trades: Sequence[TradeRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [f.name for f in fields(TradeRecord)]
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for t in trades:
            writer.writerow(asdict(t))


def write_jsonl(trades: Sequence[TradeRecord], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        for t in trades:
            fh.write(json.dumps(asdict(t)) + "\n")
