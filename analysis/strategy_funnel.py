"""Deterministic, read-only Strategy Funnel Analyzer.

The analyzer consumes already materialized JSON/JSONL/CSV evidence.  It never
imports strategy, risk, planner, execution, or forward-paper runtime modules.
Unknown stages remain ``None``: counts from different cohorts are not invented
or silently joined into a conversion funnel.
"""

from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ACTIVE_STRATEGIES = (
    "momentum_breakout",
    "momentum_breakdown",
    "trend_continuation",
    "liquidity_sweep_reversal",
    "low_vol_reclaim",
    "adaptive_momentum_continuation",
)

FUNNEL_FIELDS = (
    "detected",
    "selector_pass",
    "selector_fail",
    "score_pass",
    "score_fail",
    "risk_pass",
    "risk_fail",
    "planner_pass",
    "planner_fail",
    "executable",
    "forward_open",
    "forward_closed",
    "exchange_trades",
    "wins",
    "losses",
    "be",
)

STRATEGY_DEFINITIONS: dict[str, dict[str, Any]] = {
    "momentum_breakout": {
        "module": "strategies/momentum_breakout.py",
        "entrypoint": "MomentumBreakoutStrategy.detect",
        "direction": ["LONG"],
        "detector_gates": [
            "fresh break above 20-bar range high or prearmed setup",
            "minimum breakout displacement 0.12%",
            "volume ratio >= 0.90 in detector",
            "participation >= 0.75 and follow-through >= 0.25",
            "close position >= 0.55",
            "pullback <= 45 bps and breakout age <= 3 bars",
            "entry extension <= 0.60%",
        ],
        "rejects": [
            "trend not aligned without MTF/prearmed override",
            "no fresh breakout or breakout confirmation",
            "pullback broke level",
            "late/extended entry",
            "weak participation, volume, close or follow-through",
        ],
        "configs": [
            "PRIMARY_TIMEFRAME", "CONFIRMATION_TIMEFRAME", "ENABLED_STRATEGIES",
            "DISABLED_STRATEGIES", "MOMENTUM_MIN_VOLUME_RATIO",
            "STRATEGY_SCORE_GO_THRESHOLD",
        ],
    },
    "momentum_breakdown": {
        "module": "strategies/momentum_breakout.py",
        "entrypoint": "MomentumBreakdownStrategy.detect",
        "direction": ["SHORT"],
        "detector_gates": [
            "fresh break below 20-bar range low or prearmed setup",
            "minimum breakdown displacement 0.12%",
            "volume ratio >= 0.90 in detector",
            "participation >= 0.75 and follow-through >= 0.25",
            "close position <= 0.38",
            "failed reclaim <= 35 bps and breakdown age <= 2 bars",
            "entry extension <= 0.60%",
        ],
        "rejects": [
            "shorts disabled or bearish context absent",
            "no fresh breakdown or failed reclaim",
            "late/extended entry",
            "weak participation, volume, close or follow-through",
        ],
        "configs": [
            "PRIMARY_TIMEFRAME", "CONFIRMATION_TIMEFRAME", "ENABLE_SHORTS",
            "ENABLED_STRATEGIES", "DISABLED_STRATEGIES",
            "MOMENTUM_BREAKDOWN_MIN_VOLUME_RATIO", "STRATEGY_SCORE_GO_THRESHOLD",
        ],
    },
    "trend_continuation": {
        "module": "strategies/strategies/continuation.py",
        "entrypoint": "detect_continuation",
        "direction": ["LONG", "SHORT"],
        "detector_gates": [
            "aligned trend or MTF pressure bridge",
            "EMA20/shallow pullback followed by reclaim",
            "base volume >= 0.65 and volatility rank >= 6",
            "participation >= 0.75 and follow-through >= 0.35",
            "SHORT uses stricter pressure, volume and follow-through gates",
        ],
        "rejects": [
            "directional trend/confirmation absent",
            "reclaim or structure confirmation absent",
            "directional pressure insufficient",
            "volume/participation/follow-through insufficient",
        ],
        "configs": [
            "PRIMARY_TIMEFRAME", "CONFIRMATION_TIMEFRAME", "ENABLE_SHORTS",
            "ENABLED_STRATEGIES", "DISABLED_STRATEGIES", "STRATEGY_SCORE_GO_THRESHOLD",
        ],
    },
    "liquidity_sweep_reversal": {
        "module": "strategies/liquidity_sweep.py",
        "entrypoint": "LiquiditySweepStrategy.detect",
        "direction": ["LONG", "SHORT"],
        "detector_gates": [
            "wick through 12-bar pivot and reclaim within tolerance",
            "sweep must be within 6 recent bars",
            "displacement >= 0.12% and volume ratio >= 1.15",
            "participation >= 0.70 and follow-through >= 0.25",
            "wick fraction >= 0.25 plus directional close",
        ],
        "rejects": [
            "pivot not swept or reclaim missing",
            "sweep too old",
            "displacement, volume, wick or follow-through insufficient",
            "countertrend context without strong MTF reclaim exception",
        ],
        "configs": [
            "SWEEP_PIVOT_LOOKBACK", "SWEEP_RECENT_BARS", "SWEEP_RECLAIM_TOLERANCE_BPS",
            "MIN_SWEEP_DISPLACEMENT_PCT", "MIN_SWEEP_VOLUME_RATIO",
            "PRIMARY_TIMEFRAME", "CONFIRMATION_TIMEFRAME", "ENABLE_SHORTS",
        ],
    },
    "low_vol_reclaim": {
        "module": "strategies/strategies/low_vol_reclaim.py",
        "entrypoint": "detect_low_vol_reclaim",
        "direction": ["LONG", "SHORT"],
        "detector_gates": [
            "low-volatility EMA20 retest/reclaim",
            "volatility rank <= 55 strict or <= 65 with MTF bridge",
            "volume ratio >= 0.20 and participation >= 0.75",
            "follow-through >= 0.10 and spread <= 5 bps",
            "EMA distance <= 2.5% and retest distance <= 0.85%",
        ],
        "rejects": [
            "low-vol mode confirmation absent",
            "weak follow-through",
            "spread too wide",
            "EMA retest/reclaim missing or too extended",
            "HTF direction absent without MTF bridge",
        ],
        "configs": [
            "PRIMARY_TIMEFRAME", "CONFIRMATION_TIMEFRAME", "ENABLE_SHORTS",
            "ENABLED_STRATEGIES", "DISABLED_STRATEGIES",
            "PLANNER_MIN_RR_TO_TP1",
        ],
    },
    "adaptive_momentum_continuation": {
        "module": "app/runner.py",
        "entrypoint": "_build_fallback_candidate",
        "direction": ["LONG", "SHORT"],
        "disabled_fallback": True,
        "detector_gates": [
            "only considered when no primary candidate survives",
            "must be explicitly present in ENABLED_STRATEGIES",
            "execution-aware score >= 75 and entry quality >= 75",
            "aligned direction with pressure/expansion and feature evidence",
        ],
        "rejects": [
            "not explicitly enabled",
            "primary candidate exists",
            "alignment, execution score or entry quality insufficient",
            "required volume/range/breakout/structure evidence absent",
        ],
        "configs": [
            "ENABLED_STRATEGIES", "DISABLED_STRATEGIES", "PRIMARY_TIMEFRAME",
            "CONFIRMATION_TIMEFRAME", "ENABLE_SHORTS", "STRATEGY_SCORE_GO_THRESHOLD",
        ],
    },
}

PIPELINE_ARCHITECTURE = [
    {
        "stage": "detector",
        "module": "strategy-specific; see strategy_definitions",
        "functions": [item["entrypoint"] for item in STRATEGY_DEFINITIONS.values()],
        "output": "StrategyCandidate or None",
        "observability": "No unified detector-attempt event; candidate CSV starts after selection/scoring path.",
    },
    {
        "stage": "selector",
        "module": "strategies/strategies/selector.py",
        "functions": ["select_best_candidate", "_hard_filters", "_selector_score"],
        "gates": [
            "strategy allow/deny", "direction/alignment", "wick/body/late-entry",
            "strategy-specific evidence", "entry quality", "execution penalty",
            "retest requirement", "selector score threshold",
        ],
        "thresholds": {
            "base_min_score": 72,
            "momentum_min_score": 72,
            "momentum_prearmed_min_score": 70,
            "continuation_mtf_min_score": 74,
        },
        "output": "one selected StrategyCandidate or None",
        "observability": "Rejects are text logs; no durable structured selector event dataset.",
    },
    {
        "stage": "scoring",
        "module": "strategies/scoring.py",
        "functions": ["StrategyScorer.score"],
        "gates": ["market structure", "liquidity/volume", "alignment", "strategy evidence"],
        "configs": ["STRATEGY_SCORE_GO_THRESHOLD", "STRATEGY_SCORE_WATCH_THRESHOLD"],
        "output": "StrategyScore(total, breakdown, verdict, reasons)",
    },
    {
        "stage": "risk",
        "module": "risk/risk_manager.py",
        "functions": ["RiskManager.evaluate"],
        "gates": [
            "score verdict", "daily/weekly/consecutive-loss kill switches",
            "strategy/symbol expectancy", "coach requalification", "cluster exposure",
            "execution cost", "safe-mode score", "1D/4H opposition", "alignment",
            "session risk reduction",
        ],
        "thresholds": {
            "momentum_score": 72,
            "continuation_score_strict": 78,
            "continuation_score_mtf": 74,
            "probe_risk_multiplier": 0.5,
        },
        "output": "RiskVerdict(allowed, status, reasons, risk, leverage, max positions)",
        "observability": "Risk status is embedded in plans/decision snapshots, not a separate event table.",
    },
    {
        "stage": "planner",
        "module": "planning/trade_planner.py",
        "functions": ["TradePlanner.build"],
        "gates": [
            "master entry quality", "risk allowed", "RR/RR-to-TP1",
            "largest-loss guard", "stop/target geometry", "TP1 net edge",
            "minimum notional", "strategy-specific low-vol geometry",
        ],
        "thresholds": {
            "estimated_roundtrip_fee_bps_default": 12,
            "minimum_net_edge_bps_default": 4,
            "low_vol_min_rr_to_tp1": 1.30,
            "max_stop_to_tp1_ratio": 1.20,
        },
        "output": "TradePlan with verdict EXECUTABLE or BLOCKED",
    },
    {
        "stage": "executable",
        "module": "app/runner.py",
        "functions": ["plan.verdict == 'EXECUTABLE'"],
        "output": "Executable plan count in SCAN_CYCLE_COMPLETED",
        "observability": "Aggregate log marker; no standalone structured stage event.",
    },
    {
        "stage": "forward_paper",
        "module": "forward_paper/service.py",
        "functions": ["ForwardPaperService.process", "open_trade", "update_market"],
        "gates": [
            "plan verdict EXECUTABLE", "matching market snapshot", "critical fields complete",
            "one active identity", "append-only event integrity",
        ],
        "output": "TRADE_OPENED/PAPER_REJECTED and lifecycle events",
    },
    {
        "stage": "outcome",
        "module": "forward_paper/store.py",
        "functions": ["ForwardPaperReconstructor.reconstruct"],
        "gates": ["valid hash chain", "contiguous sequence", "complete critical fields"],
        "output": "forward_paper_outcomes.csv and data-quality JSON",
    },
]


@dataclass(frozen=True)
class SourcePaths:
    backtest_funnel: Path = Path("reports/backtests/strategy_funnel.json")
    trade_funnel: Path = Path("data_store/trades/trade_funnel_report.json")
    decisions: Path = Path("data_store/decisions/latest_decisions.json")
    exchange_trades: Path = Path("data_store/trades/latest_real_closed_trades.json")
    forward_events: Path = Path("data_store/forward_paper_events.jsonl")
    forward_outcomes: Path = Path("data_store/forward_paper_outcomes.csv")
    forward_quality: Path = Path("reports/forward_paper_data_quality.json")
    candidate_csv: Path | None = None


def _empty_metrics() -> dict[str, int | None]:
    return {field: None for field in FUNNEL_FIELDS}


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


class StrategyFunnelAnalyzer:
    """Analyze immutable snapshots without importing or calling bot code."""

    def __init__(self, root: str | Path, paths: SourcePaths | None = None) -> None:
        self.root = Path(root).resolve()
        self.paths = paths or SourcePaths()
        self.quality_issues: list[dict[str, Any]] = []
        self.source_manifest: list[dict[str, Any]] = []

    def _path(self, path: Path | None) -> Path | None:
        if path is None:
            return None
        return path if path.is_absolute() else self.root / path

    def _stable_bytes(self, relative: Path, *, required: bool = False) -> bytes | None:
        path = self._path(relative)
        assert path is not None
        if not path.exists():
            self.quality_issues.append({
                "code": "SOURCE_MISSING", "severity": "ERROR" if required else "INFO",
                "source": str(relative),
            })
            self.source_manifest.append({"path": str(relative), "status": "missing"})
            return None
        try:
            first = path.read_bytes()
            second = path.read_bytes()
        except OSError as exc:
            self.quality_issues.append({
                "code": "SOURCE_UNREADABLE", "severity": "ERROR", "source": str(relative),
                "error_type": type(exc).__name__,
            })
            return None
        if first != second:
            self.quality_issues.append({
                "code": "SOURCE_CHANGED_DURING_READ", "severity": "ERROR",
                "source": str(relative),
            })
            self.source_manifest.append({"path": str(relative), "status": "unstable"})
            return None
        digest = hashlib.sha256(first).hexdigest()
        self.source_manifest.append({
            "path": str(relative), "status": "read", "bytes": len(first), "sha256": digest,
        })
        return first

    def _json(self, relative: Path, *, required: bool = False) -> Any:
        raw = self._stable_bytes(relative, required=required)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self.quality_issues.append({
                "code": "INVALID_JSON", "severity": "ERROR", "source": str(relative),
                "error_type": type(exc).__name__,
            })
            return None

    def _csv(self, relative: Path, *, required: bool = False) -> list[dict[str, str]]:
        raw = self._stable_bytes(relative, required=required)
        if raw is None:
            return []
        try:
            return list(csv.DictReader(raw.decode("utf-8").splitlines()))
        except (UnicodeDecodeError, csv.Error) as exc:
            self.quality_issues.append({
                "code": "INVALID_CSV", "severity": "ERROR", "source": str(relative),
                "error_type": type(exc).__name__,
            })
            return []

    def _jsonl(self, relative: Path) -> list[dict[str, Any]]:
        raw = self._stable_bytes(relative)
        if raw is None or not raw.strip():
            return []
        rows: list[dict[str, Any]] = []
        try:
            lines = raw.decode("utf-8", errors="strict").splitlines()
        except UnicodeDecodeError as exc:
            self.quality_issues.append({
                "code": "INVALID_JSONL_ENCODING", "severity": "ERROR",
                "source": str(relative), "error_type": type(exc).__name__,
            })
            return []
        for line_number, line in enumerate(lines, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                self.quality_issues.append({
                    "code": "INVALID_JSONL_RECORD", "severity": "ERROR",
                    "source": str(relative), "line": line_number,
                })
                continue
            if not isinstance(value, dict):
                self.quality_issues.append({
                    "code": "NON_OBJECT_JSONL_RECORD", "severity": "ERROR",
                    "source": str(relative), "line": line_number,
                })
                continue
            rows.append(value)
        return rows

    @staticmethod
    def _dataset_row(dataset: str, strategy: str) -> dict[str, Any]:
        return {
            "dataset_scope": dataset,
            "strategy": strategy,
            **_empty_metrics(),
            "reject_reasons": [],
            "provenance": [],
            "missing_stages": [],
        }

    def _backtest_views(self) -> list[dict[str, Any]]:
        payload = self._json(self.paths.backtest_funnel, required=True)
        strategy_rows = payload.get("strategies", {}) if isinstance(payload, dict) else {}
        source_day = str(payload.get("day_utc") or "undated") if isinstance(payload, dict) else "undated"
        dataset_scope = f"backtest_funnel_{source_day}"
        views: list[dict[str, Any]] = []
        for strategy in ACTIVE_STRATEGIES:
            row = self._dataset_row(dataset_scope, strategy)
            values = strategy_rows.get(strategy)
            if isinstance(values, dict):
                candidates = _safe_int(values.get("candidates"))
                score_pass = _safe_int(values.get("candidates_go"))
                plans = _safe_int(values.get("plans"))
                executable = _safe_int(values.get("plans_executable"))
                row.update({
                    "detected": candidates,
                    "score_pass": score_pass,
                    "score_fail": max(0, candidates - score_pass),
                    "planner_pass": executable,
                    "planner_fail": max(0, plans - executable),
                    "executable": executable,
                    "provenance": [str(self.paths.backtest_funnel)],
                })
            row["missing_stages"] = [
                field for field in FUNNEL_FIELDS if row[field] is None
            ]
            views.append(row)
        return views

    def _forward_view(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        events = self._jsonl(self.paths.forward_events)
        outcomes = self._csv(self.paths.forward_outcomes)
        quality = self._json(self.paths.forward_quality) or {}

        event_ids = [str(event.get("event_id") or "") for event in events]
        duplicate_event_ids = len(event_ids) - len(set(event_ids))
        opened = {str(event.get("trade_id")) for event in events if event.get("event_type") == "TRADE_OPENED"}
        closed = {str(event.get("trade_id")) for event in events if event.get("event_type") == "TRADE_CLOSED"}
        close_without_open = sorted(closed - opened)
        open_without_close = sorted(opened - closed)
        if duplicate_event_ids:
            self.quality_issues.append({
                "code": "DUPLICATE_FORWARD_EVENT_ID", "severity": "ERROR",
                "count": duplicate_event_ids,
            })
        if close_without_open:
            self.quality_issues.append({
                "code": "FORWARD_CLOSE_WITHOUT_OPEN", "severity": "ERROR",
                "count": len(close_without_open),
            })

        events_by_strategy: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            strategy = str(payload.get("strategy") or "")
            if strategy in ACTIVE_STRATEGIES:
                events_by_strategy[strategy].append(event)

        outcomes_by_strategy: dict[str, list[dict[str, str]]] = defaultdict(list)
        for outcome in outcomes:
            if outcome.get("dataset") != "forward_paper":
                self.quality_issues.append({
                    "code": "FORWARD_DATASET_MIXING", "severity": "ERROR",
                    "trade_id": outcome.get("trade_id", ""),
                })
                continue
            strategy = outcome.get("strategy", "")
            if strategy in ACTIVE_STRATEGIES:
                outcomes_by_strategy[strategy].append(outcome)

        outcome_ids = [row.get("trade_id", "") for row in outcomes]
        duplicate_outcomes = len(outcome_ids) - len(set(outcome_ids))
        if duplicate_outcomes:
            self.quality_issues.append({
                "code": "DUPLICATE_FORWARD_OUTCOME", "severity": "ERROR",
                "count": duplicate_outcomes,
            })

        views: list[dict[str, Any]] = []
        for strategy in ACTIVE_STRATEGIES:
            row = self._dataset_row("forward_paper_current", strategy)
            strategy_events = events_by_strategy[strategy]
            strategy_outcomes = outcomes_by_strategy[strategy]
            event_open_ids = {
                str(event.get("trade_id")) for event in strategy_events
                if event.get("event_type") == "TRADE_OPENED"
            }
            wins = losses = be = 0
            for outcome in strategy_outcomes:
                result_r = _safe_float(outcome.get("result_r"))
                exit_reason = str(outcome.get("final_exit_reason") or "").upper()
                if "BREAK_EVEN" in exit_reason or exit_reason == "BE":
                    be += 1
                elif result_r is not None and result_r > 0:
                    wins += 1
                elif result_r is not None and result_r < 0:
                    losses += 1
            row.update({
                "forward_open": len(event_open_ids),
                "forward_closed": len(strategy_outcomes),
                "wins": wins,
                "losses": losses,
                "be": be,
                "provenance": [str(self.paths.forward_events), str(self.paths.forward_outcomes)],
            })
            row["missing_stages"] = [field for field in FUNNEL_FIELDS if row[field] is None]
            views.append(row)

        return views, {
            "event_count": len(events),
            "outcome_count": len(outcomes),
            "duplicate_event_ids_observed": duplicate_event_ids,
            "duplicate_outcomes_observed": duplicate_outcomes,
            "open_without_close": len(open_without_close),
            "close_without_open": len(close_without_open),
            "reported_event_chain_valid": quality.get("event_chain_valid"),
            "reported_duplicate_event_ids": quality.get("duplicate_event_ids"),
            "reported_incomplete_trades": len(quality.get("incomplete_trades") or []),
            "outcome_dataset_hash": quality.get("outcome_dataset_hash"),
        }

    @staticmethod
    def _trade_identity(row: dict[str, Any]) -> tuple[str, ...]:
        order_id = str(row.get("exchange_truth_order_id") or row.get("exchange_order_id") or "").strip()
        if order_id:
            return ("order", order_id)
        return (
            "fields", str(row.get("strategy") or ""), str(row.get("symbol") or ""),
            str(row.get("direction") or ""), str(row.get("opened_at") or ""),
            str(row.get("closed_at") or ""), str(row.get("net_pnl") or ""),
        )

    def _exchange_views(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        payload = self._json(self.paths.exchange_trades)
        rows = payload if isinstance(payload, list) else []
        seen: set[tuple[str, ...]] = set()
        duplicates = 0
        unique_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            identity = self._trade_identity(row)
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            unique_rows.append(row)
        if duplicates:
            self.quality_issues.append({
                "code": "DUPLICATE_EXCHANGE_TRADE_IDENTITY", "severity": "WARNING",
                "count": duplicates,
            })

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        unmapped = 0
        for row in unique_rows:
            strategy = str(row.get("strategy") or "")
            if strategy in ACTIVE_STRATEGIES:
                grouped[strategy].append(row)
            else:
                unmapped += 1

        views: list[dict[str, Any]] = []
        for strategy in ACTIVE_STRATEGIES:
            strategy_rows = grouped[strategy]
            wins = losses = be = 0
            for item in strategy_rows:
                result = str(item.get("result") or "").upper()
                close_reason = str(item.get("close_reason") or "").upper()
                net = _safe_float(item.get("net_pnl"))
                if result == "BE" or "BREAK_EVEN" in close_reason:
                    be += 1
                elif net is not None and net > 0:
                    wins += 1
                elif net is not None and net < 0:
                    losses += 1
            row = self._dataset_row("exchange_internal_attribution", strategy)
            row.update({
                "exchange_trades": len(strategy_rows),
                "wins": wins,
                "losses": losses,
                "be": be,
                "provenance": [str(self.paths.exchange_trades)],
            })
            row["missing_stages"] = [field for field in FUNNEL_FIELDS if row[field] is None]
            views.append(row)
        return views, {
            "rows": len(rows), "unique_rows": len(unique_rows), "duplicates": duplicates,
            "unmapped_strategy_rows": unmapped,
        }

    def _decision_quality(self) -> dict[str, Any]:
        payload = self._json(self.paths.decisions)
        rows = payload if isinstance(payload, list) else []
        valid = 0
        shifted = 0
        invalid = 0
        allowed_directions = {"LONG", "SHORT"}
        allowed_verdicts = {"GO", "WATCH", "NO_GO", "EXECUTABLE", "BLOCKED"}
        for row in rows:
            if not isinstance(row, dict):
                invalid += 1
                continue
            strategy = str(row.get("strategy") or "")
            direction = str(row.get("direction") or "")
            verdict = str(row.get("verdict") or "")
            if strategy in ACTIVE_STRATEGIES and direction in allowed_directions and verdict in allowed_verdicts:
                valid += 1
            elif direction in ACTIVE_STRATEGIES and verdict in allowed_directions:
                shifted += 1
            else:
                invalid += 1
        if shifted:
            self.quality_issues.append({
                "code": "SEMANTICALLY_SHIFTED_DECISION_ROWS", "severity": "ERROR",
                "source": str(self.paths.decisions), "count": shifted,
                "action": "excluded from funnel counts",
            })
        if invalid:
            self.quality_issues.append({
                "code": "INVALID_DECISION_ROWS", "severity": "WARNING",
                "source": str(self.paths.decisions), "count": invalid,
                "action": "excluded from funnel counts",
            })
        return {"rows": len(rows), "valid_rows": valid, "shifted_rows": shifted, "invalid_rows": invalid}

    @staticmethod
    def _normalize_reason(reason: str) -> str:
        reason = re.sub(r"\([^)]*\)", "", reason)
        reason = re.sub(r"[-+]?\d+(?:\.\d+)?(?:%|bps|R)?", "<N>", reason)
        return re.sub(r"\s+", " ", reason).strip(" |")[:300]

    @staticmethod
    def _reason_classification(reason: str) -> str:
        lowered = reason.lower()
        blocking_markers = (
            "block", "below", "disabled", "hard-pause", "kill-switch", "negative expectancy",
            "no_go", "no-go", "opposes", "paused", "too ", "missing", "weak", "poor",
            "loss_guard", "risk-off", "watch blocks", "verdict=watch", "verdict=no_go",
        )
        return "BLOCKING_OR_ADVERSE" if any(marker in lowered for marker in blocking_markers) else "CONTEXT"

    def _reject_analysis(self) -> list[dict[str, Any]]:
        payload = self._json(self.paths.trade_funnel)
        raw_rejects = payload.get("plan_rejects", {}) if isinstance(payload, dict) else {}
        aggregated: dict[tuple[str, str], dict[str, Any]] = {}
        for raw, raw_count in raw_rejects.items():
            if not isinstance(raw, str):
                continue
            strategy_match = re.search(r"(?:^| \| )strategy=([^ |]+)", raw)
            symbol_match = re.search(r"PLAN_REJECT \| ([^ |]+)", raw)
            strategy = strategy_match.group(1) if strategy_match else "UNKNOWN"
            symbol = symbol_match.group(1) if symbol_match else "UNKNOWN"
            if strategy not in ACTIVE_STRATEGIES:
                continue
            count = max(1, _safe_int(raw_count, 1))
            reason_text = raw.split("reasons=", 1)[1] if "reasons=" in raw else "reason unavailable"
            reasons = [self._normalize_reason(part) for part in reason_text.split(" | ")]
            for reason in filter(None, reasons):
                key = (strategy, reason)
                item = aggregated.setdefault(key, {
                    "strategy": strategy, "reason": reason, "count": 0,
                    "symbols": Counter(), "sessions": ["UNKNOWN"],
                    "timeframes": ["UNKNOWN"], "classification": self._reason_classification(reason),
                })
                item["count"] += count
                item["symbols"][symbol] += count

        result: list[dict[str, Any]] = []
        for item in aggregated.values():
            result.append({
                **item,
                "symbols": dict(sorted(item["symbols"].items(), key=lambda pair: (-pair[1], pair[0]))),
            })
        return sorted(result, key=lambda item: (-item["count"], item["strategy"], item["reason"]))

    def _overlap(self) -> dict[str, Any]:
        pairs = [
            {"strategy_a": left, "strategy_b": right, "same_candle_count": None, "examples": []}
            for left, right in itertools.combinations(ACTIVE_STRATEGIES, 2)
        ]
        if self.paths.candidate_csv is None:
            return {
                "status": "INSUFFICIENT_DATA",
                "reason": "No structured pre-selector candidate dataset was supplied; current telemetry stores only selected candidates.",
                "key": ["timestamp", "symbol", "direction"],
                "pairs": pairs,
            }

        rows = self._csv(self.paths.candidate_csv)
        grouped: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        examples: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        counts: Counter[tuple[str, str]] = Counter()
        for row in rows:
            strategy = row.get("strategy", "")
            timestamp = row.get("candle_timestamp") or row.get("timestamp") or ""
            symbol = row.get("symbol", "")
            direction = row.get("direction", "")
            if strategy in ACTIVE_STRATEGIES and timestamp and symbol and direction:
                grouped[(timestamp, symbol, direction)].add(strategy)
        for (timestamp, symbol, direction), strategies in grouped.items():
            for pair in itertools.combinations(sorted(strategies), 2):
                counts[pair] += 1
                if len(examples[pair]) < 5:
                    examples[pair].append({
                        "timestamp": timestamp, "symbol": symbol, "direction": direction,
                    })
        for item in pairs:
            key = (item["strategy_a"], item["strategy_b"])
            item["same_candle_count"] = counts[key]
            item["examples"] = examples[key]
        return {
            "status": "MEASURED_EXACT_TIMESTAMP",
            "reason": "Pairs share timestamp, symbol and direction in the supplied candidate dataset.",
            "key": ["timestamp", "symbol", "direction"],
            "pairs": pairs,
        }

    @staticmethod
    def _combined_strategy(
        strategy: str,
        backtest: dict[str, Any],
        forward: dict[str, Any],
        exchange: dict[str, Any],
        reject_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        combined = _empty_metrics()
        provenance: dict[str, str] = {}
        for field in ("detected", "selector_pass", "selector_fail", "score_pass", "score_fail", "risk_pass", "risk_fail", "planner_pass", "planner_fail", "executable"):
            combined[field] = backtest[field]
            if backtest[field] is not None:
                provenance[field] = backtest["dataset_scope"]
        for field in ("forward_open", "forward_closed"):
            combined[field] = forward[field]
            provenance[field] = forward["dataset_scope"]
        for field in ("exchange_trades", "wins", "losses", "be"):
            combined[field] = exchange[field]
            provenance[field] = exchange["dataset_scope"]
        strategy_rejects = [
            {"reason": row["reason"], "count": row["count"], "classification": row["classification"]}
            for row in reject_rows
            if row["strategy"] == strategy and row["classification"] == "BLOCKING_OR_ADVERSE"
        ]
        return {
            "name": strategy,
            "metrics": combined,
            "metric_provenance": provenance,
            "not_a_single_cohort": True,
            "reject_reasons": strategy_rejects,
            "text_funnel": StrategyFunnelAnalyzer._text_funnel(strategy, combined),
        }

    @staticmethod
    def _text_funnel(strategy: str, metrics: dict[str, Any]) -> str:
        labels = [
            ("detected", "detected"), ("selector_pass", "selector pass"),
            ("score_pass", "score pass"), ("risk_pass", "risk pass"),
            ("planner_pass", "planner pass"), ("executable", "executable"),
            ("forward_open", "paper opened"), ("forward_closed", "paper closed"),
        ]
        lines = [strategy]
        for field, label in labels:
            value = metrics.get(field)
            lines.extend([str(value) if value is not None else "?", "↓", label])
        return "\n".join(lines)

    def analyze(self) -> dict[str, Any]:
        self.quality_issues = []
        self.source_manifest = []
        backtest_views = self._backtest_views()
        forward_views, forward_quality = self._forward_view()
        exchange_views, exchange_quality = self._exchange_views()
        decision_quality = self._decision_quality()
        reject_rows = self._reject_analysis()
        overlap = self._overlap()

        backtest_map = {row["strategy"]: row for row in backtest_views}
        forward_map = {row["strategy"]: row for row in forward_views}
        exchange_map = {row["strategy"]: row for row in exchange_views}
        strategies = [
            self._combined_strategy(
                strategy, backtest_map[strategy], forward_map[strategy], exchange_map[strategy], reject_rows
            )
            for strategy in ACTIVE_STRATEGIES
        ]
        dataset_views = backtest_views + forward_views + exchange_views
        for row in dataset_views:
            row["reject_reasons"] = [
                {"reason": item["reason"], "count": item["count"], "classification": item["classification"]}
                for item in reject_rows
                if item["strategy"] == row["strategy"] and item["classification"] == "BLOCKING_OR_ADVERSE"
            ] if row["dataset_scope"].startswith("backtest") else []

        findings: list[str] = []
        for row in backtest_views:
            detected = row["detected"]
            executable = row["executable"]
            if detected is not None and executable is not None and detected > 0:
                not_executable = max(0, detected - executable)
                findings.append(
                    f"{row['strategy']}: {not_executable}/{detected} observed candidates "
                    f"({not_executable / detected:.1%}) did not become executable in the backtest funnel snapshot."
                )
            else:
                findings.append(f"{row['strategy']}: no candidate row exists in the backtest funnel snapshot.")
        if forward_quality["outcome_count"] == 0:
            findings.append("The current forward-paper dataset contains no completed outcome.")
        findings.append(
            f"Decision snapshot quality: {decision_quality['valid_rows']} valid, "
            f"{decision_quality['shifted_rows']} semantically shifted and "
            f"{decision_quality['invalid_rows']} otherwise invalid rows."
        )
        findings.append(
            "Selector-pass and risk-pass conversion rates are not measurable from the available structured datasets."
        )
        findings.append(
            "Detector overlap is not measurable without a pre-selector multi-candidate candle dataset."
            if overlap["status"] == "INSUFFICIENT_DATA"
            else "Detector overlap was measured using exact timestamp, symbol and direction keys."
        )

        report = {
            "schema_version": "1.0",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "analysis_policy": {
                "read_only": True,
                "imports_runtime_modules": False,
                "unknown_counts_are_null": True,
                "datasets_kept_separate": True,
                "current_runtime_log_files_read": False,
            },
            "active_strategies": list(ACTIVE_STRATEGIES),
            "strategy_definitions": STRATEGY_DEFINITIONS,
            "pipeline_architecture": PIPELINE_ARCHITECTURE,
            "strategies": strategies,
            "dataset_views": dataset_views,
            "reject_analysis": reject_rows,
            "overlap_analysis": overlap,
            "data_quality": {
                "issues": self.quality_issues,
                "forward_paper": forward_quality,
                "exchange_internal": exchange_quality,
                "decision_snapshots": decision_quality,
                "missing_stage_events": ["detector attempts", "selector pass/fail", "risk pass/fail"],
                "source_manifest": self.source_manifest,
            },
            "findings": findings,
            "remaining_questions": [
                "Which durable event should define a detector attempt before selector competition?",
                "Where should selector and risk gate outcomes be stored with candle timestamp, session and timeframe?",
                "Which exchange export rows can be mapped to strategy without inference?",
            ],
        }
        reproducible = {key: value for key, value in report.items() if key != "generated_at_utc"}
        report["analysis_hash"] = hashlib.sha256(
            json.dumps(reproducible, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return report

    @staticmethod
    def write_json(report: dict[str, Any], path: str | Path) -> None:
        _atomic_write(Path(path), json.dumps(report, indent=2, sort_keys=True) + "\n")

    @staticmethod
    def write_csv(report: dict[str, Any], path: str | Path) -> None:
        fields = ["dataset_scope", "strategy", *FUNNEL_FIELDS, "reject_reasons", "missing_stages", "provenance"]
        rows: list[list[Any]] = [fields]
        for item in report["dataset_views"]:
            rows.append([
                item.get(field) if field not in {"reject_reasons", "missing_stages", "provenance"} else
                json.dumps(item.get(field), sort_keys=True, separators=(",", ":"))
                for field in fields
            ])
        from io import StringIO

        buffer = StringIO(newline="")
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerows(rows)
        _atomic_write(Path(path), buffer.getvalue())
