from __future__ import annotations

import argparse

import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from backtesting.backtest_engine import BacktestEngine
from clients.schemas import Candle
from telemetry.csv_rotation import rotated_segments


DATA_PATH = Path("data/backtests")
REPORT_DIR = Path("reports/backtests")
LATEST_REPORT_PATH = REPORT_DIR / "latest_backtest.json"
LATEST_SUMMARY_PATH = REPORT_DIR / "latest_summary.json"
DAILY_VALIDATION_PATH = REPORT_DIR / "daily_validation.json"
STRATEGY_EXPECTANCY_PATH = REPORT_DIR / "strategy_expectancy.json"
DAILY_LEARNING_REPORT_PATH = REPORT_DIR / "daily_learning_report.json"
DAILY_LEARNING_HISTORY_DIR = REPORT_DIR / "daily_learning_history"
TRADE_DATASET_PATH = Path("logs/trade_dataset_v2.csv")
STRATEGY_DATASET_PATH = Path("logs/strategy_performance.csv")


def _configure_report_paths(out_dir: Path | None = None) -> None:
    global REPORT_DIR, LATEST_REPORT_PATH, LATEST_SUMMARY_PATH, DAILY_VALIDATION_PATH, STRATEGY_EXPECTANCY_PATH, DAILY_LEARNING_REPORT_PATH, DAILY_LEARNING_HISTORY_DIR

    if out_dir is None:
        return

    REPORT_DIR = out_dir
    LATEST_REPORT_PATH = REPORT_DIR / "latest_backtest.json"
    LATEST_SUMMARY_PATH = REPORT_DIR / "latest_summary.json"
    DAILY_VALIDATION_PATH = REPORT_DIR / "daily_validation.json"
    STRATEGY_EXPECTANCY_PATH = REPORT_DIR / "strategy_expectancy.json"
    DAILY_LEARNING_REPORT_PATH = REPORT_DIR / "daily_learning_report.json"
    DAILY_LEARNING_HISTORY_DIR = REPORT_DIR / "daily_learning_history"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CGC bot backtest and/or live validation reports.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--backtest-only", action="store_true", help="Run only candle backtest reports; skip live validation report refresh.")
    mode.add_argument("--validation-only", action="store_true", help="Run only live validation/strategy expectancy reports; skip candle backtest.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols to include, e.g. BTCUSDT,ETHUSDT,SOLUSDT.")
    parser.add_argument("--days", type=int, default=0, help="Reserved for future candle filtering; currently recorded but not applied.")
    parser.add_argument("--out-dir", default="", help="Optional output directory for reports. Default: reports/backtests.")
    return parser.parse_args()


def _parse_symbol_filter(raw_symbols: str) -> set[str]:
    return {symbol.strip().upper() for symbol in str(raw_symbols or "").split(",") if symbol.strip()}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


# Helper: find first present numeric field in a tuple of field names
def _first_numeric_field(row: dict[str, Any], field_names: tuple[str, ...]) -> tuple[str, float] | tuple[str, None]:
    for field_name in field_names:
        value = row.get(field_name)
        if value in (None, ""):
            continue
        return field_name, _safe_float(value)
    return "", None


def _load_candles_from_json(path: Path) -> list[Candle]:
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)

    candles: list[Candle] = []
    for row in rows:
        candles.append(
            Candle(
                timestamp_ms=int(row.get("timestamp") or row.get("timestamp_ms") or row.get("ts") or 0),
                open=_safe_float(row.get("open")),
                high=_safe_float(row.get("high")),
                low=_safe_float(row.get("low")),
                close=_safe_float(row.get("close")),
                volume_base=_safe_float(row.get("volume_base") or row.get("volume") or row.get("baseVolume")),
                volume_quote=_safe_float(row.get("volume_quote") or row.get("quoteVolume") or 0),
            )
        )
    return candles


def load_market_data(symbol_filter: set[str] | None = None) -> dict[str, list[Candle]]:
    DATA_PATH.mkdir(parents=True, exist_ok=True)

    market_data: dict[str, list[Candle]] = {}
    for file_path in DATA_PATH.glob("*.json"):
        symbol = file_path.stem.upper().replace("_", "")
        if symbol_filter and symbol not in symbol_filter:
            continue
        candles = _load_candles_from_json(file_path)
        if len(candles) >= 100:
            market_data[symbol] = candles

    return market_data


# Write reports (JSON) to disk: latest and timestamped
def _write_reports(result: dict[str, Any]) -> dict[str, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_UTC")
    timestamped_report = REPORT_DIR / f"{timestamp}_backtest.json"
    timestamped_summary = REPORT_DIR / f"{timestamp}_summary.json"

    expectancy_matrix = result.get("expectancy_matrix") or {}
    if not expectancy_matrix:
        expectancy_matrix = {
            "strategy_direction": result.get("strategy_direction_expectancy", {}),
            "strategy_regime": result.get("strategy_regime_expectancy", {}),
            "symbol_direction": result.get("symbol_direction_expectancy", {}),
        }

    summary = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "trades": result.get("trades"),
        "winrate": result.get("winrate"),
        "lossrate": result.get("lossrate"),
        "breakeven_rate": result.get("breakeven_rate"),
        "tp1_hit_rate": result.get("tp1_hit_rate"),
        "timed_exit_rate": result.get("timed_exit_rate"),
        "pnl": result.get("pnl"),
        "expectancy": result.get("expectancy"),
        "max_drawdown": result.get("max_drawdown"),
        "profit_factor": result.get("profit_factor"),
        "by_strategy": result.get("by_strategy", {}),
        "by_direction": result.get("by_direction", {}),
        "by_symbol": result.get("by_symbol", {}),
        "by_regime": result.get("by_regime", {}),
        "expectancy_matrix": expectancy_matrix,
        "debug": result.get("debug", {}),
    }

    for path, payload in [
        (LATEST_REPORT_PATH, result),
        (LATEST_SUMMARY_PATH, summary),
        (timestamped_report, result),
        (timestamped_summary, summary),
    ]:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    return {
        "latest_report": LATEST_REPORT_PATH,
        "latest_summary": LATEST_SUMMARY_PATH,
        "timestamped_report": timestamped_report,
        "timestamped_summary": timestamped_summary,
    }

def _load_csv_rows(path: Path) -> list[dict[str, Any]]:
    """Read `path`, concatenating any rotated backups (oldest-first) so history survives rotation."""
    rows: list[dict[str, Any]] = []
    for segment in rotated_segments(path):
        try:
            with open(segment, "r", encoding="utf-8") as f:
                rows.extend(csv.DictReader(f))
        except Exception:
            continue
    return rows


def _extract_reasons(row: dict[str, Any]) -> list[str]:
    raw = str(row.get("reasons") or row.get("reason") or row.get("blocked_reason") or "")
    if not raw:
        return ["unknown"]

    parts: list[str] = []
    for chunk in raw.replace(";", "|").split("|"):
        reason = chunk.strip()
        if not reason:
            continue
        parts.append(reason)

    return parts or ["unknown"]


def _trade_pnl(row: dict[str, Any]) -> float:
    """Return the most trustworthy closed-trade PnL.

    Priority:
    1. Bitget / exchange net position PnL fields. These are absolute USDT values and should already include fees.
    2. Explicit net PnL fields written by the bot.
    3. Realized PnL minus explicit fees, only when exchange net fields are missing.
    4. Legacy percentage/gross fields as last-resort fallback.

    This prevents green price-move trades from being counted as wins when fees made them net negative.
    """
    exchange_net_fields = (
        "bitget_position_pnl",
        "position_pnl",
        "position_pnl_usdt",
        "exchange_position_pnl",
        "exchange_position_pnl_usdt",
        "exchange_truth_position_pnl",
        "exchange_truth_pnl",
        "exchange_truth_net_pnl",
        "closed_pnl",
        "close_pnl",
    )
    field_name, exchange_net_pnl = _first_numeric_field(row, exchange_net_fields)
    if exchange_net_pnl is not None:
        return exchange_net_pnl

    explicit_net_fields = (
        "net_pnl_usdt",
        "net_realized_pnl",
        "realized_net_pnl",
        "net_pnl",
    )
    field_name, explicit_net_pnl = _first_numeric_field(row, explicit_net_fields)
    if explicit_net_pnl is not None:
        return explicit_net_pnl

    realized_field, realized_pnl = _first_numeric_field(row, ("realized_pnl", "gross_pnl", "pnl"))
    if realized_pnl is not None:
        _, fees = _first_numeric_field(row, ("fees", "fee", "fees_paid", "exchange_truth_fee", "total_fee"))
        if fees is not None:
            return realized_pnl - abs(fees)
        return realized_pnl

    return _safe_float(row.get("pnl_pct") or 0.0)



def _is_closed_trade(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or str(row.get("position_status") or "")).upper()
    event_type = str(row.get("event_type") or "").upper()
    closed_reason = str(row.get("closed_reason") or row.get("close_reason") or "")
    if "OPEN" in status and "CLOSED" not in status:
        return False
    return bool(closed_reason) or "CLOSE" in event_type or "CLOSED" in status or _trade_pnl(row) != 0.0


# Inserted helper functions for recovery trade and expectancy stats
def _is_recovery_trade(row: dict[str, Any]) -> bool:
    strategy = str(row.get("strategy") or row.get("setup_strategy") or "").lower()
    close_reason = str(row.get("closed_reason") or row.get("close_reason") or "").lower()
    event_type = str(row.get("event_type") or row.get("event") or "").lower()
    close_source = str(row.get("close_source") or "").lower()

    confidence = str(row.get("data_confidence") or "").upper().strip()
    process_verdict = str(row.get("process_verdict") or "").upper().strip()

    strategy_truth_markers = {
        "low_vol_reclaim",
        "liquidity_sweep_reversal",
        "momentum_breakout",
        "momentum_breakdown",
        "trend_continuation",
        "adaptive_momentum_continuation",
    }

    trusted_confidence = {
        "STRATEGY_TRUTH",
        "STRATEGY_TRUTH_VALIDATED",
        "EXCHANGE_TRUTH",
        "EXCHANGE_TRUTH_CLOSE",
    }

    trusted_process = {
        "STRATEGY_TRUTH_VALIDATED",
        "VALIDATED_POSITION_CLOSE",
        "EXCHANGE_TRUTH_CLOSE",
        "POSITION_CLOSED_SYNCED",
    }

    trusted_event = event_type in {"close", "position_closed", "closed_synced"}
    trusted_strategy_close = (
        strategy in strategy_truth_markers
        and trusted_event
        and (
            confidence in trusted_confidence
            or process_verdict in trusted_process
            or close_source == "bitget_order_history"
        )
    )

    hard_recovery_blob = "|".join([close_reason, close_source, process_verdict.lower()])
    hard_recovery_markers = (
        "manual_sync",
        "state_recovery",
        "recovered_",
        "legacy_close",
        "unlinked",
        "no_position_to_close",
        "closed_state_dataset_backfill",
    )
    if trusted_strategy_close and not any(marker in hard_recovery_blob for marker in hard_recovery_markers):
        return False

    blob = "|".join([strategy, close_reason, event_type, close_source])
    recovery_markers = (
        "recovered_",
        "recovery",
        "reconciliation",
        "closed_sync",
        # "exchange_position_closed_sync",  # removed as requested
        # "closed_synced",  # removed as requested
        "no_position_to_close",
        "legacy_close",
        "unlinked",
        "manual_sync",
        "state_recovery",
        "closed_state_dataset_backfill",
    )
    return any(marker in blob for marker in recovery_markers)


# Inserted helpers for data confidence and exchange truth
def _data_confidence(row: dict[str, Any]) -> str:
    confidence = str(row.get("data_confidence") or "").upper().strip()
    close_source = str(row.get("close_source") or row.get("sync_source") or "").lower()
    strategy = str(row.get("strategy") or row.get("setup_strategy") or "").lower()
    close_reason = str(row.get("closed_reason") or row.get("close_reason") or "").lower()

    confidence_aliases = {
        "STRATEGY_TRUTH_VALIDATED": "STRATEGY_TRUTH",
        "VALIDATED_POSITION_CLOSE": "STRATEGY_TRUTH",
        "EXCHANGE_TRUTH_CLOSE": "EXCHANGE_TRUTH",
    }
    if confidence:
        return confidence_aliases.get(confidence, confidence)

    if close_source == "bitget_order_history":
        return "EXCHANGE_TRUTH"

    exchange_truth_pnl = row.get("exchange_truth_pnl")
    process_verdict = str(row.get("process_verdict") or "").upper().strip()

    if process_verdict == "EXCHANGE_TRUTH_CLOSE" and exchange_truth_pnl in (None, ""):
        return "LOW_CONFIDENCE"

    event_type = str(row.get("event_type") or row.get("event") or "").upper()
    sync_source = str(row.get("sync_source") or row.get("source") or "").lower()

    strategy_truth_markers = {
        "low_vol_reclaim",
        "liquidity_sweep_reversal",
        "momentum_breakout",
        "momentum_breakdown",
        "trend_continuation",
        "adaptive_momentum_continuation",
    }

    recovery_blob = "|".join([close_source, strategy, close_reason, event_type.lower(), sync_source])
    recovery_markers_for_truth = (
        "recovered_",
        "recovery",
        "reconciliation",
        "closed_sync",
        # "exchange_position_closed_sync",  # removed as requested
        # "closed_synced",  # removed as requested
        "no_position_to_close",
        "legacy_close",
        "unlinked",
        "manual_sync",
        "state_recovery",
        "closed_state_dataset_backfill",
        "position_manager_guaranteed_close",
    )

    if (
        strategy in strategy_truth_markers
        and event_type in {"POSITION_CLOSED", "CLOSE"}
        and not any(marker in recovery_blob for marker in recovery_markers_for_truth)
    ):
        return "STRATEGY_TRUTH"

    low_confidence_markers = (
        "closed_state_dataset_backfill",
        # "exchange_position_closed_sync",  # removed as requested
        "position_manager_guaranteed_close",
        "recovered_",
        "recovery",
        # "closed_synced",  # removed as requested
        "manual_sync",
        "state_recovery",
        "no_position_to_close",
    )
    blob = "|".join([close_source, strategy, close_reason])
    if any(marker in blob for marker in low_confidence_markers):
        return "LOW_CONFIDENCE"

    return "UNKNOWN"


def _is_exchange_truth_trade(row: dict[str, Any]) -> bool:
    return _data_confidence(row) == "EXCHANGE_TRUTH"


def _expectancy_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    wins = 0
    losses = 0
    breakeven = 0
    total_pnl = 0.0
    winning_trades = []
    losing_trades = []
    tp1_tracked = 0
    tp1_hits = 0

    for row in rows:
        pnl = _trade_pnl(row)
        # PnL source is exchange/net-first via _trade_pnl; wins/losses are net-after-fees whenever Bitget truth is available.
        total_pnl += pnl
        if pnl > 0:
            wins += 1
            winning_trades.append(pnl)
        elif pnl < 0:
            losses += 1
            losing_trades.append(pnl)
        else:
            breakeven += 1

        raw_tp1 = str(row.get("tp1_hit") or "").strip().lower()
        if raw_tp1 in {"true", "1", "yes", "hit"}:
            tp1_tracked += 1
            tp1_hits += 1
        elif raw_tp1 in {"false", "0", "no"}:
            tp1_tracked += 1

    total_trades = wins + losses + breakeven
    gross_profit = sum(winning_trades)
    gross_loss = abs(sum(losing_trades))
    avg_win = gross_profit / len(winning_trades) if winning_trades else 0.0
    avg_loss = gross_loss / len(losing_trades) if losing_trades else 0.0
    largest_win = max(winning_trades) if winning_trades else 0.0
    largest_loss = abs(min(losing_trades)) if losing_trades else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0

    return {
        "trades": total_trades,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "total_pnl": round(total_pnl, 4),
        "expectancy": round(total_pnl / total_trades, 4) if total_trades else 0.0,
        "winrate": round(wins / total_trades, 4) if total_trades else 0.0,
        # None (not 0.0) when no row carries tp1 tracking, so downstream gates
        # can distinguish "missing data" from a genuine 0% hit-rate.
        "tp1_hit_rate": round(tp1_hits / tp1_tracked, 4) if tp1_tracked else None,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "largest_win": round(largest_win, 4),
        "largest_loss": round(largest_loss, 4),
        "gross_profit": round(gross_profit, 4),
        "gross_loss": round(gross_loss, 4),
        "profit_factor": round(profit_factor, 4),
    }


# Helper functions for agent log analysis
def _read_agent_log_tail(max_lines: int = 25000) -> list[str]:
    path = Path("logs/agent.log")
    if not path.exists():
        return []

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            return [line.strip() for line in lines[-max_lines:] if line.strip()]
    except Exception:
        return []


def _count_log_events(lines: list[str], marker: str) -> int:
    marker_upper = marker.upper()
    return sum(1 for line in lines if marker_upper in line.upper())


# Helper to clean up noisy reject reasons for analytics
def _clean_reject_reason(reason: str) -> str | None:
    cleaned = str(reason or "").strip()
    if not cleaned:
        return None

    lower = cleaned.lower()
    noisy_prefixes = (
        "primary_trend=",
        "confirmation_trend=",
        "alignment=",
        "volume_ratio=",
        "score_hint=",
        "volatility_rank=",
        "pressure_score=",
        "expansion_prob=",
        "entry_quality=",
        "spread=",
        "spread ",
        "notes=",
        "verdict=",
        "score=",
        "trigger_quality=",
        "trend_alignment=",
        "volume_confirmation=",
        "htf_alignment=",
        "mtf_override=",
        "risk_score=",
        "quality_score=",
        "process_verdict=",
        "trade_grade=",
        "expectancy_label=",
        "net_pnl=",
        "pnl=",
        "fees=",
        "slippage_pct=",
        "fee_leakage_pct=",
    )
    if lower.startswith(noisy_prefixes):
        return None

    noisy_contains = (
        "risk gate passed",
        "verdict=go",
        "process_verdict=",
        "trade_grade=",
        "expectancy_label=",
        "mtf_override=true",
        "mtf_override=false",
    )
    if any(item in lower for item in noisy_contains):
        return None

    noisy_exact = {
        "true",
        "false",
        "none",
        "unknown",
        "aligned_bullish",
        "aligned_bearish",
        "conflicted",
        "mixed",
        "bullish",
        "bearish",
        "neutral",
    }
    if lower in noisy_exact:
        return None

    useful_markers = (
        "blocked",
        "reject",
        "rejected",
        "too weak",
        "too high",
        "too low",
        "insufficient",
        "no candidates",
        "no a+ candidates",
        "bad alignment",
        "cooldown",
        "watch",
        "pause",
        "failed",
        "missing",
        "wait for",
        "not enough",
        "lacks",
        "weak",
    )
    if "=" in lower and not any(marker in lower for marker in useful_markers):
        return None

    return cleaned[:180]


def _top_log_symbols(lines: list[str], marker: str, limit: int = 10) -> dict[str, int]:
    counts: dict[str, int] = {}
    marker_upper = marker.upper()

    for line in lines:
        if marker_upper not in line.upper():
            continue

        symbol = "UNKNOWN"
        parts = line.replace("|", " ").split()
        for part in parts:
            cleaned = part.strip().upper().strip(",;:")
            if cleaned.endswith("USDT"):
                symbol = cleaned
                break

        counts[symbol] = counts.get(symbol, 0) + 1

    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit])


def _top_near_missing(lines: list[str], limit: int = 12) -> dict[str, int]:
    counts: dict[str, int] = {}

    for line in lines:
        upper = line.upper()
        if "NEAR_EXECUTABLE" not in upper and "NEAR_RISK_BLOCKED" not in upper and "NEAR_SELECTOR_CANDIDATE" not in upper:
            continue

        if "missing=" in line:
            raw = line.split("missing=", 1)[1].split("|", 1)[0].strip()
            for item in raw.split(","):
                reason = item.strip() or "unknown"
                counts[reason] = counts.get(reason, 0) + 1
        elif "reason=" in line:
            raw = line.split("reason=", 1)[1].split("|", 1)[0].strip()
            counts[raw or "unknown"] = counts.get(raw or "unknown", 0) + 1
        elif "reasons=" in line:
            raw = line.split("reasons=", 1)[1].split("|", 1)[0].strip()
            counts[raw or "unknown"] = counts.get(raw or "unknown", 0) + 1
        else:
            counts["unknown"] = counts.get("unknown", 0) + 1

    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit])


def _build_daily_validation() -> dict[str, Any]:
    trade_rows = _load_csv_rows(TRADE_DATASET_PATH)
    strategy_rows = _load_csv_rows(STRATEGY_DATASET_PATH)
    agent_lines = _read_agent_log_tail()

    executable = 0
    blocked = 0
    accepted = 0
    rejected = 0
    selected_candidate_events = _count_log_events(agent_lines, "selected_candidate")
    accepted_setup_events = _count_log_events(agent_lines, "ACCEPTED_SETUP")
    risk_rejected_events = _count_log_events(agent_lines, "RISK_REJECTED")
    plan_reject_events = _count_log_events(agent_lines, "PLAN_REJECT")
    plan_accepted_events = _count_log_events(agent_lines, "PLAN_ACCEPTED")
    execution_skipped_events = _count_log_events(agent_lines, "EXECUTION_SKIPPED")
    selector_reject_events = _count_log_events(agent_lines, "SELECTOR_REJECT_INTELLIGENCE")

    reject_reasons: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}

    for row in strategy_rows:
        status = str(row.get("status") or row.get("verdict") or "").upper()
        strategy = str(row.get("strategy") or "unknown")
        # reason = str(row.get("blocked_reason") or row.get("reason") or "unknown")

        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

        if "EXECUTABLE" in status:
            executable += 1
        elif "BLOCKED" in status:
            blocked += 1
        elif "ACCEPT" in status:
            accepted += 1
        elif "REJECT" in status:
            rejected += 1

        if "REJECT" in status or "BLOCKED" in status or str(row.get("stage") or "").upper() in {"SCAN_REJECT", "PLAN_REJECT"}:
            for reason in _extract_reasons(row):
                clean_reason = _clean_reject_reason(reason)
                if not clean_reason:
                    continue
                reject_reasons[clean_reason] = reject_reasons.get(clean_reason, 0) + 1

    closed_trade_rows = [row for row in trade_rows if _is_closed_trade(row)]
    exchange_truth_trade_rows = [row for row in closed_trade_rows if _is_exchange_truth_trade(row) and not _is_recovery_trade(row)]
    strategy_truth_trade_rows = [
        row for row in closed_trade_rows
        if _data_confidence(row) == "STRATEGY_TRUTH" and not _is_recovery_trade(row)
    ]
    low_confidence_trade_rows = [row for row in closed_trade_rows if _data_confidence(row) in {"LOW_CONFIDENCE", "UNKNOWN"}]
    recovery_trade_rows = [row for row in closed_trade_rows if _is_recovery_trade(row) or _data_confidence(row) in {"LOW_CONFIDENCE", "UNKNOWN"}]
    strategy_trade_rows = []

    for row in exchange_truth_trade_rows + strategy_truth_trade_rows:
        if (
            str(row.get("process_verdict") or "").upper().strip() == "EXCHANGE_TRUTH_CLOSE"
            and row.get("exchange_truth_pnl") in (None, "")
        ):
            continue
        strategy_trade_rows.append(row)

    total_stats = _expectancy_stats(closed_trade_rows)
    strategy_stats = _expectancy_stats(strategy_trade_rows)
    recovery_stats = _expectancy_stats(recovery_trade_rows)

    # Build daily learning report
    learning_report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pnl_truth_mode": "exchange_net_pnl_first_fees_included",
        "daily_trade_count": strategy_stats["trades"],
        "daily_winrate": strategy_stats["winrate"],
        "daily_avg_win": strategy_stats["avg_win"],
        "daily_avg_loss": strategy_stats["avg_loss"],
        "daily_profit_factor": strategy_stats["profit_factor"],
        "daily_total_net_pnl": strategy_stats["total_pnl"],
        "largest_win": strategy_stats["largest_win"],
        "largest_loss": strategy_stats["largest_loss"],
        "avg_win_loss_ratio": round(
            (strategy_stats["avg_win"] / strategy_stats["avg_loss"])
            if float(strategy_stats["avg_loss"] or 0.0) > 0
            else 0.0,
            4,
        ),
        "avg_win_vs_avg_loss_verdict": (
            "PASS"
            if float(strategy_stats["avg_win"] or 0.0) >= float(strategy_stats["avg_loss"] or 0.0)
            and int(strategy_stats["trades"] or 0) > 0
            else "FAIL"
        ),
        "strategy_trade_count": strategy_stats["trades"],
        "strategy_expectancy": strategy_stats["expectancy"],
        "recovery_trade_count": recovery_stats["trades"],
        "recovery_expectancy": recovery_stats["expectancy"],
        "exchange_truth_trade_count": len(exchange_truth_trade_rows),
        "strategy_truth_trade_count": len(strategy_truth_trade_rows),
        "low_confidence_trade_count": len(low_confidence_trade_rows),
        "exchange_truth_missing_pnl_count": sum(
            1
            for row in closed_trade_rows
            if str(row.get("process_verdict") or "").upper().strip() == "EXCHANGE_TRUTH_CLOSE"
            and row.get("exchange_truth_pnl") in (None, "")
        ),
        "data_confidence_verdict": (
            "TRUSTED"
            if len(strategy_trade_rows) > 0
            and len(recovery_trade_rows) <= len(strategy_trade_rows)
            and sum(
                1
                for row in closed_trade_rows
                if str(row.get("process_verdict") or "").upper().strip() == "EXCHANGE_TRUTH_CLOSE"
                and row.get("exchange_truth_pnl") in (None, "")
            ) == 0
            else "LOW_CONFIDENCE"
        ),
        "learning_verdict": (
            "BOT_HEALTHY_NO_CHANGE"
            if int(strategy_stats["trades"] or 0) > 0
            and float(strategy_stats["avg_win"] or 0.0) >= float(strategy_stats["avg_loss"] or 0.0)
            and float(strategy_stats["profit_factor"] or 0.0) >= 1.20
            and len(recovery_trade_rows) <= len(strategy_trade_rows)
            else "DATA_NOT_TRUSTWORTHY_OR_EXPECTANCY_WEAK"
        ),
    }

    wins = int(total_stats["wins"])
    losses = int(total_stats["losses"])
    breakeven = int(total_stats["breakeven"])
    total_pnl = float(total_stats["total_pnl"])
    expectancy = float(total_stats["expectancy"])

    debug_path = LATEST_SUMMARY_PATH
    latest_debug: dict[str, Any] = {}
    if debug_path.exists():
        try:
            with open(debug_path, "r", encoding="utf-8") as f:
                latest_debug = (json.load(f) or {}).get("debug", {}) or {}
        except Exception:
            latest_debug = {}

    # Write learning report to disk
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        DAILY_LEARNING_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

        with open(DAILY_LEARNING_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(learning_report, f, indent=2)

        report_stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_UTC")
        history_path = DAILY_LEARNING_HISTORY_DIR / f"{report_stamp}_daily_learning_report.json"
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(learning_report, f, indent=2)
    except Exception:
        pass

    if accepted == 0:
        accepted = max(
            accepted_setup_events,
            plan_accepted_events,
            int(latest_debug.get("risk_allowed", 0) or 0),
            int(latest_debug.get("simulated_trade", 0) or 0),
        )

    if rejected == 0:
        rejected = max(
            risk_rejected_events + plan_reject_events + execution_skipped_events + selector_reject_events,
            int(latest_debug.get("risk_rejected", 0) or 0)
            + int(latest_debug.get("selector_rejected", 0) or 0)
            + int(latest_debug.get("plan_rejected", 0) or 0),
        )

    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "strategy_rows": len(strategy_rows),
        "trade_rows": len(trade_rows),
        "closed_trade_rows": len(closed_trade_rows),
        "strategy_closed_trade_rows": len(strategy_trade_rows),
        "recovery_closed_trade_rows": len(recovery_trade_rows),
        "exchange_truth_closed_trade_rows": len(exchange_truth_trade_rows),
        "strategy_truth_closed_trade_rows": len(strategy_truth_trade_rows),
        "low_confidence_closed_trade_rows": len(low_confidence_trade_rows),
        "executable": executable,
        "blocked": blocked,
        "accepted": accepted,
        "rejected": rejected,
        "accepted_sources": {
            "strategy_dataset_accepted": accepted,
            "accepted_setup_log_events": accepted_setup_events,
            "plan_accepted_log_events": plan_accepted_events,
            "latest_debug_risk_allowed": int(latest_debug.get("risk_allowed", 0) or 0),
            "latest_debug_simulated_trade": int(latest_debug.get("simulated_trade", 0) or 0),
        },
        "rejected_sources": {
            "strategy_dataset_rejected": rejected,
            "risk_rejected_log_events": risk_rejected_events,
            "plan_reject_log_events": plan_reject_events,
            "execution_skipped_log_events": execution_skipped_events,
            "selector_reject_log_events": selector_reject_events,
            "latest_debug_risk_rejected": int(latest_debug.get("risk_rejected", 0) or 0),
            "latest_debug_selector_rejected": int(latest_debug.get("selector_rejected", 0) or 0),
            "latest_debug_plan_rejected": int(latest_debug.get("plan_rejected", 0) or 0),
        },
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "total_pnl": round(total_pnl, 4),
        "expectancy": expectancy,
        "avg_win": total_stats["avg_win"],
        "avg_loss": total_stats["avg_loss"],
        "largest_win": total_stats["largest_win"],
        "largest_loss": total_stats["largest_loss"],
        "gross_profit": total_stats["gross_profit"],
        "gross_loss": total_stats["gross_loss"],
        "profit_factor": total_stats["profit_factor"],
        "strategy_total_pnl": strategy_stats["total_pnl"],
        "strategy_expectancy_clean": strategy_stats["expectancy"],
        "strategy_winrate_clean": strategy_stats["winrate"],
        "recovery_total_pnl": recovery_stats["total_pnl"],
        "recovery_expectancy": recovery_stats["expectancy"],
        "recovery_winrate": recovery_stats["winrate"],
        "exchange_truth_total_pnl": _expectancy_stats(exchange_truth_trade_rows)["total_pnl"],
        "exchange_truth_expectancy": _expectancy_stats(exchange_truth_trade_rows)["expectancy"],
        "exchange_truth_winrate": _expectancy_stats(exchange_truth_trade_rows)["winrate"],
        "near_executable_events": _count_log_events(agent_lines, "NEAR_EXECUTABLE"),
        "near_selector_events": _count_log_events(agent_lines, "NEAR_SELECTOR_CANDIDATE"),
        "near_risk_blocked_events": _count_log_events(agent_lines, "NEAR_RISK_BLOCKED"),
        "mtf_override_events": (
            _count_log_events(agent_lines, "MTF_PREARMED_OVERRIDE")
            + _count_log_events(agent_lines, "LIQUIDITY_SWEEP_MTF_OVERRIDE")
            + _count_log_events(agent_lines, "CONTINUATION_MTF_OVERRIDE")
            + _count_log_events(agent_lines, "LOW_VOL_RECLAIM_MTF_OVERRIDE")
        ),
        "top_near_executable_symbols": _top_log_symbols(agent_lines, "NEAR_EXECUTABLE"),
        "top_near_selector_symbols": _top_log_symbols(agent_lines, "NEAR_SELECTOR_CANDIDATE"),
        "top_near_risk_symbols": _top_log_symbols(agent_lines, "NEAR_RISK_BLOCKED"),
        "top_near_missing_reasons": _top_near_missing(agent_lines),
        "top_reject_reasons": dict(sorted(reject_reasons.items(), key=lambda x: x[1], reverse=True)[:15]),
        "strategy_activity": dict(sorted(strategy_counts.items(), key=lambda x: x[1], reverse=True)),
    }


EXPECTANCY_WINDOW_DAYS = 30


def _trade_close_timestamp(trade: dict[str, Any]) -> str:
    return str(trade.get("closed_at") or trade.get("timestamp") or trade.get("created_at") or "")


def _build_strategy_expectancy(trades: list[dict[str, Any]], window_days: int = EXPECTANCY_WINDOW_DAYS) -> dict[str, Any]:
    strategy_buckets: dict[str, list[dict[str, Any]]] = {}
    recovery_buckets: dict[str, list[dict[str, Any]]] = {}

    # Rolling window: gate live strategies on *recent* behavior. All-time
    # expectancy both hides fresh degradation behind old profits and blocks a
    # fixed strategy forever behind old losses; a window lets bad history age
    # out so a strategy can re-qualify (trades < 5 in window -> WATCH probe).
    window_cutoff = ""
    if window_days > 0:
        window_cutoff = (
            datetime.now(timezone.utc) - timedelta(days=window_days)
        ).isoformat(timespec="seconds")

    for trade in trades:
        if not _is_closed_trade(trade):
            continue

        if window_cutoff and _trade_close_timestamp(trade)[:19] < window_cutoff[:19]:
            continue

        strategy = str(trade.get("strategy") or trade.get("setup_strategy") or "unknown")
        confidence = _data_confidence(trade)
        if _is_recovery_trade(trade) or confidence in {"LOW_CONFIDENCE", "UNKNOWN"}:
            recovery_buckets.setdefault(strategy, []).append(trade)
        elif confidence in {"EXCHANGE_TRUTH", "STRATEGY_TRUTH"}:
            strategy_buckets.setdefault(strategy, []).append(trade)
        else:
            recovery_buckets.setdefault(strategy, []).append(trade)

    output: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expectancy_window_days": window_days,
        "strategies": {},
        "recovery_events": {},
        "summary": {
            "strategy_trades": sum(len(rows) for rows in strategy_buckets.values()),
            "recovery_trades": sum(len(rows) for rows in recovery_buckets.values()),
        },
    }

    def _bucket_payload(rows: list[dict[str, Any]]) -> dict[str, Any]:
        stats = _expectancy_stats(rows)
        expectancy = float(stats["expectancy"])

        if expectancy > 0.25:
            status = "GOOD"
        elif expectancy > 0.0:
            status = "WATCH"
        else:
            status = "PAUSE"

        return {
            "trades": stats["trades"],
            "wins": stats["wins"],
            "losses": stats["losses"],
            "breakeven": stats["breakeven"],
            "winrate": stats["winrate"],
            "tp1_hit_rate": stats["tp1_hit_rate"],
            "total_pnl": stats["total_pnl"],
            "expectancy": stats["expectancy"],
            "avg_win": stats["avg_win"],
            "avg_loss": stats["avg_loss"],
            "largest_win": stats["largest_win"],
            "largest_loss": stats["largest_loss"],
            "profit_factor": stats["profit_factor"],
            "status": status,
        }

    for strategy, rows in strategy_buckets.items():
        output["strategies"][strategy] = _bucket_payload(rows)

    for strategy, rows in recovery_buckets.items():
        payload = _bucket_payload(rows)
        payload["status"] = "RECOVERY_ONLY"
        output["recovery_events"][strategy] = payload

    output["summary"]["strategy"] = _expectancy_stats([row for rows in strategy_buckets.values() for row in rows])
    output["summary"]["recovery"] = _expectancy_stats([row for rows in recovery_buckets.values() for row in rows])
    output["summary"]["all_closed"] = _expectancy_stats([row for rows in list(strategy_buckets.values()) + list(recovery_buckets.values()) for row in rows])

    return output


def main() -> None:
    args = _parse_args()
    symbol_filter = _parse_symbol_filter(args.symbols)
    _configure_report_paths(Path(args.out_dir) if args.out_dir else None)

    settings = get_settings()

    daily_validation: dict[str, Any] = {}
    strategy_expectancy: dict[str, Any] = {}

    if not args.backtest_only:
        daily_validation = _build_daily_validation()
        live_trade_rows = _load_csv_rows(TRADE_DATASET_PATH)
        strategy_expectancy = _build_strategy_expectancy(live_trade_rows)

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        with open(DAILY_VALIDATION_PATH, "w", encoding="utf-8") as f:
            json.dump(daily_validation, f, indent=2)
        with open(STRATEGY_EXPECTANCY_PATH, "w", encoding="utf-8") as f:
            json.dump(strategy_expectancy, f, indent=2)

    if args.validation_only:
        print("Validation complete")
        print(f"Daily validation saved to: {DAILY_VALIDATION_PATH}")
        print(f"Strategy expectancy saved to: {STRATEGY_EXPECTANCY_PATH}")
        return

    engine = BacktestEngine(settings=settings)
    market_data = load_market_data(symbol_filter=symbol_filter)
    if not market_data:
        if args.backtest_only:
            print("No backtest candle data found; no reports written.")
        else:
            print("No backtest candle data found; wrote live validation reports only.")
            print(f"Daily validation saved to: {DAILY_VALIDATION_PATH}")
            print(f"Strategy expectancy saved to: {STRATEGY_EXPECTANCY_PATH}")
        print("Put candle JSON files in: data/backtests/")
        print("Example filename: ADAUSDT.json")
        return

    result = engine.run(market_data)
    result["run_config"] = {
        "backtest_only": bool(args.backtest_only),
        "validation_only": bool(args.validation_only),
        "symbols": sorted(symbol_filter),
        "days": int(args.days or 0),
        "out_dir": str(REPORT_DIR),
    }

    trades = result.get("trade_history", []) or []

    strategy_direction_expectancy: dict[str, dict[str, float]] = {}
    strategy_regime_expectancy: dict[str, dict[str, float]] = {}
    symbol_direction_expectancy: dict[str, dict[str, float]] = {}

    def _bucket_avg(bucket: list[float]) -> float:
        if not bucket:
            return 0.0
        return round(sum(bucket) / len(bucket), 4)

    strategy_direction_buckets: dict[str, dict[str, list[float]]] = {}
    strategy_regime_buckets: dict[str, dict[str, list[float]]] = {}
    symbol_direction_buckets: dict[str, dict[str, list[float]]] = {}

    for trade in trades:
        pnl = _safe_float(trade.get("pnl_pct"), 0.0)
        strategy = str(trade.get("strategy") or "unknown")
        direction = str(trade.get("direction") or "unknown")
        regime = str(trade.get("market_regime") or "unknown")
        symbol = str(trade.get("symbol") or "unknown")

        strategy_direction_buckets.setdefault(strategy, {}).setdefault(direction, []).append(pnl)
        strategy_regime_buckets.setdefault(strategy, {}).setdefault(regime, []).append(pnl)
        symbol_direction_buckets.setdefault(symbol, {}).setdefault(direction, []).append(pnl)

    for strategy, direction_map in strategy_direction_buckets.items():
        strategy_direction_expectancy[strategy] = {
            direction: _bucket_avg(values)
            for direction, values in direction_map.items()
        }

    for strategy, regime_map in strategy_regime_buckets.items():
        strategy_regime_expectancy[strategy] = {
            regime: _bucket_avg(values)
            for regime, values in regime_map.items()
        }

    for symbol, direction_map in symbol_direction_buckets.items():
        symbol_direction_expectancy[symbol] = {
            direction: _bucket_avg(values)
            for direction, values in direction_map.items()
        }

    if strategy_direction_expectancy:
        result["strategy_direction_expectancy"] = strategy_direction_expectancy
    if strategy_regime_expectancy:
        result["strategy_regime_expectancy"] = strategy_regime_expectancy
    if symbol_direction_expectancy:
        result["symbol_direction_expectancy"] = symbol_direction_expectancy

    if not args.backtest_only:
        result["daily_validation"] = daily_validation
        result["strategy_expectancy"] = strategy_expectancy

    report_paths = _write_reports(result)

    print("Backtest complete")
    print(json.dumps(result, indent=2))
    print(f"Latest report saved to: {report_paths['latest_report']}")
    print(f"Latest summary saved to: {report_paths['latest_summary']}")
    if not args.backtest_only:
        print(f"Daily validation saved to: {DAILY_VALIDATION_PATH}")
        print(f"Strategy expectancy saved to: {STRATEGY_EXPECTANCY_PATH}")
    print(f"Timestamped report saved to: {report_paths['timestamped_report']}")
    print(f"Timestamped summary saved to: {report_paths['timestamped_summary']}")


if __name__ == "__main__":
    main()
