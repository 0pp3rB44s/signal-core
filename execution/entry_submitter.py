"""Idempotent live entry submission: persist -> submit once -> reconcile.

This is the only path allowed to create a live entry order. It guarantees that
one logical entry cannot silently become two live exchange entries:

1. derive the deterministic clientOid for the logical entry (order_identity);
2. persist the intent before the first byte goes out (order_intent_store);
3. atomically claim the submission, so a duplicate signal or a second worker
   cannot POST the same intent twice;
4. submit exactly once - the transport never retries an order POST;
5. classify the outcome and, when it is ambiguous, ask the exchange what really
   happened *by clientOid* before any further submission decision;
6. resubmit at most once, and only after the exchange definitively confirmed
   that no such order exists;
7. when the exchange state cannot be established, refuse to guess: raise a
   critical state that blocks new entries until an owner reconciles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from clients.bitget_base_client import (
    BitgetOrderNotSent,
    BitgetOrderRejected,
    BitgetOrderSubmissionAmbiguous,
    PrivateExchangeCallBlocked,
)
from execution.order_identity import (
    ENTRY_LEG_MARKET,
    OrderIdentityError,
    derive_entry_client_oid,
)
from execution.order_intent_store import (
    OrderIntentStore,
    STATE_ABANDONED,
    STATE_ABSENT,
    STATE_ADOPTED,
    STATE_AMBIGUOUS,
    STATE_FILLED,
    STATE_NOT_SENT,
    STATE_PREPARED,
    STATE_PROTECTED,
    STATE_REJECTED,
    STATE_SUBMITTED,
    STATE_UNKNOWN,
    TERMINAL_STATES,
)


# Result statuses returned to the execution service.
RESULT_ACCEPTED = "ACCEPTED"          # exchange confirmed our submission
RESULT_ADOPTED = "ADOPTED"            # reconciliation found the order; we took it over
RESULT_REJECTED = "REJECTED"          # exchange refused; no order exists
RESULT_NOT_SENT = "NOT_SENT"          # never reached the exchange; no order exists
RESULT_ABANDONED = "ABANDONED"        # resolved without a live order, retry budget spent
RESULT_BLOCKED_UNKNOWN = "BLOCKED_UNKNOWN"  # exchange state unknown -> halt entries

#: One initial submission plus at most one controlled resubmission, and the
#: resubmission is only reachable after a definitive ABSENT verdict.
MAX_ENTRY_SUBMISSIONS = 2

#: Order states that mean the order is gone and created no exposure.
_DEAD_ORDER_STATES = {"cancelled", "canceled", "rejected", "expired", "invalid"}


def _may_resubmit(resolved: "EntrySubmissionResult") -> bool:
    """Only a definitive "this order never existed" verdict unlocks a resubmission.

    An order that exists but is cancelled/rejected/expired is *not* a lost
    message: the exchange saw the intent and it is gone. Resending would be a
    new trading decision, not a recovery, so it stays terminal.
    """
    return resolved.status == RESULT_ABANDONED and resolved.classification == "ABSENT"


@dataclass
class EntrySubmissionResult:
    status: str
    client_oid: str = ""
    order_id: str = ""
    payload: dict[str, Any] | None = None
    classification: str = ""
    message: str = ""
    submissions: int = 0
    exchange_order: dict[str, Any] | None = None
    reconciled: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def has_live_order(self) -> bool:
        return self.status in {RESULT_ACCEPTED, RESULT_ADOPTED} and bool(self.order_id)

    @property
    def blocks_new_entries(self) -> bool:
        return self.status == RESULT_BLOCKED_UNKNOWN


class EntryOrderBlocked(RuntimeError):
    """Raised when exchange state is unknown and no entry may be attempted."""


class EntryOrderSubmitter:
    """Submit live entry orders exactly once per logical trade intent."""

    def __init__(
        self,
        *,
        client: Any,
        intent_store: OrderIntentStore,
        session_id: str,
        execution_mode: str,
        bot_identity: str = "bgai",
        log: logging.Logger | None = None,
    ) -> None:
        self.client = client
        self.intents = intent_store
        self.session_id = session_id
        self.execution_mode = execution_mode
        self.bot_identity = bot_identity
        self.log = log or logging.getLogger(self.__class__.__name__)

    # --- public API ------------------------------------------------------

    def client_oid_for(self, plan: Any, leg: str = ENTRY_LEG_MARKET) -> str:
        bot_identity = self.bot_identity
        if getattr(plan, "strategy", "") == "funding_crowding_continuation_24h":
            bot_identity = "cgc-fcp"
        return derive_entry_client_oid(
            plan_id=getattr(plan, "plan_id", ""),
            candidate_id=getattr(plan, "candidate_id", ""),
            symbol=getattr(plan, "symbol", ""),
            direction=getattr(plan, "direction", ""),
            strategy=getattr(plan, "strategy", ""),
            leg=leg,
            bot_identity=bot_identity,
        )

    def submit_entry(
        self,
        *,
        plan: Any,
        size: float,
        side: str,
        order_type: str,
        leg: str,
        place: Callable[[str], dict[str, Any] | None],
        notional_usdt: float = 0.0,
    ) -> EntrySubmissionResult:
        """Persist, claim, submit once, and reconcile one entry leg.

        ``place(client_oid)`` performs the actual POST and must forward the
        clientOid to the exchange.
        """
        symbol = str(getattr(plan, "symbol", "")).upper()
        direction = str(getattr(plan, "direction", "")).upper()
        plan_id = str(getattr(plan, "plan_id", ""))

        try:
            client_oid = self.client_oid_for(plan, leg=leg)
        except OrderIdentityError as exc:
            # No trustworthy identity means no safe way to recover from an
            # ambiguous response, so we must not submit at all.
            self.log.critical(
                "ENTRY_IDENTITY_UNUSABLE | %s | plan_id=%s | leg=%s | error=%s",
                symbol,
                plan_id,
                leg,
                exc,
            )
            return EntrySubmissionResult(
                status=RESULT_REJECTED,
                classification="IDENTITY_UNUSABLE",
                message=f"entry blocked: {exc}",
            )

        record = self.intents.prepare(
            client_oid=client_oid,
            plan_id=plan_id,
            candidate_id=str(getattr(plan, "candidate_id", "")),
            symbol=symbol,
            side=side,
            direction=direction,
            size=float(size),
            order_type=order_type,
            leg=leg,
            strategy=str(getattr(plan, "strategy", "")),
            session_id=self.session_id,
            execution_mode=self.execution_mode,
            notional_usdt=float(notional_usdt),
        )

        state = str(record.get("state") or STATE_PREPARED)
        attempts_so_far = int(record.get("submit_attempts") or 0)

        if state in TERMINAL_STATES:
            self.log.warning(
                "ENTRY_INTENT_ALREADY_TERMINAL | %s | plan_id=%s | client_oid=%s | state=%s",
                symbol, plan_id, client_oid, state,
            )
            return EntrySubmissionResult(
                status=RESULT_ABANDONED if state != STATE_REJECTED else RESULT_REJECTED,
                client_oid=client_oid,
                classification=state,
                message=f"intent already resolved as {state}",
                submissions=attempts_so_far,
            )

        if state == STATE_UNKNOWN:
            return self._blocked_unknown(
                symbol=symbol, plan_id=plan_id, client_oid=client_oid,
                message="prior submission left exchange state unknown",
                submissions=attempts_so_far,
            )

        # A pre-existing non-PREPARED intent means an earlier attempt already
        # touched the exchange (restart, resumed plan, duplicate invocation).
        if state != STATE_PREPARED or attempts_so_far > 0:
            self.log.critical(
                "ENTRY_RESUMED_INTENT | %s | plan_id=%s | client_oid=%s | state=%s | attempts=%s | reconciling_before_submit=True",
                symbol, plan_id, client_oid, state, attempts_so_far,
            )
            resolved = self._reconcile(
                symbol=symbol, plan_id=plan_id, client_oid=client_oid,
                submissions=attempts_so_far,
            )
            if not _may_resubmit(resolved):
                return resolved
            if attempts_so_far >= MAX_ENTRY_SUBMISSIONS:
                return resolved

        return self._submit_loop(
            symbol=symbol,
            plan_id=plan_id,
            client_oid=client_oid,
            place=place,
            submissions=attempts_so_far,
        )

    # --- submission ------------------------------------------------------

    def _submit_loop(
        self,
        *,
        symbol: str,
        plan_id: str,
        client_oid: str,
        place: Callable[[str], dict[str, Any] | None],
        submissions: int,
    ) -> EntrySubmissionResult:
        while submissions < MAX_ENTRY_SUBMISSIONS:
            attempt = self.intents.record_attempt(client_oid)
            submissions = attempt or submissions + 1

            if attempt > MAX_ENTRY_SUBMISSIONS:
                # Another worker claimed a submission concurrently.
                self.log.critical(
                    "ENTRY_SUBMISSION_BUDGET_EXHAUSTED | %s | plan_id=%s | client_oid=%s | attempts=%s",
                    symbol, plan_id, client_oid, attempt,
                )
                return self._reconcile(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid, submissions=submissions
                )

            self.log.critical(
                "ENTRY_SUBMISSION_ATTEMPT | %s | plan_id=%s | client_oid=%s | attempt=%s/%s",
                symbol, plan_id, client_oid, attempt, MAX_ENTRY_SUBMISSIONS,
            )

            try:
                payload = place(client_oid)
            except BitgetOrderRejected as exc:
                self.intents.mark(
                    client_oid, STATE_REJECTED, note="exchange business rejection",
                    classification="REJECTED",
                )
                self.log.error(
                    "ENTRY_SUBMISSION_RESULT | %s | plan_id=%s | client_oid=%s | classification=REJECTED | error=%s",
                    symbol, plan_id, client_oid, exc,
                )
                return EntrySubmissionResult(
                    status=RESULT_REJECTED, client_oid=client_oid,
                    classification="REJECTED", message=str(exc), submissions=submissions,
                )
            except BitgetOrderSubmissionAmbiguous as exc:
                self.intents.mark(
                    client_oid, STATE_AMBIGUOUS, note=str(exc)[:200], classification="AMBIGUOUS",
                )
                self.log.critical(
                    "ENTRY_SUBMISSION_RESULT | %s | plan_id=%s | client_oid=%s | classification=AMBIGUOUS | attempt=%s | error=%s",
                    symbol, plan_id, client_oid, submissions, exc,
                )
                resolved = self._reconcile(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid, submissions=submissions
                )
                if not _may_resubmit(resolved):
                    return resolved
                continue  # definitively absent -> one controlled resubmission
            except BitgetOrderNotSent as exc:
                self.intents.mark(
                    client_oid, STATE_AMBIGUOUS, note=str(exc)[:200], classification="NOT_SENT",
                )
                self.log.critical(
                    "ENTRY_SUBMISSION_RESULT | %s | plan_id=%s | client_oid=%s | classification=NOT_SENT | attempt=%s | error=%s",
                    symbol, plan_id, client_oid, submissions, exc,
                )
                # Even "not sent" is verified against the exchange before any
                # resubmission: cheap, and it can never create a duplicate.
                resolved = self._reconcile(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid, submissions=submissions
                )
                if not _may_resubmit(resolved):
                    return resolved
                continue
            except (PrivateExchangeCallBlocked, ValueError) as exc:
                # Raised before transport (guard rails, size/precision checks):
                # provably nothing was sent, and reconciliation would be noise.
                self.intents.mark(
                    client_oid, STATE_NOT_SENT, note=str(exc)[:200], classification="NOT_SENT_PRE_TRANSPORT",
                )
                self.log.error(
                    "ENTRY_SUBMISSION_RESULT | %s | plan_id=%s | client_oid=%s | classification=NOT_SENT_PRE_TRANSPORT | error=%s",
                    symbol, plan_id, client_oid, exc,
                )
                return EntrySubmissionResult(
                    status=RESULT_NOT_SENT, client_oid=client_oid,
                    classification="NOT_SENT_PRE_TRANSPORT", message=str(exc),
                    submissions=submissions,
                )
            except Exception as exc:
                # Unclassified failure: the order may exist. Never assume it does not.
                self.intents.mark(
                    client_oid, STATE_AMBIGUOUS, note=str(exc)[:200], classification="AMBIGUOUS_UNCLASSIFIED",
                )
                self.log.critical(
                    "ENTRY_SUBMISSION_RESULT | %s | plan_id=%s | client_oid=%s | classification=AMBIGUOUS_UNCLASSIFIED | error=%s",
                    symbol, plan_id, client_oid, exc,
                )
                resolved = self._reconcile(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid, submissions=submissions
                )
                if not _may_resubmit(resolved):
                    return resolved
                return EntrySubmissionResult(
                    status=RESULT_NOT_SENT, client_oid=client_oid,
                    classification="AMBIGUOUS_UNCLASSIFIED_RESOLVED_ABSENT",
                    message=str(exc), submissions=submissions,
                )

            order_id = str(self.client.extract_order_id(payload) or "")
            if not order_id:
                # Accepted-looking response without an id: reconcile rather than
                # resend, because the exchange did answer.
                self.log.critical(
                    "ENTRY_SUBMISSION_NO_ORDER_ID | %s | plan_id=%s | client_oid=%s",
                    symbol, plan_id, client_oid,
                )
                self.intents.mark(
                    client_oid, STATE_AMBIGUOUS, note="accepted response without orderId",
                    classification="AMBIGUOUS_NO_ORDER_ID",
                )
                return self._reconcile(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid, submissions=submissions
                )

            self.intents.mark(
                client_oid, STATE_SUBMITTED, note="exchange accepted",
                classification="ACCEPTED", exchange_order_id=order_id,
            )
            self.log.critical(
                "ENTRY_SUBMISSION_RESULT | %s | plan_id=%s | client_oid=%s | classification=ACCEPTED | order_id=%s | submissions=%s",
                symbol, plan_id, client_oid, order_id, submissions,
            )
            return EntrySubmissionResult(
                status=RESULT_ACCEPTED, client_oid=client_oid, order_id=order_id,
                payload=payload, classification="ACCEPTED", submissions=submissions,
                message="exchange accepted entry order",
            )

        self.intents.mark(
            client_oid, STATE_ABANDONED, note="submission budget exhausted",
            classification="ABANDONED",
        )
        self.log.critical(
            "ENTRY_SUBMISSION_ABANDONED | %s | plan_id=%s | client_oid=%s | submissions=%s",
            symbol, plan_id, client_oid, submissions,
        )
        return EntrySubmissionResult(
            status=RESULT_ABANDONED, client_oid=client_oid, classification="ABANDONED",
            message="entry abandoned after controlled resubmission budget", submissions=submissions,
        )

    # --- reconciliation --------------------------------------------------

    def _reconcile(
        self,
        *,
        symbol: str,
        plan_id: str,
        client_oid: str,
        submissions: int,
    ) -> EntrySubmissionResult:
        """Ask the exchange what happened to ``client_oid``.

        ADOPTED  the order exists -> take it over, never submit again
        ABANDONED the order definitively does not exist -> caller may resubmit
        BLOCKED_UNKNOWN the state could not be established -> halt
        """
        self.log.critical(
            "ENTRY_RECONCILIATION_STARTED | %s | plan_id=%s | client_oid=%s | submissions=%s",
            symbol, plan_id, client_oid, submissions,
        )

        try:
            lookup = self.client.find_order_by_client_oid(symbol=symbol, client_oid=client_oid)
        except Exception as exc:
            return self._blocked_unknown(
                symbol=symbol, plan_id=plan_id, client_oid=client_oid,
                message=f"order lookup failed: {exc}", submissions=submissions,
            )

        status = str((lookup or {}).get("status") or "UNKNOWN").upper()
        order = (lookup or {}).get("order") or None

        if status == "FOUND" and isinstance(order, dict):
            return self._adopt(
                symbol=symbol, plan_id=plan_id, client_oid=client_oid,
                order=order, submissions=submissions,
            )

        if status == "ABSENT":
            # The order does not exist. Before allowing a resubmission, make
            # sure no position was created by it anyway (belt and braces
            # against exchange-side eventual consistency).
            try:
                position = self._live_position(symbol)
            except Exception as exc:
                return self._blocked_unknown(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid,
                    message=f"position check failed while confirming absence: {exc}",
                    submissions=submissions,
                )
            if position is not None:
                self.log.critical(
                    "ENTRY_RECONCILIATION_POSITION_WITHOUT_ORDER | %s | plan_id=%s | client_oid=%s | position_size=%s",
                    symbol, plan_id, client_oid, self._position_size(position),
                )
                return self._blocked_unknown(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid,
                    message="exchange reports no order but a live position exists",
                    submissions=submissions,
                )

            self.intents.mark(
                client_oid, STATE_ABSENT, note="exchange confirms no such order",
                classification="ABSENT",
            )
            self.log.critical(
                "ENTRY_RECONCILIATION_ORDER_ABSENT | %s | plan_id=%s | client_oid=%s | controlled_resubmission_allowed=%s",
                symbol, plan_id, client_oid, submissions < MAX_ENTRY_SUBMISSIONS,
            )
            return EntrySubmissionResult(
                status=RESULT_ABANDONED, client_oid=client_oid, classification="ABSENT",
                message="exchange confirms no order exists", submissions=submissions,
                reconciled=True,
            )

        return self._blocked_unknown(
            symbol=symbol, plan_id=plan_id, client_oid=client_oid,
            message="exchange state for clientOid could not be established",
            submissions=submissions,
            errors=list((lookup or {}).get("errors") or []),
        )

    def _adopt(
        self,
        *,
        symbol: str,
        plan_id: str,
        client_oid: str,
        order: dict[str, Any],
        submissions: int,
    ) -> EntrySubmissionResult:
        order_id = str(order.get("orderId") or order.get("order_id") or "")
        order_state = str(order.get("state") or order.get("status") or "").lower()
        metrics = {}
        try:
            metrics = self.client.extract_fill_metrics({"data": order}) or {}
        except Exception as exc:  # metrics are analytics only; never fatal here
            self.log.warning(
                "ENTRY_ADOPT_METRICS_FAILED | %s | client_oid=%s | error=%s", symbol, client_oid, exc
            )

        if order_state in _DEAD_ORDER_STATES:
            # The order exists in history but created no exposure.
            try:
                position = self._live_position(symbol)
            except Exception as exc:
                return self._blocked_unknown(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid,
                    message=f"position check failed for {order_state} order: {exc}",
                    submissions=submissions,
                )
            if position is None:
                self.intents.mark(
                    client_oid, STATE_ABANDONED,
                    note=f"order {order_state} and no position", classification="ORDER_DEAD",
                    exchange_order_id=order_id,
                )
                self.log.critical(
                    "ENTRY_RECONCILIATION_ORDER_DEAD | %s | plan_id=%s | client_oid=%s | order_id=%s | state=%s",
                    symbol, plan_id, client_oid, order_id, order_state,
                )
                return EntrySubmissionResult(
                    status=RESULT_ABANDONED, client_oid=client_oid, order_id=order_id,
                    classification="ORDER_DEAD", submissions=submissions, reconciled=True,
                    message=f"order exists but is {order_state}; no position created",
                )

        self.intents.mark(
            client_oid, STATE_ADOPTED, note=f"adopted existing order state={order_state}",
            classification="ADOPTED", exchange_order_id=order_id,
            filled_qty=float(metrics.get("filled_qty") or 0.0),
            avg_price=float(metrics.get("avg_price") or 0.0),
        )
        self.log.critical(
            "ENTRY_ORDER_ADOPTED | %s | plan_id=%s | client_oid=%s | order_id=%s | state=%s | filled_qty=%s | avg_price=%s | resubmission_suppressed=True",
            symbol, plan_id, client_oid, order_id, order_state,
            metrics.get("filled_qty"), metrics.get("avg_price"),
        )
        return EntrySubmissionResult(
            status=RESULT_ADOPTED, client_oid=client_oid, order_id=order_id,
            payload={"data": dict(order)}, exchange_order=order,
            classification="ADOPTED", submissions=submissions, reconciled=True,
            message=f"adopted existing exchange order state={order_state}",
        )

    def _blocked_unknown(
        self,
        *,
        symbol: str,
        plan_id: str,
        client_oid: str,
        message: str,
        submissions: int,
        errors: list[str] | None = None,
    ) -> EntrySubmissionResult:
        self.intents.mark(
            client_oid, STATE_UNKNOWN, note=message[:200], classification="UNKNOWN",
        )
        self.log.critical(
            "ENTRY_STATE_UNKNOWN_NEW_ENTRIES_BLOCKED | %s | plan_id=%s | client_oid=%s | submissions=%s | reason=%s | owner_reconciliation_required=True",
            symbol, plan_id, client_oid, submissions, message,
        )
        return EntrySubmissionResult(
            status=RESULT_BLOCKED_UNKNOWN, client_oid=client_oid, classification="UNKNOWN",
            message=message, submissions=submissions, reconciled=True, errors=errors or [],
        )

    # --- protection bookkeeping -----------------------------------------

    def mark_filled(self, client_oid: str, *, filled_qty: float, avg_price: float) -> None:
        self.intents.mark(
            client_oid, STATE_FILLED, note="exchange position confirmed",
            filled_qty=float(filled_qty), avg_price=float(avg_price),
        )

    def mark_protected(self, client_oid: str, *, integrity: str) -> None:
        self.intents.mark(
            client_oid, STATE_PROTECTED, note=f"protection confirmed integrity={integrity}",
            protection_state="CONFIRMED",
        )

    def mark_closed_out(self, client_oid: str, *, reason: str) -> None:
        """Entry existed but was closed again (fail-safe); nothing left to protect."""
        self.intents.mark(
            client_oid, STATE_ABANDONED, note=f"closed out: {reason}",
            protection_state="CLOSED_OUT",
        )

    # --- restart recovery ------------------------------------------------

    def recover_pending_intents(self) -> dict[str, Any]:
        """Reconcile every unfinished intent before the strategy may enter again.

        Returns ``{"blocked": bool, "reasons": [...], "recovered": [...]}``.
        The bot never submits a fresh order here: an intent whose order the
        exchange definitively does not know is abandoned, not retried, because
        the market context that produced it is gone.
        """
        summary: dict[str, Any] = {"blocked": False, "reasons": [], "recovered": []}

        pending = self.intents.recoverable()
        blocking = self.intents.blocking()

        for record in blocking:
            summary["blocked"] = True
            summary["reasons"].append(
                f"intent {record.get('client_oid')} ({record.get('symbol')}) is in UNKNOWN state"
            )
            self.log.critical(
                "STARTUP_RECOVERY_BLOCKED_UNKNOWN_INTENT | %s | plan_id=%s | client_oid=%s | owner_reconciliation_required=True",
                record.get("symbol"), record.get("plan_id"), record.get("client_oid"),
            )

        if not pending:
            self.log.info(
                "STARTUP_RECOVERY_NO_PENDING_INTENTS | blocked=%s", summary["blocked"]
            )
            return summary

        self.log.critical(
            "STARTUP_RECOVERY_STARTED | pending_intents=%s", len(pending)
        )

        for record in pending:
            client_oid = str(record.get("client_oid") or "")
            symbol = str(record.get("symbol") or "").upper()
            plan_id = str(record.get("plan_id") or "")
            state = str(record.get("state") or "")

            resolved = self._reconcile(
                symbol=symbol, plan_id=plan_id, client_oid=client_oid,
                submissions=int(record.get("submit_attempts") or 0),
            )
            summary["recovered"].append(
                {
                    "client_oid": client_oid,
                    "symbol": symbol,
                    "plan_id": plan_id,
                    "previous_state": state,
                    "resolution": resolved.status,
                }
            )

            if resolved.status == RESULT_BLOCKED_UNKNOWN:
                summary["blocked"] = True
                summary["reasons"].append(
                    f"intent {client_oid} ({symbol}) could not be reconciled with the exchange"
                )
                continue

            if resolved.status == RESULT_ADOPTED:
                blocked, reason = self._resolve_recovered_position(
                    symbol=symbol, plan_id=plan_id, client_oid=client_oid
                )
                if blocked:
                    summary["blocked"] = True
                    summary["reasons"].append(reason)
                continue

            # ABANDONED: the exchange has no such order and no position exists.
            self.intents.mark(
                client_oid, STATE_ABANDONED,
                note="startup recovery: no order, no position; intent retired",
                classification="RECOVERED_ABSENT",
            )
            self.log.critical(
                "STARTUP_RECOVERY_INTENT_RETIRED | %s | plan_id=%s | client_oid=%s | previous_state=%s | resubmitted=False",
                symbol, plan_id, client_oid, state,
            )

        self.log.critical(
            "STARTUP_RECOVERY_COMPLETE | recovered=%s | blocked=%s | reasons=%s",
            len(summary["recovered"]), summary["blocked"], "; ".join(summary["reasons"]) or "-",
        )
        return summary

    def _resolve_recovered_position(
        self, *, symbol: str, plan_id: str, client_oid: str
    ) -> tuple[bool, str]:
        """After adopting an order at startup, decide whether entries stay blocked."""
        try:
            position = self._live_position(symbol)
        except Exception as exc:
            self.intents.mark(
                client_oid, STATE_UNKNOWN,
                note=f"startup recovery: position check failed: {exc}"[:200],
                classification="UNKNOWN",
            )
            self.log.critical(
                "STARTUP_RECOVERY_POSITION_CHECK_FAILED | %s | plan_id=%s | client_oid=%s | error=%s",
                symbol, plan_id, client_oid, exc,
            )
            return True, f"position check for {symbol} failed during startup recovery"

        if position is None:
            self.intents.mark(
                client_oid, STATE_ABANDONED,
                note="startup recovery: order adopted but no live position",
                classification="RECOVERED_NO_POSITION",
            )
            self.log.critical(
                "STARTUP_RECOVERY_ADOPTED_NO_POSITION | %s | plan_id=%s | client_oid=%s",
                symbol, plan_id, client_oid,
            )
            return False, ""

        if self._position_has_protection(position):
            self.mark_protected(client_oid, integrity="RECOVERED_EXCHANGE_PROTECTION_PRESENT")
            self.log.critical(
                "STARTUP_RECOVERY_POSITION_PROTECTED | %s | plan_id=%s | client_oid=%s | size=%s | protection_reconciled=True",
                symbol, plan_id, client_oid, self._position_size(position),
            )
            return False, ""

        self.intents.mark(
            client_oid, STATE_UNKNOWN,
            note="startup recovery: live position without confirmed exchange protection",
            classification="UNPROTECTED_POSITION",
            protection_state="MISSING",
        )
        self.log.critical(
            "STARTUP_RECOVERY_UNPROTECTED_POSITION | %s | plan_id=%s | client_oid=%s | size=%s | "
            "new_entries_blocked=True | owner_safe_protection_workflow_required=True",
            symbol, plan_id, client_oid, self._position_size(position),
        )
        return True, f"live position on {symbol} has no confirmed exchange-side protection"

    # --- exchange helpers ------------------------------------------------

    @staticmethod
    def _position_size(position: dict[str, Any]) -> float:
        for key in ("total", "size", "available", "holdVol", "positionSize"):
            try:
                value = float(position.get(key) or 0.0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _position_has_protection(position: dict[str, Any]) -> bool:
        """Exchange-side SL/TP presence, read from the same fields Bitget verifies."""
        has_sl = bool(str(position.get("stopLoss") or "") or str(position.get("stopLossId") or ""))
        has_tp = bool(
            str(position.get("takeProfit") or "") or str(position.get("takeProfitId") or "")
        )
        return has_sl and has_tp

    def _live_position(self, symbol: str) -> dict[str, Any] | None:
        """Return the live position for ``symbol``, or None when flat.

        A lookup failure raises, because "I could not check" must never be
        collapsed into "there is no position".
        """
        payload = self.client.get_all_positions()
        for position in (payload or {}).get("data") or []:
            if str(position.get("symbol") or "").upper() != symbol.upper():
                continue
            if self._position_size(position) > 0:
                return position
        return None


__all__ = [
    "EntryOrderBlocked",
    "EntryOrderSubmitter",
    "EntrySubmissionResult",
    "MAX_ENTRY_SUBMISSIONS",
    "RESULT_ABANDONED",
    "RESULT_ACCEPTED",
    "RESULT_ADOPTED",
    "RESULT_BLOCKED_UNKNOWN",
    "RESULT_NOT_SENT",
    "RESULT_REJECTED",
]
