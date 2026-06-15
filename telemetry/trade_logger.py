import csv
from datetime import datetime, timezone
from pathlib import Path


from clients.schemas import ExecutionReport, MarketSnapshot, PositionUpdate, StrategyCandidate, StrategyScore, TradePlan


# --- Trade Quality/Grade helpers ---
def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "hit", "on"}
    return bool(value)


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
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
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


class StrategyCandidateCsvLogger:
    def __init__(self, path: str = "logs/strategy_candidates.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_rows(self, rows: list[tuple[StrategyCandidate, StrategyScore]]) -> None:
        if not rows:
            return
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow([
                    "symbol","strategy","direction","verdict","score","primary_tf","confirm_tf","alignment",
                    "entry_hint","reclaim_level","invalidation","bars_since_sweep","volume_ratio_on_sweep",
                    "displacement_pct","notes","reasons"
                ])
            for candidate, score in rows:
                writer.writerow([
                    candidate.symbol, candidate.strategy, candidate.direction, score.verdict, f"{score.total:.2f}",
                    candidate.primary_granularity, candidate.confirmation_granularity, candidate.market.alignment,
                    f"{candidate.detection.entry_hint:.8f}", f"{candidate.detection.reclaim_level:.8f}",
                    f"{candidate.detection.invalidation:.8f}", candidate.detection.bars_since_sweep,
                    f"{candidate.detection.volume_ratio_on_sweep:.4f}", f"{candidate.detection.displacement_pct:.4f}",
                    " | ".join(candidate.notes), " | ".join(score.reasons),
                ])


class TradePlanCsvLogger:
    def __init__(self, path: str = "logs/trade_plans.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_rows(self, plans: list[TradePlan]) -> None:
        if not plans:
            return
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow([
                    "symbol","strategy","direction","verdict","score","entries","stop_loss","take_profits",
                    "risk_reward_ratio","account_risk_pct","leverage","position_notional_usdt","notes","reasons",
                    "decision_snapshot"
                ])
            for plan in plans:
                writer.writerow([
                    plan.symbol, plan.strategy, plan.direction, plan.verdict, f"{plan.score:.2f}",
                    " | ".join(f"{x:.8f}" for x in plan.entry_prices), f"{plan.stop_loss:.8f}",
                    " | ".join(f"{x:.8f}" for x in plan.take_profits), f"{plan.risk_reward_ratio:.2f}",
                    f"{plan.account_risk_pct:.2f}", f"{plan.leverage:.2f}", f"{plan.position_notional_usdt:.2f}",
                    " | ".join(plan.notes), " | ".join(plan.reasons),
                    " | ".join(
                        note for note in plan.notes
                        if str(note).startswith("planner_")
                    ),
                ])


# --- TradeDecisionSnapshotLogger ---
class TradeDecisionSnapshotLogger:
    def __init__(self, path: str = "logs/trade_decision_snapshots.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append_plan(self, plan: TradePlan, opened_at: str | None = None) -> str:
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow([
                    "timestamp", "opened_at", "symbol", "strategy", "direction", "verdict", "score", "decision_snapshot", "snapshot_link_key"
                ])
            timestamp = opened_at or datetime.now(timezone.utc).isoformat()
            link_key = f"{plan.symbol}|{timestamp[:19]}"
            decision_snapshot = " | ".join(
                note for note in plan.notes if str(note).startswith("planner_")
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
            "symbol", "direction", "strategy", "mode", "status", "message", "avg_entry", "expected_entry",
            "actual_entry", "slippage_pct", "fees_paid", "realized_pnl", "exchange_order_id", "stop_loss",
            "take_profits", "position_notional_usdt", "leverage",
        ]

    def _ensure_header(self) -> None:
        """Ensure executions.csv has a header without destroying existing live execution rows."""
        header = self._fieldnames()

        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(header)
            return

        try:
            with self.path.open("r", newline="", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        except Exception:
            return

        if not rows:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                csv.writer(handle).writerow(header)
            return

        first_row = [str(value).strip() for value in rows[0]]
        if first_row == header:
            return

        backup_path = self.path.with_name(f"{self.path.stem}_headerless_backup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}{self.path.suffix}")
        try:
            self.path.replace(backup_path)
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(header)
                writer.writerows(rows)
        except Exception:
            if not self.path.exists() and backup_path.exists():
                backup_path.replace(self.path)

    def append_rows(self, reports: list[ExecutionReport]) -> None:
        if not reports:
            return
        self._ensure_header()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for report in reports:
                writer.writerow([
                    report.symbol, report.direction, report.strategy, report.mode, report.status, report.message,
                    f"{report.avg_entry:.8f}",
                    f"{getattr(report, 'expected_entry', report.avg_entry):.8f}",
                    f"{getattr(report, 'actual_entry', report.avg_entry):.8f}",
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
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if not exists:
                writer.writerow([
                    "symbol","status","current_price","unrealized_pnl_pct","stop_loss","break_even_active",
                    "tp1_hit","tp2_hit","tp3_hit","note"
                ])
            for update in updates:
                # Only persist meaningful lifecycle/protection changes.
                # Price-only ticks are intentionally ignored to prevent overnight disk/CPU spam.
                pnl_bucket = int(round(update.unrealized_pnl_pct / 0.25))
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
                    f"{update.stop_loss:.8f}", update.break_even_active, update.tp1_hit, update.tp2_hit,
                    update.tp3_hit, update.note,
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
            "entry": report.avg_entry,
            "expected_entry": getattr(report, "expected_entry", report.avg_entry),
            "actual_entry": getattr(report, "actual_entry", report.avg_entry),
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
        pnl: float,
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
        fee_value = 0.0
        try:
            if fees not in ("", None):
                fee_value = float(fees)
        except (TypeError, ValueError):
            fee_value = 0.0
        net_pnl = pnl - fee_value
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
            "fees": fees,
            "net_pnl": round(net_pnl, 8),
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
            "pnl": pnl,
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
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if not exists:
                writer.writeheader()
            writer.writerow(row)


# --- TradeDatasetV2Logger ---

class TradeDatasetV2Logger:
    """Clean v2 trade dataset: one consistent schema for self-learning/backtests."""

    def __init__(self, path: str | Path = "logs/trade_dataset_v2.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

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
            "exchange_order_id",
            "tp1_hit",
            "tp2_hit",
            "tp3_hit",
            "break_even_active",
            "tp1_locked_stop_active",
            "old_stop_loss_removed",
            "last_sl_move_reason",
            "candles_held",
            "rr_to_tp1",
            "max_drawdown_pct",
            "follow_through_pct",
            "max_adverse_excursion_pct",
            "max_favorable_excursion_pct",
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
            "entry": report.avg_entry,
            "expected_entry": getattr(report, "expected_entry", report.avg_entry),
            "actual_entry": getattr(report, "actual_entry", report.avg_entry),
            "exit": "",
            "stop_loss": report.stop_loss,
            "take_profits": " | ".join(f"{x:.8f}" for x in report.take_profits),
            "notional": report.position_notional_usdt,
            "leverage": report.leverage,
            "fees": getattr(report, "fees_paid", 0.0),
            "slippage_pct": getattr(report, "slippage_pct", 0.0),
            "pnl": "",
            "net_pnl": "",
            "exchange_order_id": getattr(report, "exchange_order_id", ""),
            "tp1_hit": "",
            "tp2_hit": "",
            "tp3_hit": "",
            "break_even_active": "",
            "tp1_locked_stop_active": "",
            "old_stop_loss_removed": "",
            "last_sl_move_reason": "",
            "candles_held": "",
            "rr_to_tp1": "",
            "max_drawdown_pct": "",
            "follow_through_pct": "",
            "max_adverse_excursion_pct": "",
            "max_favorable_excursion_pct": "",
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

    def append_close(self, trade: dict, result: str, pnl: float, quality: dict) -> None:
        fees = _safe_float(trade.get("fees_paid", trade.get("fees", 0.0)), 0.0)
        net_pnl = pnl - fees
        strategy_label = _normalize_strategy_label(trade.get("strategy"), trade)
        self._append_row({
            "event_type": "CLOSE",
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": str(trade.get("symbol") or "").upper(),
            "direction": trade.get("direction", ""),
            "strategy": strategy_label,
            "status": "CLOSED",
            "result": result,
            "opened_at": trade.get("opened_at", ""),
            "closed_at": trade.get("closed_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            "entry": trade.get("entry", ""),
            "expected_entry": trade.get("expected_entry", ""),
            "actual_entry": trade.get("actual_entry", ""),
            "exit": trade.get("exit", ""),
            "stop_loss": trade.get("stop_loss", ""),
            "take_profits": " | ".join(str(x) for x in (trade.get("take_profits") or [])),
            "notional": trade.get("notional", ""),
            "leverage": trade.get("leverage", ""),
            "fees": fees,
            "slippage_pct": trade.get("slippage_pct", ""),
            "pnl": pnl,
            "net_pnl": round(net_pnl, 8),
            "exchange_order_id": trade.get("exchange_order_id", ""),
            "tp1_hit": trade.get("tp1_hit", ""),
            "tp2_hit": trade.get("tp2_hit", ""),
            "tp3_hit": trade.get("tp3_hit", ""),
            "break_even_active": trade.get("break_even_active", ""),
            "tp1_locked_stop_active": trade.get("tp1_locked_stop_active", ""),
            "old_stop_loss_removed": trade.get("old_stop_loss_removed", ""),
            "last_sl_move_reason": trade.get("last_sl_move_reason", ""),
            "candles_held": quality.get("candles_held", trade.get("candles_held", "")),
            "rr_to_tp1": quality.get("rr_to_tp1", ""),
            "max_drawdown_pct": quality.get("max_drawdown_pct", ""),
            "follow_through_pct": quality.get("follow_through_pct", ""),
            "max_adverse_excursion_pct": trade.get("max_adverse_excursion_pct", quality.get("max_drawdown_pct", "")),
            "max_favorable_excursion_pct": trade.get("max_favorable_excursion_pct", quality.get("follow_through_pct", "")),
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
            "sync_source": trade.get("sync_source", trade.get("close_source", "position_manager")),
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
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
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
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
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
        fees = _safe_float(trade.get("fees_paid", trade.get("fees", 0.0)), 0.0)
        net_pnl = pnl - fees
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
        exists = self.path.exists()
        with self.path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            if not exists:
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
            with self.path.open("r", encoding="utf-8") as f:
                return self._load(f)
        except Exception:
            return []

    def _write(self, data: list[dict]) -> None:
        with self.path.open("w", encoding="utf-8") as f:
            self._dump(data, f, indent=2)

    def log_open(self, report: ExecutionReport) -> None:
        journal = self._read()
        # Prevent duplicate OPEN rows for the same symbol when the bot restarts or replays execution state.
        for trade in reversed(journal):
            if trade.get("symbol") == report.symbol and str(trade.get("status") or "").upper() == "OPEN":
                trade.update({
                    "direction": report.direction,
                    "strategy": report.strategy,
                    "entry": report.avg_entry,
                    "expected_entry": getattr(report, "expected_entry", report.avg_entry),
                    "actual_entry": getattr(report, "actual_entry", report.avg_entry),
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
            "entry": report.avg_entry,
            "expected_entry": getattr(report, "expected_entry", report.avg_entry),
            "actual_entry": getattr(report, "actual_entry", report.avg_entry),
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
                "entry": report.avg_entry,
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

    def log_close(self, symbol: str, result: str, pnl: float) -> None:
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
            target["pnl"] = pnl
            target["closed_at"] = closed_at
            target["sync_source"] = "position_manager"

            fees_paid = _safe_float(target.get("fees_paid", target.get("fees", 0.0)), 0.0)
            quality = _trade_quality_from_journal(target, pnl=pnl, fees=fees_paid)
            target.update(quality)

            self.dataset.append_close(
                symbol=symbol_upper,
                result=result,
                pnl=pnl,
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
                pnl=pnl,
                quality=quality,
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
                    "pnl": pnl,
                    "fees": fees_paid,
                    "net_pnl": round(pnl - fees_paid, 8),
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
            self.strategy_performance.append_close_event(
                trade=target,
                result=result,
                pnl=pnl,
                quality=quality,
            )

        self._write(journal)

    def force_sync_closed(self, symbol: str, result: str = "closed_synced", pnl: float = 0.0) -> None:
        """Force-close stale journal rows when executed_trades has already synced closed state."""
        self.log_close(symbol=symbol, result=result, pnl=pnl)

def append_closed_trade_row(
    *,
    symbol: str,
    strategy: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    size: float,
    pnl: float,
    pnl_pct: float,
    close_reason: str,
    opened_at: str | None = None,
    closed_at: str | None = None,
    extra: dict | None = None,
    dataset_path: str = "logs/trade_dataset_v2.csv",
) -> None:
    """Append a guaranteed CLOSED trade row for live stop/TP/manual close events."""
    import csv
    from datetime import datetime, timezone
    from pathlib import Path

    path = Path(dataset_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    closed_at = closed_at or now
    extra = extra or {}
    strategy_label = _normalize_strategy_label(strategy, extra)

    row = {
        "timestamp": closed_at,
        "created_at": closed_at,
        "opened_at": opened_at or "",
        "closed_at": closed_at,
        "symbol": str(symbol or "").upper(),
        "strategy": strategy_label,
        "direction": str(direction or "").upper(),
        "entry_price": float(entry_price or 0.0),
        "exit_price": float(exit_price or 0.0),
        "size": float(size or 0.0),
        "pnl": float(pnl or 0.0),
        "pnl_pct": float(pnl_pct or 0.0),
        "status": "CLOSED",
        "position_status": "CLOSED",
        "event_type": "POSITION_CLOSED",
        "closed_reason": str(close_reason or "unknown"),
        "close_reason": str(close_reason or "unknown"),
    }

    if extra:
        for key, value in extra.items():
            if key not in row:
                row[str(key)] = value

    existing_fieldnames = []
    if path.exists() and path.stat().st_size > 0:
        try:
            with path.open("r", newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                existing_fieldnames = next(reader, [])
        except Exception:
            existing_fieldnames = []

    fieldnames = list(existing_fieldnames)
    for key in row.keys():
        if key not in fieldnames:
            fieldnames.append(key)

    write_header = not path.exists() or path.stat().st_size == 0

    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames or list(row.keys()), extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)
# --- Hardened closed-trade writer for v2 learning dataset ---
def append_closed_trade_row(
    position: dict | None = None,
    trade: dict | None = None,
    result: str | None = None,
    close_reason: str | None = None,
    pnl: float | int | str | None = None,
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

    resolved_reason = (
        result
        or close_reason
        or trade_payload.get("closed_reason")
        or trade_payload.get("close_reason")
        or trade_payload.get("result")
        or "closed"
    )

    resolved_pnl = pnl if pnl is not None else pnl_pct
    if resolved_pnl in (None, ""):
        resolved_pnl = (
            trade_payload.get("realized_pnl_pct")
            or trade_payload.get("pnl_pct")
            or trade_payload.get("realized_pnl")
            or trade_payload.get("pnl")
            or 0.0
        )

    try:
        resolved_pnl_float = float(resolved_pnl)
    except (TypeError, ValueError):
        resolved_pnl_float = 0.0

    if exit_price not in (None, ""):
        trade_payload["exit"] = exit_price
    elif trade_payload.get("exit") in (None, ""):
        trade_payload["exit"] = (
            trade_payload.get("exchange_truth_exit_price")
            or trade_payload.get("last_price")
            or trade_payload.get("avg_entry")
            or trade_payload.get("entry")
            or ""
        )

    if not trade_payload.get("entry") and trade_payload.get("avg_entry"):
        trade_payload["entry"] = trade_payload.get("avg_entry")

    if not trade_payload.get("closed_at"):
        trade_payload["closed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not trade_payload.get("symbol"):
        trade_payload["symbol"] = str(trade_payload.get("market") or "UNKNOWN").upper()

    trade_payload["closed_reason"] = resolved_reason
    trade_payload["close_reason"] = resolved_reason

    quality = _trade_quality_from_journal(
        trade_payload,
        pnl=resolved_pnl_float,
        fees=_safe_float(trade_payload.get("fees_paid", trade_payload.get("fees", 0.0)), 0.0),
    )

    TradeDatasetV2Logger("logs/trade_dataset_v2.csv").append_close(
        trade=trade_payload,
        result=str(resolved_reason),
        pnl=resolved_pnl_float,
        quality=quality,
    )