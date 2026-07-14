from __future__ import annotations

import csv
from collections import Counter
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telemetry.safe_io import atomic_write_json, file_lock


SCHEMA_VERSION = 1
DATASET = "forward_paper"


class ForwardPaperCorruptionError(RuntimeError):
    pass


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class ForwardPaperEventStore:
    """Append-only, interprocess-safe and hash-chained JSONL event store."""

    def __init__(self, path: str | Path = "data_store/forward_paper_events.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_unlocked(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        previous_hash = "GENESIS"
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                if not raw.strip():
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ForwardPaperCorruptionError(
                        f"invalid JSONL at line {line_number}"
                    ) from exc
                if event.get("dataset") != DATASET or event.get("schema_version") != SCHEMA_VERSION:
                    raise ForwardPaperCorruptionError(f"invalid dataset/schema at line {line_number}")
                if event.get("sequence") != len(events) + 1:
                    raise ForwardPaperCorruptionError(f"non-contiguous sequence at line {line_number}")
                if event.get("previous_hash") != previous_hash:
                    raise ForwardPaperCorruptionError(f"broken previous_hash at line {line_number}")
                supplied_hash = event.get("event_hash")
                unsigned = {key: value for key, value in event.items() if key != "event_hash"}
                calculated_hash = content_hash(unsigned)
                if supplied_hash != calculated_hash:
                    raise ForwardPaperCorruptionError(f"checksum mismatch at line {line_number}")
                previous_hash = str(supplied_hash)
                events.append(event)
        return events

    def read_events(self) -> list[dict[str, Any]]:
        with file_lock(self.path):
            return self._read_unlocked()

    def append(self, event: dict[str, Any]) -> bool:
        required = {"event_id", "trade_id", "plan_id", "event_type", "timestamp", "payload"}
        missing = sorted(key for key in required if event.get(key) in (None, ""))
        if missing:
            raise ValueError(f"forward-paper event missing fields: {','.join(missing)}")
        if event.get("dataset", DATASET) != DATASET:
            raise ValueError("non-paper event rejected")

        with file_lock(self.path):
            events = self._read_unlocked()
            if any(existing.get("event_id") == event["event_id"] for existing in events):
                return False
            stored = {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "sequence": len(events) + 1,
                "event_id": str(event["event_id"]),
                "trade_id": str(event["trade_id"]),
                "plan_id": str(event["plan_id"]),
                "event_type": str(event["event_type"]),
                "timestamp": str(event["timestamp"]),
                "payload": event["payload"],
                "previous_hash": events[-1]["event_hash"] if events else "GENESIS",
            }
            stored["event_hash"] = content_hash(stored)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(stored) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True


OUTCOME_FIELDS = [
    "schema_version", "dataset", "trade_id", "plan_id", "strategy", "symbol", "direction",
    "timeframe", "regime", "session", "config_version_hash", "git_commit", "signal_timestamp",
    "entry_timestamp", "planned_entry", "simulated_fill", "initial_stop", "initial_targets",
    "initial_risk_price", "initial_risk_currency", "initial_risk_r", "expected_reward_to_risk", "expected_move_bps",
    "spread_bps", "liquidity_assumption", "expected_fees", "volatility_rank", "strategy_score",
    "strategy_features", "exit_timestamp", "exit_price", "gross_pnl", "fees", "funding",
    "slippage", "slippage_pct", "net_pnl", "result_r", "holding_duration_seconds", "final_exit_reason",
    "mfe_price", "mfe_pct", "mfe_timestamp", "mae_price", "mae_pct", "mae_timestamp",
    "maximum_profit_giveback", "tp_touches", "sl_touches", "break_even_activated",
    "profit_lock_activated", "failed_continuation", "partial_exit_count", "event_count",
    "outcome_hash",
]


class ForwardPaperReconstructor:
    def __init__(
        self,
        store: ForwardPaperEventStore,
        outcomes_path: str | Path = "data_store/forward_paper_outcomes.csv",
        quality_path: str | Path = "reports/forward_paper_data_quality.json",
    ) -> None:
        self.store = store
        self.outcomes_path = Path(outcomes_path)
        self.quality_path = Path(quality_path)

    def reconstruct(self) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        events = self.store.read_events()
        by_trade: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            by_trade.setdefault(str(event["trade_id"]), []).append(event)

        outcomes: list[dict[str, Any]] = []
        incomplete: list[dict[str, Any]] = []
        for trade_id, trade_events in sorted(by_trade.items()):
            opened = next((event for event in trade_events if event["event_type"] == "TRADE_OPENED"), None)
            closed = next((event for event in reversed(trade_events) if event["event_type"] == "TRADE_CLOSED"), None)
            if opened is None or closed is None:
                incomplete.append({
                    "trade_id": trade_id,
                    "has_open": opened is not None,
                    "has_close": closed is not None,
                    "event_count": len(trade_events),
                })
                continue
            try:
                outcomes.append(self._outcome(opened, closed, trade_events))
            except (KeyError, TypeError, ValueError) as exc:
                incomplete.append({
                    "trade_id": trade_id,
                    "has_open": True,
                    "has_close": True,
                    "event_count": len(trade_events),
                    "reason": f"critical field invalid: {type(exc).__name__}",
                })

        outcomes.sort(key=lambda row: (row["exit_timestamp"], row["trade_id"]))
        dataset_hash = content_hash([{key: value for key, value in row.items() if key != "outcome_hash"} for row in outcomes])
        quality = {
            "schema_version": SCHEMA_VERSION,
            "dataset": DATASET,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event_count": len(events),
            "trade_count": len(by_trade),
            "complete_outcomes": len(outcomes),
            "incomplete_trades": incomplete,
            "duplicate_event_ids": len(events) - len({event["event_id"] for event in events}),
            "event_type_counts": dict(sorted(Counter(event["event_type"] for event in events).items())),
            "event_chain_valid": True,
            "outcome_dataset_hash": dataset_hash,
            "outcome_schema_fields": OUTCOME_FIELDS,
            "critical_outcome_field_coverage": {
                field: round(sum(row.get(field) not in (None, "") for row in outcomes) / len(outcomes), 4) if outcomes else None
                for field in (
                    "trade_id", "plan_id", "strategy", "symbol", "direction", "timeframe",
                    "signal_timestamp", "simulated_fill", "initial_stop", "initial_risk_currency",
                    "exit_timestamp", "exit_price", "net_pnl", "result_r", "final_exit_reason",
                )
            },
            "config_version_hashes": sorted({row["config_version_hash"] for row in outcomes}),
            "git_commits": sorted({row["git_commit"] for row in outcomes}),
            "historical_migration": {
                "imported": 0,
                "status": "NO_RELIABLE_FORWARD_PAPER_SOURCE_FOUND",
                "note": "LIVE, exchange and backtest rows were intentionally not imported.",
            },
        }
        self._write_csv(outcomes)
        atomic_write_json(self.quality_path, quality, indent=2, sort_keys=True)
        return outcomes, quality

    def _outcome(
        self,
        opened: dict[str, Any],
        closed: dict[str, Any],
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        entry = opened["payload"]
        exit_payload = closed["payload"]
        required_positive = ("simulated_fill", "initial_stop", "position_size")
        if any(float(entry[key]) <= 0 for key in required_positive):
            raise ValueError("non-positive critical entry value")
        direction = str(entry["direction"]).upper()
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("invalid direction")

        partials = [event for event in events if event["event_type"] == "PARTIAL_EXIT"]
        gross_pnl = sum(float(event["payload"]["gross_pnl"]) for event in partials) + float(exit_payload["gross_pnl"])
        exit_fees = sum(float(event["payload"].get("fee", 0.0)) for event in partials) + float(exit_payload.get("fee", 0.0))
        entry_fee = float(entry.get("entry_fee", 0.0))
        funding = sum(float(event["payload"].get("amount", 0.0)) for event in events if event["event_type"] == "FUNDING")
        fees = entry_fee + exit_fees
        net_pnl = gross_pnl - fees + funding
        initial_risk_currency = float(entry["initial_risk_currency"])
        if initial_risk_currency <= 0:
            raise ValueError("invalid initial risk")

        mfe_events = [event for event in events if event["event_type"] == "MFE_UPDATE"]
        mae_events = [event for event in events if event["event_type"] == "MAE_UPDATE"]
        mfe = max(mfe_events, key=lambda event: float(event["payload"]["excursion_pct"]), default=None)
        mae = min(mae_events, key=lambda event: float(event["payload"]["excursion_pct"]), default=None)
        size = float(entry["position_size"])
        fill = float(entry["simulated_fill"])
        mfe_gross = 0.0
        if mfe:
            mfe_price = float(mfe["payload"]["price"])
            mfe_gross = (mfe_price - fill) * size if direction == "LONG" else (fill - mfe_price) * size

        entry_time = datetime.fromisoformat(str(opened["timestamp"]).replace("Z", "+00:00"))
        exit_time = datetime.fromisoformat(str(closed["timestamp"]).replace("Z", "+00:00"))
        row = {
            "schema_version": SCHEMA_VERSION,
            "dataset": DATASET,
            "trade_id": opened["trade_id"],
            "plan_id": opened["plan_id"],
            "strategy": entry["strategy"],
            "symbol": entry["symbol"],
            "direction": direction,
            "timeframe": entry["timeframe"],
            "regime": entry["regime"],
            "session": entry["session"],
            "config_version_hash": entry["config_version_hash"],
            "git_commit": entry["git_commit"],
            "signal_timestamp": entry["signal_timestamp"],
            "entry_timestamp": opened["timestamp"],
            "planned_entry": entry["planned_entry"],
            "simulated_fill": fill,
            "initial_stop": entry["initial_stop"],
            "initial_targets": json.dumps(entry["initial_targets"], separators=(",", ":")),
            "initial_risk_price": entry["initial_risk_price"],
            "initial_risk_currency": initial_risk_currency,
            "initial_risk_r": entry.get("initial_risk_r", 1.0),
            "expected_reward_to_risk": entry["expected_reward_to_risk"],
            "expected_move_bps": entry["expected_move_bps"],
            "spread_bps": entry["spread_bps"],
            "liquidity_assumption": entry["liquidity_assumption"],
            "expected_fees": entry["expected_fees"],
            "volatility_rank": entry["volatility_rank"],
            "strategy_score": entry["strategy_score"],
            "strategy_features": canonical_json(entry["strategy_features"]),
            "exit_timestamp": closed["timestamp"],
            "exit_price": exit_payload["exit_price"],
            "gross_pnl": round(gross_pnl, 8),
            "fees": round(fees, 8),
            "funding": round(funding, 8),
            "slippage": round(float(entry.get("entry_slippage", 0.0)) + float(exit_payload.get("slippage", 0.0)), 8),
            "slippage_pct": round(float(entry.get("entry_slippage_pct", 0.0)) + float(exit_payload.get("slippage_pct", 0.0)), 8),
            "net_pnl": round(net_pnl, 8),
            "result_r": round(net_pnl / initial_risk_currency, 8),
            "holding_duration_seconds": round((exit_time - entry_time).total_seconds(), 3),
            "final_exit_reason": exit_payload["exit_reason"],
            "mfe_price": mfe["payload"]["price"] if mfe else "",
            "mfe_pct": mfe["payload"]["excursion_pct"] if mfe else "",
            "mfe_timestamp": mfe["timestamp"] if mfe else "",
            "mae_price": mae["payload"]["price"] if mae else "",
            "mae_pct": mae["payload"]["excursion_pct"] if mae else "",
            "mae_timestamp": mae["timestamp"] if mae else "",
            "maximum_profit_giveback": round(max(0.0, mfe_gross - gross_pnl), 8),
            "tp_touches": sum(event["event_type"] == "TP_TOUCH" for event in events),
            "sl_touches": sum(event["event_type"] == "SL_TOUCH" for event in events),
            "break_even_activated": any(event["event_type"] == "BREAK_EVEN_ACTIVATED" for event in events),
            "profit_lock_activated": any(event["event_type"] == "PROFIT_LOCK_ACTIVATED" for event in events),
            "failed_continuation": any(event["event_type"] == "FAILED_CONTINUATION" for event in events),
            "partial_exit_count": len(partials),
            "event_count": len(events),
        }
        row["outcome_hash"] = content_hash(row)
        return row

    def _write_csv(self, outcomes: list[dict[str, Any]]) -> None:
        self.outcomes_path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self.outcomes_path):
            temporary = self.outcomes_path.with_name(f".{self.outcomes_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=OUTCOME_FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(outcomes)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.outcomes_path)
            finally:
                temporary.unlink(missing_ok=True)
