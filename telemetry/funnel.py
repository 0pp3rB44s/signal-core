"""Structured, append-only funnel telemetry with fail-open runtime integration."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from candidate_lifecycle import deterministic_candidate_id, lifecycle_key
from telemetry.safe_io import atomic_write_json, file_lock


SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1
GENESIS = "GENESIS"
EVENT_TYPES = frozenset({
    "DETECTOR_ATTEMPT", "DETECTOR_DECISION", "SELECTOR_DECISION",
    "SCORING_DECISION", "RISK_DECISION", "PLANNER_DECISION",
    "EXECUTABLE_DECISION", "FORWARD_PAPER_LINK",
    "OUTCOME_LINK",
})
EVENT_ORDER = {
    event_type: index for index, event_type in enumerate((
        "DETECTOR_ATTEMPT", "DETECTOR_DECISION", "SELECTOR_DECISION",
        "SCORING_DECISION", "RISK_DECISION", "PLANNER_DECISION",
        "EXECUTABLE_DECISION", "FORWARD_PAPER_LINK", "OUTCOME_LINK",
    ))
}
PASS_FAIL = frozenset({"PASS", "FAIL"})
REASON_CODES = frozenset({
    "ATTEMPTED", "DETECTED", "NO_DETECTION", "DETECTOR_ERROR",
    "SELECTED", "NOT_SELECTED", "NO_CANDIDATES", "SELECTOR_ERROR",
    "SCORE_GO", "SCORE_WATCH", "SCORE_NO_GO", "RISK_ALLOWED",
    "RISK_BLOCKED", "PLAN_EXECUTABLE", "PLAN_BLOCKED", "FORWARD_LINKED",
    "FORWARD_NOT_ELIGIBLE", "UNKNOWN_DECISION",
    "FALLBACK_ATTEMPTED", "FALLBACK_DETECTED", "FALLBACK_NO_DETECTION",
    "SIGNAL_COOLDOWN", "RECENT_CLOSE_COOLDOWN", "SYMBOL_COOLDOWN",
    "DUPLICATE_CONTINUATION", "WEEKLY_FREEZE", "DAILY_DEFENSIVE",
    "CONSECUTIVE_LOSS_LIMIT", "EXPECTANCY_BLOCK", "SYMBOL_EXPECTANCY_PAUSE",
    "HTF_OPPOSITION", "SCORE_THRESHOLD", "ORDERBOOK_RISK",
    "EXECUTION_COST", "NET_EDGE", "RR_GEOMETRY", "MIN_NOTIONAL",
})
REQUIRED_FIELDS = (
    "schema_version", "event_id", "lifecycle_key", "scan_id", "candidate_id", "event_type",
    "event_timestamp_utc", "strategy", "symbol", "direction", "timeframe",
    "candle_open_timestamp", "signal_timestamp", "session", "regime",
    "pass_fail", "primary_reason_code", "secondary_reason_codes",
    "config_hash", "git_commit",
)
LEGACY_REQUIRED_FIELDS = tuple(field for field in REQUIRED_FIELDS if field != "lifecycle_key")

log = logging.getLogger("funnel_telemetry")


class FunnelTelemetryCorruptionError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_scan_id() -> str:
    """Create one opaque ID to be reused for an entire scan cycle."""
    return str(uuid.uuid4())


def deterministic_event_id(scan_id: str, candidate_id: str, event_type: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"funnel:{scan_id}:{candidate_id}:{event_type}"))


def safe_git_commit(root: str | Path = ".") -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True,
            text=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"


def safe_config_hash(settings: Any) -> str:
    """Hash only non-secret funnel-relevant settings from an explicit allowlist."""
    names = (
        "primary_granularity", "confirmation_granularity", "enable_shorts",
        "enabled_strategies", "disabled_strategies", "strategy_score_go_threshold",
        "strategy_score_watch_threshold", "planner_min_rr", "planner_min_rr_to_tp1",
        "forward_paper_only", "fast_lane_enabled", "fast_lane_granularity",
    )
    def normalized(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, tuple, set, frozenset)):
            return sorted(str(item) for item in value)
        return str(value)

    values = {name: normalized(getattr(settings, name, None)) for name in names}
    return content_hash(values)


def _validate_event(event: dict[str, Any]) -> None:
    missing = [key for key in REQUIRED_FIELDS if event.get(key) in (None, "")]
    if missing:
        raise ValueError(f"funnel event missing fields: {','.join(missing)}")
    if event["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported funnel schema_version")
    if event["event_type"] not in EVENT_TYPES:
        raise ValueError("unsupported funnel event_type")
    if event["pass_fail"] not in PASS_FAIL:
        raise ValueError("pass_fail must be PASS or FAIL")
    if event["primary_reason_code"] not in REASON_CODES:
        raise ValueError("unknown primary_reason_code")
    secondary = event["secondary_reason_codes"]
    if not isinstance(secondary, list) or any(code not in REASON_CODES for code in secondary):
        raise ValueError("unknown secondary_reason_codes")


def classify_reason_codes(values: Iterable[Any]) -> list[str]:
    """Map existing human-readable reasons to stable telemetry-only codes."""
    text = " | ".join(str(value).lower() for value in values if value not in (None, ""))
    rules = (
        ("WEEKLY_FREEZE", ("weekly freeze",)),
        ("DAILY_DEFENSIVE", ("daily defensive", "day_defensive")),
        ("CONSECUTIVE_LOSS_LIMIT", ("consecutive loss",)),
        ("SYMBOL_EXPECTANCY_PAUSE", ("symbol paused by expectancy",)),
        ("EXPECTANCY_BLOCK", ("expectancy", "hard-pause", "hard pause")),
        ("HTF_OPPOSITION", ("htf", "primary trend", "alignment")),
        ("SCORE_THRESHOLD", ("score below", "score verdict", "minimum score")),
        ("ORDERBOOK_RISK", ("orderbook",)),
        ("EXECUTION_COST", ("spread", "execution-cost", "execution cost")),
        ("NET_EDGE", ("net_edge", "net edge", "fees buffer")),
        ("RR_GEOMETRY", ("risk_reward", "risk reward", "rr", "geometry")),
        ("MIN_NOTIONAL", ("notional",)),
    )
    return [code for code, needles in rules if any(needle in text for needle in needles)]


def _validate_stored_event(
    event: dict[str, Any], *, expected_sequence: int, previous_hash: str,
) -> None:
    if event.get("schema_version") == LEGACY_SCHEMA_VERSION:
        missing = [key for key in LEGACY_REQUIRED_FIELDS if event.get(key) in (None, "")]
        if missing:
            raise FunnelTelemetryCorruptionError("legacy event missing fields")
    else:
        _validate_event(event)
    if event.get("sequence") != expected_sequence:
        raise FunnelTelemetryCorruptionError("invalid sequence")
    if event.get("previous_hash") != previous_hash:
        raise FunnelTelemetryCorruptionError("broken chain")
    supplied = event.get("event_hash")
    unsigned = {key: value for key, value in event.items() if key != "event_hash"}
    if supplied != content_hash(unsigned):
        raise FunnelTelemetryCorruptionError("checksum mismatch")


def _advance_lifecycle(
    event: dict[str, Any], stages: dict[str, tuple[int, str]],
) -> None:
    key = lifecycle_key(str(event["candidate_id"]), str(event["event_type"]))
    if event.get("lifecycle_key") != key:
        raise FunnelTelemetryCorruptionError("invalid lifecycle_key")
    if key in stages:
        raise FunnelTelemetryCorruptionError("duplicate lifecycle_key")
    rank = EVENT_ORDER[str(event["event_type"])]
    stages[key] = (rank, str(event["pass_fail"]))


class FunnelEventStore:
    """Interprocess-safe JSONL store that rejects corruption and duplicate IDs."""

    def __init__(
        self,
        path: str | Path = "data_store/funnel_events.jsonl",
        quality_path: str | Path = "reports/funnel_data_quality.json",
    ) -> None:
        self.path = Path(path)
        self.quality_path = Path(quality_path)
        self._index_initialized = False
        self._index_inode: int | None = None
        self._index_offset = 0
        self._sequence = 0
        self._last_hash = GENESIS
        self._event_ids: set[str] = set()
        self._lifecycle_stages: dict[str, tuple[int, str]] = {}
        self._lifecycle_tails: dict[str, tuple[str, str]] = {}

    def _reset_index(self) -> None:
        self._index_initialized = True
        self._index_inode = None
        self._index_offset = 0
        self._sequence = 0
        self._last_hash = GENESIS
        self._event_ids = set()
        self._lifecycle_stages = {}
        self._lifecycle_tails = {}

    def _index_event(self, event: dict[str, Any]) -> None:
        event_id = str(event["event_id"])
        if event_id in self._event_ids:
            raise FunnelTelemetryCorruptionError("duplicate event_id")
        if event.get("schema_version") == SCHEMA_VERSION:
            _advance_lifecycle(event, self._lifecycle_stages)
        key = str(event["candidate_id"])
        self._event_ids.add(event_id)
        self._lifecycle_tails[key] = (
            str(event["event_type"]), str(event["pass_fail"]),
        )
        self._sequence = int(event["sequence"])
        self._last_hash = str(event["event_hash"])

    def _sync_index_unlocked(self) -> None:
        if not self.path.exists():
            self._reset_index()
            return
        stat = self.path.stat()
        if (
            not self._index_initialized
            or self._index_inode != stat.st_ino
            or stat.st_size < self._index_offset
        ):
            self._reset_index()
            self._index_inode = stat.st_ino
        elif stat.st_size == self._index_offset:
            return

        with self.path.open("rb") as handle:
            handle.seek(self._index_offset)
            while True:
                raw = handle.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    raise FunnelTelemetryCorruptionError("truncated JSONL record")
                try:
                    event = json.loads(raw.decode("utf-8", errors="strict"))
                    _validate_stored_event(
                        event,
                        expected_sequence=self._sequence + 1,
                        previous_hash=self._last_hash,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    raise FunnelTelemetryCorruptionError("invalid JSONL record") from exc
                self._index_event(event)
            self._index_offset = handle.tell()
        self._index_inode = stat.st_ino

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        previous_hash = GENESIS
        event_ids: set[str] = set()
        lifecycle_stages: dict[str, tuple[int, str]] = {}
        try:
            handle = self.path.open("r", encoding="utf-8")
            with handle:
                for line_number, raw in enumerate(handle, 1):
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                        raise FunnelTelemetryCorruptionError(
                            f"invalid JSONL at line {line_number}"
                        ) from exc
                    try:
                        _validate_stored_event(
                            event,
                            expected_sequence=len(events) + 1,
                            previous_hash=previous_hash,
                        )
                        if str(event["event_id"]) in event_ids:
                            raise FunnelTelemetryCorruptionError("duplicate event_id")
                        if event.get("schema_version") == SCHEMA_VERSION:
                            _advance_lifecycle(event, lifecycle_stages)
                    except (ValueError, FunnelTelemetryCorruptionError) as exc:
                        raise FunnelTelemetryCorruptionError(
                            f"invalid event at line {line_number}: {exc}"
                        ) from exc
                    event_ids.add(str(event["event_id"]))
                    previous_hash = str(event["event_hash"])
                    visible = dict(event)
                    visible["identity_status"] = "LINKED" if event.get("schema_version") == SCHEMA_VERSION else "LEGACY_UNLINKED"
                    if event.get("schema_version") == LEGACY_SCHEMA_VERSION:
                        visible["candidate_id"] = ""
                    events.append(visible)
        except UnicodeDecodeError as exc:
            raise FunnelTelemetryCorruptionError("invalid JSONL encoding") from exc
        return events

    def read_events(self) -> list[dict[str, Any]]:
        with file_lock(self.path):
            return self._read_unlocked()

    def append(self, event: dict[str, Any]) -> bool:
        event = dict(event)
        event.setdefault("schema_version", SCHEMA_VERSION)
        if event.get("candidate_id") and event.get("event_type"):
            event.setdefault("lifecycle_key", lifecycle_key(event["candidate_id"], event["event_type"]))
        _validate_event(event)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.path):
            self._sync_index_unlocked()
            if str(event["event_id"]) in self._event_ids:
                self._write_quality_from_index(duplicate_event_ids=1)
                return False
            if event["lifecycle_key"] in self._lifecycle_stages:
                self._write_quality_from_index(duplicate_event_ids=0)
                return False
            trial_stages = dict(self._lifecycle_stages)
            _advance_lifecycle(event, trial_stages)
            stored = {key: event[key] for key in REQUIRED_FIELDS}
            if event.get("details"):
                stored["details"] = event["details"]
            stored["sequence"] = self._sequence + 1
            stored["previous_hash"] = self._last_hash
            stored["event_hash"] = content_hash(stored)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(stored) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            stat = self.path.stat()
            self._index_offset = stat.st_size
            self._index_inode = stat.st_ino
            self._index_event(stored)
            self._write_quality_from_index(duplicate_event_ids=0)
            return True

    def audit(self) -> dict[str, Any]:
        with file_lock(self.path):
            try:
                events = self._read_unlocked()
            except FunnelTelemetryCorruptionError as exc:
                quality = {
                    "schema_version": SCHEMA_VERSION, "checked_at_utc": utc_now(),
                    "event_chain_valid": False, "event_count": None,
                    "duplicate_event_ids": None, "missing_required_fields": None,
                    "lifecycle_violations": None, "incomplete_lifecycles": None,
                    "error_type": type(exc).__name__,
                }
                atomic_write_json(self.quality_path, quality, indent=2, sort_keys=True)
                return quality
            self._reset_index()
            self._sync_index_unlocked()
            return self._write_quality_from_index(duplicate_event_ids=0)

    def _incomplete_lifecycles(self) -> int:
        terminal = {
            ("DETECTOR_DECISION", "FAIL"),
            ("SELECTOR_DECISION", "FAIL"),
            ("FORWARD_PAPER_LINK", "PASS"),
            ("FORWARD_PAPER_LINK", "FAIL"),
            ("OUTCOME_LINK", "PASS"),
        }
        return sum(tail not in terminal for tail in self._lifecycle_tails.values())

    def _write_quality_from_index(self, duplicate_event_ids: int) -> dict[str, Any]:
        quality = {
            "schema_version": SCHEMA_VERSION, "checked_at_utc": utc_now(),
            "event_chain_valid": True, "event_count": self._sequence,
            "duplicate_event_ids": duplicate_event_ids,
            "missing_required_fields": 0,
            "lifecycle_violations": 0,
            "incomplete_lifecycles": self._incomplete_lifecycles(),
            "last_event_hash": self._last_hash,
        }
        atomic_write_json(self.quality_path, quality, indent=2, sort_keys=True)
        return quality


class FunnelTelemetry:
    """Small fail-open facade used by the trading loop."""

    def __init__(self, settings: Any = None, store: FunnelEventStore | None = None) -> None:
        if isinstance(settings, FunnelEventStore) and store is None:
            store, settings = settings, None
        self.store = store or FunnelEventStore()
        self.config_hash = safe_config_hash(settings)
        self.git_commit = safe_git_commit()

    def emit_safe(self, **event: Any) -> bool:
        try:
            return self.store.append(event)
        except Exception as exc:  # telemetry must never interrupt trading
            log.warning("FUNNEL_TELEMETRY_FAILED | error_type=%s", type(exc).__name__)
            return False

    def event(
        self, *, scan_id: str, candidate_id: str, event_type: str, strategy: str,
        symbol: str, direction: str, timeframe: str, candle_open_timestamp: str,
        signal_timestamp: str, session: str, regime: str, passed: bool,
        primary_reason_code: str, secondary_reason_codes: Iterable[str] = (),
        details: dict[str, Any] | None = None,
    ) -> bool:
        return self.emit_safe(
            schema_version=SCHEMA_VERSION,
            event_id=deterministic_event_id(scan_id, candidate_id, event_type),
            lifecycle_key=lifecycle_key(candidate_id, event_type),
            scan_id=scan_id, candidate_id=candidate_id, event_type=event_type,
            event_timestamp_utc=utc_now(), strategy=strategy, symbol=symbol,
            direction=direction, timeframe=timeframe,
            candle_open_timestamp=str(candle_open_timestamp),
            signal_timestamp=signal_timestamp, session=session, regime=regime,
            pass_fail="PASS" if passed else "FAIL",
            primary_reason_code=primary_reason_code,
            secondary_reason_codes=list(secondary_reason_codes),
            config_hash=self.config_hash, git_commit=self.git_commit,
            details=details or {},
        )

    def record(
        self, candidate: Any, event_type: str, *, scan_id: str, passed: bool,
        reason: str, plan_id: str = "", trade_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> bool:
        candidate_id = str(getattr(candidate, "candidate_id", "") or "")
        if not candidate_id:
            raise ValueError("schema v2 candidate requires candidate_id")
        reason_codes = {
            "DETECTOR_DECISION": "DETECTED" if passed else "NO_DETECTION",
            "SELECTOR_DECISION": "SELECTED" if passed else "NOT_SELECTED",
            "SCORING_DECISION": "SCORE_GO" if passed else "SCORE_NO_GO",
            "RISK_DECISION": "RISK_ALLOWED" if passed else "RISK_BLOCKED",
            "PLANNER_DECISION": "PLAN_EXECUTABLE" if passed else "PLAN_BLOCKED",
            "EXECUTABLE_DECISION": "PLAN_EXECUTABLE" if passed else "PLAN_BLOCKED",
            "FORWARD_PAPER_LINK": "FORWARD_LINKED" if passed else "FORWARD_NOT_ELIGIBLE",
            "OUTCOME_LINK": "FORWARD_LINKED" if passed else "UNKNOWN_DECISION",
        }
        detail_payload = dict(details or {})
        if plan_id:
            detail_payload["plan_id"] = plan_id
        if trade_id:
            detail_payload["trade_id"] = trade_id
        timestamp = str(getattr(candidate, "candidate_candle_open_timestamp_ms", "") or "")
        return self.event(
            scan_id=scan_id, candidate_id=candidate_id, event_type=event_type,
            strategy=str(getattr(candidate, "strategy", "UNKNOWN")),
            symbol=str(getattr(candidate, "symbol", "UNKNOWN")),
            direction=str(getattr(candidate, "direction", "UNKNOWN")),
            timeframe=str(getattr(candidate, "primary_granularity", "UNKNOWN")),
            candle_open_timestamp=timestamp, signal_timestamp=utc_now(),
            session="UNKNOWN", regime="UNKNOWN", passed=passed,
            primary_reason_code=reason_codes[event_type], details={"reason": reason, **detail_payload},
        )


def snapshot_context(snapshot: Any) -> dict[str, str]:
    candles = list(getattr(getattr(snapshot, "primary", None), "candles", []) or [])
    candle_timestamp = str(getattr(candles[-1], "timestamp_ms", "UNKNOWN")) if candles else "UNKNOWN"
    context = dict(getattr(snapshot, "context", {}) or {})
    return {
        "symbol": str(getattr(snapshot, "symbol", "UNKNOWN")),
        "timeframe": str(getattr(getattr(snapshot, "primary", None), "granularity", "UNKNOWN")),
        "candle_open_timestamp": candle_timestamp,
        "signal_timestamp": utc_now(),
        "session": str(context.get("session") or "UNKNOWN"),
        "regime": str(context.get("regime") or getattr(snapshot, "alignment", "UNKNOWN")),
    }
