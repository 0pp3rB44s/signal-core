
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

BASE_PATH = Path(__file__).resolve().parents[1]
if str(BASE_PATH) not in sys.path:
    sys.path.insert(0, str(BASE_PATH))

from execution.execution_service import _safe_float
from telemetry.csv_rotation import rotated_segments
from telemetry.trade_logger import _safe_bool

LOGS_PATH = BASE_PATH / "logs"
STATE_PATH = BASE_PATH / "state"
DATA_STORE = BASE_PATH / "data_store"
REPORTS_PATH = BASE_PATH / "reports"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_csv(path: Path) -> list[dict[str, Any]]:
    """Read `path`, concatenating any rotated backups (oldest-first) so history survives rotation."""
    rows: list[dict[str, Any]] = []
    for segment in rotated_segments(path):
        try:
            with segment.open("r", encoding="utf-8", newline="") as handle:
                rows.extend(csv.DictReader(handle))
        except Exception:
            continue
    return rows


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return default


# --- Helper: Parse NEAR_TP_SEEN log events and attach to dataset rows ---

def _parse_log_timestamp(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        local_dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
        return local_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return ""


def _read_near_tp_log_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    paths = sorted(LOGS_PATH.glob("agent.log*")) + sorted(LOGS_PATH.glob("bot.out*"))
    pattern = re.compile(
        r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*?\| POSITION\d+ \| (?P<symbol>[A-Z0-9]+USDT) \| .*?note=NEAR_TP_SEEN distance=(?P<distance>[-+]?\d+(?:\.\d+)?)% target=(?P<target>[-+]?\d+(?:\.\d+)?)"
    )

    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    match = pattern.search(line)
                    if not match:
                        continue
                    events.append(
                        {
                            "timestamp": _parse_log_timestamp(match.group("ts")),
                            "symbol": match.group("symbol").upper(),
                            "distance_to_tp_pct": _safe_float(match.group("distance"), 999.0),
                            "target": _safe_float(match.group("target"), 0.0),
                            "source_file": str(path),
                        }
                    )
        except Exception:
            continue

    events = [event for event in events if event.get("timestamp") and event.get("symbol")]
    events.sort(key=lambda event: event.get("timestamp", ""))
    return events


def _signed_time_distance_seconds(left: str, right: str) -> float:
    if not left or not right:
        return 999999999.0
    try:
        left_dt = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_dt = datetime.fromisoformat(right.replace("Z", "+00:00"))
        return (right_dt - left_dt).total_seconds()
    except ValueError:
        return 999999999.0


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    tmp.replace(path)


# Helper: normalize any record payload into list-of-dicts for dataset summary

def _as_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("data", "trades", "events", "positions"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
        return [payload]
    return []


# Helper: Compute trade dataset stats

def _trade_dataset_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    close_rows = [row for row in rows if str(row.get("event_type") or "").upper() == "CLOSE"]
    open_rows = [row for row in rows if str(row.get("event_type") or "").upper() == "OPEN"]
    real_close_rows = [
        row for row in close_rows
        if str(row.get("symbol") or "").upper() != "TESTUSDT"
        and str(row.get("data_confidence") or "").upper() != "TEST_ONLY"
    ]
    symbols = sorted({str(row.get("symbol") or "").upper() for row in real_close_rows if row.get("symbol")})
    return {
        "trade_dataset_rows_total": len(rows),
        "trade_dataset_open_rows": len(open_rows),
        "trade_dataset_close_rows": len(close_rows),
        "trade_dataset_real_close_rows": len(real_close_rows),
        "trade_dataset_real_symbols": symbols,
    }


# Attach NEAR_TP_SEEN log context to dataset rows
def _attach_near_tp_log_context(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = _read_near_tp_log_events()
    if not events:
        return rows

    events_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        events_by_symbol.setdefault(str(event.get("symbol") or "").upper(), []).append(event)

    for row in rows:
        if str(row.get("event_type") or "").upper() != "CLOSE":
            continue
        symbol = str(row.get("symbol") or "").upper()
        closed_at = str(row.get("closed_at") or row.get("timestamp") or "")
        symbol_events = events_by_symbol.get(symbol, [])
        if not symbol_events or not closed_at:
            continue

        best_event = None
        best_distance = 999999999.0
        for event in symbol_events:
            event_ts = str(event.get("timestamp") or "")
            distance = _signed_time_distance_seconds(event_ts, closed_at)
            if distance < 0:
                continue
            if distance < best_distance:
                best_event = event
                best_distance = distance

        if best_event is None or best_distance > 21600:
            continue

        existing_distance = _safe_float(row.get("min_distance_to_tp_pct"), 999.0)
        log_distance = _safe_float(best_event.get("distance_to_tp_pct"), 999.0)
        row["near_tp_seen"] = True
        row["near_tp_log_matched"] = True
        row["near_tp_log_match_distance_seconds"] = round(best_distance, 3)
        row["near_tp_distance_pct"] = log_distance
        row["min_distance_to_tp_pct"] = min(existing_distance, log_distance)
        if _safe_float(row.get("tp1"), 0.0) <= 0 and _safe_float(row.get("take_profit"), 0.0) <= 0:
            row["tp1"] = _safe_float(best_event.get("target"), 0.0)
        row["near_tp_target"] = _safe_float(best_event.get("target"), 0.0)
        row["near_tp_source_file"] = best_event.get("source_file", "")

    return rows

# --- Trade Autopsy helper ---
def _trade_autopsy_report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a process-first autopsy report from real closed trades.

    This report is analysis-only. It must not change execution behavior.
    It is designed to answer: did the bot lose because of bad entries,
    failed follow-through, near-TP reversals, fees, or profit giveback?
    """
    closed_rows = [
        row for row in rows
        if str(row.get("event_type") or "").upper() == "CLOSE"
        and str(row.get("symbol") or "").upper() != "TESTUSDT"
        and str(row.get("data_confidence") or "").upper() != "TEST_ONLY"
    ]

    autopsies: list[dict[str, Any]] = []
    verdict_counts: dict[str, int] = {}
    near_tp_count = 0
    giveback_count = 0
    good_entry_bad_exit_count = 0

    for row in closed_rows:
        symbol = str(row.get("symbol") or "").upper()
        direction = str(row.get("direction") or "").upper()
        strategy = str(row.get("strategy") or "")
        timestamp = str(row.get("closed_at") or row.get("timestamp") or "")

        entry = _safe_float(row.get("entry"))
        exit_price = _safe_float(row.get("exit"))
        stop_loss = _safe_float(row.get("stop_loss"))
        mfe_pct = _safe_float(row.get("max_favorable_excursion_pct"))
        mae_pct = _safe_float(row.get("max_adverse_excursion_pct"))
        trade_duration_seconds = _safe_float(row.get("trade_duration_seconds"), 0.0)
        time_to_first_green_seconds = _safe_float(row.get("time_to_first_green_seconds"), 0.0)
        time_to_first_red_seconds = _safe_float(row.get("time_to_first_red_seconds"), 0.0)
        time_to_mfe_seconds = _safe_float(row.get("time_to_mfe_seconds"), 0.0)
        time_to_mae_seconds = _safe_float(row.get("time_to_mae_seconds"), 0.0)
        time_to_near_tp_seconds = _safe_float(row.get("time_to_near_tp_seconds"), 0.0)
        immediate_adverse_move_pct = _safe_float(row.get("immediate_adverse_move_pct"), 0.0)
        first_5m_pnl = _safe_float(row.get("first_5m_pnl"), 0.0)
        first_3_candles_result = str(row.get("first_3_candles_result") or "")
        pnl = _safe_float(row.get("exchange_truth_pnl", row.get("net_pnl", row.get("pnl"))))
        fees = abs(_safe_float(row.get("exchange_truth_fee", row.get("fees"))))
        net_after_fee_estimate = pnl - fees if fees else pnl

        take_profit_raw = str(row.get("take_profits") or "")
        take_profits: list[float] = []
        for part in take_profit_raw.replace(",", "|").split("|"):
            value = _safe_float(part.strip())
            if value:
                take_profits.append(value)
        tp1 = take_profits[0] if take_profits else 0.0

        tp_distance_pct = 0.0
        if entry and tp1:
            tp_distance_pct = abs(tp1 - entry) / entry * 100

        tp_reach_pct = 0.0
        if tp_distance_pct > 0:
            tp_reach_pct = max(0.0, min(999.0, mfe_pct / tp_distance_pct * 100))

        min_distance_to_tp_pct = _safe_float(row.get("min_distance_to_tp_pct"), 999.0)
        near_tp_distance_pct = _safe_float(row.get("near_tp_distance_pct"), 999.0)
        near_tp_seen = _safe_bool(row.get("near_tp_seen"))

        near_tp_by_reach = 80.0 <= tp_reach_pct < 100.0
        deep_near_tp_by_reach = 90.0 <= tp_reach_pct < 100.0
        near_tp_by_distance = min(min_distance_to_tp_pct, near_tp_distance_pct) <= 0.18

        near_tp = near_tp_by_reach or near_tp_seen or near_tp_by_distance
        deep_near_tp = deep_near_tp_by_reach or min(min_distance_to_tp_pct, near_tp_distance_pct) <= 0.08
        tp1_hit = _safe_bool(row.get("tp1_hit")) or tp_reach_pct >= 100.0
        break_even_active = _safe_bool(row.get("break_even_active"))

        gave_back_profit = mfe_pct > 0.0 and pnl <= 0.0
        gave_back_after_near_tp = near_tp and pnl <= 0.0
        fee_flipped_trade = pnl > 0.0 and net_after_fee_estimate <= 0.0
        weak_follow_through = mfe_pct < 0.20 and not tp1_hit
        bad_entry = mae_pct <= -0.75 and mfe_pct < 0.20 and pnl < 0.0

        immediate_adverse = (
            (
                time_to_first_red_seconds > 0.0
                and (time_to_first_green_seconds <= 0.0 or time_to_first_red_seconds < time_to_first_green_seconds)
                and mae_pct <= -0.25
            )
            or immediate_adverse_move_pct <= -0.25
            or first_3_candles_result == "RED_START"
        )
        good_from_start = (
            (
                time_to_first_green_seconds > 0.0
                and (time_to_first_red_seconds <= 0.0 or time_to_first_green_seconds < time_to_first_red_seconds)
                and mfe_pct >= 0.20
            )
            or first_3_candles_result == "GREEN_START"
        )
        choppy_start = (
            (
                time_to_first_green_seconds > 0.0
                and time_to_first_red_seconds > 0.0
                and abs(time_to_first_green_seconds - time_to_first_red_seconds) <= 300.0
            )
            or first_3_candles_result == "MIXED_START"
        )

        if immediate_adverse:
            entry_acceptance_verdict = "IMMEDIATE_REJECTION"
        elif good_from_start:
            entry_acceptance_verdict = "GOOD_FROM_START"
        elif choppy_start:
            entry_acceptance_verdict = "CHOPPY_START"
        else:
            entry_acceptance_verdict = "UNKNOWN_START"

        if bad_entry:
            verdict = "BAD_ENTRY"
        elif gave_back_after_near_tp:
            verdict = "NEAR_TP_REVERSAL"
        elif gave_back_profit and mfe_pct >= 0.20:
            verdict = "GOOD_ENTRY_BAD_EXIT"
        elif weak_follow_through:
            verdict = "FAILED_TO_FOLLOW_THROUGH"
        elif fee_flipped_trade:
            verdict = "FEE_FLIPPED_SCRATCH"
        elif tp1_hit and pnl > 0.0:
            verdict = "CLEAN_WIN"
        elif pnl > 0.0:
            verdict = "SMALL_WIN"
        elif break_even_active and pnl >= 0.0:
            verdict = "PROTECTED_SCRATCH"
        else:
            verdict = "NORMAL_LOSS"

        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
        if near_tp:
            near_tp_count += 1
        if gave_back_profit:
            giveback_count += 1
        if verdict == "GOOD_ENTRY_BAD_EXIT":
            good_entry_bad_exit_count += 1

        autopsies.append(
            {
                "timestamp": timestamp,
                "symbol": symbol,
                "direction": direction,
                "strategy": strategy,
                "entry": entry,
                "exit": exit_price,
                "stop_loss": stop_loss,
                "tp1": tp1,
                "tp_distance_pct": round(tp_distance_pct, 5),
                "tp_reach_pct": round(tp_reach_pct, 2),
                "near_tp_seen": near_tp_seen,
                "min_distance_to_tp_pct": round(min_distance_to_tp_pct, 5) if min_distance_to_tp_pct < 999.0 else None,
                "near_tp_distance_pct": round(near_tp_distance_pct, 5) if near_tp_distance_pct < 999.0 else None,
                "near_tp_log_matched": _safe_bool(row.get("near_tp_log_matched")),
                "near_tp_log_match_distance_seconds": _safe_float(row.get("near_tp_log_match_distance_seconds"), 0.0),
                "near_tp_target": _safe_float(row.get("near_tp_target"), 0.0),
                "near_tp_source_file": row.get("near_tp_source_file", ""),
                "near_tp": near_tp,
                "deep_near_tp": deep_near_tp,
                "tp1_hit": tp1_hit,
                "mfe_pct": round(mfe_pct, 5),
                "mae_pct": round(mae_pct, 5),
                "trade_duration_seconds": round(trade_duration_seconds, 3),
                "time_to_first_green_seconds": round(time_to_first_green_seconds, 3),
                "time_to_first_red_seconds": round(time_to_first_red_seconds, 3),
                "time_to_mfe_seconds": round(time_to_mfe_seconds, 3),
                "time_to_mae_seconds": round(time_to_mae_seconds, 3),
                "time_to_near_tp_seconds": round(time_to_near_tp_seconds, 3),
                "immediate_adverse_move_pct": round(immediate_adverse_move_pct, 5),
                "first_5m_pnl": round(first_5m_pnl, 5),
                "first_3_candles_result": first_3_candles_result,
                "entry_acceptance_verdict": entry_acceptance_verdict,
                "immediate_adverse": immediate_adverse,
                "good_from_start": good_from_start,
                "choppy_start": choppy_start,
                "pnl": round(pnl, 8),
                "fees": round(fees, 8),
                "net_after_fee_estimate": round(net_after_fee_estimate, 8),
                "gave_back_profit": gave_back_profit,
                "gave_back_after_near_tp": gave_back_after_near_tp,
                "fee_flipped_trade": fee_flipped_trade,
                "break_even_active": break_even_active,
                "data_confidence": row.get("data_confidence", ""),
                "process_verdict": row.get("process_verdict", ""),
                "autopsy_verdict": verdict,
            }
        )

    total = len(autopsies)
    near_tp_rows = [row for row in autopsies if row["near_tp"]]
    deep_near_tp_rows = [row for row in autopsies if row["deep_near_tp"]]
    near_tp_loss_rows = [row for row in autopsies if row["near_tp"] and row["pnl"] <= 0.0]
    deep_near_tp_loss_rows = [row for row in autopsies if row["deep_near_tp"] and row["pnl"] <= 0.0]
    giveback_rows = [row for row in autopsies if row["gave_back_profit"]]
    near_tp_giveback_rows = [row for row in autopsies if row["near_tp"] and row["gave_back_profit"]]

    entry_acceptance_counts: dict[str, int] = {}
    for row in autopsies:
        entry_verdict = str(row.get("entry_acceptance_verdict") or "UNKNOWN_START")
        entry_acceptance_counts[entry_verdict] = entry_acceptance_counts.get(entry_verdict, 0) + 1

    timing_rows = [row for row in autopsies if _safe_float(row.get("trade_duration_seconds"), 0.0) > 0.0]

    def _avg_timing(field: str) -> float:
        values = [_safe_float(row.get(field), 0.0) for row in timing_rows]
        values = [value for value in values if value > 0.0]
        return round(sum(values) / max(1, len(values)), 3)

    def _avg_from_rows(items: list[dict[str, Any]], field: str) -> float:
        values = [_safe_float(row.get(field), 0.0) for row in items]
        values = [value for value in values if value > 0.0]
        return round(sum(values) / max(1, len(values)), 5)

    def _avg_giveback_after_near_tp() -> float:
        values: list[float] = []
        for row in near_tp_giveback_rows:
            mfe = _safe_float(row.get("mfe_pct"), 0.0)
            pnl_value = _safe_float(row.get("pnl"), 0.0)
            values.append(max(0.0, mfe - max(0.0, pnl_value)))
        return round(sum(values) / max(1, len(values)), 5)

    return {
        "generated_at": _now(),
        "source": "data_store/trades/latest_real_closed_trades.json + logs/trade_dataset_v2.csv",
        "trade_count": total,
        "summary": {
            "near_tp_count": near_tp_count,
            "near_tp_rate": round(near_tp_count / max(1, total), 4),
            "near_tp_loss_count": len(near_tp_loss_rows),
            "near_tp_loss_rate": round(len(near_tp_loss_rows) / max(1, total), 4),
            "near_tp_loss_rate_within_near_tp": round(len(near_tp_loss_rows) / max(1, len(near_tp_rows)), 4),
            "deep_near_tp_count": len(deep_near_tp_rows),
            "deep_near_tp_rate": round(len(deep_near_tp_rows) / max(1, total), 4),
            "deep_near_tp_loss_count": len(deep_near_tp_loss_rows),
            "deep_near_tp_loss_rate": round(len(deep_near_tp_loss_rows) / max(1, total), 4),
            "deep_near_tp_loss_rate_within_deep_near_tp": round(len(deep_near_tp_loss_rows) / max(1, len(deep_near_tp_rows)), 4),
            "avg_tp_reach_pct_before_loss": _avg_from_rows(near_tp_loss_rows, "tp_reach_pct"),
            "avg_time_to_near_tp_seconds_on_losses": _avg_from_rows(near_tp_loss_rows, "time_to_near_tp_seconds"),
            "avg_giveback_after_near_tp_pct": _avg_giveback_after_near_tp(),
            "near_tp_giveback_count": len(near_tp_giveback_rows),
            "near_tp_giveback_rate": round(len(near_tp_giveback_rows) / max(1, total), 4),
            "profit_giveback_count": giveback_count,
            "profit_giveback_rate": round(giveback_count / max(1, total), 4),
            "good_entry_bad_exit_count": good_entry_bad_exit_count,
            "good_entry_bad_exit_rate": round(good_entry_bad_exit_count / max(1, total), 4),
            "entry_acceptance_counts": entry_acceptance_counts,
            "avg_trade_duration_seconds": _avg_timing("trade_duration_seconds"),
            "avg_time_to_first_green_seconds": _avg_timing("time_to_first_green_seconds"),
            "avg_time_to_first_red_seconds": _avg_timing("time_to_first_red_seconds"),
            "avg_time_to_mfe_seconds": _avg_timing("time_to_mfe_seconds"),
            "avg_time_to_mae_seconds": _avg_timing("time_to_mae_seconds"),
            "avg_time_to_near_tp_seconds": _avg_timing("time_to_near_tp_seconds"),
            "verdict_counts": verdict_counts,
        },
        "near_tp_losses": near_tp_loss_rows[-100:],
        "deep_near_tp_losses": deep_near_tp_loss_rows[-100:],
        "near_tp_givebacks": near_tp_giveback_rows[-100:],
        "profit_givebacks": giveback_rows[-100:],
        "trades": autopsies[-500:],
    }



def _safe_float(value: Any, default: float = 0.0) -> float:
    if value in (None, ""):
        return default
    try:
        cleaned = str(value).replace("USDT", "").replace(",", "").strip()
        return float(cleaned)
    except (TypeError, ValueError):
        return default

# Helper for safe bool conversion
def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "hit", "on"}


def _parse_bitget_symbol_side(raw: Any) -> tuple[str, str]:
    text = str(raw or "").strip()
    parts = text.split()
    symbol = parts[0].strip().upper() if parts else ""
    direction = ""
    if "LONG" in text.upper():
        direction = "LONG"
    elif "SHORT" in text.upper():
        direction = "SHORT"
    return symbol, direction


def _parse_bitget_amount(raw: Any) -> float:
    text = str(raw or "").strip()
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else 0.0


def _parse_bitget_time(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        local_dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("Europe/Amsterdam"))
        return local_dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    except ValueError:
        return ""


def _read_bitget_position_history() -> list[dict[str, Any]]:
    raw_dir = DATA_STORE / "raw"
    if not raw_dir.exists():
        return []

    paths = sorted(raw_dir.glob("*position*history*.csv")) + sorted(raw_dir.glob("*Position*History*.csv"))
    rows: list[dict[str, Any]] = []

    for path in paths:
        try:
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                for raw_row in reader:
                    symbol, direction = _parse_bitget_symbol_side(raw_row.get("Futures"))
                    if not symbol:
                        continue
                    rows.append(
                        {
                            "symbol": symbol,
                            "direction": direction,
                            "opened_at_utc": _parse_bitget_time(raw_row.get("Opening time")),
                            "closed_at_utc": _parse_bitget_time(raw_row.get("Closed time")),
                            "entry": _safe_float(raw_row.get("Average entry price")),
                            "exit": _safe_float(raw_row.get("Average closing price")),
                            "size": _parse_bitget_amount(raw_row.get("Closed amount")),
                            "closed_value": _safe_float(raw_row.get("Closed value")),
                            "position_pnl": _safe_float(raw_row.get("Position Pnl")),
                            "realized_pnl": _safe_float(raw_row.get("Realized PnL")),
                            "fees": abs(_safe_float(raw_row.get("Fees")))
                            + abs(_safe_float(raw_row.get("Opening fee")))
                            + abs(_safe_float(raw_row.get("Closing fee"))),
                            "source_file": str(path),
                        }
                    )
        except Exception:
            continue
    return rows


def _time_distance_seconds(left: str, right: str) -> float:
    if not left or not right:
        return 999999999.0
    try:
        left_dt = datetime.fromisoformat(left.replace("Z", "+00:00"))
        right_dt = datetime.fromisoformat(right.replace("Z", "+00:00"))
        return abs((left_dt - right_dt).total_seconds())
    except ValueError:
        return 999999999.0


# Helper: check if a row already has exchange truth
def _already_has_exchange_truth(row: dict[str, Any]) -> bool:
    confidence = str(row.get("data_confidence") or "").upper()
    process_verdict = str(row.get("process_verdict") or "").upper()
    exchange_truth_pnl = row.get("exchange_truth_pnl")
    return (
        confidence == "EXCHANGE_TRUTH"
        and "EXCHANGE_TRUTH" in process_verdict
        and exchange_truth_pnl not in (None, "")
    )


def _enrich_with_bitget_position_history(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    bitget_rows = _read_bitget_position_history()
    if not bitget_rows:
        return rows, {"bitget_position_history_rows": 0, "bitget_position_history_matches": 0}

    used_indexes: set[int] = set()
    matches = 0
    existing_exchange_truth_rows = 0
    unmatched_close_rows: list[dict[str, Any]] = []

    for row in rows:
        if str(row.get("event_type") or "").upper() != "CLOSE":
            continue

        if _already_has_exchange_truth(row):
            existing_exchange_truth_rows += 1
            continue

        symbol = str(row.get("symbol") or "").upper().strip()
        direction = str(row.get("direction") or "").upper().strip()
        closed_at = str(row.get("closed_at") or row.get("timestamp") or "")

        best_index = None
        best_distance = 999999999.0
        for index, bitget_row in enumerate(bitget_rows):
            if index in used_indexes:
                continue
            if bitget_row.get("symbol") != symbol:
                continue
            if direction and bitget_row.get("direction") and bitget_row.get("direction") != direction:
                continue
            distance = _time_distance_seconds(closed_at, str(bitget_row.get("closed_at_utc") or ""))
            if distance < best_distance:
                best_index = index
                best_distance = distance

        if best_index is None or best_distance > 1800:
            unmatched_close_rows.append(
                {
                    "symbol": symbol,
                    "direction": direction,
                    "closed_at": closed_at,
                    "best_distance_seconds": round(best_distance, 3) if best_distance < 999999999.0 else None,
                    "reason": "no_bitget_match_within_30m",
                    "data_confidence": row.get("data_confidence", ""),
                    "process_verdict": row.get("process_verdict", ""),
                    "exchange_truth_pnl": row.get("exchange_truth_pnl", ""),
                }
            )
            continue

        bitget_row = bitget_rows[best_index]
        used_indexes.add(best_index)
        matches += 1

        row["data_confidence"] = "EXCHANGE_TRUTH"
        row["process_verdict"] = "BITGET_POSITION_HISTORY_MATCHED"
        row["sync_source"] = "bitget_position_history_import"
        row["exchange_truth_exit_price"] = bitget_row["exit"]
        row["exchange_truth_size"] = bitget_row["size"]
        row["exchange_truth_pnl"] = bitget_row["position_pnl"]
        row["exchange_truth_fee"] = bitget_row["fees"]
        row["fees"] = bitget_row["fees"]
        row["pnl"] = bitget_row["position_pnl"]
        row["net_pnl"] = bitget_row["position_pnl"]
        row["exit"] = bitget_row["exit"]
        row["position_size"] = bitget_row["size"]
        row["bitget_realized_pnl"] = bitget_row["realized_pnl"]
        row["bitget_closed_value"] = bitget_row["closed_value"]
        row["bitget_position_history_matched"] = True
        row["bitget_position_history_time_distance_seconds"] = round(best_distance, 3)

    total_exchange_truth_covered = existing_exchange_truth_rows + matches
    return rows, {
        "bitget_position_history_rows": len(bitget_rows),
        "bitget_position_history_matches": matches,
        "existing_exchange_truth_rows": existing_exchange_truth_rows,
        "total_exchange_truth_covered": total_exchange_truth_covered,
        "bitget_position_history_unmatched_close_rows": len(unmatched_close_rows),
        "bitget_position_history_match_rate": round(total_exchange_truth_covered / max(1, total_exchange_truth_covered + len(unmatched_close_rows)), 4),
        "unmatched_close_rows": unmatched_close_rows[-100:],
    }


def _daily_learning_report(rows: list[dict[str, Any]], trade_autopsy: dict[str, Any]) -> dict[str, Any]:
    closed_rows = [
        row for row in rows
        if str(row.get("event_type") or "").upper() == "CLOSE"
        and str(row.get("symbol") or "").upper() != "TESTUSDT"
        and str(row.get("data_confidence") or "").upper() != "TEST_ONLY"
    ]

    today = datetime.now(timezone.utc).date()
    today_rows: list[dict[str, Any]] = []
    for row in closed_rows:
        ts = str(row.get("closed_at") or row.get("timestamp") or "")
        try:
            row_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if row_dt.date() == today:
            today_rows.append(row)

    total_net_pnl = round(sum(_safe_float(row.get("exchange_truth_pnl", row.get("net_pnl", row.get("pnl")))) for row in today_rows), 8)
    wins = [row for row in today_rows if _safe_float(row.get("exchange_truth_pnl", row.get("net_pnl", row.get("pnl")))) > 0.0]
    losses = [row for row in today_rows if _safe_float(row.get("exchange_truth_pnl", row.get("net_pnl", row.get("pnl")))) < 0.0]

    consecutive_losses = 0
    for row in reversed(today_rows):
        pnl = _safe_float(row.get("exchange_truth_pnl", row.get("net_pnl", row.get("pnl"))))
        if pnl < 0.0:
            consecutive_losses += 1
            continue
        if pnl > 0.0:
            break

    consecutive_wins = 0
    for row in reversed(today_rows):
        pnl = _safe_float(row.get("exchange_truth_pnl", row.get("net_pnl", row.get("pnl"))))
        if pnl > 0.0:
            consecutive_wins += 1
            continue
        if pnl < 0.0:
            break

    gross_profit = sum(_safe_float(row.get("exchange_truth_pnl", row.get("net_pnl", row.get("pnl")))) for row in wins)
    gross_loss = abs(sum(_safe_float(row.get("exchange_truth_pnl", row.get("net_pnl", row.get("pnl")))) for row in losses))
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

    trade_count = len(today_rows)
    exchange_truth_rows = [row for row in today_rows if str(row.get("data_confidence") or "").upper() == "EXCHANGE_TRUTH"]
    data_confidence_verdict = "HIGH_CONFIDENCE" if trade_count >= 10 and len(exchange_truth_rows) / max(1, trade_count) >= 0.8 else "LOW_CONFIDENCE"

    return {
        "generated_at": _now(),
        "date_utc": today.isoformat(),
        "daily_trade_count": trade_count,
        "daily_wins": len(wins),
        "daily_losses": len(losses),
        "daily_winrate": round(len(wins) / max(1, trade_count), 4),
        "daily_total_net_pnl": total_net_pnl,
        "daily_profit_factor": profit_factor,
        "consecutive_losses": consecutive_losses,
        "consecutive_wins": consecutive_wins,
        "exchange_truth_rows": len(exchange_truth_rows),
        "data_confidence_verdict": data_confidence_verdict,
        "trade_autopsy_summary": trade_autopsy.get("summary", {}),
    }


# --- Trade Funnel Report ---
def _trade_funnel_report(decision_rows: list[dict[str, Any]]) -> dict[str, Any]:
    strategy_counts: dict[str, int] = {}
    symbol_counts: dict[str, int] = {}
    hard_blocks: dict[str, int] = {}
    plan_rejects: dict[str, int] = {}
    risk_blocks: dict[str, int] = {}
    rr_to_tp1_blocks: dict[str, int] = {}
    dns_errors = 0
    api_retryable_errors = 0
    live_entry_count = 0
    close_count = 0

    strategy_keys = ("strategy", "selected_strategy", "candidate_strategy", "setup_strategy", "strategy_name")
    symbol_keys = ("symbol", "pair", "market", "ticker")

    def _first_value(row: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = str(row.get(key) or "").strip()
            if value:
                return value
        return ""

    def _count_reason(bucket: dict[str, int], reason: str) -> None:
        cleaned = re.sub(r"\s+", " ", str(reason or "").strip())
        if not cleaned:
            return
        bucket[cleaned] = bucket.get(cleaned, 0) + 1

    for row in decision_rows:
        symbol = _first_value(row, symbol_keys).upper()
        strategy = _first_value(row, strategy_keys).lower()

        if not strategy or strategy.endswith("USDT"):
            strategy = "unknown"
        if not symbol and str(row.get("strategy") or "").upper().endswith("USDT"):
            symbol = str(row.get("strategy") or "").upper()

        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1
        if symbol:
            symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1

        text_fields = []
        for key, value in row.items():
            if value in (None, ""):
                continue
            text_fields.append(f"{key}={value}")
        reasons_blob = " | ".join(text_fields)
        upper_blob = reasons_blob.upper()

        if "HARD_BLOCK" in upper_blob:
            _count_reason(hard_blocks, reasons_blob[:500])

        if "PLAN_REJECT" in upper_blob or "PLAN_REJECTED" in upper_blob:
            _count_reason(plan_rejects, reasons_blob[:500])

        if (
            "RISK" in upper_blob
            or "RR_TO_TP1" in upper_blob
            or "LARGEST_LOSS_GUARD" in upper_blob
            or "DAY_DEFENSIVE" in upper_blob
            or "BLOCKED" in upper_blob
        ):
            _count_reason(risk_blocks, reasons_blob[:500])

        if "RR_TO_TP1" in upper_blob or "BELOW MINIMUM" in upper_blob:
            _count_reason(rr_to_tp1_blocks, reasons_blob[:500])

    log_paths = sorted(LOGS_PATH.glob("bot.out*")) + sorted(LOGS_PATH.glob("agent.log*"))
    log_patterns = {
        "hard": re.compile(r"(strategy weighting HARD_BLOCK[^\n]*|HARD_BLOCK[^\n]*)", re.IGNORECASE),
        "plan": re.compile(r"(PLAN_REJECT[^\n]*|MASTER_ENTRY_QUALITY_BLOCKED[^\n]*)", re.IGNORECASE),
        "risk": re.compile(r"(NEAR_RISK_BLOCKED[^\n]*|LARGEST_LOSS_GUARD[^\n]*|DAY_DEFENSIVE[^\n]*|risk_status=BLOCKED[^\n]*)", re.IGNORECASE),
        "rr": re.compile(r"(RR_TO_TP1[^\n]*|rr_to_tp1[^\n]*below minimum[^\n]*)", re.IGNORECASE),
    }

    for path in log_paths:
        try:
            with path.open("r", encoding="utf-8", errors="ignore") as handle:
                for line in handle:
                    upper_line = line.upper()
                    if "DNS" in upper_line or "RESOLUTION_FAILURE" in upper_line:
                        dns_errors += 1
                    if "BITGETRETRYABLEERROR" in upper_line or "RETRYABLE" in upper_line:
                        api_retryable_errors += 1
                    if "LIVE_ENTRY_FILLED" in upper_line or "POSITION_OPENED" in upper_line:
                        live_entry_count += 1
                    if "POSITION_CLOSED" in upper_line:
                        close_count += 1

                    for name, pattern in log_patterns.items():
                        match = pattern.search(line)
                        if not match:
                            continue
                        reason = match.group(1).strip()
                        if name == "hard":
                            _count_reason(hard_blocks, reason)
                        elif name == "plan":
                            _count_reason(plan_rejects, reason)
                        elif name == "risk":
                            _count_reason(risk_blocks, reason)
                        elif name == "rr":
                            _count_reason(rr_to_tp1_blocks, reason)
        except Exception:
            continue

    return {
        "generated_at": _now(),
        "decision_rows": len(decision_rows),
        "live_entry_count_from_logs": live_entry_count,
        "position_close_count_from_logs": close_count,
        "dns_error_count_from_logs": dns_errors,
        "api_retryable_error_count_from_logs": api_retryable_errors,
        "strategy_activity": dict(sorted(strategy_counts.items(), key=lambda x: x[1], reverse=True)),
        "symbol_activity": dict(sorted(symbol_counts.items(), key=lambda x: x[1], reverse=True)[:50]),
        "hard_blocks": dict(sorted(hard_blocks.items(), key=lambda x: x[1], reverse=True)[:50]),
        "plan_rejects": dict(sorted(plan_rejects.items(), key=lambda x: x[1], reverse=True)[:50]),
        "risk_blocks": dict(sorted(risk_blocks.items(), key=lambda x: x[1], reverse=True)[:50]),
        "rr_to_tp1_blocks": dict(sorted(rr_to_tp1_blocks.items(), key=lambda x: x[1], reverse=True)[:50]),
    }


# --- RR to TP1 Autopsy ---
from typing import Any

def _rr_to_tp1_autopsy(decision_rows: list[dict[str, Any]]) -> dict[str, Any]:
    blocked_rows: list[dict[str, Any]] = []
    strategy_counts: dict[str, int] = {}

    for row in decision_rows:
        blob = " | ".join(str(v) for v in row.values() if v not in (None, ""))
        upper_blob = blob.upper()

        if "RR_TO_TP1" not in upper_blob:
            continue

        strategy = str(
            row.get("strategy")
            or row.get("selected_strategy")
            or row.get("candidate_strategy")
            or "unknown"
        )

        symbol = str(row.get("symbol") or row.get("pair") or "")

        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

        blocked_rows.append({
            "symbol": symbol,
            "strategy": strategy,
            "score": row.get("score"),
            "rr": row.get("rr"),
            "rr_to_tp1": row.get("rr_to_tp1"),
            "tp1_move_bps": row.get("tp1_move_bps"),
            "minimum_tp1_move_bps": row.get("minimum_tp1_move_bps"),
            "reason": blob[:800],
        })

    return {
        "generated_at": _now(),
        "rr_to_tp1_block_count": len(blocked_rows),
        "strategy_breakdown": dict(sorted(strategy_counts.items(), key=lambda x: x[1], reverse=True)),
        "examples": blocked_rows[:200],
    }


def build_dataset() -> dict[str, Any]:
    DATA_STORE.mkdir(exist_ok=True)

    market_context = _read_csv(LOGS_PATH / "market_context.csv")
    trade_dataset_raw = _read_csv(LOGS_PATH / "trade_dataset_v2.csv")
    trade_dataset_raw = _attach_near_tp_log_context(trade_dataset_raw)
    trade_dataset, bitget_truth_stats = _enrich_with_bitget_position_history(trade_dataset_raw)
    decision_snapshots = _read_csv(LOGS_PATH / "trade_decision_snapshots.csv")

    executed_trades = _read_json(STATE_PATH / "executed_trades.json", [])
    execution_events = _read_json(STATE_PATH / "execution_events.json", [])
    position_events = _read_json(STATE_PATH / "position_events.json", [])

    executed_trade_records = _as_records(executed_trades)
    execution_event_records = _as_records(execution_events)
    position_event_records = _as_records(position_events)
    trade_stats = _trade_dataset_stats(trade_dataset)
    trade_autopsy = _trade_autopsy_report(trade_dataset)
    daily_learning = _daily_learning_report(trade_dataset, trade_autopsy)
    trade_funnel = _trade_funnel_report(decision_snapshots)
    rr_to_tp1_autopsy = _rr_to_tp1_autopsy(decision_snapshots)

    payload = {
        "generated_at": _now(),
        "source_files": {
            "market_context": str(LOGS_PATH / "market_context.csv"),
            "trade_dataset": str(LOGS_PATH / "trade_dataset_v2.csv"),
            "decision_snapshots": str(LOGS_PATH / "trade_decision_snapshots.csv"),
            "executed_trades": str(STATE_PATH / "executed_trades.json"),
            "execution_events": str(STATE_PATH / "execution_events.json"),
            "position_events": str(STATE_PATH / "position_events.json"),
        },
        "counts": {
            "market_context_rows": len(market_context),
            "trade_dataset_rows": len(trade_dataset),
            "trade_dataset_open_rows": trade_stats["trade_dataset_open_rows"],
            "trade_dataset_close_rows": trade_stats["trade_dataset_close_rows"],
            "trade_dataset_real_close_rows": trade_stats["trade_dataset_real_close_rows"],
            "decision_snapshot_rows": len(decision_snapshots),
            "executed_trades": len(executed_trade_records),
            "execution_events": len(execution_event_records),
            "position_events": len(position_event_records),
            "bitget_position_history_rows": bitget_truth_stats.get("bitget_position_history_rows", 0),
            "bitget_position_history_matches": bitget_truth_stats.get("bitget_position_history_matches", 0),
        },
        "data": {
            "market_context": market_context[-5000:],
            "trade_dataset": trade_dataset[-5000:],
            "decision_snapshots": decision_snapshots[-5000:],
            "executed_trades": executed_trade_records[-5000:],
            "execution_events": execution_event_records[-5000:],
            "position_events": position_event_records[-5000:],
        },
    }

    _write_json(DATA_STORE / "exports" / "latest_dataset_bundle.json", payload)
    _write_json(DATA_STORE / "trades" / "latest_trades.json", payload["data"]["trade_dataset"])
    _write_json(DATA_STORE / "trades" / "latest_real_closed_trades.json", [row for row in payload["data"]["trade_dataset"] if str(row.get("event_type") or "").upper() == "CLOSE" and str(row.get("symbol") or "").upper() != "TESTUSDT" and str(row.get("data_confidence") or "").upper() != "TEST_ONLY"])
    _write_json(DATA_STORE / "trades" / "unmatched_bitget_position_history_closes.json", bitget_truth_stats.get("unmatched_close_rows", []))
    _write_json(DATA_STORE / "trades" / "trade_autopsy_report.json", trade_autopsy)
    _write_json(REPORTS_PATH / "backtests" / "trade_autopsy_report.json", trade_autopsy)
    _write_json(DATA_STORE / "decisions" / "latest_decisions.json", payload["data"]["decision_snapshots"])
    _write_json(DATA_STORE / "raw" / "latest_market_context.json", payload["data"]["market_context"])
    _write_json(DATA_STORE / "trades" / "daily_learning_report.json", daily_learning)
    _write_json(DATA_STORE / "trades" / "trade_funnel_report.json", trade_funnel)
    _write_json(DATA_STORE / "trades" / "rr_to_tp1_autopsy.json", rr_to_tp1_autopsy)

    summary = {
        "generated_at": payload["generated_at"],
        "counts": payload["counts"],
        "trade_stats": trade_stats,
        "trade_autopsy_summary": trade_autopsy.get("summary", {}),
        "daily_learning_summary": daily_learning,
        "trade_funnel_summary": trade_funnel,
        "rr_to_tp1_autopsy_summary": rr_to_tp1_autopsy,
        "bitget_truth_stats": bitget_truth_stats,
        "exchange_truth_match_verdict": "PASS" if bitget_truth_stats.get("bitget_position_history_match_rate", 0.0) >= 0.95 else "NEEDS_REVIEW",
        "verdict": "OK" if payload["counts"]["market_context_rows"] > 0 else "NO_MARKET_CONTEXT",
    }
    _write_json(DATA_STORE / "backtests" / "dataset_summary.json", summary)

    return summary


if __name__ == "__main__":
    result = build_dataset()
    print(json.dumps(result, indent=2))