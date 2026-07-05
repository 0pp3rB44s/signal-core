"""Dashboard v2 data provider — one flat, honest data shape.

Every section is either genuinely live (Bitget API, state files, log tails
updated by the running bot every scan cycle) or explicitly labeled as a
point-in-time snapshot (backtest reference data, the audit report) with its
own `generated_at` timestamp, so it's never confused with live state.
"""

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents_v2.learning.learning_service import learning_service

from app.config import get_settings
from clients.bitget_rest import BitgetRestClient
from dashboard_v2.bot_control import is_bot_running

BASE_PATH = Path(__file__).resolve().parents[1]
LOGS_PATH = BASE_PATH / "logs"
STATE_PATH = BASE_PATH / "state"
REPORTS_PATH = BASE_PATH / "reports" / "backtests"
AUDIT_REPORT_PATH = BASE_PATH / "agents_v2" / "reports" / "audit.json"
COACH_DECISIONS_PATH = BASE_PATH / "agents_v2" / "reports" / "coach_decisions.json"

_DASHBOARD_CACHE: dict[str, Any] = {"timestamp": 0.0, "data": None}
CACHE_SECONDS = 3


# ---------------------------------------------------------------- helpers --

def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _tail_bytes(path: Path, max_bytes: int) -> bytes:
    """Read up to the last max_bytes of a file without loading it fully into
    memory. logs/bot.out (tens of MB) and logs/strategy_performance.csv
    (hundreds of MB, unbounded growth) made every dashboard refresh do a full
    read + parse of the entire file, repeatedly, every few seconds."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size > max_bytes:
            handle.seek(size - max_bytes)
            handle.readline()  # drop the partial line at the seek point
        return handle.read()


def _read_lines(path: Path, limit: int = 50) -> list[str]:
    if not path.exists():
        return []
    try:
        chunk = _tail_bytes(path, max(limit * 2000, 200_000))
        lines = chunk.decode("utf-8", errors="ignore").splitlines()
        return [line.strip() for line in lines[-limit:] if line.strip()]
    except Exception:
        return []


def _read_csv_rows(path: Path, limit: int = 500) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            header_line = handle.readline()
        if not header_line:
            return []
        fieldnames = next(csv.reader([header_line]))

        chunk = _tail_bytes(path, max(limit * 1000, 200_000))
        text = chunk.decode("utf-8", errors="ignore")
        # The first line of the tail chunk may be a truncated mid-row read;
        # drop it rather than risk a misaligned/garbage row.
        lines = text.split("\n")[1:]
        rows = [row for row in csv.DictReader(lines, fieldnames=fieldnames) if row]
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


# --------------------------------------------------------- live bitget data --

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
        orders.append({
            "symbol": str(row.get("symbol") or ""),
            "plan_type": str(row.get("planType") or ""),
            "hold_side": str(row.get("holdSide") or ""),
            "trigger_price": _safe_float(row.get("triggerPrice") or row.get("executePrice")),
            "size": _safe_float(row.get("size")),
            "status": str(row.get("status") or row.get("state") or "OPEN"),
        })
    return orders


def _normalize_positions(raw_positions: Any, tpsl_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    state_positions = _read_json(STATE_PATH / "executed_trades.json") or []
    state_by_symbol = {
        str(p.get("symbol") or ""): p for p in state_positions if isinstance(p, dict)
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

        distance_to_sl_pct = 0.0
        if price > 0 and isinstance(sl, (int, float)) and sl > 0:
            distance_to_sl_pct = abs(price - sl) / price * 100.0

        live_rr = 0.0
        if entry > 0 and isinstance(sl, (int, float)) and sl > 0:
            risk = abs(entry - sl)
            reward = abs(price - entry)
            live_rr = reward / risk if risk > 0 else 0.0

        positions.append({
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
            "distance_to_sl_pct": round(distance_to_sl_pct, 3),
            "live_rr": round(live_rr, 3),
            "break_even_active": bool(state.get("break_even_active")),
            "tp1_hit": bool(state.get("tp1_hit")),
            "tp2_hit": bool(state.get("tp2_hit")),
            "tp3_hit": bool(state.get("tp3_hit")),
            "protection_verified": bool(state.get("protection_verified")),
        })
    return positions


def _fallback_positions() -> list[dict[str, Any]]:
    state_positions = _read_json(STATE_PATH / "positions.json") or []
    positions = []
    for p in state_positions:
        positions.append({
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
            "distance_to_sl_pct": 0.0,
            "live_rr": 0.0,
        })
    return positions


def _live_bitget_data() -> tuple[dict[str, float], list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    try:
        settings = get_settings()
        client = BitgetRestClient(settings=settings)

        account_payload = client.get_accounts(product_type=settings.bitget_product_type)
        positions_payload = client.get_all_positions(product_type=settings.bitget_product_type)

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
        return (
            {"balance": 0.0, "equity": 0.0, "available": 0.0, "used_margin": 0.0},
            _fallback_positions(),
            errors,
        )


# -------------------------------------------------- protection & alerts --

def _build_protection_alerts() -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    positions = _read_json(STATE_PATH / "executed_trades.json") or []
    events = _read_json(STATE_PATH / "position_events.json") or []

    for p in positions if isinstance(positions, list) else []:
        if p.get("status") != "OPEN":
            continue
        symbol = str(p.get("symbol") or "UNKNOWN")
        stop_loss = _safe_float(p.get("stop_loss"))
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
        sl, tp = p.get("sl"), p.get("tp")
        tp1_hit = bool(state.get("tp1_hit"))
        be_active = bool(state.get("break_even_active"))
        trailing_active = bool(state.get("trailing_active"))
        protection_verified = bool(state.get("protection_verified"))
        exchange_synced = bool(state.get("exchange_synced", protection_verified))

        has_sl = sl not in (None, "", "MISSING")
        has_tp = tp not in (None, "", "MISSING")

        if not has_sl or not has_tp:
            level, status = "danger", "MISSING PROTECTION"
        elif tp1_hit and not be_active:
            level, status = "danger", "TP1 HIT · BE NOT ACTIVE"
        elif be_active:
            level, status = "success", "BE ACTIVE"
        elif protection_verified:
            level, status = "success", "PROTECTED"
        else:
            level, status = "warning", "PROTECTION UNVERIFIED"

        rows.append({
            "symbol": symbol,
            "direction": p.get("direction"),
            "status": status,
            "level": level,
            "sl": sl,
            "tp": tp,
            "tp1_hit": tp1_hit,
            "tp2_hit": bool(state.get("tp2_hit")),
            "tp3_hit": bool(state.get("tp3_hit")),
            "be_active": be_active,
            "trailing_active": trailing_active,
            "exchange_synced": exchange_synced,
            "orphan_risk": not exchange_synced and has_sl,
        })
    return rows


# ---------------------------------------------------------------- AI coach --

def _build_ai_coach_panel() -> dict[str, Any]:
    """Read the real learning-coach gate state (risk/risk_manager.py's
    _ai_agent_gate reads this same file) instead of a hardcoded strategy list."""
    payload = _read_json(COACH_DECISIONS_PATH) or {}
    decisions = payload.get("decisions") or []

    blocked_strategies: dict[str, str] = {}
    blocked_symbols: dict[str, str] = {}
    notes: list[dict[str, Any]] = []

    for decision in decisions:
        if not isinstance(decision, dict):
            continue
        action = str(decision.get("action") or "")
        target = str(decision.get("target") or "")
        reason = str(decision.get("reason") or "")
        level = str(decision.get("level") or "info")

        if action == "reduce_strategy_exposure" and target:
            blocked_strategies[target.lower()] = reason
        elif action == "avoid_symbol_until_improved" and target:
            blocked_symbols[target.upper()] = reason
        else:
            notes.append({"level": level, "action": action, "target": target, "reason": reason})

    return {
        "decision_count": payload.get("decision_count", len(decisions)),
        "blocked_strategies": [{"strategy": k, "reason": v} for k, v in blocked_strategies.items()],
        "blocked_symbols": [{"symbol": k, "reason": v} for k, v in blocked_symbols.items()],
        "notes": notes[:8],
        "level": "danger" if (blocked_strategies or blocked_symbols) else "success",
    }


# -------------------------------------------------------------- risk panel --

def _build_live_risk_panel(wallet: dict[str, float], positions: list[dict[str, Any]], equity_curve: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    max_positions = int(getattr(settings, "max_open_positions", 0) or 0)
    risk_pct = float(getattr(settings, "account_risk_per_trade_pct", 0.0) or 0.0)
    leverage = float(getattr(settings, "default_leverage", 0.0) or 0.0)
    max_daily_loss_pct = float(getattr(settings, "max_daily_loss_pct", 0.0) or 0.0)
    hard_daily_stop_pct = float(getattr(settings, "hard_daily_stop_pct", 0.0) or 0.0)

    equity = float(wallet.get("equity") or wallet.get("balance") or 0.0)
    risk_budget = equity * (risk_pct / 100.0)
    open_positions = len(positions)
    exposure = sum(_safe_float(pos.get("notional")) for pos in positions)
    exposure_pct = (exposure / equity * 100.0) if equity > 0 else 0.0

    state_positions = _read_json(STATE_PATH / "executed_trades.json") or []
    open_trade_risk = 0.0
    for row in state_positions if isinstance(state_positions, list) else []:
        if not isinstance(row, dict) or str(row.get("status") or "").upper() != "OPEN":
            continue
        entry = _safe_float(row.get("avg_entry") or row.get("entry"))
        stop = _safe_float(row.get("stop_loss"))
        size = _safe_float(row.get("size"))
        if entry > 0 and stop > 0 and size > 0:
            open_trade_risk += abs(entry - stop) * size

    daily_pnl = _safe_float(equity_curve.get("daily_pnl"))
    weekly_pnl = _safe_float(equity_curve.get("weekly_pnl"))
    max_drawdown = _safe_float(equity_curve.get("max_drawdown"))

    daily_loss_pct = abs(daily_pnl) / equity * 100.0 if equity > 0 and daily_pnl < 0 else 0.0
    weekly_loss_pct = abs(weekly_pnl) / equity * 100.0 if equity > 0 and weekly_pnl < 0 else 0.0
    open_risk_pct = open_trade_risk / equity * 100.0 if equity > 0 else 0.0

    risk_state, level, alerts = "SAFE", "success", []

    if max_positions and open_positions >= max_positions:
        risk_state, level = "WATCH", "warning"
        alerts.append("max positions reached")

    if max_daily_loss_pct and daily_loss_pct >= max_daily_loss_pct:
        risk_state, level = "WATCH", "warning"
        alerts.append("daily soft loss limit reached")

    if hard_daily_stop_pct and daily_loss_pct >= hard_daily_stop_pct:
        risk_state, level = "DANGER", "danger"
        alerts.append("daily hard stop reached — kill-switch active")

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
    }


# --------------------------------------------------------- equity & pnl --

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
        pnl_value = _safe_float(row.get("pnl"))
        if pnl_value == 0.0:
            pnl_pct = _safe_float(row.get("realized_pnl_pct"))
            notional = _safe_float(row.get("notional") or row.get("position_notional_usdt"))
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
        "points": points[-30:],
        "level": "success" if total_pnl >= 0 else "danger",
    }


def _build_periodic_pnl(equity_curve: dict[str, Any]) -> dict[str, Any]:
    total_pnl = _safe_float(equity_curve.get("total_pnl"))
    daily_pnl = _safe_float(equity_curve.get("daily_pnl"))
    weekly_pnl = _safe_float(equity_curve.get("weekly_pnl"))
    return {
        "daily": round(daily_pnl, 4),
        "weekly": round(weekly_pnl, 4),
        "monthly": round(weekly_pnl * 4, 4),
        "total": round(total_pnl, 4),
        "level": "success" if total_pnl >= 0 else "danger",
    }


def _execution_status(row: dict[str, Any]) -> str:
    return str(row.get("status") or row.get("result") or "").upper()


def _execution_strategy(row: dict[str, Any]) -> str:
    return str(row.get("strategy") or row.get("setup") or row.get("strategy_name") or "").lower()


def _build_performance_stats() -> dict[str, Any]:
    execution_rows = _read_csv_rows(LOGS_PATH / "executions.csv", limit=1000)
    executed = [row for row in execution_rows if _execution_status(row) == "EXECUTED"]
    skipped = [row for row in execution_rows if _execution_status(row) == "SKIPPED"]
    failed = [row for row in execution_rows if _execution_status(row) in {"ERROR", "FAILED", "FAIL_SAFE", "FAIL-SAFE"}]

    return {
        "total_events": len(execution_rows),
        "executed_trades": len(executed),
        "skipped_trades": len(skipped),
        "failed_trades": len(failed),
        "latest_executions": list(reversed(execution_rows[-8:])),
    }


def _build_execution_leakage_panel() -> dict[str, Any]:
    rows = _read_strategy_performance_rows(limit=2000)
    close_rows = [row for row in rows if str(row.get("event_type") or "") == "TRADE_CLOSE"]

    total_fees = sum(_safe_float(row.get("fees")) for row in close_rows)
    total_net = sum(_safe_float(row.get("net_pnl")) for row in close_rows)
    avg_slippage = sum(_safe_float(row.get("slippage_pct")) for row in close_rows) / len(close_rows) if close_rows else 0.0
    avg_fee_leakage = sum(_safe_float(row.get("fee_leakage_pct")) for row in close_rows) / len(close_rows) if close_rows else 0.0

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
    }


# -------------------------------------------------------- scanner / candidates --

def _parse_scan_line(line: str) -> dict[str, Any] | None:
    if " | SCAN | " not in line:
        return None

    parts = [part.strip() for part in line.split("|")]
    if len(parts) < 6:
        return None

    # parts: [timestamp, level, logger, "SCAN", symbol, rest...]
    symbol = parts[4]
    text = " | ".join(parts[5:])

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
        "volatility_rank": round(volatility_rank, 1),
        "volume_expansion": has_volume_expansion,
        "level": level,
    }


def _build_volatility_heatmap() -> list[dict[str, Any]]:
    rows_by_symbol: dict[str, dict[str, Any]] = {}
    for line in reversed(_read_lines(LOGS_PATH / "bot.out", limit=500)):
        row = _parse_scan_line(line)
        if not row or row["symbol"] in rows_by_symbol:
            continue
        rows_by_symbol[row["symbol"]] = row

    rows = list(rows_by_symbol.values())
    rows.sort(key=lambda row: (row.get("volatility_rank", 0), row.get("volume_ratio", 0), row.get("score_hint", 0)), reverse=True)
    return rows[:15]


def _build_candidate_board() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for line in reversed(_read_lines(LOGS_PATH / "bot.out", limit=250)):
        if " | SCAN | " not in line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 6:
            continue
        # parts: [timestamp, level, logger, "SCAN", symbol, rest...]
        symbol = parts[4]
        rest = " | ".join(parts[5:])
        if any(row["symbol"] == symbol for row in candidates):
            continue
        candidates.append({
            "symbol": symbol,
            "summary": rest[:220],
            "level": "success" if "score_hint=80" in rest or "score_hint=75" in rest else "warning" if "score_hint=65" in rest or "score_hint=70" in rest else "neutral",
        })
        if len(candidates) >= 12:
            break
    return candidates


# -------------------------------------------------------- rejection intel --

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
    if "risk" in text:
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


def _build_rejection_analytics() -> dict[str, Any]:
    reason_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    grouped_recent: dict[tuple[str, str], dict[str, Any]] = {}

    for line in reversed(_read_lines(LOGS_PATH / "bot.out", limit=1500)):
        upper = line.upper()
        if "NO_SETUP" not in upper and "REJECTED_SETUP" not in upper and "SKIPPED" not in upper:
            continue

        reason = _classify_rejection_reason(line)
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

        parts = [part.strip() for part in line.split("|")]
        symbol = "UNKNOWN"
        for marker in ("NO_SETUP", "REJECTED_SETUP", "SKIPPED"):
            if marker in parts:
                idx = parts.index(marker)
                if len(parts) > idx + 1:
                    symbol = parts[idx + 1].strip()
                break
        else:
            if len(parts) >= 5:
                symbol = parts[4].strip()

        if symbol in {"NO_SETUP", "REJECTED_SETUP", "SKIPPED"}:
            symbol = "UNKNOWN"
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1

        group_key = (symbol, reason)
        if group_key not in grouped_recent:
            grouped_recent[group_key] = {
                "symbol": symbol,
                "reason": reason,
                "count": 0,
                "level": "warning" if "REJECTED" in upper or "SKIPPED" in upper else "neutral",
            }
        grouped_recent[group_key]["count"] += 1

    top_reasons = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:8]
    top_symbols = sorted(symbol_counts.items(), key=lambda item: item[1], reverse=True)[:8]
    recent_rows = sorted(grouped_recent.values(), key=lambda row: row["count"], reverse=True)[:12]

    return {
        "total": sum(reason_counts.values()),
        "top_reasons": [{"reason": reason, "count": count} for reason, count in top_reasons],
        "top_symbols": [{"symbol": symbol, "count": count} for symbol, count in top_symbols],
        "recent": recent_rows,
    }


def _build_no_trade_intelligence_panel() -> dict[str, Any]:
    rows = _read_strategy_performance_rows(limit=2500)
    reject_rows = [row for row in rows if str(row.get("stage") or "") in {"SCAN_REJECT", "PLAN_REJECT"}]

    reason_counts: dict[str, int] = {}
    for row in reject_rows:
        for part in str(row.get("reasons") or "").split("|"):
            reason = part.strip()
            if not reason:
                continue
            key = reason.split("=", 1)[0].strip()
            reason_counts[key] = reason_counts.get(key, 0) + 1

    top_reasons = sorted(reason_counts.items(), key=lambda item: item[1], reverse=True)[:10]

    return {
        "total_rejects": len(reject_rows),
        "top_reasons": [{"reason": reason, "count": count} for reason, count in top_reasons],
        "recent": list(reversed(reject_rows[-10:])),
    }


# -------------------------------------------------------- strategy performance (live) --

def _build_strategy_performance_panel() -> dict[str, Any]:
    rows = _read_strategy_performance_rows(limit=2000)
    by_strategy: dict[str, dict[str, Any]] = {}

    for row in rows:
        strategy = str(row.get("strategy") or "UNKNOWN")
        event_type = str(row.get("event_type") or "")
        verdict = str(row.get("verdict") or "")

        bucket = by_strategy.setdefault(strategy, {
            "strategy": strategy, "setup_events": 0, "executables": 0,
            "closed_trades": 0, "wins": 0, "net_pnl": 0.0, "tp1_hits": 0,
        })

        if event_type == "SETUP_EVENT":
            bucket["setup_events"] += 1
            if verdict == "EXECUTABLE":
                bucket["executables"] += 1

        if event_type == "TRADE_CLOSE":
            bucket["closed_trades"] += 1
            net_pnl = _safe_float(row.get("net_pnl"))
            bucket["net_pnl"] += net_pnl
            if net_pnl > 0:
                bucket["wins"] += 1
            if str(row.get("tp1_hit") or "").lower() == "true":
                bucket["tp1_hits"] += 1

    strategy_rows: list[dict[str, Any]] = []
    for bucket in by_strategy.values():
        closed = int(bucket["closed_trades"])
        setup_events = int(bucket["setup_events"])
        net_pnl = float(bucket["net_pnl"])
        winrate = bucket["wins"] / closed if closed else 0.0
        expectancy = net_pnl / closed if closed else 0.0
        executable_rate = bucket["executables"] / setup_events if setup_events else 0.0

        if closed >= 5 and expectancy > 0:
            status, level = "GOOD", "success"
        elif closed >= 5 and expectancy < 0:
            status, level = "PAUSE", "danger"
        elif setup_events >= 20 and executable_rate <= 0.01:
            status, level = "TOO_STRICT", "warning"
        else:
            status, level = "WATCH", "warning"

        strategy_rows.append({
            "strategy": bucket["strategy"],
            "setup_events": setup_events,
            "executables": bucket["executables"],
            "executable_rate": round(executable_rate, 3),
            "closed_trades": closed,
            "winrate": round(winrate, 3),
            "net_pnl": round(net_pnl, 4),
            "expectancy": round(expectancy, 4),
            "status": status,
            "level": level,
        })

    strategy_rows.sort(key=lambda row: (row["level"] == "success", row["expectancy"], row["executables"]), reverse=True)

    return {
        "total_events": len(rows),
        "strategies": strategy_rows[:12],
        "level": "success" if any(row["level"] == "success" for row in strategy_rows) else "warning",
    }


# -------------------------------------------------------------- cooldowns --

def _build_cooldown_panel() -> list[dict[str, Any]]:
    settings = get_settings()
    cooldown_minutes = int(getattr(settings, "symbol_cooldown_minutes", 30) or 30)
    events = _read_json(STATE_PATH / "execution_events.json") or []
    events_list = events.get("data") if isinstance(events, dict) else events
    now = datetime.now(timezone.utc)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()

    for event in reversed(events_list if isinstance(events_list, list) else []):
        if not isinstance(event, dict) or str(event.get("status") or "").upper() != "EXECUTED":
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

        remaining = cooldown_minutes - int((now - ts).total_seconds() / 60.0)
        if remaining <= 0:
            continue
        rows.append({"symbol": symbol, "remaining_minutes": remaining})
        if len(rows) >= 8:
            break

    return rows


# --------------------------------------------- backtest reference (non-live) --

def _strategy_status(expectancy: float, trades: int) -> tuple[str, str]:
    if trades < 5:
        return "WATCH", "warning"
    if expectancy > 0.15:
        return "GOOD", "success"
    if expectancy < 0.0:
        return "PAUSE", "danger"
    return "WATCH", "warning"


def _build_backtest_reference() -> dict[str, Any]:
    """Point-in-time backtest snapshot -- explicitly NOT live, labeled with
    its own generated_at so it's never confused with real-time trading state."""
    summary_path = REPORTS_PATH / "latest_summary.json"
    summary = _read_json(summary_path) or {}
    by_strategy = summary.get("by_strategy") or {}
    by_symbol = summary.get("by_symbol") or {}

    generated_at = None
    if summary_path.exists():
        generated_at = datetime.fromtimestamp(summary_path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")

    strategy_rows = []
    for name, raw in by_strategy.items():
        if not isinstance(raw, dict):
            continue
        trades = int(raw.get("trades") or 0)
        expectancy = _safe_float(raw.get("expectancy"))
        status, level = _strategy_status(expectancy, trades)
        strategy_rows.append({
            "name": name,
            "trades": trades,
            "winrate": round(_safe_float(raw.get("winrate")), 3),
            "expectancy": round(expectancy, 4),
            "tp1_hit_rate": round(_safe_float(raw.get("tp1_hit_rate")), 3),
            "status": status,
            "level": level,
        })
    strategy_rows.sort(key=lambda row: row["expectancy"], reverse=True)

    symbol_rows = []
    for symbol, raw in by_symbol.items():
        if not isinstance(raw, dict):
            continue
        trades = int(raw.get("trades") or 0)
        expectancy = _safe_float(raw.get("expectancy"))
        status, level = _strategy_status(expectancy, trades)
        symbol_rows.append({
            "symbol": symbol,
            "trades": trades,
            "winrate": round(_safe_float(raw.get("winrate")), 3),
            "expectancy": round(expectancy, 4),
            "status": status,
            "level": level,
        })
    symbol_rows.sort(key=lambda row: row["expectancy"], reverse=True)

    return {
        "generated_at": generated_at,
        "available": summary_path.exists(),
        "strategies": strategy_rows[:10],
        "symbols": symbol_rows[:10],
    }


def _build_audit_panel() -> dict[str, Any]:
    audit = _read_json(AUDIT_REPORT_PATH)
    generated_at = None
    if AUDIT_REPORT_PATH.exists():
        generated_at = datetime.fromtimestamp(AUDIT_REPORT_PATH.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")

    if isinstance(audit, dict):
        audit = dict(audit)
        audit["generated_at"] = generated_at
        audit["available"] = True
        return audit

    return {
        "available": False,
        "generated_at": generated_at,
        "overall": {
            "overall_score": 0,
            "overall_grade": "UNKNOWN",
            "primary_risk": "Audit report not available.",
            "primary_strength": "Run morning_audit.py to generate audit.json.",
            "weakest_module": "unknown",
        },
        "performance": {},
        "runtime_health": {},
    }


def _build_learning_panel() -> dict[str, Any]:
    try:
        learning_service.reload()
        summary = learning_service.get_summary()
        if isinstance(summary, dict):
            return summary
    except Exception as exc:
        return {
            "metadata": {},
            "diagnosis": {"warnings": [f"Learning Engine unavailable: {exc}"], "recommendations": []},
            "best_strategy": None,
            "worst_strategy": None,
        }
    return {
        "metadata": {},
        "diagnosis": {"warnings": ["Learning Engine report not available."], "recommendations": []},
        "best_strategy": None,
        "worst_strategy": None,
    }


# ---------------------------------------------------------- execution feed --

def _build_execution_timeline() -> list[dict[str, Any]]:
    rows = _read_strategy_performance_rows(limit=120)
    timeline: list[dict[str, Any]] = []

    for row in reversed(rows[-30:]):
        event_type = str(row.get("event_type") or "EVENT")
        stage = str(row.get("stage") or row.get("status") or "")
        symbol = str(row.get("symbol") or "-")
        strategy = str(row.get("strategy") or "-")
        net_pnl = _safe_float(row.get("net_pnl"))

        level = "neutral"
        if "REJECT" in stage:
            level = "warning"
        if event_type == "TRADE_CLOSE":
            level = "success" if net_pnl > 0 else "danger" if net_pnl < 0 else "neutral"

        timeline.append({
            "timestamp": row.get("timestamp") or row.get("closed_at") or "",
            "event_type": event_type,
            "symbol": symbol,
            "strategy": strategy,
            "net_pnl": round(net_pnl, 4),
            "level": level,
            "summary": f"{symbol} · {event_type} · {stage or strategy}",
        })

    return timeline[:20]


# ------------------------------------------------------------ bot status --

def _build_bot_panel(recent_errors: list[str]) -> dict[str, Any]:
    settings = get_settings()
    running, pid = is_bot_running()

    return {
        "running": running,
        "pid": pid,
        "status": "RUNNING" if running else "STOPPED",
        "env": getattr(settings, "app_env", "unknown"),
        "mode": getattr(settings, "app_mode", "unknown"),
        "execution_mode": getattr(settings, "execution_mode", "unknown"),
        "execution_enabled": bool(getattr(settings, "execution_enabled", False)),
        "scan_interval_sec": int(getattr(settings, "scan_interval_sec", 0) or 0),
        "recent_errors": recent_errors[-5:],
        "level": "danger" if recent_errors else ("success" if running else "danger"),
    }


# ---------------------------------------------------------------- main API --

def get_dashboard_data() -> dict[str, Any]:
    global _DASHBOARD_CACHE

    now = time.time()
    if _DASHBOARD_CACHE["data"] is not None and now - float(_DASHBOARD_CACHE["timestamp"]) < CACHE_SECONDS:
        return _DASHBOARD_CACHE["data"]

    wallet, positions, live_errors = _live_bitget_data()

    # Match the actual log LEVEL field ("| ERROR |"/"| CRITICAL |"), not just
    # any line containing the substring "error"/"fail" -- normal scan output
    # includes plenty of INFO-level lines like "result=FAIL" that aren't
    # actual bot errors.
    recent_log_errors = [
        line for line in _read_lines(LOGS_PATH / "bot.out", limit=200)
        if " | ERROR | " in line or " | CRITICAL | " in line
    ]

    position_protection = _build_position_protection_status(positions)
    protection_alerts = _build_protection_alerts()
    protection_summary = {
        "open_positions": len(positions),
        "protected_positions": len([r for r in position_protection if r.get("level") == "success"]),
        "warning_positions": len([r for r in position_protection if r.get("level") == "warning"]),
        "danger_positions": len([r for r in position_protection if r.get("level") == "danger"]),
        "orphan_risk_positions": len([r for r in position_protection if r.get("orphan_risk")]),
    }

    equity_curve = _build_equity_curve_panel(wallet)

    data = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "refresh_interval_seconds": CACHE_SECONDS,
        },
        "bot": _build_bot_panel(recent_log_errors + live_errors),
        "wallet": wallet,
        "positions": positions,
        "position_protection": position_protection,
        "protection_alerts": protection_alerts,
        "protection_summary": protection_summary,
        "risk": _build_live_risk_panel(wallet, positions, equity_curve),
        "ai_coach": _build_ai_coach_panel(),
        "candidate_board": _build_candidate_board(),
        "volatility_heatmap": _build_volatility_heatmap(),
        "rejection_analytics": _build_rejection_analytics(),
        "no_trade_intelligence": _build_no_trade_intelligence_panel(),
        "strategy_performance": _build_strategy_performance_panel(),
        "execution_leakage": _build_execution_leakage_panel(),
        "equity_curve": equity_curve,
        "periodic_pnl": _build_periodic_pnl(equity_curve),
        "stats": _build_performance_stats(),
        "cooldowns": _build_cooldown_panel(),
        "execution_timeline": _build_execution_timeline(),
        "backtest_reference": _build_backtest_reference(),
        "audit": _build_audit_panel(),
        "learning": _build_learning_panel(),
    }

    _DASHBOARD_CACHE["timestamp"] = now
    _DASHBOARD_CACHE["data"] = data
    return data
