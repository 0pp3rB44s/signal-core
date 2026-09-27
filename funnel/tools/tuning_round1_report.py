"""CLI: run Tuning Round 1's A/B (funnel.tools.tuning_round1_ab) and print every required metric."""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from typing import Any

from funnel.tools.tuning_round1_ab import VariantState, run, _cluster_of


def _profit_factor(pnls: list[float]) -> float | None:
    winners = sum(p for p in pnls if p > 0)
    losers = sum(p for p in pnls if p < 0)
    if losers == 0:
        return None
    return round(winners / abs(losers), 4)


def _max_drawdown(trades: list[dict[str, Any]]) -> float:
    ordered = sorted(trades, key=lambda t: t["closed_t_ms"])
    equity = peak = max_dd = 0.0
    for t in ordered:
        equity += t["net_pnl"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 6)


def _herfindahl(counter: Counter) -> float:
    total = sum(counter.values())
    if total == 0:
        return 0.0
    return round(sum((n / total) ** 2 for n in counter.values()), 4)


def _max_simultaneous_same_cluster(variant: VariantState) -> int:
    return max((max(counts.values(), default=0) for _t, counts in variant.concurrent_open_log), default=0)


def metrics_for(variant: VariantState, *, exclude_symbols: set[str] | None = None) -> dict[str, Any]:
    exclude_symbols = exclude_symbols or set()
    trades = [t for t in variant.trades if t["symbol"] not in exclude_symbols]
    pnls = [t["net_pnl"] for t in trades]
    gross = [t["gross_pnl"] for t in trades]
    fees = sum(t["fee"] for t in trades)
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p < 0]
    by_symbol = Counter(t["symbol"] for t in trades)
    by_strategy = Counter(t["strategy"] for t in trades)
    return {
        "n_signals": len([s for s in variant.signals if s["symbol"] not in exclude_symbols]),
        "n_accepted": len([a for a in variant.accepted if a["symbol"] not in exclude_symbols]),
        "n_trades": len(trades),
        "gross_pnl": round(sum(gross), 4),
        "net_pnl": round(sum(pnls), 4),
        "fees": round(fees, 4),
        "profit_factor": _profit_factor(pnls),
        "mean_trade": round(statistics.mean(pnls), 4) if pnls else None,
        "median_trade": round(statistics.median(pnls), 4) if pnls else None,
        "win_rate": round(len(winners) / len(pnls), 4) if pnls else None,
        "avg_winner": round(statistics.mean(winners), 4) if winners else None,
        "avg_loser": round(statistics.mean(losers), 4) if losers else None,
        "max_drawdown": _max_drawdown(trades),
        "symbol_concentration_hhi": _herfindahl(by_symbol),
        "strategy_concentration_hhi": _herfindahl(by_strategy),
        "top_symbol_share": round(max(by_symbol.values()) / sum(by_symbol.values()), 4) if by_symbol else None,
        "top_strategy_share": round(max(by_strategy.values()) / sum(by_strategy.values()), 4) if by_strategy else None,
        "by_symbol_n": dict(by_symbol),
        "by_strategy_n": dict(by_strategy),
        "max_simultaneous_same_cluster": _max_simultaneous_same_cluster(variant) if not exclude_symbols else None,
        "turnover_trades": len(trades),
    }


def robustness(variant: VariantState) -> dict[str, Any]:
    all_symbols = {t["symbol"] for t in variant.trades}
    by_sym_pnl = defaultdict(float)
    for t in variant.trades:
        by_sym_pnl[t["symbol"]] += t["net_pnl"]
    best_symbol = max(by_sym_pnl, key=by_sym_pnl.get) if by_sym_pnl else None
    worst_symbol = min(by_sym_pnl, key=by_sym_pnl.get) if by_sym_pnl else None
    by_week_pnl = defaultdict(float)
    for t in variant.trades:
        week = t["closed_t_ms"] // (7 * 86400_000)
        by_week_pnl[week] += t["net_pnl"]
    best_week = max(by_week_pnl, key=by_week_pnl.get) if by_week_pnl else None
    worst_week = min(by_week_pnl, key=by_week_pnl.get) if by_week_pnl else None
    out = {
        "excluding_BTCUSDT": metrics_for(variant, exclude_symbols={"BTCUSDT"}),
        "excluding_best_symbol": metrics_for(variant, exclude_symbols={best_symbol} if best_symbol else set()),
        "excluding_worst_symbol": metrics_for(variant, exclude_symbols={worst_symbol} if worst_symbol else set()),
    }
    if best_week is not None:
        excl = {t["symbol"] for t in variant.trades if t["closed_t_ms"] // (7 * 86400_000) == best_week}
        out["excluding_best_week_note"] = f"week_bucket={best_week}, pnl={round(by_week_pnl[best_week],4)}"
    if worst_week is not None:
        out["excluding_worst_week_note"] = f"week_bucket={worst_week}, pnl={round(by_week_pnl[worst_week],4)}"
    out["by_symbol_pnl"] = {k: round(v, 4) for k, v in by_sym_pnl.items()}
    out["by_week_pnl"] = {str(k): round(v, 4) for k, v in by_week_pnl.items()}
    return out


def main() -> None:
    # NAV=100.0: CAPITAL_MIN_USDT, the floor of the owner-approved capital band the frozen cap value
    # was mechanistically derived against (TUNING_ROUND1_CAP_SELECTION_2026-09-27.md). Testing at any
    # other NAV would evaluate the cap outside the conditions it was actually derived for.
    result = run(nav_usdt=100.0)
    a, b, blocked = result["variant_a"], result["variant_b"], result["blocked_by_cap"]
    m_a, m_b = metrics_for(a), metrics_for(b)
    blocked_pnl = 0.0
    blocked_detail = []
    for blk in blocked:
        matches = [t for t in a.trades if t["symbol"] == blk["symbol"] and t["strategy"] == blk["strategy"]
                  and abs(t["opened_t_ms"] - blk["t_ms"]) < 2000]
        for t in matches:
            blocked_pnl += t["net_pnl"]
            blocked_detail.append({**blk, "a_realized_net_pnl": t["net_pnl"]})
    report = {
        "ORIGINAL": m_a, "TUNED": m_b,
        "ORIGINAL_robustness": robustness(a), "TUNED_robustness": robustness(b),
        "TRADES_BLOCKED_BY_NEW_CAP": len(blocked),
        "PNL_OF_BLOCKED_TRADES_diagnostic_only": round(blocked_pnl, 4),
        "blocked_detail": blocked_detail,
        "rejections_by_stage_ORIGINAL": dict(a.rejections_by_stage),
        "rejections_by_stage_TUNED": dict(b.rejections_by_stage),
    }
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
