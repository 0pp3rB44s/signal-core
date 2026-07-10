from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


TRADE_DATASET = Path("logs/trade_dataset_v2.csv")


@dataclass
class GroupStats:
    trades: int = 0
    wins: int = 0
    net_pnl: float = 0.0

    @property
    def winrate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0


@dataclass
class TradeAnalysis:
    since: str
    trades: int
    wins: int
    net_pnl: float
    gross_wins: float
    gross_losses: float
    fees: float
    by_strategy: dict[str, GroupStats] = field(default_factory=dict)
    by_direction: dict[str, GroupStats] = field(default_factory=dict)
    by_duration: dict[str, GroupStats] = field(default_factory=dict)

    @property
    def winrate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0

    @property
    def gross_edge_before_fees(self) -> float:
        # net_pnl in the dataset is Bitget netProfit (fee-inclusive),
        # so adding fees back approximates the pre-fee edge.
        return self.net_pnl + self.fees


def _duration_bucket(opened_at: str, closed_at: str) -> str:
    try:
        opened = datetime.fromisoformat(opened_at)
        closed = datetime.fromisoformat(closed_at)
    except (ValueError, TypeError):
        return "unknown"
    minutes = (closed - opened).total_seconds() / 60.0
    if minutes < 60:
        return "<1h"
    return ">=1h"


def analyze_trades(csv_path: Path | str = TRADE_DATASET, days: int = 14) -> TradeAnalysis:
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    since = since_dt.strftime("%Y-%m-%d")

    analysis = TradeAnalysis(
        since=since, trades=0, wins=0, net_pnl=0.0,
        gross_wins=0.0, gross_losses=0.0, fees=0.0,
    )
    by_strategy: dict[str, GroupStats] = defaultdict(GroupStats)
    by_direction: dict[str, GroupStats] = defaultdict(GroupStats)
    by_duration: dict[str, GroupStats] = defaultdict(GroupStats)

    path = Path(csv_path)
    if not path.exists():
        return analysis

    with path.open() as handle:
        for row in csv.DictReader(handle):
            if row.get("event_type") != "CLOSE":
                continue
            if (row.get("timestamp") or "") < since:
                continue
            try:
                pnl = float(row.get("net_pnl") or 0.0)
            except ValueError:
                continue
            try:
                fee = abs(float(row.get("fees") or 0.0))
            except ValueError:
                fee = 0.0

            analysis.trades += 1
            analysis.net_pnl += pnl
            analysis.fees += fee
            if pnl > 0:
                analysis.wins += 1
                analysis.gross_wins += pnl
            else:
                analysis.gross_losses += pnl

            for grouping, key in (
                (by_strategy, row.get("strategy") or "(unknown)"),
                (by_direction, row.get("direction") or "(unknown)"),
                (by_duration, _duration_bucket(row.get("opened_at") or "", row.get("closed_at") or "")),
            ):
                stats = grouping[key]
                stats.trades += 1
                stats.net_pnl += pnl
                if pnl > 0:
                    stats.wins += 1

    analysis.by_strategy = dict(by_strategy)
    analysis.by_direction = dict(by_direction)
    analysis.by_duration = dict(by_duration)
    return analysis


def format_report(analysis: TradeAnalysis) -> str:
    lines = [
        f"Trade performance since {analysis.since} ({analysis.trades} closed trades):",
        f"- net pnl: {analysis.net_pnl:+.4f} USDT | winrate: {analysis.winrate:.1f}%",
        f"- gross edge before fees: {analysis.gross_edge_before_fees:+.4f} | fees paid: {analysis.fees:.4f}",
        "",
        "By strategy (sorted by pnl):",
    ]
    for name, stats in sorted(analysis.by_strategy.items(), key=lambda kv: kv[1].net_pnl):
        lines.append(f"- {name}: n={stats.trades} wr={stats.winrate:.1f}% pnl={stats.net_pnl:+.4f}")
    lines.append("")
    lines.append("By direction:")
    for name, stats in sorted(analysis.by_direction.items()):
        lines.append(f"- {name}: n={stats.trades} wr={stats.winrate:.1f}% pnl={stats.net_pnl:+.4f}")
    lines.append("")
    lines.append("By duration:")
    for name, stats in sorted(analysis.by_duration.items()):
        lines.append(f"- {name}: n={stats.trades} wr={stats.winrate:.1f}% pnl={stats.net_pnl:+.4f}")
    return "\n".join(lines)
