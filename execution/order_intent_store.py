"""Persisted order-intent records: the write-ahead log for live entry orders.

The intent is written to disk **before** the first network submission. That is
what makes an ambiguous response recoverable: after a timeout, a 5xx, or a hard
process crash, the bot can still name the order it may have created (by its
deterministic clientOid) and ask the exchange what actually happened, instead of
blindly POSTing again.

State model
-----------
    PREPARED    intent persisted, nothing sent yet
    SUBMITTING  a POST is in flight (or died in flight)
    SUBMITTED   exchange accepted, orderId known
    AMBIGUOUS   transport/5xx failure - the order may or may not exist
    ADOPTED     reconciliation found the order on the exchange
    ABSENT      exchange definitively confirms no such order exists
    REJECTED    exchange business rejection - no order was created (terminal)
    NOT_SENT    request never reached the exchange (terminal for that attempt)
    FILLED      resulting position confirmed on the exchange
    PROTECTED   SL/TP confirmed present exactly once (terminal, happy path)
    UNKNOWN     reconciliation unavailable - CRITICAL, blocks all new entries
    ABANDONED   resolved without a live order; nothing left to do (terminal)

Persistence is atomic (temp file + fsync + os.replace) and serialized across
processes by JsonStateStore's interprocess lock.
"""

from __future__ import annotations

import copy
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from execution.state_store import JsonStateStore


STATE_PREPARED = "PREPARED"
STATE_SUBMITTING = "SUBMITTING"
STATE_SUBMITTED = "SUBMITTED"
STATE_AMBIGUOUS = "AMBIGUOUS"
STATE_ADOPTED = "ADOPTED"
STATE_ABSENT = "ABSENT"
STATE_REJECTED = "REJECTED"
STATE_NOT_SENT = "NOT_SENT"
STATE_FILLED = "FILLED"
STATE_PROTECTED = "PROTECTED"
STATE_UNKNOWN = "UNKNOWN"
STATE_ABANDONED = "ABANDONED"

#: States that need no further reconciliation at startup.
TERMINAL_STATES = frozenset(
    {STATE_REJECTED, STATE_PROTECTED, STATE_ABANDONED, STATE_NOT_SENT}
)

#: States that require the exchange to be consulted before new entries are allowed.
RECOVERABLE_STATES = frozenset(
    {
        STATE_PREPARED,
        STATE_SUBMITTING,
        STATE_SUBMITTED,
        STATE_AMBIGUOUS,
        STATE_ADOPTED,
        STATE_ABSENT,
        STATE_FILLED,
    }
)

#: States that must block every new entry until an owner resolves them.
BLOCKING_STATES = frozenset({STATE_UNKNOWN})

DEFAULT_INTENT_PATH = "state/order_intents.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rows_copy(loaded: Any) -> list[dict[str, Any]]:
    """Deep-copy the loaded rows.

    JsonStateStore.update() only writes when the mutator's result differs from
    what it loaded, so mutating the loaded object in place would silently skip
    the fsync. Every mutator therefore edits a copy.
    """
    if not isinstance(loaded, list):
        return []
    return copy.deepcopy(loaded)


def new_session_id() -> str:
    """Process-scoped id, used for forensics only - never for order identity."""
    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


class OrderIntentStore:
    """Atomic, crash-safe store of live entry-order intents keyed by clientOid."""

    def __init__(self, path: str = DEFAULT_INTENT_PATH, max_records: int = 500) -> None:
        self.store = JsonStateStore(path)
        self.max_records = int(max_records)
        self.log = logging.getLogger(self.__class__.__name__)

    # --- reads -----------------------------------------------------------

    def all(self) -> list[dict[str, Any]]:
        records = self.store.load(default=[])
        return list(records) if isinstance(records, list) else []

    def get(self, client_oid: str) -> dict[str, Any] | None:
        for record in self.all():
            if record.get("client_oid") == client_oid:
                return record
        return None

    def recoverable(self) -> list[dict[str, Any]]:
        return [row for row in self.all() if row.get("state") in RECOVERABLE_STATES]

    def blocking(self) -> list[dict[str, Any]]:
        return [row for row in self.all() if row.get("state") in BLOCKING_STATES]

    # --- writes ----------------------------------------------------------

    def prepare(
        self,
        *,
        client_oid: str,
        plan_id: str,
        candidate_id: str,
        symbol: str,
        side: str,
        direction: str,
        size: float,
        order_type: str,
        leg: str,
        strategy: str,
        session_id: str,
        execution_mode: str,
        notional_usdt: float = 0.0,
    ) -> dict[str, Any]:
        """Persist the intent before any network call.

        Idempotent by clientOid: re-preparing an existing logical entry returns
        the stored record untouched, so a duplicate signal or a restart can
        never mint a second identity for the same trade intent.
        """
        record = {
            "client_oid": client_oid,
            "plan_id": plan_id,
            "candidate_id": candidate_id,
            "symbol": symbol,
            "side": side,
            "direction": direction,
            "size": float(size),
            "notional_usdt": float(notional_usdt),
            "order_type": order_type,
            "leg": leg,
            "strategy": strategy,
            "session_id": session_id,
            "execution_mode": execution_mode,
            "state": STATE_PREPARED,
            "created_at": _now(),
            "updated_at": _now(),
            "submit_attempts": 0,
            "exchange_order_id": "",
            "filled_qty": 0.0,
            "avg_price": 0.0,
            "protection_state": "PENDING",
            "classification": "",
            "history": [{"state": STATE_PREPARED, "at": _now(), "note": "intent persisted"}],
        }

        existing_holder: list[dict[str, Any]] = []

        def _mutate(loaded: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows = _rows_copy(loaded)
            for row in rows:
                if row.get("client_oid") == client_oid:
                    existing_holder.append(row)
                    return rows
            rows.append(record)
            return self._prune(rows)

        self.store.update(default=[], mutator=_mutate)

        if existing_holder:
            existing = existing_holder[0]
            self.log.warning(
                "ORDER_INTENT_ALREADY_PERSISTED | plan_id=%s | client_oid=%s | state=%s | attempts=%s",
                existing.get("plan_id"),
                client_oid,
                existing.get("state"),
                existing.get("submit_attempts"),
            )
            return existing

        self.log.warning(
            "ORDER_INTENT_PERSISTED | plan_id=%s | client_oid=%s | symbol=%s | side=%s | size=%s | type=%s | leg=%s | session=%s",
            plan_id,
            client_oid,
            symbol,
            side,
            size,
            order_type,
            leg,
            session_id,
        )
        return record

    def mark(
        self,
        client_oid: str,
        state: str,
        *,
        note: str = "",
        **fields: Any,
    ) -> dict[str, Any] | None:
        """Transition one intent and persist it atomically."""
        updated_holder: list[dict[str, Any]] = []

        def _mutate(loaded: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows = _rows_copy(loaded)
            for row in rows:
                if row.get("client_oid") != client_oid:
                    continue
                row["state"] = state
                row["updated_at"] = _now()
                for key, value in fields.items():
                    row[key] = value
                history = row.get("history")
                if not isinstance(history, list):
                    history = []
                history.append({"state": state, "at": _now(), "note": note})
                row["history"] = history[-40:]
                updated_holder.append(row)
                break
            return rows

        self.store.update(default=[], mutator=_mutate)

        if not updated_holder:
            self.log.error(
                "ORDER_INTENT_MISSING_FOR_TRANSITION | client_oid=%s | state=%s", client_oid, state
            )
            return None

        updated = updated_holder[0]
        self.log.warning(
            "ORDER_INTENT_STATE | plan_id=%s | client_oid=%s | state=%s | attempts=%s | order_id=%s | note=%s",
            updated.get("plan_id"),
            client_oid,
            state,
            updated.get("submit_attempts"),
            updated.get("exchange_order_id") or "-",
            note or "-",
        )
        return updated

    def record_attempt(self, client_oid: str) -> int:
        """Move the intent to SUBMITTING and return the 1-based attempt number."""
        attempt_holder: list[int] = []

        def _mutate(loaded: list[dict[str, Any]]) -> list[dict[str, Any]]:
            rows = _rows_copy(loaded)
            for row in rows:
                if row.get("client_oid") != client_oid:
                    continue
                attempt = int(row.get("submit_attempts") or 0) + 1
                row["submit_attempts"] = attempt
                row["state"] = STATE_SUBMITTING
                row["updated_at"] = _now()
                history = row.get("history")
                if not isinstance(history, list):
                    history = []
                history.append(
                    {"state": STATE_SUBMITTING, "at": _now(), "note": f"attempt={attempt}"}
                )
                row["history"] = history[-40:]
                attempt_holder.append(attempt)
                break
            return rows

        self.store.update(default=[], mutator=_mutate)
        return attempt_holder[0] if attempt_holder else 0

    def _prune(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop the oldest terminal records once the log grows past the cap."""
        if len(rows) <= self.max_records:
            return rows
        keep = [row for row in rows if row.get("state") not in TERMINAL_STATES]
        terminal = [row for row in rows if row.get("state") in TERMINAL_STATES]
        allowance = max(0, self.max_records - len(keep))
        return keep + terminal[-allowance:] if allowance else keep


__all__ = [
    "BLOCKING_STATES",
    "DEFAULT_INTENT_PATH",
    "OrderIntentStore",
    "RECOVERABLE_STATES",
    "STATE_ABANDONED",
    "STATE_ABSENT",
    "STATE_ADOPTED",
    "STATE_AMBIGUOUS",
    "STATE_FILLED",
    "STATE_NOT_SENT",
    "STATE_PREPARED",
    "STATE_PROTECTED",
    "STATE_REJECTED",
    "STATE_SUBMITTED",
    "STATE_SUBMITTING",
    "STATE_UNKNOWN",
    "TERMINAL_STATES",
    "new_session_id",
]
