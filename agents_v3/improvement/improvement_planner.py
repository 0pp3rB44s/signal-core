from __future__ import annotations

from dataclasses import dataclass

from agents_v3.repository.repo_indexer import RepoIndex
from agents_v3.improvement.docs_memory import DocsMemory
from agents_v3.tools.trade_analyzer import analyze_trades


MIN_TRADES_FOR_SIGNAL = 30


@dataclass
class ImprovementItem:
    priority: int
    title: str
    reason: str
    suggested_task: str


def build_performance_items(days: int = 14) -> list[ImprovementItem]:
    """Data-driven backlog items from live trading performance. This is what
    lets the agent chase the current biggest loss driver instead of a static
    list."""
    items: list[ImprovementItem] = []
    try:
        analysis = analyze_trades(days=days)
    except Exception:
        return items

    if analysis.trades < MIN_TRADES_FOR_SIGNAL:
        return items

    if analysis.net_pnl < 0 and analysis.fees > abs(analysis.gross_edge_before_fees) * 0.5:
        items.append(ImprovementItem(
            1,
            "Fee drag exceeds edge",
            f"Since {analysis.since}: net {analysis.net_pnl:+.2f} but {analysis.fees:.2f} fees on gross edge {analysis.gross_edge_before_fees:+.2f}.",
            "Reduce fee drag: use trade_stats and search_code on maker_entry to check the maker fill rate, then tune maker entry settings or reduce trade frequency in the strategy filters.",
        ))

    worst_name, worst = None, None
    for name, stats in analysis.by_strategy.items():
        if stats.trades >= MIN_TRADES_FOR_SIGNAL and stats.net_pnl < 0 and stats.winrate < 35.0:
            if worst is None or stats.net_pnl < worst.net_pnl:
                worst_name, worst = name, stats
    if worst_name and worst:
        items.append(ImprovementItem(
            1,
            f"Strategy {worst_name} is bleeding",
            f"{worst.trades} trades, winrate {worst.winrate:.1f}%, pnl {worst.net_pnl:+.2f} since {analysis.since}.",
            f"Tighten the entry filter of the {worst_name} strategy in strategies/ so it takes fewer low-quality setups; use trade_stats and read the strategy file first.",
        ))

    directions = analysis.by_direction
    if len(directions) == 2:
        (name_a, a), (name_b, b) = sorted(directions.items(), key=lambda kv: kv[1].net_pnl)
        if a.trades >= MIN_TRADES_FOR_SIGNAL and a.net_pnl < 0 <= b.net_pnl:
            items.append(ImprovementItem(
                2,
                f"{name_a} trades are net negative",
                f"{name_a}: {a.trades} trades {a.net_pnl:+.2f} vs {name_b}: {b.net_pnl:+.2f} since {analysis.since}.",
                f"Add a stricter confluence requirement for {name_a} entries in the strategy selector; ground the change in trade_stats output.",
            ))

    short_hold = analysis.by_duration.get("<1h")
    long_hold = analysis.by_duration.get(">=1h")
    if (
        short_hold and long_hold
        and short_hold.trades >= MIN_TRADES_FOR_SIGNAL
        and short_hold.net_pnl < 0 <= long_hold.net_pnl
    ):
        items.append(ImprovementItem(
            2,
            "Sub-1h churn loses money",
            f"<1h: {short_hold.net_pnl:+.2f} over {short_hold.trades} trades; >=1h: {long_hold.net_pnl:+.2f} since {analysis.since}.",
            "Reduce sub-hour churn: investigate which entry signals produce trades that close within an hour and tighten those filters in strategies/.",
        ))

    return items


def build_improvement_backlog(index: RepoIndex | None = None, git_changed_files: list[str] | None = None, docs_memory: DocsMemory | None = None) -> list[ImprovementItem]:
    changed = git_changed_files or []
    items: list[ImprovementItem] = list(build_performance_items())

    # docs/TODO.md open checkboxes follow the live performance signals.
    if docs_memory and docs_memory.todo_open_items:
        for raw in docs_memory.todo_open_items:
            task = raw.replace("- [ ]", "").strip().rstrip(".")
            if not task:
                continue
            items.append(ImprovementItem(
                3,
                "Docs TODO task",
                "Open TODO item found in docs/TODO.md.",
                task,
            ))

    if any(".cgcagent_pending.patch" in f for f in changed):
        items.append(ImprovementItem(
            4,
            "Patch apply cleanup",
            "A pending patch file exists and should not remain after successful apply.",
            "Verify pending patch cleanup and make it idempotent.",
        ))

    sorted_items = sorted(items, key=lambda x: x.priority)
    for idx, item in enumerate(sorted_items, start=1):
        item.priority = idx
    return sorted_items
