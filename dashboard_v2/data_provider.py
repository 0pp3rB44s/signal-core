import json
from pathlib import Path
from typing import Any
import csv
from datetime import datetime, timezone, timedelta
import time

from app.config import get_settings
from clients.bitget_rest import BitgetRestClient
from risk.risk_manager import RiskManager

BASE_PATH = Path(__file__).resolve().parents[1]
LOGS_PATH = BASE_PATH / "logs"
STATE_PATH = BASE_PATH / "state"

REPORTS_PATH = BASE_PATH / "reports" / "backtests"

_DASHBOARD_CACHE = {
    "timestamp": 0.0,
    "data": None,
}

CACHE_SECONDS = 3



def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# Inserted function as requested
def _build_protection_alerts() -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []

    positions = _read_json(STATE_PATH / "executed_trades.json") or []
    events = _read_json(STATE_PATH / "position_events.json") or []

    for p in positions if isinstance(positions, list) else []:
        if p.get("status") != "OPEN":
            continue

        symbol = str(p.get("symbol") or "UNKNOWN")
        stop_loss = float(p.get("stop_loss") or 0)
        take_profits = p.get("take_profits") or []
        tp1_hit = bool(p.get("tp1_hit"))
        break_even_active = bool(p.get("break_even_active"))
        protection_verified = bool(p.get("protection_verified"))

        if stop_loss <= 0 or not take_profits:
            alerts.append({"level": "danger", "message": f"{symbol}: open position missing SL/TP"})
        elif not protection_verified:
            alerts.append({"level": "warning", "message": f"{symbol}: protection not verified"})

        if tp1_hit and not break_even_active:
            alerts.append({"level": "danger", "message": f"{symbol}: TP1 hit but BE not active"})

    if isinstance(events, list):
        for e in events[-10:]:
            note = str(e.get("note") or e.get("message") or "").lower()
            symbol = str(e.get("symbol") or "")

            if "be" in note or "break_even" in note:
                alerts.append({"level": "success", "message": f"{symbol}: SL moved to BE"})
            elif "fail" in note:
                alerts.append({"level": "danger", "message": f"{symbol}: protection failure detected"})

    return alerts


def _build_position_protection_status(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    state_positions = _read_json(STATE_PATH / "executed_trades.json") or []

    state_by_symbol = {
        str(p.get("symbol") or ""): p
        for p in state_positions
        if isinstance(p, dict) and p.get("status") == "OPEN"
    }

    for p in positions:
        symbol = str(p.get("symbol") or "UNKNOWN")
        state = state_by_symbol.get(symbol, {})
        sl = p.get("sl")
        tp = p.get("tp")
        tp1_hit = bool(state.get("tp1_hit"))
        tp2_hit = bool(state.get("tp2_hit"))
        tp3_hit = bool(state.get("tp3_hit"))
        be_active = bool(state.get("break_even_active"))
        trailing_active = bool(state.get("trailing_active"))
        protection_verified = bool(state.get("protection_verified"))
        exchange_synced = bool(state.get("exchange_synced", protection_verified))

        has_sl = sl not in (None, "", "MISSING")
        has_tp = tp not in (None, "", "MISSING")

        if not has_sl or not has_tp:
            level = "danger"
            status = "MISSING PROTECTION"
        elif tp1_hit and not be_active:
            level = "danger"
            status = "TP1 HIT · BE NOT ACTIVE"
        elif be_active:
            level = "success"
            status = "BE ACTIVE"
        elif protection_verified:
            level = "success"
            status = "PROTECTED"
        else:
            level = "warning"
            status = "PROTECTION UNVERIFIED"

        rows.append({
            "symbol": symbol,
            "direction": p.get("direction"),
            "status": status,
            "level": level,
            "sl": sl,
            "tp": tp,
            "tp1_hit": tp1_hit,
            "tp2_hit": tp2_hit,
            "tp3_hit": tp3_hit,
            "be_active": be_active,
            "trailing_active": trailing_active,
            "exchange_synced": exchange_synced,
            "protection_verified": protection_verified,
            "orphan_risk": not exchange_synced and has_sl,
        })

    return rows



def _parse_scan_line(line: str) -> dict[str, Any] | None:
    if " | SCAN | " not in line:
        return None

    parts = [part.strip() for part in line.split("|")]
    if len(parts) < 5:
        return None

    symbol = parts[3]
    text = " | ".join(parts[4:])

    def _extract_float(marker: str, fallback: float = 0.0) -> float:
        try:
            after = text.split(marker, 1)[1]
            token = after.strip().split()[0].replace("%", "").replace(";", "")
            return float(token)
        except Exception:
            return fallback

    alignment = "unknown"
    if "align=" in text:
        alignment = text.split("align=", 1)[1].split("|", 1)[0].strip()

    primary_trend = "unknown"
    if "15m=" in text:
        primary_trend = text.split("15m=", 1)[1].split()[0].strip()

    confirmation_trend = "unknown"
    if "1H=" in text:
        confirmation_trend = text.split("1H=", 1)[1].split("|", 1)[0].strip()

    score_hint = _extract_float("score_hint=")
    volume_ratio = _extract_float("vr=")
    move_pct = _extract_float("15m=" if "15m=" in text else "move=")
    volatility_rank = _extract_float("volatility rank")

    has_volume_expansion = "volume expansion" in text.lower()

    if alignment.startswith("aligned") and volume_ratio >= 1.2 and score_hint >= 65:
        level = "success"
    elif has_volume_expansion or volatility_rank >= 25 or score_hint >= 60:
        level = "warning"
    else:
        level = "neutral"

    return {
        "symbol": symbol,
        "alignment": alignment,
        "primary_trend": primary_trend,
        "confirmation_trend": confirmation_trend,
        "score_hint": round(score_hint, 1),
        "volume_ratio": round(volume_ratio, 2),
        "move_pct": round(move_pct, 3),
        "volatility_rank": round(volatility_rank, 1),
        "volume_expansion": has_volume_expansion,
        "level": level,
    }


def _build_volatility_heatmap() -> list[dict[str, Any]]:
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    lines = _read_lines(LOGS_PATH / "bot.out", limit=500)

    for line in reversed(lines):
        row = _parse_scan_line(line)
        if not row:
            continue
        if row["symbol"] in rows_by_symbol:
            continue
        rows_by_symbol[row["symbol"]] = row

    rows = list(rows_by_symbol.values())
    rows.sort(
        key=lambda row: (
            row.get("volatility_rank", 0),
            row.get("volume_ratio", 0),
            row.get("score_hint", 0),
        ),
        reverse=True,
    )
    return rows[:12]

def _build_candidate_board() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    lines = _read_lines(LOGS_PATH / "bot.out", limit=250)

    for line in reversed(lines):
        if " | SCAN | " not in line:
            continue

        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 5:
            continue

        symbol = parts[3] if len(parts) > 3 else "UNKNOWN"
        rest = " | ".join(parts[4:])

        if any(row["symbol"] == symbol for row in candidates):
            continue

        candidates.append({
            "symbol": symbol,
            "summary": rest,
            "level": "success" if "score_hint=80" in rest or "score_hint=75" in rest else "warning" if "score_hint=65" in rest or "score_hint=70" in rest else "neutral",
        })

        if len(candidates) >= 5:
            break

    return candidates


def _build_bot_health(last_log_lines: list[str], live_errors: list[str]) -> dict[str, Any]:
    error_lines = [line for line in last_log_lines if "error" in line.lower() or "fail" in line.lower()]
    latest_scan = next((line for line in reversed(last_log_lines) if " | SCAN | " in line), "")
    latest_summary = next((line for line in reversed(last_log_lines) if " | SUMMARY | " in line), "")
    latest_setup = next((line for line in reversed(last_log_lines) if " | SETUP | " in line or " | PLAN | " in line), "")

    if live_errors or error_lines:
        level = "danger"
    elif latest_scan:
        level = "success"
    else:
        level = "warning"

    return {
        "level": level,
        "latest_scan": latest_scan,
        "latest_summary": latest_summary,
        "latest_setup": latest_setup,
        "recent_errors": error_lines[-5:],
        "live_error_count": len(live_errors),
    }




def _strategy_status(expectancy: float, trades: int) -> tuple[str, str]:
    if trades < 5:
        return "WATCH", "warning"
    if expectancy > 0.15:
        return "GOOD", "success"
    if expectancy < 0.0:
        return "PAUSE", "danger"
    return "WATCH", "warning"



def _symbol_status(expectancy: float, trades: int, tp1_hit_rate: float) -> tuple[str, str]:
    if trades < 3:
        return "WATCH", "warning"
    if expectancy > 0.20 and tp1_hit_rate >= 0.45:
        return "GOOD", "success"
    if expectancy < 0.0:
        return "PAUSE", "danger"
    return "WATCH", "warning"


def _build_symbol_expectancy() -> list[dict[str, Any]]:
    summary = _read_json(REPORTS_PATH / "latest_summary.json") or {}
    by_symbol = summary.get("by_symbol") or {}

    rows: list[dict[str, Any]] = []
    for symbol, raw in by_symbol.items():
        if not isinstance(raw, dict):
            continue

        trades = int(raw.get("trades") or 0)
        expectancy = _safe_float(raw.get("expectancy"))
        winrate = _safe_float(raw.get("winrate"))
        tp1_hit_rate = _safe_float(raw.get("tp1_hit_rate"))
        timed_exit_rate = _safe_float(raw.get("timed_exit_rate"))
        pnl = _safe_float(raw.get("pnl"))
        status, level = _symbol_status(expectancy, trades, tp1_hit_rate)

        rows.append({
            "symbol": symbol,
            "trades": trades,
            "winrate": round(winrate, 3),
            "expectancy": round(expectancy, 3),
            "tp1_hit_rate": round(tp1_hit_rate, 3),
            "timed_exit_rate": round(timed_exit_rate, 3),
            "pnl": round(pnl, 3),
            "status": status,
            "level": level,
        })

    rows.sort(key=lambda row: (row["expectancy"], row["tp1_hit_rate"], row["trades"]), reverse=True)
    return rows[:12]


def _build_expectancy_matrix() -> dict[str, Any]:
    summary = _read_json(REPORTS_PATH / "latest_summary.json") or {}
    matrix = summary.get("expectancy_matrix") or {}

    def _flatten_matrix(section: dict[str, Any], label_a: str, label_b: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not isinstance(section, dict):
            return rows

        for left_key, right_map in section.items():
            if not isinstance(right_map, dict):
                continue
            for right_key, expectancy in right_map.items():
                exp = _safe_float(expectancy)
                if exp > 0.15:
                    level = "success"
                    status = "GOOD"
                elif exp < 0:
                    level = "danger"
                    status = "PAUSE"
                else:
                    level = "warning"
                    status = "WATCH"

                rows.append({
                    label_a: left_key,
                    label_b: right_key,
                    "expectancy": round(exp, 4),
                    "status": status,
                    "level": level,
                })

        rows.sort(key=lambda row: row["expectancy"], reverse=True)
        return rows[:12]

    return {
        "strategy_direction": _flatten_matrix(matrix.get("strategy_direction") or {}, "strategy", "direction"),
        "strategy_regime": _flatten_matrix(matrix.get("strategy_regime") or {}, "strategy", "regime"),
        "symbol_direction": _flatten_matrix(matrix.get("symbol_direction") or {}, "symbol", "direction"),
    }


def _build_strategy_expectancy() -> list[dict[str, Any]]:
    summary = _read_json(REPORTS_PATH / "latest_summary.json") or {}
    by_strategy = summary.get("by_strategy") or {}

    expected_rows = [
        ("Liquidity Sweep", "LONG", "liquidity_sweep_reversal"),
        ("Liquidity Sweep", "SHORT", "liquidity_sweep_reversal_short"),
        ("Momentum Breakout", "LONG", "momentum_breakout"),
        ("Momentum Breakdown", "SHORT", "momentum_breakdown"),
    ]

    rows: list[dict[str, Any]] = []
    for label, direction, key in expected_rows:
        raw = by_strategy.get(key) or {}
        trades = int(raw.get("trades") or 0)
        expectancy = _safe_float(raw.get("expectancy"))
        winrate = _safe_float(raw.get("winrate"))
        tp1_hit_rate = _safe_float(raw.get("tp1_hit_rate"))
        timed_exit_rate = _safe_float(raw.get("timed_exit_rate"))
        pnl = _safe_float(raw.get("pnl"))
        status, level = _strategy_status(expectancy, trades)

        rows.append({
            "name": label,
            "direction": direction,
            "key": key,
            "trades": trades,
            "winrate": round(winrate, 3),
            "expectancy": round(expectancy, 3),
            "tp1_hit_rate": round(tp1_hit_rate, 3),
            "timed_exit_rate": round(timed_exit_rate, 3),
            "pnl": round(pnl, 3),
            "status": status,
            "level": level,
        })

    return rows


def _build_strategy_weighting_status() -> list[dict[str, Any]]:
    summary = _read_json(REPORTS_PATH / "latest_summary.json") or {}
    by_strategy = summary.get("by_strategy") or {}

    rows: list[dict[str, Any]] = []
    for strategy, raw in by_strategy.items():
        if not isinstance(raw, dict):
            continue

        trades = int(raw.get("trades") or 0)
        expectancy = _safe_float(raw.get("expectancy"))
        tp1_hit_rate = _safe_float(raw.get("tp1_hit_rate"))
        winrate = _safe_float(raw.get("winrate"))

        if trades < 5:
            status = "WATCH"
            level = "warning"
            action = "insufficient data"
        elif expectancy < 0 or tp1_hit_rate < 0.25:
            status = "PAUSE"
            level = "danger"
            action = "block / reduce priority"
        elif expectancy >= 0.15 and tp1_hit_rate >= 0.45:
            status = "BOOST"
            level = "success"
            action = "increase priority"
        else:
            status = "WATCH"
            level = "warning"
            action = "normal priority"

        rows.append({
            "strategy": strategy,
            "trades": trades,
            "expectancy": round(expectancy, 3),
            "tp1_hit_rate": round(tp1_hit_rate, 3),
            "winrate": round(winrate, 3),
            "status": status,
            "level": level,
            "action": action,
        })

    rows.sort(key=lambda row: (row["status"] == "BOOST", row["expectancy"], row["tp1_hit_rate"]), reverse=True)
    return rows[:10]


# New function: _build_optimization_advice
def _build_optimization_advice() -> dict[str, Any]:
    strategy_rows = _build_strategy_weighting_status()

    boost: list[str] = []
    watch: list[str] = []
    pause: list[str] = []
    regime_notes: list[str] = []
    risk_suggestions: list[str] = []

    for row in strategy_rows:
        strategy = str(row.get("strategy") or "unknown")
        status = str(row.get("status") or "WATCH")
        expectancy = _safe_float(row.get("expectancy"), 0.0)
        tp1_rate = _safe_float(row.get("tp1_hit_rate"), 0.0)
        trades = int(row.get("trades") or 0)

        label = (
            f"{strategy} | exp={expectancy:.3f} | "
            f"tp1={tp1_rate:.2f} | trades={trades}"
        )

        if status == "BOOST":
            boost.append(label)
        elif status == "PAUSE":
            pause.append(label)
        else:
            watch.append(label)

    if boost:
        regime_notes.append("Momentum/trend continuation currently strongest")

    if pause:
        risk_suggestions.append("Reduce allocation to paused strategies")

    if not boost:
        risk_suggestions.append("No strategy currently qualifies for BOOST")

    level = "success" if boost else "warning"

    return {
        "mode": "LIVE_EXPECTANCY",
        "status": "OK",
        "symbols_to_pause": pause[:6],
        "strategies_to_boost": boost[:6],
        "strategies_to_watch": watch[:6],
        "regime_notes": regime_notes[:6],
        "risk_suggestions": risk_suggestions[:6],
        "level": level,
    }


def _build_strategy_control() -> list[dict[str, Any]]:
    return [
        {"name": "Liquidity Sweep", "direction": "LONG", "mode": "LIVE", "level": "success"},
        {"name": "Liquidity Sweep", "direction": "SHORT", "mode": "LIVE", "level": "success"},
        {"name": "Momentum Breakout", "direction": "LONG", "mode": "LIVE", "level": "success"},
        {"name": "Momentum Breakdown", "direction": "SHORT", "mode": "LIVE", "level": "success"},
    ]


def _build_live_risk_panel(wallet: dict[str, float], positions: list[dict[str, Any]]) -> dict[str, Any]:
    settings = get_settings()
    max_positions = int(getattr(settings, "max_open_positions", 0) or 0)
    risk_pct = float(getattr(settings, "account_risk_per_trade_pct", 0.0) or 0.0)
    leverage = float(getattr(settings, "default_leverage", 0.0) or 0.0)
    max_daily_loss_pct = float(getattr(settings, "max_daily_loss_pct", 0.0) or 0.0)
    hard_daily_stop_pct = float(getattr(settings, "hard_daily_stop_pct", 0.0) or 0.0)

    equity = float(wallet.get("equity") or wallet.get("balance") or 0.0)
    risk_budget = equity * (risk_pct / 100.0)
    open_positions = len(positions)
    exposure = sum(float(pos.get("notional") or 0.0) for pos in positions)
    exposure_pct = (exposure / equity * 100.0) if equity > 0 else 0.0

    state_positions = _read_json(STATE_PATH / "executed_trades.json") or []
    open_trade_risk = 0.0
    for row in state_positions if isinstance(state_positions, list) else []:
        if not isinstance(row, dict) or str(row.get("status") or "").upper() != "OPEN":
            continue
        entry = _safe_float(row.get("avg_entry") or row.get("entry"), 0.0)
        stop = _safe_float(row.get("stop_loss"), 0.0)
        size = _safe_float(row.get("size"), 0.0)
        if entry > 0 and stop > 0 and size > 0:
            open_trade_risk += abs(entry - stop) * size

    equity_curve = _build_equity_curve_panel(wallet)
    daily_pnl = _safe_float(equity_curve.get("daily_pnl"), 0.0)
    weekly_pnl = _safe_float(equity_curve.get("weekly_pnl"), 0.0)
    max_drawdown = _safe_float(equity_curve.get("max_drawdown"), 0.0)

    daily_loss_pct = abs(daily_pnl) / equity * 100.0 if equity > 0 and daily_pnl < 0 else 0.0
    weekly_loss_pct = abs(weekly_pnl) / equity * 100.0 if equity > 0 and weekly_pnl < 0 else 0.0
    open_risk_pct = open_trade_risk / equity * 100.0 if equity > 0 else 0.0

    risk_state = "SAFE"
    level = "success"
    alerts: list[str] = []

    if max_positions and open_positions >= max_positions:
        risk_state = "WATCH"
        level = "warning"
        alerts.append("max positions reached")

    if max_daily_loss_pct and daily_loss_pct >= max_daily_loss_pct:
        risk_state = "WATCH"
        level = "warning"
        alerts.append("daily soft loss limit reached")

    if hard_daily_stop_pct and daily_loss_pct >= hard_daily_stop_pct:
        risk_state = "DANGER"
        level = "danger"
        alerts.append("daily hard stop reached")

    if exposure_pct >= 250:
        risk_state = "WATCH" if risk_state != "DANGER" else risk_state
        level = "warning" if level != "danger" else level
        alerts.append("high exposure")

    if open_risk_pct > max(risk_pct * max(open_positions, 1) * 1.5, risk_pct + 0.5):
        risk_state = "WATCH" if risk_state != "DANGER" else risk_state
        level = "warning" if level != "danger" else level
        alerts.append("open trade risk elevated")

    return {
        "level": level,
        "risk_state": risk_state,
        "alerts": alerts,
        "open_positions": open_positions,
        "max_positions": max_positions,
        "risk_pct": round(risk_pct, 3),
        "risk_budget": round(risk_budget, 4),
        "leverage": leverage,
        "equity": round(equity, 4),
        "exposure": round(exposure, 4),
        "exposure_pct": round(exposure_pct, 3),
        "open_trade_risk": round(open_trade_risk, 4),
        "open_trade_risk_pct": round(open_risk_pct, 3),
        "daily_pnl": round(daily_pnl, 4),
        "weekly_pnl": round(weekly_pnl, 4),
        "daily_loss_pct": round(daily_loss_pct, 3),
        "weekly_loss_pct": round(weekly_loss_pct, 3),
        "max_drawdown": round(max_drawdown, 3),
        "max_daily_loss_pct": round(max_daily_loss_pct, 3),
        "hard_daily_stop_pct": round(hard_daily_stop_pct, 3),
        "compounding": "ON",
    }


def _classify_rejection_reason(line: str) -> str:
    text = line.lower()

    if "weak_entry_close" in text or "strong continuation close" in text:
        return "weak continuation close"
    if "volume" in text and ("low" in text or "ratio" in text or "expansion" in text):
        return "volume filter"
    if "bad alignment" in text or "aligned" in text or "conflicted" in text or "mixed" in text:
        return "alignment filter"
    if "cooldown" in text:
        return "symbol cooldown"
    if "risk" in text or "risk rejected" in text:
        return "risk gate"
    if "confirmation missing" in text:
        return "confirmation missing"
    if "no a+" in text or "no candidates" in text or "no_setup" in text:
        return "no A+ candidate"
    if "rr" in text or "reward" in text:
        return "risk/reward filter"
    if "notional" in text or "min live" in text:
        return "notional filter"
    if "score" in text or "verdict" in text:
        return "score gate"

    return "other"



def _build_equity_curve_panel(wallet: dict[str, float]) -> dict[str, Any]:
    journal = _read_json(STATE_PATH / "live_trade_journal.json") or []
    executed_trades = _read_json(STATE_PATH / "executed_trades.json") or []
    settings = get_settings()

    start_equity = float(getattr(settings, "account_equity_usdt", 0.0) or 0.0)
    live_equity = float(wallet.get("equity") or wallet.get("balance") or start_equity)

    closed_rows: list[dict[str, Any]] = []

    for row in journal if isinstance(journal, list) else []:
        if isinstance(row, dict) and str(row.get("status") or "").upper() == "CLOSED":
            closed_rows.append(row)

    for row in executed_trades if isinstance(executed_trades, list) else []:
        if isinstance(row, dict) and str(row.get("status") or "").upper() in {"CLOSED", "CLOSED_SYNCED"}:
            closed_rows.append(row)

    closed_rows.sort(key=lambda row: str(row.get("closed_at") or row.get("timestamp") or row.get("opened_at") or ""))

    cumulative = start_equity
    peak = start_equity
    max_drawdown = 0.0
    daily_pnl = 0.0
    weekly_pnl = 0.0
    now = datetime.now(timezone.utc)
    points: list[dict[str, Any]] = []

    for idx, row in enumerate(closed_rows, start=1):
        pnl_value = _safe_float(row.get("pnl"), 0.0)

        if pnl_value == 0.0:
            pnl_pct = _safe_float(row.get("realized_pnl_pct"), 0.0)
            notional = _safe_float(row.get("notional") or row.get("position_notional_usdt"), 0.0)
            pnl_value = notional * (pnl_pct / 100.0) if notional else 0.0

        cumulative += pnl_value
        peak = max(peak, cumulative)
        drawdown = ((peak - cumulative) / peak * 100.0) if peak > 0 else 0.0
        max_drawdown = max(max_drawdown, drawdown)

        raw_closed_at = row.get("closed_at") or row.get("timestamp") or row.get("opened_at")
        closed_at = None
        if raw_closed_at:
            try:
                closed_at = datetime.fromisoformat(str(raw_closed_at).replace("Z", "+00:00"))
                if closed_at.tzinfo is None:
                    closed_at = closed_at.replace(tzinfo=timezone.utc)
            except ValueError:
                closed_at = None

        if closed_at:
            age_seconds = (now - closed_at).total_seconds()
            if age_seconds <= 86400:
                daily_pnl += pnl_value
            if age_seconds <= 604800:
                weekly_pnl += pnl_value

        points.append({
            "n": idx,
            "symbol": row.get("symbol", "-"),
            "equity": round(cumulative, 4),
            "pnl": round(pnl_value, 4),
            "drawdown": round(drawdown, 3),
        })

    total_pnl = cumulative - start_equity
    growth_pct = (total_pnl / start_equity * 100.0) if start_equity > 0 else 0.0

    return {
        "start_equity": round(start_equity, 4),
        "live_equity": round(live_equity, 4),
        "closed_trade_equity": round(cumulative, 4),
        "total_pnl": round(total_pnl, 4),
        "growth_pct": round(growth_pct, 3),
        "daily_pnl": round(daily_pnl, 4),
        "weekly_pnl": round(weekly_pnl, 4),
        "max_drawdown": round(max_drawdown, 3),
        "closed_trades": len(closed_rows),
        "points": points[-20:],
        "level": "success" if total_pnl >= 0 else "danger",
    }


# --- MARKET SNAPSHOTS, PERIODIC PNL, DASHBOARD META ---

def _build_market_snapshots(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    snapshots: list[dict[str, Any]] = []

    focus_symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    seen_symbols = {
        str(position.get("symbol") or "")
        for position in positions
        if isinstance(position, dict)
    }

    for symbol in focus_symbols:
        matching_position = next(
            (
                position
                for position in positions
                if str(position.get("symbol") or "") == symbol
            ),
            None,
        )

        pnl_pct = _safe_float(
            matching_position.get("pnl_pct") if matching_position else 0.0,
            0.0,
        )

        direction = str(matching_position.get("direction") or "WATCH") if matching_position else "WATCH"

        if pnl_pct > 1:
            regime = "TRENDING"
            level = "success"
        elif pnl_pct < -1:
            regime = "VOLATILE"
            level = "danger"
        else:
            regime = "RANGING"
            level = "warning"

        snapshots.append({
            "symbol": symbol,
            "direction": direction,
            "pnl_pct": round(pnl_pct, 3),
            "regime": regime,
            "level": level,
            "active_position": symbol in seen_symbols,
        })

    return snapshots


# New blocks inserted as requested
def _build_market_sparklines(market_snapshots: list[dict[str, Any]], volatility_heatmap: list[dict[str, Any]]) -> list[dict[str, Any]]:
    heatmap_by_symbol = {
        str(row.get("symbol") or ""): row
        for row in volatility_heatmap
        if isinstance(row, dict)
    }

    rows: list[dict[str, Any]] = []
    for snapshot in market_snapshots:
        symbol = str(snapshot.get("symbol") or "UNKNOWN")
        heat = heatmap_by_symbol.get(symbol, {})
        base_score = _safe_float(heat.get("score_hint"), 50.0)
        volume_ratio = _safe_float(heat.get("volume_ratio"), 1.0)
        volatility_rank = _safe_float(heat.get("volatility_rank"), 20.0)
        pnl_pct = _safe_float(snapshot.get("pnl_pct"), 0.0)

        raw_points = [
            base_score * 0.72,
            base_score * 0.84,
            base_score + (volume_ratio * 4),
            base_score + volatility_rank * 0.18,
            base_score + pnl_pct * 3,
            base_score + volume_ratio * 6,
        ]
        points = [round(max(5.0, min(100.0, value)), 2) for value in raw_points]

        rows.append({
            "symbol": symbol,
            "points": points,
            "score_hint": round(base_score, 2),
            "volume_ratio": round(volume_ratio, 3),
            "volatility_rank": round(volatility_rank, 2),
            "level": snapshot.get("level", "warning"),
        })

    return rows


def _build_position_telemetry(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    for position in positions:
        entry = _safe_float(position.get("entry"), 0.0)
        price = _safe_float(position.get("price"), 0.0)
        pnl = _safe_float(position.get("pnl"), 0.0)
        pnl_pct = _safe_float(position.get("pnl_pct"), 0.0)
        notional = _safe_float(position.get("notional"), 0.0)
        leverage = _safe_float(position.get("leverage"), 0.0)
        sl_value = _safe_float(position.get("sl"), 0.0)
        direction = str(position.get("direction") or "").upper()

        distance_to_sl_pct = 0.0
        if price > 0 and sl_value > 0:
            distance_to_sl_pct = abs(price - sl_value) / price * 100.0

        live_rr = 0.0
        if entry > 0 and sl_value > 0:
            risk = abs(entry - sl_value)
            reward = abs(price - entry)
            live_rr = reward / risk if risk > 0 else 0.0

        protection_level = "success"
        if position.get("sl") == "MISSING" or position.get("tp") == "MISSING":
            protection_level = "danger"
        elif not bool(position.get("protection_verified")):
            protection_level = "warning"

        rows.append({
            "symbol": position.get("symbol", "UNKNOWN"),
            "direction": direction or "UNKNOWN",
            "entry": entry,
            "price": price,
            "pnl": round(pnl, 4),
            "pnl_pct": round(pnl_pct, 3),
            "notional": round(notional, 4),
            "leverage": leverage,
            "distance_to_sl_pct": round(distance_to_sl_pct, 3),
            "live_rr": round(live_rr, 3),
            "break_even_active": bool(position.get("break_even_active")),
            "tp1_hit": bool(position.get("tp1_hit")),
            "tp2_hit": bool(position.get("tp2_hit")),
            "tp3_hit": bool(position.get("tp3_hit")),
            "protection_level": protection_level,
            "level": "success" if pnl >= 0 and protection_level != "danger" else protection_level,
        })

    return rows


def _build_execution_timeline() -> list[dict[str, Any]]:
    rows = _read_strategy_performance_rows(limit=120)
    log_lines = _read_lines(LOGS_PATH / "bot.out", limit=120)
    timeline: list[dict[str, Any]] = []

    for row in reversed(rows[-30:]):
        event_type = str(row.get("event_type") or "EVENT")
        stage = str(row.get("stage") or row.get("status") or "")
        symbol = str(row.get("symbol") or "-")
        strategy = str(row.get("strategy") or "-")
        verdict = str(row.get("verdict") or row.get("process_verdict") or "")
        net_pnl = _safe_float(row.get("net_pnl"), 0.0)

        level = "neutral"
        if "REJECT" in stage or "FAIL" in verdict:
            level = "warning"
        if event_type == "TRADE_CLOSE" and net_pnl < 0:
            level = "danger"
        if event_type == "TRADE_CLOSE" and net_pnl > 0:
            level = "success"
        if verdict == "EXECUTABLE":
            level = "success"

        timeline.append({
            "timestamp": row.get("timestamp") or row.get("closed_at") or "",
            "event_type": event_type,
            "stage": stage,
            "symbol": symbol,
            "strategy": strategy,
            "verdict": verdict,
            "net_pnl": round(net_pnl, 4),
            "level": level,
            "summary": f"{symbol} · {event_type} · {stage or verdict or strategy}",
        })

    for line in reversed(log_lines[-20:]):
        upper = line.upper()
        if not any(marker in upper for marker in ("EXECUTED", "REJECTED", "TP1", "TP2", "TP3", "BREAK_EVEN", "FAIL", "ERROR")):
            continue
        level = "neutral"
        if "ERROR" in upper or "FAIL" in upper:
            level = "danger"
        elif "TP" in upper or "BREAK_EVEN" in upper or "EXECUTED" in upper:
            level = "success"
        elif "REJECTED" in upper:
            level = "warning"
        timeline.append({
            "timestamp": "log",
            "event_type": "LOG",
            "stage": "RUNTIME",
            "symbol": "-",
            "strategy": "-",
            "verdict": "-",
            "net_pnl": 0.0,
            "level": level,
            "summary": line[-220:],
        })

    return timeline[:30]


def _build_strategy_heatmap(expectancy_matrix: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    strategy_regime = expectancy_matrix.get("strategy_regime", []) if isinstance(expectancy_matrix, dict) else []

    for row in strategy_regime:
        if not isinstance(row, dict):
            continue
        rows.append({
            "x": row.get("strategy", "UNKNOWN"),
            "y": row.get("regime", "UNKNOWN"),
            "value": _safe_float(row.get("expectancy"), 0.0),
            "status": row.get("status", "WATCH"),
            "level": row.get("level", "warning"),
        })

    return rows[:24]


def _build_periodic_pnl(equity_curve: dict[str, Any]) -> dict[str, Any]:
    total_pnl = _safe_float(equity_curve.get("total_pnl"), 0.0)
    daily_pnl = _safe_float(equity_curve.get("daily_pnl"), 0.0)
    weekly_pnl = _safe_float(equity_curve.get("weekly_pnl"), 0.0)

    monthly_estimate = weekly_pnl * 4

    return {
        "daily": round(daily_pnl, 4),
        "weekly": round(weekly_pnl, 4),
        "monthly": round(monthly_estimate, 4),
        "total": round(total_pnl, 4),
        "level": "success" if total_pnl >= 0 else "danger",
    }


def _build_dashboard_meta() -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "refresh_interval_seconds": CACHE_SECONDS,
        "dashboard_version": "V4",
        "mode": "REALTIME",
    }


# V4 dashboard model builder
def _build_dashboard_v4_model(
    *,
    wallet: dict[str, float],
    positions: list[dict[str, Any]],
    stats: dict[str, Any],
    protection_summary: dict[str, Any],
    protection_alerts: list[dict[str, str]],
    position_protection: list[dict[str, Any]],
    candidate_board: list[dict[str, Any]],
    volatility_heatmap: list[dict[str, Any]],
    rejection_analytics: dict[str, Any],
    strategy_performance_panel: dict[str, Any],
    no_trade_intelligence: dict[str, Any],
    execution_leakage: dict[str, Any],
    equity_curve: dict[str, Any],
    periodic_pnl: dict[str, Any],
    market_snapshots: list[dict[str, Any]],
    market_sparklines: list[dict[str, Any]],
    position_telemetry: list[dict[str, Any]],
    execution_timeline: list[dict[str, Any]],
    strategy_heatmap: list[dict[str, Any]],
    bot_health: dict[str, Any],
    strategy_expectancy: list[dict[str, Any]],
    strategy_weighting_status: list[dict[str, Any]],
    optimization_advice: dict[str, Any],
    symbol_expectancy: list[dict[str, Any]],
    expectancy_matrix: dict[str, Any],
    live_risk: dict[str, Any],
    edge_panel: dict[str, Any],
    cooldown_panel: list[dict[str, Any]],
    live_source: dict[str, Any],
) -> dict[str, Any]:
    open_positions = len(positions)
    danger_alerts = [alert for alert in protection_alerts if alert.get("level") == "danger"]
    warning_alerts = [alert for alert in protection_alerts if alert.get("level") == "warning"]

    top_candidate = candidate_board[0] if candidate_board else None
    top_strategy = strategy_performance_panel.get("strategies", [None])[0] if strategy_performance_panel.get("strategies") else None
    top_symbol = symbol_expectancy[0] if symbol_expectancy else None

    executive_status = "SAFE"
    executive_level = "success"

    if live_risk.get("level") == "danger" or danger_alerts:
        executive_status = "DANGER"
        executive_level = "danger"
    elif live_risk.get("level") == "warning" or warning_alerts:
        executive_status = "WATCH"
        executive_level = "warning"

    trade_lifecycle = edge_panel.get("tp_protection_state", {}) if isinstance(edge_panel, dict) else {}

    return {
        "version": "V4",
        "executive": {
            "status": executive_status,
            "level": executive_level,
            "wallet_equity": wallet.get("equity", 0.0),
            "available": wallet.get("available", 0.0),
            "used_margin": wallet.get("used_margin", 0.0),
            "open_positions": open_positions,
            "daily_pnl": periodic_pnl.get("daily", 0.0),
            "weekly_pnl": periodic_pnl.get("weekly", 0.0),
            "monthly_pnl": periodic_pnl.get("monthly", 0.0),
            "total_pnl": periodic_pnl.get("total", 0.0),
            "risk_state": live_risk.get("risk_state", "UNKNOWN"),
            "risk_level": live_risk.get("level", "warning"),
            "bot_level": bot_health.get("level", "warning"),
            "data_source": live_source.get("source", "UNKNOWN"),
            "data_source_level": live_source.get("level", "warning"),
            "top_candidate": top_candidate,
            "top_strategy": top_strategy,
            "top_symbol": top_symbol,
        },
        "positions": {
            "open": positions,
            "telemetry": position_telemetry,
            "protection": position_protection,
            "summary": protection_summary,
            "lifecycle": trade_lifecycle,
            "alerts": protection_alerts[:10],
        },
        "markets": {
            "snapshots": market_snapshots,
            "sparklines": market_sparklines,
            "candidate_board": candidate_board,
            "volatility_heatmap": volatility_heatmap,
            "cooldowns": cooldown_panel,
        },
        "performance": {
            "periodic_pnl": periodic_pnl,
            "equity_curve": equity_curve,
            "stats": stats,
            "edge": edge_panel,
            "execution_leakage": execution_leakage,
        },
        "strategy": {
            "performance": strategy_performance_panel,
            "expectancy": strategy_expectancy,
            "weighting": strategy_weighting_status,
            "symbols": symbol_expectancy,
            "matrix": expectancy_matrix,
            "heatmap": strategy_heatmap,
            "optimization_advice": optimization_advice,
        },
        "risk": {
            "live": live_risk,
            "protection_summary": protection_summary,
            "protection_alerts": protection_alerts[:10],
            "position_protection": position_protection,
        },
        "intelligence": {
            "rejections": rejection_analytics,
            "no_trade": no_trade_intelligence,
            "volatility_heatmap": volatility_heatmap,
            "candidate_board": candidate_board,
            "bot_health": bot_health,
        },
        "system": {
            "bot_health": bot_health,
            "live_source": live_source,
            "execution_leakage": execution_leakage,
            "execution_timeline": execution_timeline,
            "recent_errors": bot_health.get("recent_errors", []),
            "live_errors": bot_health.get("live_error_count", 0),
        },
    }

def _build_strategy_isolation_status() -> dict[str, Any]:
    import os
    return {
        "enabled": os.getenv("STRATEGY_ISOLATION_ENABLED", "false"),
        "enabled_strategies": os.getenv("ENABLED_STRATEGIES", ""),
        "disabled_strategies": os.getenv("DISABLED_STRATEGIES", ""),
    }

def _build_rejection_analytics() -> dict[str, Any]:
    lines = _read_lines(LOGS_PATH / "bot.out", limit=1500)
    reason_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    recent: list[dict[str, Any]] = []
    grouped_recent: dict[tuple[str, str], dict[str, Any]] = {}

    for line in reversed(lines):
        upper = line.upper()
        if "NO_SETUP" not in upper and "REJECTED_SETUP" not in upper and "SKIPPED" not in upper:
            continue

        reason = _classify_rejection_reason(line)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

        parts = [part.strip() for part in line.split("|")]
        symbol = "UNKNOWN"

        if "NO_SETUP" in parts:
            idx = parts.index("NO_SETUP")
            if len(parts) > idx + 1:
                symbol = parts[idx + 1].strip()
        elif "REJECTED_SETUP" in parts:
            idx = parts.index("REJECTED_SETUP")
            if len(parts) > idx + 1:
                symbol = parts[idx + 1].strip()
        elif "SKIPPED" in parts:
            idx = parts.index("SKIPPED")
            if len(parts) > idx + 1:
                symbol = parts[idx + 1].strip()
        elif len(parts) >= 5:
            symbol = parts[4].strip()

        if symbol in {"NO_SETUP", "REJECTED_SETUP", "SKIPPED", "UNKNOWN"}:
            symbol = "UNKNOWN"

        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1

        group_key = (symbol, reason)

        if group_key not in grouped_recent:
            grouped_recent[group_key] = {
                "symbol": symbol,
                "parsed": symbol != "UNKNOWN",
                "reason": reason,
                "count": 0,
                "last_line": line,
                "level": "warning" if "REJECTED" in upper or "SKIPPED" in upper else "neutral",
            }

        grouped_recent[group_key]["count"] += 1
        grouped_recent[group_key]["last_line"] = line

    top_reasons = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:8]
    top_symbols = sorted(symbol_counts.items(), key=lambda item: item[1], reverse=True)[:8]

    grouped_recent_rows = sorted(
        grouped_recent.values(),
        key=lambda row: row["count"],
        reverse=True,
    )[:12]

    return {
        "total": sum(reason_counts.values()),
        "top_reasons": [
            {"reason": reason, "count": count, "level": "warning" if count else "neutral"}
            for reason, count in top_reasons
        ],
        "top_symbols": [
            {"symbol": symbol, "count": count}
            for symbol, count in top_symbols
        ],
        "recent": grouped_recent_rows,
    }



def _build_strategy_performance_panel() -> dict[str, Any]:
    rows = _read_strategy_performance_rows(limit=2000)
    by_strategy: dict[str, dict[str, Any]] = {}

    for row in rows:
        strategy = str(row.get("strategy") or "UNKNOWN")
        event_type = str(row.get("event_type") or "")
        stage = str(row.get("stage") or "")
        verdict = str(row.get("verdict") or "")

        bucket = by_strategy.setdefault(strategy, {
            "strategy": strategy,
            "setup_events": 0,
            "scan_rejects": 0,
            "plan_rejects": 0,
            "executables": 0,
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "net_pnl": 0.0,
            "fees": 0.0,
            "score_sum": 0.0,
            "score_count": 0,
            "tp1_hits": 0,
            "bad_losses": 0,
            "good_losses": 0,
            "high_edge_wins": 0,
        })

        if event_type == "SETUP_EVENT":
            bucket["setup_events"] += 1
            if stage == "SCAN_REJECT":
                bucket["scan_rejects"] += 1
            if stage == "PLAN_REJECT":
                bucket["plan_rejects"] += 1
            if verdict == "EXECUTABLE":
                bucket["executables"] += 1

            score = _safe_float(row.get("score"), 0.0)
            if score:
                bucket["score_sum"] += score
                bucket["score_count"] += 1

        if event_type == "TRADE_CLOSE":
            bucket["closed_trades"] += 1
            net_pnl = _safe_float(row.get("net_pnl"), 0.0)
            fees = _safe_float(row.get("fees"), 0.0)
            bucket["net_pnl"] += net_pnl
            bucket["fees"] += fees

            if net_pnl > 0:
                bucket["wins"] += 1
            elif net_pnl < 0:
                bucket["losses"] += 1

            if str(row.get("tp1_hit") or "").lower() == "true":
                bucket["tp1_hits"] += 1

            process = str(row.get("process_verdict") or "")
            label = str(row.get("expectancy_label") or "")
            if process == "BAD_LOSS" or label == "LOW_EDGE_FAILURE":
                bucket["bad_losses"] += 1
            if process == "GOOD_LOSS" or label == "GOOD_PROTECTION_LOSS":
                bucket["good_losses"] += 1
            if process == "WINNER" or label == "HIGH_EDGE_WIN":
                bucket["high_edge_wins"] += 1

    strategy_rows: list[dict[str, Any]] = []
    for bucket in by_strategy.values():
        closed = int(bucket["closed_trades"])
        setup_events = int(bucket["setup_events"])
        wins = int(bucket["wins"])
        net_pnl = float(bucket["net_pnl"])
        avg_score = bucket["score_sum"] / bucket["score_count"] if bucket["score_count"] else 0.0
        winrate = wins / closed if closed else 0.0
        expectancy = net_pnl / closed if closed else 0.0
        executable_rate = bucket["executables"] / setup_events if setup_events else 0.0
        tp1_rate = bucket["tp1_hits"] / closed if closed else 0.0

        if closed >= 5 and expectancy > 0:
            status = "GOOD"
            level = "success"
        elif closed >= 5 and expectancy < 0:
            status = "PAUSE"
            level = "danger"
        elif setup_events >= 20 and executable_rate <= 0.01:
            status = "TOO_STRICT"
            level = "warning"
        else:
            status = "WATCH"
            level = "warning"

        strategy_rows.append({
            "strategy": bucket["strategy"],
            "setup_events": setup_events,
            "scan_rejects": bucket["scan_rejects"],
            "plan_rejects": bucket["plan_rejects"],
            "executables": bucket["executables"],
            "executable_rate": round(executable_rate, 3),
            "closed_trades": closed,
            "wins": wins,
            "losses": bucket["losses"],
            "winrate": round(winrate, 3),
            "net_pnl": round(net_pnl, 4),
            "fees": round(float(bucket["fees"]), 4),
            "expectancy": round(expectancy, 4),
            "avg_score": round(avg_score, 2),
            "tp1_rate": round(tp1_rate, 3),
            "bad_losses": bucket["bad_losses"],
            "good_losses": bucket["good_losses"],
            "high_edge_wins": bucket["high_edge_wins"],
            "status": status,
            "level": level,
        })

    strategy_rows.sort(
        key=lambda row: (row["level"] == "success", row["expectancy"], row["executables"]),
        reverse=True,
    )

    return {
        "total_events": len(rows),
        "strategies": strategy_rows[:12],
        "recent_events": list(reversed(rows[-20:])),
        "level": "success" if any(row["level"] == "success" for row in strategy_rows) else "warning",
    }


def _build_no_trade_intelligence_panel() -> dict[str, Any]:
    rows = _read_strategy_performance_rows(limit=2500)
    reject_rows = [row for row in rows if str(row.get("stage") or "") in {"SCAN_REJECT", "PLAN_REJECT"}]

    reason_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}

    for row in reject_rows:
        reasons_text = str(row.get("reasons") or "")
        symbol = str(row.get("symbol") or "UNKNOWN")
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1

        for part in reasons_text.split("|"):
            reason = part.strip()
            if not reason:
                continue
            key = reason.split("=", 1)[0].strip()
            reason_counts[key] = reason_counts.get(key, 0) + 1

    top_reasons = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:10]
    top_symbols = sorted(symbol_counts.items(), key=lambda item: item[1], reverse=True)[:10]

    return {
        "total_rejects": len(reject_rows),
        "top_reasons": [{"reason": reason, "count": count} for reason, count in top_reasons],
        "top_symbols": [{"symbol": symbol, "count": count} for symbol, count in top_symbols],
        "recent": list(reversed(reject_rows[-15:])),
        "level": "warning" if reject_rows else "neutral",
    }


def _build_execution_leakage_panel() -> dict[str, Any]:
    rows = _read_strategy_performance_rows(limit=2000)
    close_rows = [row for row in rows if str(row.get("event_type") or "") == "TRADE_CLOSE"]

    total_fees = sum(_safe_float(row.get("fees"), 0.0) for row in close_rows)
    total_net = sum(_safe_float(row.get("net_pnl"), 0.0) for row in close_rows)
    avg_slippage = sum(_safe_float(row.get("slippage_pct"), 0.0) for row in close_rows) / len(close_rows) if close_rows else 0.0
    avg_fee_leakage = sum(_safe_float(row.get("fee_leakage_pct"), 0.0) for row in close_rows) / len(close_rows) if close_rows else 0.0

    level = "success"
    if avg_slippage >= 0.15 or avg_fee_leakage >= 0.12 or total_net < 0:
        level = "danger"
    elif avg_slippage >= 0.05 or avg_fee_leakage >= 0.06:
        level = "warning"

    return {
        "closed_trades": len(close_rows),
        "total_fees": round(total_fees, 4),
        "total_net_pnl": round(total_net, 4),
        "avg_slippage_pct": round(avg_slippage, 5),
        "avg_fee_leakage_pct": round(avg_fee_leakage, 5),
        "level": level,
        "recent": list(reversed(close_rows[-10:])),
    }


def _count_log_events(lines: list[str]) -> dict[str, int]:
    counters = {
        "no_setup": 0,
        "accepted": 0,
        "rejected": 0,
        "executed": 0,
        "skipped": 0,
        "fail_safe": 0,
        "tp1": 0,
        "tp2": 0,
        "tp3": 0,
        "be": 0,
        "closed_synced": 0,
    }

    for line in lines:
        upper = line.upper()
        lower = line.lower()

        if "NO_SETUP" in upper:
            counters["no_setup"] += 1
        if "ACCEPTED_SETUP" in upper:
            counters["accepted"] += 1
        if "REJECTED_SETUP" in upper:
            counters["rejected"] += 1
        if " EXECUTED" in upper or "| EXECUTED" in upper or "STATUS=EXECUTED" in upper:
            counters["executed"] += 1
        if " SKIPPED" in upper or "| SKIPPED" in upper or "STATUS=SKIPPED" in upper:
            counters["skipped"] += 1
        if "FAIL-SAFE" in upper or "FAIL_SAFE" in upper or "FAILSAFE" in upper:
            counters["fail_safe"] += 1
        if "TP1" in upper:
            counters["tp1"] += 1
        if "TP2" in upper:
            counters["tp2"] += 1
        if "TP3" in upper:
            counters["tp3"] += 1
        if "BREAK_EVEN" in upper or "FEE-ADJUSTED BE" in upper or "MOVED TO BE" in upper or " SL MOVED TO BE" in upper or "be active" in lower:
            counters["be"] += 1
        if "CLOSED_SYNCED" in upper:
            counters["closed_synced"] += 1

    return counters

def _build_edge_panel() -> dict[str, Any]:
    journal = _read_json(STATE_PATH / "live_trade_journal.json") or []
    events = _read_json(STATE_PATH / "execution_events.json") or []
    position_events = _read_json(STATE_PATH / "position_events.json") or []
    executed_trades = _read_json(STATE_PATH / "executed_trades.json") or []
    log_lines = _read_lines(LOGS_PATH / "bot.out", limit=1200)
    log_counts = _count_log_events(log_lines)

    closed = [
        t for t in journal
        if isinstance(t, dict)
        and str(t.get("status") or "").upper() == "CLOSED"
    ]

    open_trades = [
        t for t in executed_trades
        if isinstance(t, dict)
        and str(t.get("status") or "").upper() == "OPEN"
    ]

    unique_open_symbols: set[str] = set()
    cleaned_open_trades: list[dict[str, Any]] = []

    for trade in open_trades:
        symbol = str(trade.get("symbol") or "UNKNOWN")
        if symbol in unique_open_symbols:
            continue
        unique_open_symbols.add(symbol)
        cleaned_open_trades.append(trade)

    open_trades = cleaned_open_trades

    closed_synced_trades = [
        t for t in executed_trades
        if isinstance(t, dict) and str(t.get("status") or "").upper() in {"CLOSED", "CLOSED_SYNCED"}
    ]

    wins = [t for t in closed if _safe_float(t.get("pnl")) > 0]
    losses = [t for t in closed if _safe_float(t.get("pnl")) < 0]
    pnl = sum(_safe_float(t.get("pnl")) for t in closed)
    expectancy = pnl / len(closed) if closed else 0.0

    executed_events = [e for e in events if isinstance(e, dict) and str(e.get("status") or "").upper() == "EXECUTED"]
    skipped_events = [e for e in events if isinstance(e, dict) and str(e.get("status") or "").upper() == "SKIPPED"]
    fail_safe_events = [
        e for e in events
        if isinstance(e, dict)
        and (
            "fail-safe" in " ".join(str(v).lower() for v in e.values())
            or "fail_safe" in " ".join(str(v).lower() for v in e.values())
        )
    ]

    tp1_events = log_counts["tp1"]
    tp2_events = log_counts["tp2"]
    tp3_events = log_counts["tp3"]
    be_events = log_counts["be"]

    for event in position_events if isinstance(position_events, list) else []:
        if not isinstance(event, dict):
            continue
        note = str(event.get("note") or event.get("message") or "").lower()
        if "tp1" in note:
            tp1_events += 1
        if "tp2" in note:
            tp2_events += 1
        if "tp3" in note:
            tp3_events += 1
        if "be" in note or "break_even" in note or "fee-adjusted" in note:
            be_events += 1

    return {
        "closed_trades": max(len(closed), len(closed_synced_trades), log_counts["closed_synced"]),
        "open_trades": len(unique_open_symbols),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(len(wins) / len(closed), 3) if closed else 0.0,
        "pnl": round(pnl, 4),
        "expectancy": round(expectancy, 4),
        "executed_events": max(len(executed_events), log_counts["executed"]),
        "skipped_events": max(len(skipped_events), log_counts["skipped"]),
        "accepted_events": log_counts["accepted"],
        "rejected_events": log_counts["rejected"],
        "no_setup_events": log_counts["no_setup"],
        "fail_safe_events": max(len(fail_safe_events), log_counts["fail_safe"]),
        "closed_synced_events": log_counts["closed_synced"],
        "tp1_events": tp1_events,
        "tp2_events": tp2_events,
        "tp3_events": tp3_events,
        "be_events": be_events,
        "tp_protection_state": {
            "tp1_hits": tp1_events,
            "tp2_hits": tp2_events,
            "tp3_hits": tp3_events,
            "be_moves": be_events,
            "open_positions": len(unique_open_symbols),
            "closed_positions": max(len(closed), len(closed_synced_trades), log_counts["closed_synced"]),
        },
    }


def _build_cooldown_panel() -> list[dict[str, Any]]:
    settings = get_settings()
    cooldown_minutes = int(getattr(settings, "symbol_cooldown_minutes", 30) or 30)
    events = _read_json(STATE_PATH / "execution_events.json") or []
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for event in reversed(events if isinstance(events, list) else []):
        if not isinstance(event, dict):
            continue
        if str(event.get("status") or "").upper() != "EXECUTED":
            continue

        symbol = str(event.get("symbol") or "")
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)

        raw_ts = event.get("timestamp")
        if not raw_ts:
            continue

        try:
            ts = datetime.fromisoformat(str(raw_ts).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        age_minutes = (now - ts).total_seconds() / 60.0
        remaining = max(0, cooldown_minutes - int(age_minutes))
        if remaining <= 0:
            continue

        rows.append({"symbol": symbol, "remaining_minutes": remaining, "level": "warning"})

        if len(rows) >= 6:
            break

    return rows


def _build_live_source_panel(wallet: dict[str, float], positions: list[dict[str, Any]], live_errors: list[str]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "source": "BITGET_API" if not live_errors else "LOCAL_FALLBACK",
        "level": "success" if not live_errors else "warning",
        "timestamp_utc": now.isoformat(timespec="seconds"),
        "wallet_live": bool(wallet.get("equity") or wallet.get("balance")),
        "positions_live": isinstance(positions, list),
        "error_count": len(live_errors),
    }

def _read_lines(path: Path, limit: int = 50) -> list[str]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [line.strip() for line in lines[-limit:] if line.strip()]
    except Exception:
        return []


def _read_csv_rows(path: Path, limit: int = 500) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        return rows[-limit:]
    except Exception:
        return []

def _read_strategy_performance_rows(limit: int = 1000) -> list[dict[str, Any]]:
    return _read_csv_rows(LOGS_PATH / "strategy_performance.csv", limit=limit)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _first_float(source: dict[str, Any], keys: list[str], default: float = 0.0) -> float:
    for key in keys:
        if key in source:
            value = _safe_float(source.get(key), default)
            if value != 0.0:
                return value
    return default


def _as_rows(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        for key in ("list", "orderList", "entrustedList", "resultList", "data", "rows"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [row for row in nested if isinstance(row, dict)]
        return []
    return []


def _position_size(row: dict[str, Any]) -> float:
    return _first_float(row, ["total", "size", "available", "holdVol"], 0.0)


def _position_direction(row: dict[str, Any]) -> str:
    hold_side = str(row.get("holdSide") or row.get("posSide") or "").lower()
    if hold_side == "long":
        return "LONG"
    if hold_side == "short":
        return "SHORT"
    return str(row.get("direction") or "-").upper()


def _pnl_pct(direction: str, entry: float, price: float) -> float:
    if entry <= 0 or price <= 0:
        return 0.0
    if direction == "LONG":
        return (price - entry) / entry * 100
    if direction == "SHORT":
        return (entry - price) / entry * 100
    return 0.0


def _normalize_wallet(account: dict[str, Any], positions: list[dict[str, Any]]) -> dict[str, float]:
    equity = _first_float(account, ["usdtEquity", "equity", "accountEquity", "marginEquity", "totalEquity"], 0.0)
    available = _first_float(account, ["available", "availableBalance", "maxOpenAvailable", "crossedMaxAvailable"], 0.0)
    balance = _first_float(account, ["usdtEquity", "equity", "accountEquity", "available", "availableBalance"], equity)

    used_margin = 0.0
    for position in positions:
        notional = _safe_float(position.get("notional"))
        leverage = _safe_float(position.get("leverage"), 1.0) or 1.0
        used_margin += notional / leverage if notional > 0 else 0.0

    return {
        "balance": round(balance, 4),
        "equity": round(equity, 4),
        "available": round(available, 4),
        "used_margin": round(used_margin, 4),
    }


def _normalize_tpsl_orders(raw_tpsl: Any) -> list[dict[str, Any]]:
    orders = []
    for row in _as_rows(raw_tpsl):
        orders.append(
            {
                "symbol": str(row.get("symbol") or ""),
                "plan_type": str(row.get("planType") or ""),
                "hold_side": str(row.get("holdSide") or ""),
                "trigger_price": _safe_float(row.get("triggerPrice") or row.get("executePrice")),
                "size": _safe_float(row.get("size")),
                "status": str(row.get("status") or row.get("state") or "OPEN"),
            }
        )
    return orders


def _normalize_positions(raw_positions: Any, tpsl_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state_positions = _read_json(STATE_PATH / "executed_trades.json") or []

    state_by_symbol = {
        str(p.get("symbol") or ""): p
        for p in state_positions
        if isinstance(p, dict)
    }

    positions = []
    for row in _as_rows(raw_positions):
        size = _position_size(row)
        if size <= 0:
            continue

        symbol = str(row.get("symbol") or "")
        state = state_by_symbol.get(symbol, {})

        direction = _position_direction(row)
        entry = _first_float(row, ["openPriceAvg", "averageOpenPrice", "avgEntryPrice", "entryPrice"], 0.0)
        price = _first_float(row, ["markPrice", "lastPrice"], entry)
        leverage = _first_float(row, ["leverage"], 1.0) or 1.0
        notional = _first_float(row, ["positionValue", "notional", "usdtValue"], 0.0)
        if notional <= 0 and price > 0:
            notional = price * size

        pnl = _first_float(row, ["unrealizedPL", "unrealizedPnl", "upl"], 0.0)
        pnl_pct = _pnl_pct(direction, entry, price)

        matching = [order for order in tpsl_orders if order.get("symbol") == symbol]
        stop_orders = [order for order in matching if order.get("plan_type") == "loss_plan"]
        tp_orders = [order for order in matching if order.get("plan_type") == "profit_plan"]

        sl = stop_orders[0]["trigger_price"] if stop_orders else "MISSING"
        tp_values = [order["trigger_price"] for order in tp_orders if _safe_float(order.get("trigger_price")) > 0]
        tp = ", ".join(str(value) for value in tp_values) if tp_values else "MISSING"

        positions.append(
            {
                "symbol": symbol,
                "direction": direction,
                "entry": round(entry, 8),
                "price": round(price, 8),
                "pnl": round(pnl, 4),
                "pnl_pct": round(pnl_pct, 3),
                "size": size,
                "notional": round(notional, 4),
                "leverage": leverage,
                "sl": sl,
                "tp": tp,
                "break_even_active": bool(state.get("break_even_active")),
                "tp1_hit": bool(state.get("tp1_hit")),
                "tp2_hit": bool(state.get("tp2_hit")),
                "tp3_hit": bool(state.get("tp3_hit")),
                "protection_verified": bool(state.get("protection_verified")),
            }
        )
    return positions


def _fallback_positions() -> list[dict[str, Any]]:
    state_positions = _read_json(STATE_PATH / "positions.json") or []
    positions = []
    for p in state_positions:
        positions.append(
            {
                "symbol": p.get("symbol"),
                "direction": p.get("direction"),
                "entry": p.get("entry"),
                "price": p.get("price") or p.get("entry"),
                "pnl": p.get("pnl", 0),
                "pnl_pct": p.get("pnl_pct", 0),
                "size": p.get("size"),
                "notional": p.get("notional", 0),
                "leverage": p.get("leverage", "-"),
                "sl": p.get("stop_loss", "MISSING"),
                "tp": p.get("take_profit", "MISSING"),
            }
        )
    return positions


def _execution_status(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("result") or "").upper()


def _execution_strategy(row: dict[str, Any]) -> str:
    return str(row.get("strategy") or row.get("setup") or row.get("strategy_name") or "").lower()


def _build_performance_stats() -> dict[str, Any]:
    execution_rows = _read_csv_rows(LOGS_PATH / "executions.csv", limit=1000)
    execution_events = _read_json(STATE_PATH / "execution_events.json") or []
    position_events = _read_json(STATE_PATH / "position_events.json") or []

    executed = [row for row in execution_rows if _execution_status(row) == "EXECUTED"]
    skipped = [row for row in execution_rows if _execution_status(row) == "SKIPPED"]
    failed = [row for row in execution_rows if _execution_status(row) in {"ERROR", "FAILED", "FAIL_SAFE", "FAIL-SAFE"}]

    alpha_rows = [row for row in execution_rows if "sweep" in _execution_strategy(row)]
    continuation_rows = [row for row in execution_rows if "continuation" in _execution_strategy(row)]
    blocked_continuation = [
        row
        for row in execution_rows
        if "continuation" in _execution_strategy(row) and _execution_status(row) == "SKIPPED"
    ]

    fail_safe_count = 0
    for row in execution_rows:
        text = " ".join(str(value) for value in row.values()).lower()
        if "fail-safe" in text or "fail_safe" in text:
            fail_safe_count += 1
    for event in execution_events:
        if isinstance(event, dict):
            text = " ".join(str(value) for value in event.values()).lower()
            if "fail-safe" in text or "fail_safe" in text:
                fail_safe_count += 1

    closed_events = [
        event for event in position_events
        if isinstance(event, dict) and str(event.get("status") or "").upper() == "CLOSED"
    ]

    latest_rows = execution_rows[-10:]

    return {
        "total_events": len(execution_rows),
        "executed_trades": len(executed),
        "skipped_trades": len(skipped),
        "failed_trades": len(failed),
        "alpha_events": len(alpha_rows),
        "continuation_events": len(continuation_rows),
        "blocked_continuation": len(blocked_continuation),
        "fail_safe_events": fail_safe_count,
        "log_counters": _count_log_events(_read_lines(LOGS_PATH / "bot.out", limit=1200)),
        "closed_position_events": len(closed_events),
        "latest_executions": list(reversed(latest_rows)),
    }


def _live_bitget_data() -> tuple[dict[str, float], list[dict[str, Any]], list[str]]:
    errors: list[str] = []

    try:
        settings = get_settings()
        client = BitgetRestClient(settings=settings)

        account_payload = client.get_accounts(product_type=settings.bitget_product_type)
        positions_payload = client.get_all_positions(product_type=settings.bitget_product_type)

        # TP/SL can fail without making wallet/positions useless.
        try:
            tpsl_payload = client.get_tpsl_orders(product_type=settings.bitget_product_type)
        except Exception as exc:
            errors.append(f"TP/SL API warning: {exc}")
            tpsl_payload = {"data": []}

        account_rows = _as_rows(account_payload.get("data") if isinstance(account_payload, dict) else account_payload)
        account = account_rows[0] if account_rows else {}

        raw_tpsl = tpsl_payload.get("data") if isinstance(tpsl_payload, dict) else tpsl_payload
        raw_positions = positions_payload.get("data") if isinstance(positions_payload, dict) else positions_payload

        tpsl_orders = _normalize_tpsl_orders(raw_tpsl)
        positions = _normalize_positions(raw_positions, tpsl_orders)
        wallet = _normalize_wallet(account, positions)
        return wallet, positions, errors

    except Exception as exc:
        errors.append(f"Live Bitget API failed: {exc}")
        return {
            "balance": 0.0,
            "equity": 0.0,
            "available": 0.0,
            "used_margin": 0.0,
        }, _fallback_positions(), errors


def get_dashboard_data() -> dict[str, Any]:
    """Dashboard 2.0 data provider. Bitget live first, local files as fallback."""

    global _DASHBOARD_CACHE

    now = time.time()
    if (
        _DASHBOARD_CACHE["data"] is not None
        and now - float(_DASHBOARD_CACHE["timestamp"]) < CACHE_SECONDS
    ):
        return _DASHBOARD_CACHE["data"]

    wallet, positions, live_errors = _live_bitget_data()

    bot_status = "UNKNOWN"
    last_log_lines = _read_lines(LOGS_PATH / "bot.out", limit=50)

    if not last_log_lines:
        bot_status = "STOPPED"
    else:
        last_line = last_log_lines[-1].lower()
        if "error" in last_line or "fail" in last_line:
            bot_status = "ERROR"
        else:
            bot_status = "RUNNING"

    executions = _read_lines(LOGS_PATH / "executions.csv", limit=20)
    stats = _build_performance_stats()
    protection_alerts = _build_protection_alerts()
    position_protection = _build_position_protection_status(positions)
    candidate_board = _build_candidate_board()
    volatility_heatmap = _build_volatility_heatmap()
    rejection_analytics = _build_rejection_analytics()
    strategy_performance_panel = _build_strategy_performance_panel()
    no_trade_intelligence = _build_no_trade_intelligence_panel()
    execution_leakage = _build_execution_leakage_panel()
    equity_curve = _build_equity_curve_panel(wallet)
    market_snapshots = _build_market_snapshots(positions)
    market_sparklines = _build_market_sparklines(market_snapshots, volatility_heatmap)
    position_telemetry = _build_position_telemetry(positions)
    execution_timeline = _build_execution_timeline()
    periodic_pnl = _build_periodic_pnl(equity_curve)
    dashboard_meta = _build_dashboard_meta()
    bot_health = _build_bot_health(last_log_lines, live_errors)
    strategy_control = _build_strategy_control()
    strategy_expectancy = _build_strategy_expectancy()
    strategy_weighting_status = _build_strategy_weighting_status()
    optimization_advice = _build_optimization_advice()
    symbol_expectancy = _build_symbol_expectancy()
    expectancy_matrix = _build_expectancy_matrix()
    strategy_heatmap = _build_strategy_heatmap(expectancy_matrix)
    live_risk = _build_live_risk_panel(wallet, positions)
    edge_panel = _build_edge_panel()
    cooldown_panel = _build_cooldown_panel()
    live_source = _build_live_source_panel(wallet, positions, live_errors)

    protection_summary = {
        "open_positions": len(positions),
        "protected_positions": len([row for row in position_protection if row.get("level") == "success"]),
        "warning_positions": len([row for row in position_protection if row.get("level") == "warning"]),
        "danger_positions": len([row for row in position_protection if row.get("level") == "danger"]),
        "be_active_positions": len([row for row in position_protection if row.get("be_active")]),
        "trailing_active_positions": len([row for row in position_protection if row.get("trailing_active")]),
        "orphan_risk_positions": len([row for row in position_protection if row.get("orphan_risk")]),
        "exchange_synced_positions": len([row for row in position_protection if row.get("exchange_synced")]),
    }

    dashboard_v4 = _build_dashboard_v4_model(
        wallet=wallet,
        positions=positions,
        stats=stats,
        protection_summary=protection_summary,
        protection_alerts=protection_alerts,
        position_protection=position_protection,
        candidate_board=candidate_board,
        volatility_heatmap=volatility_heatmap,
        rejection_analytics=rejection_analytics,
        strategy_performance_panel=strategy_performance_panel,
        no_trade_intelligence=no_trade_intelligence,
        execution_leakage=execution_leakage,
        equity_curve=equity_curve,
        periodic_pnl=periodic_pnl,
        market_snapshots=market_snapshots,
        market_sparklines=market_sparklines,
        position_telemetry=position_telemetry,
        execution_timeline=execution_timeline,
        strategy_heatmap=strategy_heatmap,
        bot_health=bot_health,
        strategy_expectancy=strategy_expectancy,
        strategy_weighting_status=strategy_weighting_status,
        optimization_advice=optimization_advice,
        symbol_expectancy=symbol_expectancy,
        expectancy_matrix=expectancy_matrix,
        live_risk=live_risk,
        edge_panel=edge_panel,
        cooldown_panel=cooldown_panel,
        live_source=live_source,
    )

    dashboard_data = {
        "wallet": wallet,
        "positions": positions,
        "bot": {
            "status": bot_status,
            "logs": last_log_lines,
            "live_errors": live_errors,
        },
        "executions": executions,
        "stats": stats,
        "protection_alerts": protection_alerts,
        "position_protection": position_protection,
        "protection_summary": protection_summary,
        "candidate_board": candidate_board,
        "volatility_heatmap": volatility_heatmap,
        "rejection_analytics": rejection_analytics,
        "strategy_performance_panel": strategy_performance_panel,
        "no_trade_intelligence": no_trade_intelligence,
        "execution_leakage": execution_leakage,
        "equity_curve": equity_curve,
        "market_snapshots": market_snapshots,
        "market_sparklines": market_sparklines,
        "position_telemetry": position_telemetry,
        "execution_timeline": execution_timeline,
        "strategy_heatmap": strategy_heatmap,
        "periodic_pnl": periodic_pnl,
        "dashboard_meta": dashboard_meta,
        "bot_health": bot_health,
        "strategy_control": strategy_control,
        "strategy_expectancy": strategy_expectancy,
        "strategy_weighting_status": strategy_weighting_status,
        "optimization_advice": optimization_advice,
        "symbol_expectancy": symbol_expectancy,
        "expectancy_matrix": expectancy_matrix,
        "live_risk": live_risk,
        "edge_panel": edge_panel,
        "cooldown_panel": cooldown_panel,
        "live_source": live_source,
        "dashboard_v4": dashboard_v4,
    }

    _DASHBOARD_CACHE["timestamp"] = now
    _DASHBOARD_CACHE["data"] = dashboard_data

    return dashboard_data