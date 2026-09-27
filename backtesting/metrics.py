from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict
from typing import Dict, List, Any, Protocol



class TradeLike(Protocol):
    symbol: str
    strategy: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    result: str
    pnl_pct: float
    candles_held: int
    tp1_hit: bool
    timed_exit: bool
    regime: str


def _trade_regime(trade: TradeLike) -> str:
    regime = getattr(trade, "regime", "")
    if regime:
        return str(regime).lower()

    if trade.direction.upper() == "LONG":
        return "bullish"
    if trade.direction.upper() == "SHORT":
        return "bearish"
    return "chop"


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def summarize(trades: List[TradeLike]) -> Dict[str, Any]:
    if not trades:
        return {
            "trades": 0,
            "winrate": 0.0,
            "lossrate": 0.0,
            "breakeven_rate": 0.0,
            "tp1_hit_rate": 0.0,
            "timed_exit_rate": 0.0,
            "pnl": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "max_dd": 0.0,
            "by_strategy": {},
            "by_symbol": {},
            "by_regime": {},
            "expectancy_matrix": {
                "strategy_direction": {},
                "strategy_regime": {},
                "symbol_direction": {},
            },
        }

    wins = [t for t in trades if t.pnl_pct > 0]
    losses = [t for t in trades if t.pnl_pct < 0]
    breakevens = [t for t in trades if t.pnl_pct == 0]
    tp1_hits = [t for t in trades if getattr(t, "tp1_hit", False)]
    timed_exits = [t for t in trades if getattr(t, "timed_exit", False)]

    total = len(trades)
    winrate = _safe_div(len(wins), total)
    lossrate = _safe_div(len(losses), total)
    breakeven_rate = _safe_div(len(breakevens), total)
    tp1_hit_rate = _safe_div(len(tp1_hits), total)
    timed_exit_rate = _safe_div(len(timed_exits), total)
    pnl = sum(t.pnl_pct for t in trades)

    avg_win = _safe_div(sum(t.pnl_pct for t in wins), len(wins))
    avg_loss = _safe_div(sum(t.pnl_pct for t in losses), len(losses))

    expectancy = _safe_div(pnl, total)

    gross_profit = sum(t.pnl_pct for t in wins)
    gross_loss = -sum(t.pnl_pct for t in losses)
    profit_factor = _safe_div(gross_profit, gross_loss)

    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.pnl_pct
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    by_strategy = defaultdict(list)
    by_symbol = defaultdict(list)
    by_regime = defaultdict(list)

    by_strategy_direction = defaultdict(list)
    by_strategy_regime = defaultdict(list)
    by_symbol_direction = defaultdict(list)

    for t in trades:
        regime = _trade_regime(t)
        direction = str(getattr(t, "direction", "UNKNOWN")).upper()

        by_strategy[t.strategy].append(t)
        by_symbol[t.symbol].append(t)
        by_regime[regime].append(t)

        by_strategy_direction[f"{t.strategy}:{direction}"].append(t)
        by_strategy_regime[f"{t.strategy}:{regime}"].append(t)
        by_symbol_direction[f"{t.symbol}:{direction}"].append(t)

    def _group_stats(group: List[TradeLike]) -> Dict[str, Any]:
        if not group:
            return {"trades": 0}
        g_wins = [x for x in group if x.pnl_pct > 0]
        g_losses = [x for x in group if x.pnl_pct < 0]
        g_breakevens = [x for x in group if x.pnl_pct == 0]
        g_tp1_hits = [x for x in group if getattr(x, "tp1_hit", False)]
        g_timed_exits = [x for x in group if getattr(x, "timed_exit", False)]
        g_total = len(group)
        g_winrate = _safe_div(len(g_wins), g_total)
        g_lossrate = _safe_div(len(g_losses), g_total)
        g_breakeven_rate = _safe_div(len(g_breakevens), g_total)
        g_tp1_hit_rate = _safe_div(len(g_tp1_hits), g_total)
        g_timed_exit_rate = _safe_div(len(g_timed_exits), g_total)
        g_pnl = sum(x.pnl_pct for x in group)
        g_avg_win = _safe_div(sum(x.pnl_pct for x in g_wins), len(g_wins))
        g_avg_loss = _safe_div(sum(x.pnl_pct for x in g_losses), len(g_losses))
        g_expectancy = _safe_div(g_pnl, g_total)
        return {
            "trades": g_total,
            "winrate": round(g_winrate, 3),
            "lossrate": round(g_lossrate, 3),
            "breakeven_rate": round(g_breakeven_rate, 3),
            "tp1_hit_rate": round(g_tp1_hit_rate, 3),
            "timed_exit_rate": round(g_timed_exit_rate, 3),
            "pnl": round(g_pnl, 3),
            "avg_win": round(g_avg_win, 3),
            "avg_loss": round(g_avg_loss, 3),
            "expectancy": round(g_expectancy, 3),
        }

    by_strategy_stats = {k: _group_stats(v) for k, v in by_strategy.items()}
    by_symbol_stats = {k: _group_stats(v) for k, v in by_symbol.items()}
    by_regime_stats = {k: _group_stats(v) for k, v in by_regime.items()}

    by_strategy_direction_stats = {
        k: _group_stats(v) for k, v in by_strategy_direction.items()
    }
    by_strategy_regime_stats = {
        k: _group_stats(v) for k, v in by_strategy_regime.items()
    }
    by_symbol_direction_stats = {
        k: _group_stats(v) for k, v in by_symbol_direction.items()
    }

    return {
        "trades": total,
        "winrate": round(winrate, 3),
        "lossrate": round(lossrate, 3),
        "breakeven_rate": round(breakeven_rate, 3),
        "tp1_hit_rate": round(tp1_hit_rate, 3),
        "timed_exit_rate": round(timed_exit_rate, 3),
        "pnl": round(pnl, 3),
        "avg_win": round(avg_win, 3),
        "avg_loss": round(avg_loss, 3),
        "expectancy": round(expectancy, 3),
        "profit_factor": round(profit_factor, 3),
        "max_dd": round(max_dd, 3),
        "by_strategy": by_strategy_stats,
        "by_symbol": by_symbol_stats,
        "by_regime": by_regime_stats,
        "expectancy_matrix": {
            "strategy_direction": by_strategy_direction_stats,
            "strategy_regime": by_strategy_regime_stats,
            "symbol_direction": by_symbol_direction_stats,
        },
    }


def attach_trade_log(trades: List[TradeLike]) -> List[dict]:
    return [asdict(t) for t in trades]