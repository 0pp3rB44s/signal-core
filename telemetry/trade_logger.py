import csv
import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


from clients.schemas import ExecutionReport, MarketSnapshot, PositionUpdate, StrategyCandidate, StrategyScore, TradePlan
from telemetry.close_record_sources import (
    ECONOMIC_CLOSE_EVENT_TYPES,
    PROVISIONAL_CLOSE_EVENT_TYPE,
)
from telemetry.csv_rotation import rotate_if_needed
from telemetry.safe_io import locked_open
from candidate_lifecycle import deterministic_plan_id
from execution.executor_identity import ExecutionIdentity


# --- Trade Quality/Grade helpers ---
class MoneyFieldError(TypeError):
    """A non-money value reached a USDT column."""


def _money_or_none(value, *, field: str, symbol: str = "") -> float | None:
    """Return USDT as a float, or None when the caller has no figure yet.

    The single place where "is this money?" is decided for the close datasets.
    ``None`` and ``""`` both mean *not known yet* — a provisional close — and
    leave the column empty. Anything else must be numeric; a value that is not
    is a caller bug (a ROI percentage or a formatted string reaching a money
    column) and raises instead of being coerced to 0.0, because a silent 0.0 is
    indistinguishable from a real break-even trade and would quietly corrupt the
    weekly PnL meter that gates live trading.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise MoneyFieldError(f"{field}={value!r} is a bool, not USDT ({symbol})")
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise MoneyFieldError(f"{field}={value!r} is not finite ({symbol})")
        return parsed
    except (TypeError, ValueError) as exc:
        raise MoneyFieldError(
            f"{field}={value!r} ({type(value).__name__}) is not a USDT amount ({symbol})"
        ) from exc


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _shift_iso_seconds(stamp: str, offset_seconds: int) -> str:
    """Return ``stamp`` moved by ``offset_seconds``, truncated to whole seconds.

    Used only to build duplicate-detection keys, so an unparsable timestamp
    degrades to the plain second-precision prefix rather than raising.
    """
    text = str(stamp or "").strip()
    if not text:
        return ""
    if offset_seconds == 0:
        return text[:19]
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return text[:19]
    return (parsed + timedelta(seconds=offset_seconds)).isoformat(timespec="seconds")[:19]


def _safe_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "hit", "on"}
    return bool(value)


def _report_planned_entry(report: ExecutionReport) -> float:
    return _safe_float(getattr(report, "planned_avg_entry", report.avg_entry))


def _report_exchange_entry(report: ExecutionReport) -> float:
    return _safe_float(getattr(report, "exchange_avg_entry", 0.0))


def _report_executed_or_planned_entry(report: ExecutionReport) -> float:
    """Reporting boundary: executed truth wins; planned remains labeled."""
    return _report_exchange_entry(report) or _report_planned_entry(report)


# --- Strategy label normalization helper ---
def _normalize_strategy_label(strategy: str | None, extra: dict | None = None) -> str:
    """Avoid blank/unknown strategy labels in production analytics."""
    raw = str(strategy or "").strip()
    if raw and raw.lower() not in {"unknown", "none", "null", "na", "n/a"}:
        return raw

    extra = extra or {}
    close_source = str(extra.get("close_source") or extra.get("source") or "").strip().lower()

    if "protection" in close_source:
        return "protection_repair_close"
    if "reconcile" in close_source or "sync" in close_source or "exchange_position_closed" in close_source:
        return "reconciliation_close"
    if "manual" in close_source:
        return "manual_close"
    if "tp3" in close_source:
        return "tp3_close"
    if "stop" in close_source:
        return "stop_close"

    return "recovered_unlinked_close"


def _trade_quality_from_journal(trade: dict, pnl: float, fees: float = 0.0) -> dict:
    """Score trade quality by process, not by profit alone."""
    score = 0
    notes: list[str] = []
    expectancy_label = "NEUTRAL"
    process_verdict = "UNCLASSIFIED"
    protection_failure = False

    net_pnl = pnl - fees
    tp1_hit = _safe_bool(trade.get("tp1_hit"))
    tp2_hit = _safe_bool(trade.get("tp2_hit"))
    tp3_hit = _safe_bool(trade.get("tp3_hit"))
    break_even_active = _safe_bool(trade.get("break_even_active"))

    entry = _safe_float(trade.get("entry"))
    stop_loss = _safe_float(trade.get("stop_loss"))
    take_profits = trade.get("take_profits") or []
    tp1 = _safe_float(take_profits[0]) if isinstance(take_profits, list) and take_profits else 0.0

    risk_distance = abs(entry - stop_loss) if entry and stop_loss else 0.0
    reward_distance = abs(tp1 - entry) if entry and tp1 else 0.0
    rr_to_tp1 = (reward_distance / risk_distance) if risk_distance > 0 else 0.0

    candles_held = int(_safe_float(trade.get("candles_held"), 0.0))
    max_drawdown_pct = _safe_float(
        trade.get("max_drawdown_pct", trade.get("max_adverse_excursion_pct", 0.0)),
        0.0,
    )
    follow_through_pct = _safe_float(
        trade.get("follow_through_pct", trade.get("max_favorable_excursion_pct", 0.0)),
        0.0,
    )
    entry_volume_ratio = _safe_float(
        trade.get("entry_volume_ratio", trade.get("volume_ratio", 0.0)),
        0.0,
    )
    timed_exit = str(trade.get("closed_reason") or trade.get("result") or "").lower() in {
        "timed_exit",
        "time_exit",
        "timeout",
    }
    slippage_pct = abs(_safe_float(trade.get("slippage_pct", 0.0), 0.0))
    fee_leakage_pct = abs(fees / entry * 100) if entry > 0 else 0.0
    entry_reason_text = str(trade.get("entry_reason") or "").lower()
    close_reason_text = str(
        trade.get("close_reason")
        or trade.get("closed_reason")
        or trade.get("result")
        or ""
    ).lower()

    protection_failure = (
        "protection_repair_failed" in close_reason_text
        or "unprotected_position" in close_reason_text
        or "failed_closed" in close_reason_text
    )

    sync_source_text = str(
        trade.get("sync_source")
        or trade.get("close_source")
        or trade.get("data_confidence")
        or ""
    ).upper()

    exchange_truth = "EXCHANGE_TRUTH" in sync_source_text
    low_confidence = "LOW_CONFIDENCE" in sync_source_text
    exchange_truth_pnl_available = trade.get("exchange_truth_pnl") not in (None, "")

    if low_confidence and not exchange_truth_pnl_available:
        net_pnl = 0.0
        notes.append("pnl truth missing; win/loss scoring blocked")

    if tp1_hit:
        score += 25
        notes.append("TP1 hit")
    if tp2_hit:
        score += 20
        notes.append("TP2 hit")
    if tp3_hit:
        score += 20
        notes.append("TP3 hit")
    if break_even_active:
        score += 15
        notes.append("BE/protection active")
    if net_pnl > 0:
        score += 10
        notes.append("net positive after fees")
    elif net_pnl < 0 and break_even_active:
        score += 5
        notes.append("loss controlled after protection")
    if rr_to_tp1 >= 1.0:
        score += 10
        notes.append(f"RR to TP1 ok ({rr_to_tp1:.2f})")
    elif rr_to_tp1 > 0:
        notes.append(f"RR to TP1 weak ({rr_to_tp1:.2f})")

    if tp1_hit and candles_held > 0:
        if candles_held <= 3:
            score += 8
            notes.append(f"fast TP1 ({candles_held} candles)")
        elif candles_held <= 8:
            score += 4
            notes.append(f"normal TP speed ({candles_held} candles)")
        else:
            notes.append(f"slow TP speed ({candles_held} candles)")

    if max_drawdown_pct:
        if abs(max_drawdown_pct) <= 0.50:
            score += 7
            notes.append(f"low drawdown ({max_drawdown_pct:.2f}%)")
        elif abs(max_drawdown_pct) <= 1.25:
            score += 3
            notes.append(f"controlled drawdown ({max_drawdown_pct:.2f}%)")
        else:
            score -= 5
            notes.append(f"high drawdown ({max_drawdown_pct:.2f}%)")

    if entry_volume_ratio:
        if entry_volume_ratio >= 1.50:
            score += 5
            notes.append(f"strong entry volume ({entry_volume_ratio:.2f})")
        elif entry_volume_ratio < 0.80:
            score -= 3
            notes.append(f"weak entry volume ({entry_volume_ratio:.2f})")

    if follow_through_pct:
        if follow_through_pct >= 0.60:
            score += 5
            notes.append(f"good follow-through ({follow_through_pct:.2f}%)")
        elif follow_through_pct < 0.20:
            score -= 3
            notes.append(f"weak follow-through ({follow_through_pct:.2f}%)")

    if timed_exit:
        score -= 5
        notes.append("timed exit")

    if slippage_pct >= 0.15:
        score -= 6
        notes.append(f"high slippage ({slippage_pct:.3f}%)")
    elif slippage_pct <= 0.03 and slippage_pct > 0:
        score += 2
        notes.append(f"clean execution ({slippage_pct:.3f}%)")

    if fee_leakage_pct >= 0.12:
        score -= 5
        notes.append(f"high fee leakage ({fee_leakage_pct:.3f}%)")
    elif fee_leakage_pct <= 0.04 and fee_leakage_pct > 0:
        score += 2
        notes.append(f"low fee leakage ({fee_leakage_pct:.3f}%)")

    if "late" in entry_reason_text or "chase" in entry_reason_text:
        score -= 8
        notes.append("possible chase entry")

    if tp1_hit and not tp2_hit and net_pnl <= 0:
        score -= 5
        notes.append("tp1 reached but profits leaked")

    if tp1_hit and break_even_active and net_pnl >= 0:
        score += 4
        notes.append("protection lifecycle worked")

    score = max(0, min(100, score))

    if score >= 85:
        grade = "A+"
    elif score >= 75:
        grade = "A"
    elif score >= 65:
        grade = "B"
    elif score >= 50:
        grade = "C"
    else:
        grade = "D"

    # Add analytics for exchange_truth and low_confidence closes
    if exchange_truth:
        notes.append("exchange truth close")

    if low_confidence:
        notes.append("low confidence close")

    if net_pnl > 0 and score >= 75:
        expectancy_label = "HIGH_EDGE_WIN"
        process_verdict = "WINNER"
    elif net_pnl > 0:
        expectancy_label = "LOW_QUALITY_WIN"
        process_verdict = "MESSY_WIN"
    elif net_pnl < 0 and break_even_active:
        expectancy_label = "GOOD_PROTECTION_LOSS"
        process_verdict = "GOOD_LOSS"
    elif net_pnl < 0 and score < 45:
        expectancy_label = "LOW_EDGE_FAILURE"
        process_verdict = "BAD_LOSS"
    elif net_pnl < 0:
        expectancy_label = "NORMAL_LOSS"
        process_verdict = "ACCEPTABLE_LOSS"

    if protection_failure:
        expectancy_label = "PROTECTION_FAILURE"
        process_verdict = "EXECUTION_FAILURE"
        notes.append("protection failure close")

    return {
        "trade_grade": grade,
        "quality_score": score,
        "expectancy_label": expectancy_label,
        "process_verdict": process_verdict,
        "quality_notes": " | ".join(notes),
        "rr_to_tp1": round(rr_to_tp1, 4),
        "candles_held": candles_held,
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "follow_through_pct": round(follow_through_pct, 4),
        "entry_volume_ratio": round(entry_volume_ratio, 4),
        "slippage_pct": round(slippage_pct, 5),
        "fee_leakage_pct": round(fee_leakage_pct, 5),
        "protection_failure": protection_failure,
        "timed_exit": timed_exit,
        "exchange_truth": exchange_truth,
        "low_confidence": low_confidence,
    }


class MarketScanCsvLogger:
    def __init__(self, path: str = "logs/market_scan.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_rows(self, snapshots: list[MarketSnapshot]) -> None:
        rotate_if_needed(self.path)
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if handle.tell() == 0:
                writer.writerow([
                    "symbol","alignment","score_hint","primary_tf","primary_trend","primary_change_pct",
                    "primary_volume_ratio","confirm_tf","confirm_trend","close","notes"
                ])
            for snapshot in snapshots:
                writer.writerow([
                    snapshot.symbol, snapshot.alignment, f"{snapshot.score_hint:.2f}", snapshot.primary.granularity,
                    snapshot.primary.trend, f"{snapshot.primary.change_pct:.4f}", f"{snapshot.primary.volume_ratio_20:.4f}",
                    snapshot.confirmation.granularity, snapshot.confirmation.trend, f"{snapshot.primary.latest_close:.8f}",
                    " | ".join(snapshot.notes),
                ])


def _rotate_on_schema_change(path: Path, expected_header: list[str]) -> None:
    """Force-rotate a CSV whose on-disk header doesn't match the expected schema,
    so new columns start cleanly in a fresh segment instead of corrupting rows."""
    if not path.exists():
        return
    try:
        with locked_open(path, "r", newline="", encoding="utf-8") as handle:
            existing = next(csv.reader(handle), [])
    except Exception:
        return
    if existing and [c.strip() for c in existing] != expected_header:
        rotate_if_needed(path, max_bytes=0)


class StrategyCandidateCsvLogger:
    HEADER = [
        "timestamp","candidate_id","plan_id","strategy_id","executor_id","host_id","pid","production_sha","credential_fingerprint","candidate_candle_open_timestamp_ms","symbol","strategy","direction","verdict","score","primary_tf","confirm_tf","alignment",
        "entry_hint","reclaim_level","invalidation","bars_since_sweep","volume_ratio_on_sweep",
        "displacement_pct","notes","reasons"
    ]

    def __init__(self, path: str = "logs/strategy_candidates.csv", settings=None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = (
            ExecutionIdentity.from_settings(settings).as_dict()
            if settings is not None and settings.is_live_execution else {}
        )

    def append_rows(self, rows: list[tuple[StrategyCandidate, StrategyScore]]) -> None:
        if not rows:
            return
        rotate_if_needed(self.path)
        _rotate_on_schema_change(self.path, self.HEADER)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if handle.tell() == 0:
                writer.writerow(self.HEADER)
            for candidate, score in rows:
                writer.writerow([
                    now,
                    candidate.candidate_id, deterministic_plan_id(candidate.candidate_id),
                    candidate.strategy,
                    self.identity.get("executor_id", ""), self.identity.get("host_id", ""),
                    self.identity.get("pid", ""), self.identity.get("production_sha", ""),
                    self.identity.get("credential_fingerprint", ""),
                    candidate.candidate_candle_open_timestamp_ms,
                    candidate.symbol, candidate.strategy, candidate.direction, score.verdict, f"{score.total:.2f}",
                    candidate.primary_granularity, candidate.confirmation_granularity, candidate.market.alignment,
                    f"{candidate.detection.entry_hint:.8f}", f"{candidate.detection.reclaim_level:.8f}",
                    f"{candidate.detection.invalidation:.8f}", candidate.detection.bars_since_sweep,
                    f"{candidate.detection.volume_ratio_on_sweep:.4f}", f"{candidate.detection.displacement_pct:.4f}",
                    " | ".join(candidate.notes), " | ".join(score.reasons),
                ])


class TradePlanCsvLogger:
    HEADER = [
        "timestamp","candidate_id","plan_id","symbol","strategy","direction","verdict","score","entries","stop_loss","take_profits",
        "risk_reward_ratio","account_risk_pct","leverage","position_notional_usdt","notes","reasons",
        "decision_snapshot"
    ]

    def __init__(self, path: str = "logs/trade_plans.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_rows(self, plans: list[TradePlan]) -> None:
        if not plans:
            return
        rotate_if_needed(self.path)
        _rotate_on_schema_change(self.path, self.HEADER)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if handle.tell() == 0:
                writer.writerow(self.HEADER)
            for plan in plans:
                writer.writerow([
                    now,
                    plan.candidate_id, plan.plan_id,
                    plan.symbol, plan.strategy, plan.direction, plan.verdict, f"{plan.score:.2f}",
                    " | ".join(f"{x:.8f}" for x in plan.entry_prices), f"{plan.stop_loss:.8f}",
                    " | ".join(f"{x:.8f}" for x in plan.take_profits), f"{plan.risk_reward_ratio:.2f}",
                    f"{plan.account_risk_pct:.2f}", f"{plan.leverage:.2f}", f"{plan.position_notional_usdt:.2f}",
                    " | ".join(plan.notes), " | ".join(plan.reasons),
                    " | ".join(
                        str(note) for note in plan.notes
                        if str(note).startswith("planner_")
                        or str(note).startswith("rr_to_tp1=")
                        or str(note).startswith("rr_to_tp2=")
                        or str(note).startswith("tp1_move_bps=")
                        or str(note).startswith("minimum_tp1_move_bps=")
                        or str(note).startswith("strong_continuation_quality=")
                        or str(note).startswith("master_entry_quality_passed=")
                    ),
                ])


# --- TradeDecisionSnapshotLogger ---
class TradeDecisionSnapshotLogger:
    def __init__(self, path: str = "logs/trade_decision_snapshots.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_plan(self, plan: TradePlan, opened_at: str | None = None) -> str:
        rotate_if_needed(self.path)
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if handle.tell() == 0:
                writer.writerow([
                    "timestamp", "opened_at", "symbol", "strategy", "direction", "verdict", "score", "decision_snapshot", "snapshot_link_key"
                ])
            timestamp = opened_at or datetime.now(timezone.utc).isoformat()
            link_key = f"{plan.symbol}|{timestamp[:19]}"
            decision_snapshot = " | ".join(
                str(note) for note in plan.notes
                if str(note).startswith("planner_")
                or str(note).startswith("rr_to_tp1=")
                or str(note).startswith("rr_to_tp2=")
                or str(note).startswith("tp1_move_bps=")
                or str(note).startswith("minimum_tp1_move_bps=")
                or str(note).startswith("strong_continuation_quality=")
                or str(note).startswith("master_entry_quality_passed=")
            )
            writer.writerow([
                timestamp,
                timestamp,
                plan.symbol,
                plan.strategy,
                plan.direction,
                plan.verdict,
                f"{plan.score:.2f}",
                decision_snapshot,
                link_key,
            ])

            return timestamp


class ExecutionCsvLogger:
    def __init__(self, path: str = "logs/executions.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _fieldnames(self) -> list[str]:
        return [
            "timestamp", "candidate_id", "plan_id", "symbol", "direction", "strategy", "mode", "status", "message", "avg_entry", "expected_entry",
            "planned_avg_entry", "exchange_avg_entry", "exchange_avg_entry_source",
            "position_lifecycle_id", "confirmed_position_size", "confirmed_opening_fee_usdt",
            "actual_entry", "slippage_pct", "fees_paid", "realized_pnl", "exchange_order_id", "stop_loss",
            "take_profits", "position_notional_usdt", "leverage",
        ]

    def _ensure_header(self) -> None:
        """Ensure executions.csv has a header without destroying existing live execution rows."""
        header = self._fieldnames()

        if not self.path.exists() or self.path.stat().st_size == 0:
            with locked_open(self.path, "w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(header)
            return

        try:
            with locked_open(self.path, "r", newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        except Exception:
            return

        if not rows:
            with locked_open(self.path, "w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(header)
            return

        first_row = [str(value).strip() for value in rows[0]]
        if first_row == header:
            return

        backup_path = self.path.with_name(f"{self.path.stem}_headerless_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}{self.path.suffix}")
        try:
            self.path.replace(backup_path)
            with locked_open(self.path, "w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
        except Exception:
            if not self.path.exists() and backup_path.exists():
                backup_path.replace(self.path)

    def append_rows(self, reports: list[ExecutionReport]) -> None:
        if not reports:
            return
        rotate_if_needed(self.path)
        _rotate_on_schema_change(self.path, self._fieldnames())
        self._ensure_header()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for report in reports:
                writer.writerow([
                    now,
                    report.candidate_id, report.plan_id,
                    report.symbol, report.direction, report.strategy, report.mode, report.status, report.message,
                    f"{_report_planned_entry(report):.8f}",
                    f"{getattr(report, 'expected_entry', _report_planned_entry(report)):.8f}",
                    f"{_report_planned_entry(report):.8f}",
                    f"{_report_exchange_entry(report):.8f}",
                    getattr(report, "exchange_avg_entry_source", ""),
                    getattr(report, "position_lifecycle_id", ""),
                    f"{_safe_float(getattr(report, 'confirmed_position_size', 0.0)):.8f}",
                    f"{_safe_float(getattr(report, 'confirmed_opening_fee_usdt', 0.0)):.8f}",
                    f"{getattr(report, 'actual_entry', _report_planned_entry(report)):.8f}",
                    f"{getattr(report, 'slippage_pct', 0.0):.5f}",
                    f"{getattr(report, 'fees_paid', 0.0):.8f}",
                    f"{getattr(report, 'realized_pnl', 0.0):.8f}",
                    getattr(report, "exchange_order_id", ""),
                    f"{report.stop_loss:.8f}",
                    " | ".join(f"{x:.8f}" for x in report.take_profits), f"{report.position_notional_usdt:.2f}",
                    f"{report.leverage:.2f}",
                ])


class PositionUpdateCsvLogger:
    def __init__(self, path: str = "logs/position_updates.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._last_signatures: dict[str, tuple] = {}
        self._last_written_pnl_bucket: dict[str, int] = {}

    def append_rows(self, updates: list[PositionUpdate]) -> None:
        if not updates:
            return
        rotate_if_needed(self.path)
        fieldnames = [
            "symbol", "status", "current_price", "unrealized_pnl_pct",
            "price_return_pct", "margin_roi_pct", "estimated_net_return_pct",
            "stop_loss", "break_even_active", "tp1_hit", "tp2_hit", "tp3_hit",
            "planned_avg_entry", "exchange_avg_entry", "exchange_avg_entry_source",
            "protection_state", "confirmed_stop", "calculated_be_plus_fees", "note",
        ]
        _rotate_on_schema_change(self.path, fieldnames)
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if handle.tell() == 0:
                writer.writerow(fieldnames)
            for update in updates:
                # Only persist meaningful lifecycle/protection changes.
                # Price-only ticks are intentionally ignored to prevent overnight disk/CPU spam.
                price_return_value = _safe_float(
                    getattr(update, "price_return_pct", update.unrealized_pnl_pct)
                )
                pnl_bucket = int(round(price_return_value / 0.25))
                signature = (
                    update.status,
                    round(update.stop_loss, 6),
                    bool(update.break_even_active),
                    bool(update.tp1_hit),
                    bool(update.tp2_hit),
                    bool(update.tp3_hit),
                    update.note,
                )

                previous_signature = self._last_signatures.get(update.symbol)
                previous_bucket = self._last_written_pnl_bucket.get(update.symbol)

                should_write = previous_signature != signature or previous_bucket != pnl_bucket

                if not should_write:
                    continue

                self._last_signatures[update.symbol] = signature
                self._last_written_pnl_bucket[update.symbol] = pnl_bucket

                writer.writerow([
                    update.symbol, update.status, f"{update.current_price:.8f}", f"{update.unrealized_pnl_pct:.3f}",
                    f"{price_return_value:.3f}",
                    f"{_safe_float(getattr(update, 'margin_roi_pct', 0.0)):.3f}",
                    f"{_safe_float(getattr(update, 'estimated_net_return_pct', 0.0)):.3f}",
                    f"{update.stop_loss:.8f}", update.break_even_active, update.tp1_hit,
                    update.tp2_hit, update.tp3_hit,
                    f"{_safe_float(getattr(update, 'planned_avg_entry', 0.0)):.8f}",
                    f"{_safe_float(getattr(update, 'exchange_avg_entry', 0.0)):.8f}",
                    getattr(update, "exchange_avg_entry_source", ""),
                    getattr(update, "protection_state", ""),
                    f"{_safe_float(getattr(update, 'confirmed_stop', 0.0)):.8f}",
                    f"{_safe_float(getattr(update, 'calculated_be_plus_fees', 0.0)):.8f}",
                    update.note,
                ])


class TradeDatasetLogger:
    def __init__(self, path: str | Path = "logs/trade_dataset.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_open(self, report: ExecutionReport) -> None:
        self._append_row({
            "event_type": "OPEN",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": report.symbol,
            "direction": report.direction,
            "strategy": report.strategy,
            "status": report.status,
            "result": "",
            "entry": _report_executed_or_planned_entry(report),
            "planned_avg_entry": _report_planned_entry(report),
            "exchange_avg_entry": _report_exchange_entry(report),
            "exchange_avg_entry_source": getattr(report, "exchange_avg_entry_source", ""),
            "expected_entry": getattr(report, "expected_entry", _report_planned_entry(report)),
            "actual_entry": getattr(report, "actual_entry", _report_planned_entry(report)),
            "slippage": getattr(report, "slippage_pct", 0.0),
            "fees": getattr(report, "fees_paid", 0.0),
            "net_pnl": "",
            "trade_grade": "",
            "quality_score": "",
            "quality_notes": "",
            "rr_to_tp1": "",
            "max_drawdown_pct": "",
            "follow_through_pct": "",
            "entry_volume_ratio": "",
            "timed_exit": "",
            "exit": "",
            "stop_loss": report.stop_loss,
            "take_profits": " | ".join(f"{x:.8f}" for x in report.take_profits),
            "notional": report.position_notional_usdt,
            "leverage": report.leverage,
            "pnl": "",
            "tp1_hit": "",
            "tp2_hit": "",
            "tp3_hit": "",
            "break_even_active": "",
            "candles_held": "",
            "reason_closed": "",
            "entry_reason": getattr(report, "entry_reason", ""),
            "active_signals": " | ".join(getattr(report, "active_signals", []) or []),
            "score_breakdown": " | ".join(getattr(report, "score_breakdown", []) or []),
            "volatility_state": getattr(report, "volatility_state", ""),
            "alignment": getattr(report, "alignment", ""),
            "risk_verdict": getattr(report, "risk_verdict", ""),
            "close_reason": "",
            "message": report.message,
        })

    def append_close(
        self,
        symbol: str,
        result: str,
        pnl: float | None,
        exit_price: float | str = "",
        tp1_hit: bool | str = "",
        tp2_hit: bool | str = "",
        tp3_hit: bool | str = "",
        break_even_active: bool | str = "",
        candles_held: int | str = "",
        fees: float | str = "",
        trade_grade: str = "",
        quality_score: int | str = "",
        quality_notes: str = "",
        rr_to_tp1: float | str = "",
        max_drawdown_pct: float | str = "",
        follow_through_pct: float | str = "",
        entry_volume_ratio: float | str = "",
        timed_exit: bool | str = "",
    ) -> None:
        # `pnl` is money in USDT, or None when the caller does not yet know it.
        # A provisional close (position gone, exchange PnL not yet reported) has
        # no monetary figure, and inventing one — 0.0, or the empty string this
        # used to receive — would either understate a real result or crash on the
        # arithmetic below. Both money columns stay empty instead.
        #
        # Anything that is neither None nor numeric is a caller bug: it means a
        # non-money value (a ROI percentage, a formatted string) reached a money
        # column. Fail closed and name the offender rather than coercing it.
        monetary_pnl = _money_or_none(pnl, field="pnl", symbol=symbol)
        fee_value = _money_or_none(fees, field="fees", symbol=symbol)
        net_pnl = "" if monetary_pnl is None else monetary_pnl - abs(fee_value or 0.0)
        self._append_row({
            "event_type": "CLOSE",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": symbol.upper(),
            "direction": "",
            "strategy": "legacy_close_unlinked",
            "status": "CLOSED",
            "result": result,
            "entry": "",
            "expected_entry": "",
            "actual_entry": "",
            "slippage": "",
            "fees": "" if fee_value is None else fee_value,
            "net_pnl": "" if net_pnl == "" else round(net_pnl, 8),
            "trade_grade": trade_grade,
            "quality_score": quality_score,
            "quality_notes": quality_notes,
            "rr_to_tp1": rr_to_tp1,
            "max_drawdown_pct": max_drawdown_pct,
            "follow_through_pct": follow_through_pct,
            "entry_volume_ratio": entry_volume_ratio,
            "timed_exit": timed_exit,
            "exit": exit_price,
            "stop_loss": "",
            "take_profits": "",
            "notional": "",
            "leverage": "",
            "pnl": "" if monetary_pnl is None else monetary_pnl,
            "tp1_hit": tp1_hit,
            "tp2_hit": tp2_hit,
            "tp3_hit": tp3_hit,
            "break_even_active": break_even_active,
            "candles_held": candles_held,
            "reason_closed": result,
            "entry_reason": "",
            "active_signals": "",
            "score_breakdown": "",
            "volatility_state": "",
            "alignment": "",
            "risk_verdict": "",
            "close_reason": result,
            "message": "",
        })

    def _append_row(self, row: dict) -> None:
        fieldnames = [
            "event_type",
            "timestamp",
            "symbol",
            "direction",
            "strategy",
            "status",
            "result",
            "entry",
            "planned_avg_entry",
            "exchange_avg_entry",
            "exchange_avg_entry_source",
            "expected_entry",
            "actual_entry",
            "slippage",
            "fees",
            "net_pnl",
            "trade_grade",
            "quality_score",
            "quality_notes",
            "rr_to_tp1",
            "max_drawdown_pct",
            "follow_through_pct",
            "entry_volume_ratio",
            "timed_exit",
            "exit",
            "stop_loss",
            "take_profits",
            "notional",
            "leverage",
            "pnl",
            "tp1_hit",
            "tp2_hit",
            "tp3_hit",
            "break_even_active",
            "candles_held",
            "reason_closed",
            "entry_reason",
            "active_signals",
            "score_breakdown",
            "volatility_state",
            "alignment",
            "risk_verdict",
            "close_reason",
            "message",
        ]
        rotate_if_needed(self.path)
        _rotate_on_schema_change(self.path, fieldnames)
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerow(row)


# --- TradeDatasetV2Logger ---

class TradeDatasetV2Logger:
    """Clean v2 trade dataset: one consistent schema for self-learning/backtests."""

    def __init__(self, path: str | Path = "logs/trade_dataset_v2.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        #: identity -> lifecycle id that claimed it ("" when the row had none).
        self._seen_close_keys: dict[tuple, str] | None = None

    #: Two writers can report the same close a second apart: ``position_manager``
    #: stamps the moment it observed the position was flat, ``bitget_position_history``
    #: the exchange's own timestamp. One second of tolerance links them without
    #: being wide enough to merge two genuine closes of the same symbol and
    #: direction, which the 30-minute post-execution cooldown already prevents.
    CLOSE_TIME_TOLERANCE_SECONDS = 1

    def _close_identities(
        self,
        symbol: str,
        direction: str,
        closed_at: str,
        position_lifecycle_id: str = "",
        exchange_order_id: str = "",
        client_oid: str = "",
    ) -> list[tuple]:
        """Every identity under which this close may already be known.

        Deliberately ordered strongest first, and deliberately *not* short-circuiting:
        the provisional row carries no lifecycle id while the exchange-truth row
        does, so a single key can never link the pair. Emitting all of them and
        matching on any overlap is what makes the two rows recognisable as one
        trade. ``pnl`` is never part of an identity — the two rows disagree about
        the money, which is the whole reason they must be linked.
        """
        identities: list[tuple] = []

        lifecycle_id = str(position_lifecycle_id or "").strip()
        if lifecycle_id:
            identities.append(("LIFECYCLE", lifecycle_id))

        for raw in (exchange_order_id, client_oid):
            token = str(raw or "").strip()
            if token:
                identities.append(("ORDER", token))

        stamp = str(closed_at or "").strip()
        sym = str(symbol or "").upper()
        direction_upper = str(direction or "").upper()
        if sym and stamp:
            for offset in range(
                -self.CLOSE_TIME_TOLERANCE_SECONDS,
                self.CLOSE_TIME_TOLERANCE_SECONDS + 1,
            ):
                shifted = _shift_iso_seconds(stamp, offset)
                if shifted:
                    identities.append(("TRADE", sym, direction_upper, shifted))

        return identities

    def _is_duplicate_close(self, identities: list[tuple], lifecycle_id: str = "") -> bool:
        """Duplicate closes (re-syncs, replays) poison expectancy; block them.

        Keys are seeded from the on-disk tail once per process so duplicates are
        also caught across restarts. Each key remembers which lifecycle claimed
        it, because two *different* lifecycles closing in the same second are two
        real trades: a timestamp match must not merge them. The loose
        symbol/direction/second identity therefore only decides the outcome when
        at least one of the two rows has no lifecycle id — which is exactly the
        provisional-versus-exchange-truth case it exists for.
        """
        if self._seen_close_keys is None:
            self._seen_close_keys = {}
            if self.path.exists():
                try:
                    with locked_open(self.path, "r", newline="", encoding="utf-8") as handle:
                        rows = list(csv.DictReader(handle))
                    for row in rows[-500:]:
                        event_type = str(row.get("event_type") or "").upper()
                        if event_type not in ECONOMIC_CLOSE_EVENT_TYPES:
                            continue
                        row_lifecycle = str(row.get("position_lifecycle_id") or "").strip()
                        for identity in self._close_identities(
                            row.get("symbol"),
                            row.get("direction"),
                            row.get("closed_at") or row.get("timestamp"),
                            row_lifecycle,
                            row.get("exchange_entry_order_id"),
                            row.get("exchange_entry_client_oid"),
                        ):
                            self._seen_close_keys.setdefault(identity, row_lifecycle)
                except Exception:
                    pass

        lifecycle_id = str(lifecycle_id or "").strip()
        for identity in identities:
            if identity not in self._seen_close_keys:
                continue
            if identity[0] in ("LIFECYCLE", "ORDER"):
                return True
            seen_lifecycle = self._seen_close_keys[identity]
            if lifecycle_id and seen_lifecycle and lifecycle_id != seen_lifecycle:
                continue
            return True

        for identity in identities:
            self._seen_close_keys.setdefault(identity, lifecycle_id)
            if lifecycle_id and not self._seen_close_keys[identity]:
                self._seen_close_keys[identity] = lifecycle_id
        return False

    def _fieldnames(self) -> list[str]:
        return [
            "event_type",
            "timestamp",
            "symbol",
            "direction",
            "strategy",
            "status",
            "result",
            "opened_at",
            "closed_at",
            "entry",
            "planned_avg_entry",
            "exchange_avg_entry",
            "exchange_avg_entry_source",
            "position_lifecycle_id",
            "exchange_entry_order_id",
            "exchange_entry_client_oid",
            "confirmed_position_size",
            "confirmed_opening_fee_usdt",
            "expected_entry",
            "actual_entry",
            "exit",
            "stop_loss",
            "take_profits",
            "notional",
            "leverage",
            "fees",
            "slippage_pct",
            "pnl",
            "net_pnl",
            "gross_pnl",
            "open_fee",
            "close_fee",
            "funding",
            "exchange_position_id",
            "exchange_open_time",
            "exchange_close_time",
            "price_return_pct",
            "margin_roi_pct",
            "exchange_order_id",
            "tp1_hit",
            "tp2_hit",
            "tp3_hit",
            "break_even_active",
            "tp1_locked_stop_active",
            "old_stop_loss_removed",
            "last_sl_move_reason",
            "protection_state",
            "confirmed_stop",
            "candles_held",
            "rr_to_tp1",
            "max_drawdown_pct",
            "follow_through_pct",
            "max_adverse_excursion_pct",
            "max_favorable_excursion_pct",
            "trade_duration_seconds",
            "time_to_first_green_seconds",
            "time_to_first_red_seconds",
            "time_to_mfe_seconds",
            "time_to_mae_seconds",
            "time_to_near_tp_seconds",
            "immediate_adverse_move_pct",
            "first_5m_pnl",
            "first_3_candles_result",
            "entry_volume_ratio",
            "timed_exit",
            "trade_grade",
            "quality_score",
            "quality_notes",
            "entry_reason",
            "active_signals",
            "score_breakdown",
            "volatility_state",
            "alignment",
            "risk_verdict",
            "close_reason",
            "sync_source",
            "data_confidence",
            "process_verdict",
            "failure_type",
            "exchange_truth_order_id",
            "exchange_truth_exit_price",
            "exchange_truth_size",
            "exchange_truth_pnl",
            "exchange_truth_fee",
            "snapshot_link_key",
            "position_size",
            "message",
            # --- lineage (2026-08-10) -------------------------------------
            # Appended, never inserted: _rotate_on_schema_change rotates the
            # file when the header changes, and a new column at the end keeps
            # every existing reader's positional assumptions intact.
            #
            # These are not new identities. ExecutionReport and the stored
            # trade record already carry both; they simply never reached the
            # CSV, which is why a plan could not be traced to the position it
            # became. Nothing here is reconstructed from timestamp, symbol or
            # price -- those are research heuristics, not identity.
            "strategy_id", "executor_id", "host_id", "pid", "production_sha",
            "credential_fingerprint", "client_id_namespace",
            "original_entry", "original_sl", "original_tp1", "original_tp2", "original_rr",
            "mfe_bps", "mae_bps", "first_mfe_at", "max_mfe_at", "first_mae_at", "max_mae_at",
            "maker_fees", "taker_fees", "total_fees",
            "plan_id",
            "candidate_id",
        ]

    def append_open(self, report: ExecutionReport) -> None:
        self._append_row({
            "event_type": "OPEN",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": report.symbol,
            "direction": report.direction,
            "strategy": report.strategy,
            "status": report.status,
            "result": "",
            "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "closed_at": "",
            "entry": _report_executed_or_planned_entry(report),
            "planned_avg_entry": _report_planned_entry(report),
            "exchange_avg_entry": _report_exchange_entry(report),
            "exchange_avg_entry_source": getattr(report, "exchange_avg_entry_source", ""),
            "position_lifecycle_id": getattr(report, "position_lifecycle_id", ""),
            "exchange_entry_order_id": getattr(report, "exchange_entry_order_id", ""),
            "exchange_entry_client_oid": getattr(report, "exchange_entry_client_oid", ""),
            # From the report of the plan that actually executed. Not the
            # latest candidate, not a same-symbol match, not a nearest
            # timestamp -- attribution by proximity is how the wrong plan gets
            # credited for a position.
            "plan_id": getattr(report, "plan_id", ""),
            "candidate_id": getattr(report, "candidate_id", ""),
            **{
                key: getattr(report, key, "")
                for key in (
                    "strategy_id", "executor_id", "host_id", "pid", "production_sha",
                    "credential_fingerprint", "client_id_namespace", "original_entry",
                    "original_sl", "original_tp1", "original_tp2", "original_rr",
                )
            },
            "confirmed_position_size": getattr(report, "confirmed_position_size", ""),
            "confirmed_opening_fee_usdt": getattr(report, "confirmed_opening_fee_usdt", ""),
            "expected_entry": getattr(report, "expected_entry", _report_planned_entry(report)),
            "actual_entry": getattr(report, "actual_entry", _report_planned_entry(report)),
            "exit": "",
            "stop_loss": report.stop_loss,
            "take_profits": " | ".join(f"{x:.8f}" for x in report.take_profits),
            "notional": report.position_notional_usdt,
            "leverage": report.leverage,
            "fees": getattr(report, "fees_paid", 0.0),
            "slippage_pct": getattr(report, "slippage_pct", 0.0),
            "pnl": "",
            "net_pnl": "",
            "price_return_pct": "",
            "margin_roi_pct": "",
            "exchange_order_id": getattr(report, "exchange_order_id", ""),
            "tp1_hit": "",
            "tp2_hit": "",
            "tp3_hit": "",
            "break_even_active": "",
            "tp1_locked_stop_active": "",
            "old_stop_loss_removed": "",
            "last_sl_move_reason": "",
            "protection_state": getattr(report, "protection_state", ""),
            "confirmed_stop": getattr(report, "confirmed_stop", ""),
            "candles_held": "",
            "rr_to_tp1": "",
            "max_drawdown_pct": "",
            "follow_through_pct": "",
            "max_adverse_excursion_pct": "",
            "max_favorable_excursion_pct": "",
            "trade_duration_seconds": "",
            "time_to_first_green_seconds": "",
            "time_to_first_red_seconds": "",
            "time_to_mfe_seconds": "",
            "time_to_mae_seconds": "",
            "time_to_near_tp_seconds": "",
            "immediate_adverse_move_pct": "",
            "first_5m_pnl": "",
            "first_3_candles_result": "",
            "entry_volume_ratio": "",
            "timed_exit": "",
            "trade_grade": "",
            "quality_score": "",
            "quality_notes": "",
            "entry_reason": getattr(report, "entry_reason", ""),
            "active_signals": " | ".join(getattr(report, "active_signals", []) or []),
            "score_breakdown": " | ".join(getattr(report, "score_breakdown", []) or []),
            "volatility_state": getattr(report, "volatility_state", ""),
            "alignment": getattr(report, "alignment", ""),
            "risk_verdict": getattr(report, "risk_verdict", ""),
            "close_reason": "",
            "sync_source": "execution_service",
            "data_confidence": "STRATEGY_TRUTH",
            "process_verdict": "OPEN_EXECUTION_CONFIRMED",
            "failure_type": "",
            "exchange_truth_order_id": "",
            "exchange_truth_exit_price": "",
            "exchange_truth_size": "",
            "exchange_truth_pnl": "",
            "exchange_truth_fee": "",
            "snapshot_link_key": f"{report.symbol}|{datetime.now(timezone.utc).isoformat(timespec='seconds')[:19]}",
            "position_size": getattr(report, "size", ""),
            "message": report.message,
        })

    def append_close(
        self,
        trade: dict,
        result: str,
        pnl: float | None,
        quality: dict,
        margin_roi_pct: float | None = None,
    ) -> None:
        """Append one close row.

        A row is economic (``event_type=CLOSE``) only when the money came from
        the exchange or was handed in as USDT. Without that, the position is
        still known to be closed, but the row is written as
        ``CLOSE_PROVISIONAL`` with empty money columns: no consumer of
        ``ECONOMIC_CLOSE_EVENT_TYPES`` reads it, and the return percentage is
        preserved under its own name for observability.
        """
        exchange_truth_pnl = trade.get("exchange_truth_pnl")
        exchange_truth_fee = trade.get("exchange_truth_fee")
        exchange_net = _money_or_none(
            trade.get("exchange_truth_net_profit"),
            field="exchange_truth_net_profit",
            symbol=str(trade.get("symbol") or ""),
        )

        provisional = False
        if exchange_net is not None:
            required = {
                name: _money_or_none(trade.get(name), field=name, symbol=str(trade.get("symbol") or ""))
                for name in (
                    "exchange_truth_gross_pnl",
                    "exchange_truth_open_fee",
                    "exchange_truth_close_fee",
                    "exchange_truth_funding",
                )
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise MoneyFieldError(f"exchange close contract missing fields: {missing}")
            pnl = required["exchange_truth_gross_pnl"]
            fees = abs(required["exchange_truth_open_fee"]) + abs(required["exchange_truth_close_fee"])
            net_pnl = exchange_net
        elif exchange_truth_pnl not in ("", None):
            pnl = _safe_float(exchange_truth_pnl)
            fees = _safe_float(exchange_truth_fee, 0.0) if exchange_truth_fee not in ("", None) else 0.0
            net_pnl = pnl
        elif pnl is not None:
            pnl = _safe_float(pnl)
            fees = _safe_float(trade.get("fees_paid", trade.get("fees", 0.0)), 0.0)
            net_pnl = pnl - abs(fees)
        else:
            provisional = True
            fees = _safe_float(trade.get("fees_paid", trade.get("fees", 0.0)), 0.0)
            pnl = None
            net_pnl = None

        maker_fees: float | str = ""
        taker_fees: float | str = ""
        if not provisional:
            open_fee = abs(_safe_float(
                trade.get("exchange_truth_open_fee", trade.get("exchange_opening_fee_usdt", 0.0)),
                0.0,
            ))
            close_fee = abs(_safe_float(trade.get("exchange_truth_close_fee", 0.0), 0.0))
            entry_liquidity = str(trade.get("entry_liquidity") or "maker").lower()
            maker_fees = open_fee if entry_liquidity == "maker" else 0.0
            # V2 exits are exchange TP/SL or emergency market closes.
            taker_fees = close_fee + (0.0 if entry_liquidity == "maker" else open_fee)

        strategy_label = _normalize_strategy_label(trade.get("strategy"), trade)
        closed_at = trade.get("closed_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
        opened_at = trade.get("opened_at", "")
        if not opened_at and trade.get("opened_at_ms") not in (None, ""):
            opened_at = datetime.fromtimestamp(
                int(trade["opened_at_ms"]) / 1000, timezone.utc
            ).isoformat(timespec="milliseconds")
        identities = self._close_identities(
            trade.get("symbol"),
            trade.get("direction"),
            closed_at,
            trade.get("position_lifecycle_id"),
            trade.get("exchange_entry_order_id"),
            trade.get("exchange_entry_client_oid"),
        )
        # Provisional rows never block their economic replacement. Economic
        # rows use the repository-wide identity matcher, including rotations,
        # open-time and size tolerances.
        if not provisional:
            from execution.close_dedup import DedupOutcome, economic_close_status
            dedup = economic_close_status(self.path, trade)
            if dedup is DedupOutcome.BLOCKED_UNREADABLE:
                # This is the writer. An unreadable segment means a prior
                # economic row for this lifecycle may already exist, so writing
                # here could double-count the trade in the freeze meter.
                logging.getLogger("trade_dataset").critical(
                    "CLOSE_WRITE_REFUSED_DEDUP_UNCERTAIN | %s | lifecycle=%s | "
                    "dataset unreadable | no economic CLOSE appended",
                    str(trade.get("symbol") or "UNKNOWN"),
                    trade.get("position_lifecycle_id") or "UNKNOWN",
                )
                return
            if dedup is DedupOutcome.FOUND:
                return
        self._append_row({
            "event_type": PROVISIONAL_CLOSE_EVENT_TYPE if provisional else "CLOSE",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": str(trade.get("symbol") or "").upper(),
            "direction": trade.get("direction", ""),
            "strategy": strategy_label,
            "status": "CLOSED",
            "result": result,
            "opened_at": opened_at,
            "closed_at": closed_at,
            "entry": trade.get("exchange_avg_entry") or trade.get("entry", ""),
            "planned_avg_entry": trade.get("planned_avg_entry", ""),
            "exchange_avg_entry": trade.get("exchange_avg_entry", ""),
            "exchange_avg_entry_source": trade.get("exchange_avg_entry_source", ""),
            "position_lifecycle_id": trade.get("position_lifecycle_id", ""),
            # Carried from the stored position record, so a close inherits the
            # lineage the open established. No re-matching happens here: a
            # second attribution attempt at close time could disagree with the
            # first, and legacy positions simply carry blanks.
            "plan_id": trade.get("plan_id", ""),
            "candidate_id": trade.get("candidate_id", ""),
            **{
                key: trade.get(key, "")
                for key in (
                    "strategy_id", "executor_id", "host_id", "pid", "production_sha",
                    "credential_fingerprint", "client_id_namespace", "original_entry",
                    "original_sl", "original_tp1", "original_tp2", "original_rr",
                )
            },
            "mfe_bps": (
                _safe_float(trade.get("max_favorable_excursion_pct"), 0.0) * 100.0
            ),
            "mae_bps": (
                _safe_float(trade.get("max_adverse_excursion_pct"), 0.0) * 100.0
            ),
            "first_mfe_at": trade.get("first_mfe_at", ""),
            "max_mfe_at": trade.get("max_mfe_at", ""),
            "first_mae_at": trade.get("first_mae_at", ""),
            "max_mae_at": trade.get("max_mae_at", ""),
            "maker_fees": maker_fees,
            "taker_fees": taker_fees,
            "total_fees": "" if provisional else fees,
            "exchange_entry_order_id": trade.get("exchange_entry_order_id", ""),
            "exchange_entry_client_oid": trade.get("exchange_entry_client_oid", ""),
            "confirmed_position_size": trade.get(
                "confirmed_position_size",
                trade.get("exchange_truth_size", trade.get("position_size", "")),
            ),
            "confirmed_opening_fee_usdt": trade.get(
                "confirmed_opening_fee_usdt",
                trade.get("exchange_opening_fee_usdt", ""),
            ),
            "expected_entry": trade.get("expected_entry", ""),
            "actual_entry": trade.get("actual_entry", ""),
            "exit": trade.get("exit", ""),
            "stop_loss": trade.get("stop_loss", ""),
            "take_profits": " | ".join(str(x) for x in (trade.get("take_profits") or [])),
            "notional": trade.get("notional", ""),
            "leverage": trade.get("leverage", ""),
            # Money columns stay empty on a provisional close. Writing a
            # placeholder here is precisely how a ROI percentage once became a
            # USDT figure inside the weekly kill-switch.
            "fees": "" if provisional else fees,
            "slippage_pct": trade.get("slippage_pct", ""),
            "pnl": "" if pnl is None else pnl,
            "net_pnl": "" if net_pnl is None else round(net_pnl, 8),
            "gross_pnl": "" if provisional else trade.get("exchange_truth_gross_pnl", trade.get("gross_pnl", "")),
            "open_fee": "" if provisional else trade.get("exchange_truth_open_fee", trade.get("open_fee", "")),
            "close_fee": "" if provisional else trade.get("exchange_truth_close_fee", trade.get("close_fee", "")),
            "funding": "" if provisional else trade.get("exchange_truth_funding", trade.get("funding", "")),
            "exchange_position_id": trade.get("exchange_position_id", ""),
            "exchange_open_time": trade.get("exchange_open_time", trade.get("opened_at_ms", "")),
            "exchange_close_time": trade.get("exchange_close_time", ""),
            "price_return_pct": trade.get("price_return_pct", ""),
            "margin_roi_pct": (
                margin_roi_pct
                if margin_roi_pct is not None
                else trade.get(
                    "margin_roi_pct",
                    trade.get("realized_margin_roi_pct", ""),
                )
            ),
            "exchange_order_id": trade.get("exchange_order_id", ""),
            "tp1_hit": trade.get("tp1_hit", ""),
            "tp2_hit": trade.get("tp2_hit", ""),
            "tp3_hit": trade.get("tp3_hit", ""),
            "break_even_active": trade.get("break_even_active", ""),
            "tp1_locked_stop_active": trade.get("tp1_locked_stop_active", ""),
            "old_stop_loss_removed": trade.get("old_stop_loss_removed", ""),
            "last_sl_move_reason": trade.get("last_sl_move_reason", ""),
            "protection_state": trade.get("protection_state", ""),
            "confirmed_stop": trade.get("confirmed_stop", ""),
            "candles_held": quality.get("candles_held", trade.get("candles_held", "")),
            "rr_to_tp1": quality.get("rr_to_tp1", ""),
            "max_drawdown_pct": quality.get("max_drawdown_pct", ""),
            "follow_through_pct": quality.get("follow_through_pct", ""),
            "max_adverse_excursion_pct": trade.get("max_adverse_excursion_pct", quality.get("max_drawdown_pct", "")),
            "max_favorable_excursion_pct": trade.get("max_favorable_excursion_pct", quality.get("follow_through_pct", "")),
            "trade_duration_seconds": trade.get("trade_duration_seconds", ""),
            "time_to_first_green_seconds": trade.get("time_to_first_green_seconds", ""),
            "time_to_first_red_seconds": trade.get("time_to_first_red_seconds", ""),
            "time_to_mfe_seconds": trade.get("time_to_mfe_seconds", ""),
            "time_to_mae_seconds": trade.get("time_to_mae_seconds", ""),
            "time_to_near_tp_seconds": trade.get("time_to_near_tp_seconds", ""),
            "immediate_adverse_move_pct": trade.get("immediate_adverse_move_pct", ""),
            "first_5m_pnl": trade.get("first_5m_pnl", ""),
            "first_3_candles_result": trade.get("first_3_candles_result", ""),
            "entry_volume_ratio": quality.get("entry_volume_ratio", ""),
            "timed_exit": quality.get("timed_exit", ""),
            "trade_grade": quality.get("trade_grade", ""),
            "quality_score": quality.get("quality_score", ""),
            "quality_notes": quality.get("quality_notes", ""),
            "entry_reason": trade.get("entry_reason", ""),
            "active_signals": trade.get("active_signals", ""),
            "score_breakdown": trade.get("score_breakdown", ""),
            "volatility_state": trade.get("volatility_state", ""),
            "alignment": trade.get("alignment", ""),
            "risk_verdict": trade.get("risk_verdict", ""),
            "close_reason": result,
            "sync_source": (
                trade.get("sync_source")
                or trade.get("close_source")
                or ("bitget_position_history" if exchange_net is not None or exchange_truth_pnl not in ("", None) else "position_manager")
            ),
            "data_confidence": trade.get("data_confidence", ""),
            "process_verdict": trade.get("process_verdict", quality.get("process_verdict", "")),
            "failure_type": trade.get("failure_type", ""),
            "exchange_truth_order_id": trade.get("exchange_truth_order_id", ""),
            "exchange_truth_exit_price": trade.get("exchange_truth_exit_price", ""),
            "exchange_truth_size": trade.get("exchange_truth_size", ""),
            "exchange_truth_pnl": trade.get("exchange_truth_pnl", ""),
            "exchange_truth_fee": trade.get("exchange_truth_fee", ""),
            "snapshot_link_key": trade.get("snapshot_link_key", ""),
            "position_size": trade.get("position_size", trade.get("size", "")),
            "message": trade.get("message", ""),
        })

    def _append_row(self, row: dict) -> None:
        fieldnames = self._fieldnames()
        rotate_if_needed(self.path)
        # Appending 68-column rows into a file whose header still has an older
        # 59-column schema silently shifts every value into the wrong column
        # (observed live 2026-07-07: trade_grade landed in close_reason).
        _rotate_on_schema_change(self.path, fieldnames)
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerow(row)


# --- ValidationEventLogger ---

class ValidationEventLogger:
    """Lightweight validation audit trail for live lifecycle proof."""

    def __init__(self, path: str | Path = "logs/validation_events.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_event(
        self,
        *,
        event_type: str,
        symbol: str,
        status: str = "",
        strategy: str = "",
        direction: str = "",
        message: str = "",
        details: dict | None = None,
    ) -> None:
        details = details or {}
        strategy_label = _normalize_strategy_label(strategy, details)
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event_type": event_type,
            "symbol": str(symbol or "").upper(),
            "status": status,
            "strategy": strategy_label,
            "direction": direction,
            "message": message,
            "details": " | ".join(f"{key}={value}" for key, value in details.items()),
        }
        self._append_row(row)

    def _append_row(self, row: dict) -> None:
        fieldnames = [
            "timestamp",
            "event_type",
            "symbol",
            "status",
            "strategy",
            "direction",
            "message",
            "details",
        ]
        rotate_if_needed(self.path)
        _rotate_on_schema_change(self.path, fieldnames)
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerow(row)


# --- StrategyPerformanceLogger ---

class StrategyPerformanceLogger:
    """Strategy-level audit trail for learning expectancy per strategy."""

    def __init__(self, path: str | Path = "logs/strategy_performance.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_setup_event(
        self,
        *,
        symbol: str,
        strategy: str,
        direction: str,
        verdict: str,
        score: float | str = "",
        stage: str = "SETUP",
        reasons: list[str] | str | None = None,
        notes: list[str] | str | None = None,
    ) -> None:
        strategy_label = _normalize_strategy_label(strategy, {"source": stage})
        self._append_row({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event_type": "SETUP_EVENT",
            "stage": stage,
            "symbol": str(symbol or "").upper(),
            "strategy": strategy_label,
            "direction": direction or "",
            "verdict": verdict or "",
            "score": score,
            "result": "",
            "pnl": "",
            "fees": "",
            "net_pnl": "",
            "tp1_hit": "",
            "tp2_hit": "",
            "tp3_hit": "",
            "break_even_active": "",
            "trade_grade": "",
            "quality_score": "",
            "expectancy_label": "",
            "process_verdict": "",
            "slippage_pct": "",
            "fee_leakage_pct": "",
            "reasons": self._join(reasons),
            "notes": self._join(notes),
        })

    def append_close_event(self, *, trade: dict, result: str, pnl: float, quality: dict) -> None:
        exchange_truth_pnl = trade.get("exchange_truth_pnl")
        exchange_truth_fee = trade.get("exchange_truth_fee")

        if exchange_truth_pnl not in ("", None):
            pnl = _safe_float(exchange_truth_pnl)
            fees = _safe_float(exchange_truth_fee, 0.0) if exchange_truth_fee not in ("", None) else 0.0
            net_pnl = pnl
        else:
            fees = _safe_float(trade.get("fees_paid", trade.get("fees", 0.0)), 0.0)
            net_pnl = pnl - abs(fees)

        strategy_label = _normalize_strategy_label(trade.get("strategy"), trade)
        self._append_row({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event_type": "TRADE_CLOSE",
            "stage": "CLOSE",
            "symbol": str(trade.get("symbol") or "").upper(),
            "strategy": strategy_label,
            "direction": str(trade.get("direction") or ""),
            "verdict": "CLOSED",
            "score": trade.get("score", ""),
            "result": result,
            "pnl": pnl,
            "fees": fees,
            "net_pnl": round(net_pnl, 8),
            "tp1_hit": trade.get("tp1_hit", ""),
            "tp2_hit": trade.get("tp2_hit", ""),
            "tp3_hit": trade.get("tp3_hit", ""),
            "break_even_active": trade.get("break_even_active", ""),
            "trade_grade": quality.get("trade_grade", ""),
            "quality_score": quality.get("quality_score", ""),
            "expectancy_label": quality.get("expectancy_label", ""),
            "process_verdict": quality.get("process_verdict", ""),
            "slippage_pct": quality.get("slippage_pct", trade.get("slippage_pct", "")),
            "fee_leakage_pct": quality.get("fee_leakage_pct", ""),
            "reasons": result,
            "notes": quality.get("quality_notes", ""),
        })

    def _append_row(self, row: dict) -> None:
        fieldnames = [
            "timestamp",
            "event_type",
            "stage",
            "symbol",
            "strategy",
            "direction",
            "verdict",
            "score",
            "result",
            "pnl",
            "fees",
            "net_pnl",
            "tp1_hit",
            "tp2_hit",
            "tp3_hit",
            "break_even_active",
            "trade_grade",
            "quality_score",
            "expectancy_label",
            "process_verdict",
            "slippage_pct",
            "fee_leakage_pct",
            "reasons",
            "notes",
        ]
        rotate_if_needed(self.path)
        _rotate_on_schema_change(self.path, fieldnames)
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerow(row)

    @staticmethod
    def _join(value: list[str] | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return " | ".join(str(item) for item in value)


class LiveTradeJournalLogger:
    def __init__(self, path: str = "state/live_trade_journal.json") -> None:
        from json import dump, load
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._dump = dump
        self._load = load
        self.dataset = TradeDatasetLogger()
        self.dataset_v2 = TradeDatasetV2Logger()
        self.validation = ValidationEventLogger()
        self.strategy_performance = StrategyPerformanceLogger()

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            with locked_open(self.path, "r", encoding="utf-8") as f:
                return self._load(f)
        except Exception:
            return []

    def _write(self, data: list[dict]) -> None:
        with locked_open(self.path, "w", encoding="utf-8") as f:
            self._dump(data, f, indent=2)

    def log_open(self, report: ExecutionReport) -> None:
        journal = self._read()
        # Prevent duplicate OPEN rows for the same symbol when the bot restarts or replays execution state.
        for trade in reversed(journal):
            if trade.get("symbol") == report.symbol and str(trade.get("status") or "").upper() == "OPEN":
                trade.update({
                    "direction": report.direction,
                    "strategy": report.strategy,
                    "entry": _report_executed_or_planned_entry(report),
                    "planned_avg_entry": _report_planned_entry(report),
                    "exchange_avg_entry": _report_exchange_entry(report),
                    "exchange_avg_entry_source": getattr(report, "exchange_avg_entry_source", ""),
                    "expected_entry": getattr(report, "expected_entry", _report_planned_entry(report)),
                    "actual_entry": getattr(report, "actual_entry", _report_planned_entry(report)),
                    "slippage_pct": getattr(report, "slippage_pct", 0.0),
                    "fees_paid": getattr(report, "fees_paid", 0.0),
                    "exchange_order_id": getattr(report, "exchange_order_id", ""),
                    "stop_loss": report.stop_loss,
                    "take_profits": report.take_profits,
                    "leverage": report.leverage,
                    "notional": report.position_notional_usdt,
                })
                self._write(journal)
                return
        journal.append({
            "symbol": report.symbol,
            "direction": report.direction,
            "strategy": report.strategy,
            "entry": _report_executed_or_planned_entry(report),
            "planned_avg_entry": _report_planned_entry(report),
            "exchange_avg_entry": _report_exchange_entry(report),
            "exchange_avg_entry_source": getattr(report, "exchange_avg_entry_source", ""),
            "expected_entry": getattr(report, "expected_entry", _report_planned_entry(report)),
            "actual_entry": getattr(report, "actual_entry", _report_planned_entry(report)),
            "slippage_pct": getattr(report, "slippage_pct", 0.0),
            "fees_paid": getattr(report, "fees_paid", 0.0),
            "exchange_order_id": getattr(report, "exchange_order_id", ""),
            "stop_loss": report.stop_loss,
            "take_profits": report.take_profits,
            "leverage": report.leverage,
            "notional": report.position_notional_usdt,
            "status": "OPEN",
            "result": None,
            "pnl": None,
            "opened_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "closed_at": None,
        })
        self.dataset.append_open(report)
        self.dataset_v2.append_open(report)
        self.validation.append_event(
            event_type="TRADE_OPEN_LOGGED",
            symbol=report.symbol,
            status=report.status,
            strategy=report.strategy,
            direction=report.direction,
            message=report.message,
            details={
                "planned_avg_entry": _report_planned_entry(report),
                "exchange_avg_entry": _report_exchange_entry(report),
                "exchange_avg_entry_source": getattr(report, "exchange_avg_entry_source", ""),
                "stop_loss": report.stop_loss,
                "tp_count": len(report.take_profits),
                "leverage": report.leverage,
                "notional": report.position_notional_usdt,
                "fees": getattr(report, "fees_paid", 0.0),
                "slippage_pct": getattr(report, "slippage_pct", 0.0),
                "exchange_order_id": getattr(report, "exchange_order_id", ""),
            },
        )
        self._write(journal)

    def log_close(
        self,
        symbol: str,
        result: str,
        pnl: float | None = None,
        *,
        margin_roi_pct: float | None = None,
    ) -> None:
        """Close the journal row for ``symbol``.

        ``pnl`` is money in USDT and nothing else. ``margin_roi_pct`` is a return
        percentage and is never promoted into a money column — callers that only
        know the percentage (``PositionManager``, which runs before Bitget
        reports realized PnL) must pass it under that name.

        When neither exchange truth nor a monetary ``pnl`` is available the close
        is recorded as provisional: the position is known to be flat, but no
        money figure is asserted, so the weekly-PnL kill-switch and expectancy
        both skip it until the exchange-confirmed row arrives.
        """
        journal = self._read()
        closed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        symbol_upper = symbol.upper()

        target = None

        # Prefer the latest OPEN trade for this symbol.
        for trade in reversed(journal):
            if trade.get("symbol") == symbol_upper and str(trade.get("status") or "").upper() == "OPEN":
                target = trade
                break

        # If no OPEN row exists, close the latest matching non-closed row as a recovery path.
        if target is None:
            for trade in reversed(journal):
                if trade.get("symbol") == symbol_upper and str(trade.get("status") or "").upper() != "CLOSED":
                    target = trade
                    break

        if target is not None:
            target["status"] = "CLOSED"
            target["result"] = result
            target["closed_at"] = closed_at
            target["sync_source"] = "position_manager"
            if margin_roi_pct is not None:
                target["margin_roi_pct"] = margin_roi_pct

            exchange_truth_pnl = target.get("exchange_truth_pnl")
            exchange_truth_fee = target.get("exchange_truth_fee")
            if exchange_truth_pnl not in ("", None):
                pnl = _safe_float(exchange_truth_pnl)
                fees_paid = _safe_float(exchange_truth_fee, 0.0) if exchange_truth_fee not in ("", None) else 0.0
                quality_pnl = pnl
                quality_fees = 0.0
                target["pnl"] = pnl
            elif pnl is not None:
                fees_paid = _safe_float(target.get("fees_paid", target.get("fees", 0.0)), 0.0)
                quality_pnl = _safe_float(pnl)
                quality_fees = fees_paid
                target["pnl"] = quality_pnl
            else:
                # No exchange truth and no monetary figure: provisional close.
                fees_paid = _safe_float(target.get("fees_paid", target.get("fees", 0.0)), 0.0)
                quality_pnl = 0.0
                quality_fees = fees_paid
            quality = _trade_quality_from_journal(target, pnl=quality_pnl, fees=quality_fees)
            target.update(quality)

            # ``pnl`` stays None on a provisional close; downstream money columns
            # are left empty rather than filled with a stand-in value.
            monetary_pnl = pnl

            self.dataset.append_close(
                symbol=symbol_upper,
                result=result,
                pnl=monetary_pnl if monetary_pnl is not None else "",
                exit_price=target.get("exit", ""),
                tp1_hit=target.get("tp1_hit", ""),
                tp2_hit=target.get("tp2_hit", ""),
                tp3_hit=target.get("tp3_hit", ""),
                break_even_active=target.get("break_even_active", ""),
                candles_held=target.get("candles_held", ""),
                fees=target.get("fees_paid", target.get("fees", "")),
                trade_grade=quality.get("trade_grade", ""),
                quality_score=quality.get("quality_score", ""),
                quality_notes=quality.get("quality_notes", ""),
                rr_to_tp1=quality.get("rr_to_tp1", ""),
                max_drawdown_pct=quality.get("max_drawdown_pct", ""),
                follow_through_pct=quality.get("follow_through_pct", ""),
                entry_volume_ratio=quality.get("entry_volume_ratio", ""),
                timed_exit=quality.get("timed_exit", ""),
            )

            self.dataset_v2.append_close(
                trade=target,
                result=result,
                pnl=monetary_pnl,
                quality=quality,
                margin_roi_pct=margin_roi_pct,
            )

            self.validation.append_event(
                event_type="TRADE_CLOSE_LOGGED",
                symbol=symbol_upper,
                status="CLOSED",
                strategy=_normalize_strategy_label(target.get("strategy"), target),
                direction=str(target.get("direction", "")),
                message=str(target.get("message", "")),
                details={
                    "result": result,
                    "pnl": monetary_pnl if monetary_pnl is not None else "",
                    "fees": fees_paid,
                    "net_pnl": (
                        ""
                        if monetary_pnl is None
                        else round(
                            monetary_pnl
                            if exchange_truth_pnl not in ("", None)
                            else monetary_pnl - abs(fees_paid),
                            8,
                        )
                    ),
                    "margin_roi_pct": margin_roi_pct if margin_roi_pct is not None else "",
                    "provisional": monetary_pnl is None,
                    "tp1_hit": target.get("tp1_hit", ""),
                    "tp2_hit": target.get("tp2_hit", ""),
                    "tp3_hit": target.get("tp3_hit", ""),
                    "break_even_active": target.get("break_even_active", ""),
                    "tp1_locked_stop_active": target.get("tp1_locked_stop_active", ""),
                    "old_stop_loss_removed": target.get("old_stop_loss_removed", ""),
                    "last_sl_move_reason": target.get("last_sl_move_reason", ""),
                    "trade_grade": quality.get("trade_grade", ""),
                    "quality_score": quality.get("quality_score", ""),
                    "rr_to_tp1": quality.get("rr_to_tp1", ""),
                },
            )
            target["strategy"] = _normalize_strategy_label(target.get("strategy"), target)
            if monetary_pnl is not None:
                # Strategy performance is an economic ledger; a provisional close
                # contributes nothing to it until exchange truth lands.
                self.strategy_performance.append_close_event(
                    trade=target,
                    result=result,
                    pnl=monetary_pnl,
                    quality=quality,
                )

        self._write(journal)

    def force_sync_closed(self, symbol: str, result: str = "closed_synced", pnl: float = 0.0) -> None:
        """Force-close stale journal rows when executed_trades has already synced closed state."""
        self.log_close(symbol=symbol, result=result, pnl=pnl)

# --- Hardened closed-trade writer for v2 learning dataset ---
def append_closed_trade_row(
    position: dict | None = None,
    trade: dict | None = None,
    result: str | None = None,
    close_reason: str | None = None,
    pnl: float | int | str | None = None,
    margin_roi_pct: float | int | str | None = None,
    pnl_pct: float | int | str | None = None,
    exit_price: float | int | str | None = None,
    extra: dict | None = None,
    **kwargs,
) -> None:
    trade_payload: dict = {}

    if isinstance(position, dict):
        trade_payload.update(position)
    if isinstance(trade, dict):
        trade_payload.update(trade)
    if isinstance(extra, dict):
        trade_payload.update(extra)
    if kwargs:
        trade_payload.update(kwargs)
    if margin_roi_pct not in (None, ""):
        trade_payload["margin_roi_pct"] = margin_roi_pct
    if pnl_pct not in (None, ""):
        trade_payload["price_return_pct"] = pnl_pct

    resolved_reason = (
        result
        or close_reason
        or trade_payload.get("closed_reason")
        or trade_payload.get("close_reason")
        or trade_payload.get("result")
        or "closed"
    )

    exchange_truth_pnl = trade_payload.get("exchange_truth_pnl")
    exchange_truth_net = trade_payload.get("exchange_truth_net_profit")
    if exchange_truth_net not in ("", None):
        resolved_pnl = trade_payload.get("exchange_truth_gross_pnl")
        if resolved_pnl in ("", None):
            raise ValueError("exchange_truth_gross_pnl is required with exchange_truth_net_profit")
    elif exchange_truth_pnl not in ("", None):
        resolved_pnl = exchange_truth_pnl
    elif pnl not in ("", None):
        resolved_pnl = pnl
    elif trade_payload.get("realized_pnl") not in ("", None):
        resolved_pnl = trade_payload.get("realized_pnl")
    elif trade_payload.get("pnl") not in ("", None):
        resolved_pnl = trade_payload.get("pnl")
    else:
        raise ValueError(
            "monetary PnL is required; margin ROI and price-return percentages "
            "must never be written as USDT"
        )

    try:
        resolved_pnl_float = float(resolved_pnl)
    except (TypeError, ValueError) as exc:
        raise ValueError("monetary PnL must be numeric") from exc

    if exit_price not in (None, ""):
        trade_payload["exit"] = exit_price
    elif trade_payload.get("exit") in (None, ""):
        trade_payload["exit"] = (
            trade_payload.get("exchange_truth_exit_price")
            or trade_payload.get("last_price")
            or ""
        )

    if not trade_payload.get("entry") and trade_payload.get("exchange_avg_entry"):
        trade_payload["entry"] = trade_payload.get("exchange_avg_entry")

    if not trade_payload.get("closed_at"):
        trade_payload["closed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not trade_payload.get("symbol"):
        trade_payload["symbol"] = str(trade_payload.get("market") or "UNKNOWN").upper()

    trade_payload["closed_reason"] = resolved_reason
    trade_payload["close_reason"] = resolved_reason

    exchange_truth_fee = trade_payload.get("exchange_truth_fee")
    if exchange_truth_net not in ("", None):
        resolved_pnl_float = _safe_float(trade_payload.get("exchange_truth_gross_pnl"))
        quality_fees = abs(_safe_float(trade_payload.get("exchange_truth_open_fee"))) + abs(
            _safe_float(trade_payload.get("exchange_truth_close_fee"))
        )
    elif exchange_truth_pnl not in ("", None):
        resolved_pnl_float = _safe_float(exchange_truth_pnl)
        quality_fees = 0.0
    else:
        quality_fees = _safe_float(trade_payload.get("fees_paid", trade_payload.get("fees", 0.0)), 0.0)

    quality = _trade_quality_from_journal(
        trade_payload,
        pnl=resolved_pnl_float,
        fees=quality_fees,
    )

    TradeDatasetV2Logger("logs/trade_dataset_v2.csv").append_close(
        trade=trade_payload,
        result=str(resolved_reason),
        pnl=resolved_pnl_float,
        quality=quality,
    )


def append_exchange_truth_close(
    *,
    position: dict,
    economics,
    close_reason: str,
    dataset_path: str | Path = "logs/trade_dataset_v2.csv",
) -> None:
    """Serialize the explicit exchange close contract without recomputing net."""
    required = (
        "gross_pnl", "open_fee", "close_fee", "funding", "net_profit",
        "exchange_position_id", "symbol", "side", "open_time", "close_time",
        "size", "open_price", "close_price",
    )
    values = {name: economics.get(name) for name in required}
    missing = [name for name, value in values.items() if value in (None, "")]
    if missing:
        raise MoneyFieldError(f"exchange close contract missing fields: {missing}")
    side = str(values["side"]).lower()
    if side not in {"long", "short"}:
        raise ValueError(f"exchange close contract has invalid side: {values['side']!r}")
    monetary = {
        name: _money_or_none(values[name], field=name, symbol=str(values["symbol"]))
        for name in ("gross_pnl", "open_fee", "close_fee", "funding", "net_profit")
    }
    expected = (
        Decimal(str(monetary["gross_pnl"]))
        - abs(Decimal(str(monetary["open_fee"])))
        - abs(Decimal(str(monetary["close_fee"])))
        + Decimal(str(monetary["funding"]))
    )
    if abs(expected - Decimal(str(monetary["net_profit"]))) > Decimal("0.0000001"):
        raise MoneyFieldError(
            f"exchange close contract inconsistent: formula={expected} net={monetary['net_profit']}"
        )

    payload = dict(position)
    payload.update({
        "symbol": values["symbol"],
        "direction": "LONG" if side == "long" else "SHORT",
        "opened_at": payload.get("opened_at") or datetime.fromtimestamp(
            int(values["open_time"]) / 1000, timezone.utc
        ).isoformat(timespec="milliseconds"),
        "closed_at": datetime.fromtimestamp(
            int(values["close_time"]) / 1000, timezone.utc
        ).isoformat(timespec="milliseconds"),
        "exchange_avg_entry": values["open_price"],
        "confirmed_position_size": values["size"],
        "exit": values["close_price"],
        "exchange_position_id": values["exchange_position_id"],
        "exchange_open_time": values["open_time"],
        "exchange_close_time": values["close_time"],
        "exchange_truth_gross_pnl": values["gross_pnl"],
        "exchange_truth_open_fee": values["open_fee"],
        "exchange_truth_close_fee": values["close_fee"],
        "exchange_truth_funding": values["funding"],
        "exchange_truth_net_profit": values["net_profit"],
        "sync_source": economics.get("sync_source", "bitget_position_history"),
        "close_source": economics.get("sync_source", "bitget_position_history"),
        "data_confidence": "EXCHANGE_TRUTH",
    })
    fees = abs(float(values["open_fee"])) + abs(float(values["close_fee"]))
    quality = _trade_quality_from_journal(payload, pnl=float(values["gross_pnl"]), fees=fees)
    TradeDatasetV2Logger(dataset_path).append_close(
        trade=payload,
        result=close_reason,
        pnl=float(values["gross_pnl"]),
        quality=quality,
    )
