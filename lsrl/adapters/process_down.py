"""Process-down protection for resting LSRL entries.

THE PROBLEM. A passive LSRL limit entry rests on the exchange. If the process or the host dies while it rests and it
fills, nothing is watching: the position is naked for the whole outage. Restart recovery (the exposure state machine)
cannot help until a process exists again, and a watchdog on the same host dies with the host.

THE ONLY PROTECTION THAT SURVIVES A DEAD PROCESS IS ONE THE EXCHANGE HOLDS. Bitget's order-level `presetStopLossPrice`
(Place Order, classic futures API; returned by Get Order Detail as presetStopLossPrice / presetStopLossType) attaches a
stop to the ORDER, so the stop comes into existence at the fill with no client involved. Every LSRL entry therefore
carries the frozen protective stop (the same price the executor would install after a fill).

WHAT THE DOCUMENTATION DOES NOT SAY, AND THIS REPOSITORY'S OWN INCIDENT (2026-08-01) MAKES DANGEROUS TO GUESS:
whether a preset stop materialises as a position-level `pos_loss` or as per-order `loss_plan`s, what quantity it
covers across several rungs and partial fills, and whether it coexists with the stop moves the shared client makes.
(Bitget accepted a `loss_plan` beside a `pos_loss` and cancelled it about a second later.) That behaviour has to be
OBSERVED on the exchange. So MICRO_LIVE refuses to run at all until a machine-checkable ATTESTATION of exactly that
observation exists, is bound to this exact release SHA, has not expired, and matches a digest pinned OUTSIDE the file
(an artifact can never vouch for itself). Without it the executor cannot be constructed, so no entry can rest
unprotected: there is no configuration, flag or environment variable that turns this off.

The attestation is produced by `lsrl.tools.probe_preset_stop`, run by the OWNER against a Bitget DEMO account. This
module only VERIFIES it; it never talks to an exchange.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ATTESTATION_KIND = "PRESET_STOP_SEMANTICS"
ATTESTATION_SCHEMA = 2
MAX_VALIDITY_S = 14 * 24 * 3600
ALLOWED_ENVIRONMENTS = ("BITGET_DEMO", "BITGET_LIVE_MINIMAL")
#: The attestation can grant a capability the executor may use only because the exchange was OBSERVED to allow it.
KNOWN_CAPABILITIES = ("plan_place_first_ok",)
ALLOWED_MODELS = ("POSITION_LEVEL", "PLAN_LEVEL")

#: The observations the design depends on, ONCE PER SIDE (LSRL is symmetric: a long-only observation says nothing about shorts).
BASE_CHECKS = (
    "preset_accepted_on_limit_order",            # the exchange accepted presetStopLossPrice and echoed it on the order
    "preset_accepted_at_strategy_geometry",      # ... also at the strategy's real (ticks-close) stop distances
    "unfilled_cancel_leaves_no_stop",            # cancelling an unfilled order leaves no plan order behind
    "stop_exists_after_fill_without_client",     # after a FULL fill a stop is on the exchange with NO client stop request
    "partial_fill_stop_covers_filled",           # after a PARTIAL fill the stop covers what has filled
    "partial_fill_cancel_keeps_stop_for_filled_size",   # cancelling the remainder leaves the stop for the filled part
    "stop_covers_every_fill",                    # a second entry with the same preset leaves the WHOLE position covered
    "stop_survives_settle",                      # still there after the settle window (the 2026-08-01 lesson)
    "executor_stop_move_coexists",               # a stop move leaves exactly one live stop at the new price
    "later_fill_with_old_preset_is_repairable",  # a rung filling after the move carries its OLD preset: no lasting worse stop
    "cleanup_after_flat",                        # after the position is closed no orphan plan order remains
)
SIDES = ("long", "short")
#: Global observations (not per side).
GLOBAL_CHECKS = ("model_same_on_both_sides",)
#: Every observation the design depends on. A missing or failed one refuses the attestation.
REQUIRED_CHECKS = tuple(f"{base}:{side}" for side in SIDES for base in BASE_CHECKS) + GLOBAL_CHECKS


class AttestationRefused(RuntimeError):
    """The process-down attestation is missing or does not prove what MICRO_LIVE needs. Never overridable."""


@dataclass(frozen=True)
class ProcessDownProtection:
    """What a verified attestation grants: the executor may rest entries that carry a preset stop."""
    observed_model: str
    environment: str
    code_sha: str
    digest: str
    #: Observed on BOTH sides: a second loss_plan placed BESIDE the live one survives, so a plan-level stop can be moved
    #: place-new-then-cancel-old (never a moment without a stop). False = the move stays cancel-then-place (a known residual).
    plan_place_first_ok: bool = False


def _hex(value: Any, n: int) -> bool:
    return isinstance(value, str) and len(value) == n and all(c in "0123456789abcdef" for c in value)


def verify_attestation(path: str | Path, expected_sha256: str, *, code_sha: str, now: float | None = None,
                       accept_demo: bool = False) -> ProcessDownProtection:
    """`expected_sha256` and `code_sha` come from the reviewed release policy / the running checkout, NEVER from the file.
    `accept_demo` is the OWNER'S explicit decision that a Bitget DEMO observation is acceptable evidence for real money (Agent C,
    A-3): it defaults to False, so a demo-only attestation is refused until the owner has said otherwise."""
    now = time.time() if now is None else now
    if not _hex(str(expected_sha256).lower(), 64):
        raise AttestationRefused("ATTESTATION_DIGEST_NOT_PINNED")
    if not _hex(code_sha, 40):
        raise AttestationRefused("RELEASE_SHA_NOT_CLEAN_40_HEX")           # 'abc+dirty' / 'UNKNOWN' land here
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            raise AttestationRefused("ATTESTATION_FILE_MISSING")
        raw = p.read_bytes()
    except OSError as exc:
        raise AttestationRefused("ATTESTATION_UNREADABLE") from exc
    digest = hashlib.sha256(raw).hexdigest()
    if digest != str(expected_sha256).lower():
        raise AttestationRefused("ATTESTATION_DIGEST_MISMATCH")
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise AttestationRefused("ATTESTATION_NOT_JSON") from exc
    if not isinstance(doc, dict):
        raise AttestationRefused("ATTESTATION_NOT_AN_OBJECT")
    if doc.get("kind") != ATTESTATION_KIND or doc.get("schema") != ATTESTATION_SCHEMA:
        raise AttestationRefused("ATTESTATION_KIND_OR_SCHEMA")
    if doc.get("decision") != "PASS":
        raise AttestationRefused("ATTESTATION_DECISION_NOT_PASS")
    if doc.get("environment") not in ALLOWED_ENVIRONMENTS:
        raise AttestationRefused("ATTESTATION_ENVIRONMENT")
    if doc.get("environment") == "BITGET_DEMO" and not accept_demo:
        raise AttestationRefused("ATTESTATION_DEMO_NOT_ACCEPTED_BY_OWNER")
    if doc.get("observed_model") not in ALLOWED_MODELS:
        raise AttestationRefused("ATTESTATION_MODEL_UNKNOWN")
    if doc.get("code_sha") != code_sha:
        raise AttestationRefused("ATTESTATION_NOT_BOUND_TO_THIS_RELEASE")
    created, expires = doc.get("created_at"), doc.get("expires_at")
    for value in (created, expires):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise AttestationRefused("ATTESTATION_TIMES_INVALID")
    if not (created <= now + 60 and now < expires <= created + MAX_VALIDITY_S):
        raise AttestationRefused("ATTESTATION_EXPIRED_OR_UNBOUNDED")
    checks = doc.get("checks")
    if not isinstance(checks, list):
        raise AttestationRefused("ATTESTATION_CHECKS_MISSING")
    passed = {c.get("id") for c in checks if isinstance(c, dict) and c.get("passed") is True}
    failed = [c.get("id") for c in checks if isinstance(c, dict) and c.get("passed") is not True]
    if failed:
        raise AttestationRefused("ATTESTATION_CHECK_FAILED:" + ",".join(str(f) for f in failed))
    missing = [c for c in REQUIRED_CHECKS if c not in passed]
    if missing:
        raise AttestationRefused("ATTESTATION_CHECK_MISSING:" + ",".join(missing))
    if doc.get("sides_tested") != list(SIDES):
        raise AttestationRefused("ATTESTATION_NOT_BOTH_SIDES")
    caps = doc.get("capabilities")
    if not isinstance(caps, dict) or set(caps) - set(KNOWN_CAPABILITIES) or any(not isinstance(v, bool) for v in caps.values()):
        raise AttestationRefused("ATTESTATION_CAPABILITIES_INVALID")
    evidence = doc.get("evidence")
    orders = evidence.get("orders") if isinstance(evidence, dict) else None
    if (not isinstance(evidence, dict) or not evidence.get("run_id") or not isinstance(orders, list) or not orders
            or not all(isinstance(o, dict) and o.get("orderId") and o.get("clientOid") for o in orders)):
        raise AttestationRefused("ATTESTATION_NO_RAW_EXCHANGE_EVIDENCE")     # a bare 'passed: true' is not evidence (Agent C, A-2)
    return ProcessDownProtection(doc["observed_model"], doc["environment"], code_sha, digest,
                                 plan_place_first_ok=bool(caps.get("plan_place_first_ok")) and doc["observed_model"] == "PLAN_LEVEL")


def load_from_settings(settings: Any, *, code_sha: str, now: float | None = None) -> ProcessDownProtection:
    path = str(getattr(settings, "lsrl_process_down_attestation_path", "") or "").strip()
    pinned = str(getattr(settings, "lsrl_process_down_attestation_sha256", "") or "").strip()
    if not path or not pinned:
        raise AttestationRefused("PROCESS_DOWN_ATTESTATION_NOT_CONFIGURED")
    accept_demo = getattr(settings, "lsrl_accept_demo_process_down_evidence", False) is True
    return verify_attestation(path, pinned, code_sha=code_sha, now=now, accept_demo=accept_demo)
