"""Deterministic exchange identity (clientOid) for live entry orders.

Why this module exists
----------------------
A live entry POST that times out (or returns 5xx) is *ambiguous*: Bitget may
already have accepted the order. Without a stable clientOid the bot has no way
to ask "did my order arrive?" and no way to stop the exchange from accepting the
same intent twice. Every logical entry therefore gets exactly one clientOid that
is:

* deterministic  - the same logical entry always derives the same value;
* stable         - identical across in-process retries and process restarts;
* distinct       - separate intended entries never collide;
* opaque         - a SHA-256 digest, so no credential or account data can leak
                   into an exchange field or into logs;
* format-safe    - short, ``[A-Za-z0-9_-]`` only, well inside Bitget's limit.

Derivation inputs are the *persisted logical* identity, never wall-clock time
and never a per-attempt UUID.
"""

from __future__ import annotations

import hashlib
import json
import re

from candidate_lifecycle import deterministic_plan_id


# Bitget accepts up to 64 characters for clientOid; we stay well below it.
MAX_CLIENT_OID_LENGTH = 64
CLIENT_OID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Namespace so a clientOid seen on the exchange is attributable to this bot.
DEFAULT_BOT_IDENTITY = "bgai"

# One logical entry can legitimately need two *different* exchange orders: the
# post-only maker attempt and the market fallback. They are separate orders, so
# they get separate (still deterministic) identities.
ENTRY_LEG_MARKET = "market"
ENTRY_LEG_MAKER = "maker"
_LEG_CODES = {ENTRY_LEG_MARKET: "m", ENTRY_LEG_MAKER: "k"}

_DIGEST_LENGTH = 26


class OrderIdentityError(ValueError):
    """Raised when a plan cannot produce a trustworthy exchange identity."""


def validate_plan_identity(
    *,
    plan_id: str,
    candidate_id: str,
) -> list[str]:
    """Return the list of reasons why ``plan_id`` is unusable as an identity root.

    An empty list means the plan identity satisfies every requirement:
    present, non-trivial, and provably derived from the deterministic candidate
    identity (which is itself a hash of strategy/symbol/direction/candle-open,
    so it is stable across retries and restarts).
    """
    reasons: list[str] = []

    plan_id = str(plan_id or "").strip()
    candidate_id = str(candidate_id or "").strip()

    if not plan_id:
        reasons.append("plan_id_missing")
    if not candidate_id:
        reasons.append("candidate_id_missing")

    if plan_id and candidate_id:
        try:
            expected = deterministic_plan_id(candidate_id)
        except ValueError:
            reasons.append("candidate_id_invalid")
        else:
            if plan_id != expected:
                # A plan_id that is not the canonical function of candidate_id
                # cannot be assumed stable across restarts.
                reasons.append("plan_id_not_deterministic_from_candidate_id")

    return reasons


def validate_client_oid(client_oid: str) -> str:
    """Return ``client_oid`` if it satisfies the exchange format constraints."""
    value = str(client_oid or "")
    if not CLIENT_OID_PATTERN.match(value):
        raise OrderIdentityError(
            f"clientOid violates exchange format constraints: length={len(value)}"
        )
    if len(value) > MAX_CLIENT_OID_LENGTH:
        raise OrderIdentityError(
            f"clientOid exceeds {MAX_CLIENT_OID_LENGTH} characters: length={len(value)}"
        )
    return value


def derive_entry_client_oid(
    *,
    plan_id: str,
    candidate_id: str,
    symbol: str,
    direction: str,
    strategy: str,
    leg: str = ENTRY_LEG_MARKET,
    bot_identity: str = DEFAULT_BOT_IDENTITY,
) -> str:
    """Derive the stable clientOid for one logical entry leg.

    Raises ``OrderIdentityError`` when the plan identity is not trustworthy;
    the caller must then refuse to submit rather than fall back to a random id.
    """
    reasons = validate_plan_identity(plan_id=plan_id, candidate_id=candidate_id)
    if reasons:
        raise OrderIdentityError(
            f"plan identity unusable for exchange idempotency: {','.join(reasons)}"
        )

    leg_key = str(leg or "").strip().lower()
    if leg_key not in _LEG_CODES:
        raise OrderIdentityError(f"unsupported entry leg: {leg}")

    identity_root = str(bot_identity or DEFAULT_BOT_IDENTITY).strip().lower()
    if not re.match(r"^[a-z0-9_-]{1,16}$", identity_root):
        raise OrderIdentityError(f"bot identity is not clientOid-safe: {bot_identity!r}")

    # Field order is part of the explicit identity contract (same convention as
    # candidate_lifecycle.identity), so the digest can never silently change.
    material = json.dumps(
        [
            ("namespace", "bitget-entry-order-v1"),
            ("bot_identity", identity_root),
            ("strategy", str(strategy or "").strip().lower()),
            ("symbol", str(symbol or "").strip().upper()),
            ("direction", str(direction or "").strip().upper()),
            ("leg", leg_key),
            ("plan_id", str(plan_id).strip()),
            ("candidate_id", str(candidate_id).strip()),
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]

    return validate_client_oid(f"{identity_root}-{_LEG_CODES[leg_key]}-{digest}")


__all__ = [
    "CLIENT_OID_PATTERN",
    "DEFAULT_BOT_IDENTITY",
    "ENTRY_LEG_MAKER",
    "ENTRY_LEG_MARKET",
    "MAX_CLIENT_OID_LENGTH",
    "OrderIdentityError",
    "derive_entry_client_oid",
    "validate_client_oid",
    "validate_plan_identity",
]
