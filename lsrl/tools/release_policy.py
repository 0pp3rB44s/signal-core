"""The reviewed RELEASE POLICY: the one owner-authored file the ceremony's approvals hang from. It approves nothing by itself.

    {
      "kind": "LSRL_MICROLIVE_RELEASE_POLICY", "schema": 1, "decision": "APPROVE",
      "created_at": <epoch s>, "expires_at": <epoch s, at most 7 days after created_at>,
      "approved": {"sha": <40 hex>, "config_hash": <64 hex>, "runtime_config_hash": <64 hex>, "account": <non-secret id>,
                   "capital_ceiling_usdt": <owner's choice, 100..150>, "exposure_ceiling_usdt": <owner's choice, up to 5 x capital>},
      "artifacts": {"prospective_green": {"path": ..., "sha256": ...}, "engineering_approval": {...},
                    "process_down_protection": {...}, "exchange_semantics": {...}}
    }

HOW IT IS TRUSTED. Its own SHA-256 is typed by the OWNER at the terminal (`owner_ceremony.read_pinned_digest`): it cannot come from a flag, an environment
variable, a file or a pipe, so the file cannot vouch for itself and a copied or edited file is refused. Every artifact it lists is again pinned by digest; the
ceremony then re-verifies each artifact's CONTENT (decision, expiry, and a binding to the exact release SHA and frozen config). Nothing in this module chooses
the capital: `capital_ceiling_usdt` is whatever the owner wrote, refused unless it lies inside the fixed band.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lsrl.capital import CapitalPolicy, CapitalRefused

KIND = "LSRL_MICROLIVE_RELEASE_POLICY"
SCHEMA = 1
MAX_VALIDITY_S = 7 * 24 * 3600
ARTIFACT_GATES = ("prospective_green", "engineering_approval", "process_down_protection", "exchange_semantics")
_HEX = "0123456789abcdef"


class PolicyRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactRef:
    path: Path
    sha256: str


@dataclass(frozen=True)
class ReleasePolicy:
    sha256: str                      # of the policy file itself (typed by the owner)
    approved_sha: str
    config_hash: str
    runtime_config_hash: str
    account: str
    capital: CapitalPolicy
    artifacts: dict[str, ArtifactRef]
    created_at: float
    expires_at: float


def _hex(value: Any, n: int) -> bool:
    return type(value) is str and len(value) == n and all(c in _HEX for c in value)


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def load_policy(path: Path, typed_digest: str, *, now: float | None = None) -> ReleasePolicy:
    now = time.time() if now is None else now
    if not _hex(typed_digest, 64):
        raise PolicyRefused("POLICY_DIGEST_NOT_TYPED")
    try:
        if path.is_symlink() or not path.is_file():
            raise PolicyRefused("POLICY_NOT_PRESENT")
        raw = path.read_bytes()
    except OSError as exc:
        raise PolicyRefused("POLICY_UNREADABLE") from exc
    if hashlib.sha256(raw).hexdigest() != typed_digest:
        raise PolicyRefused("POLICY_DIGEST_MISMATCH")
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise PolicyRefused("POLICY_NOT_JSON") from exc
    if not isinstance(doc, dict) or doc.get("kind") != KIND or doc.get("schema") != SCHEMA:
        raise PolicyRefused("POLICY_KIND_OR_SCHEMA")
    if doc.get("decision") != "APPROVE":
        raise PolicyRefused("POLICY_NOT_APPROVED")
    created, expires = doc.get("created_at"), doc.get("expires_at")
    if not (_finite(created) and _finite(expires)) or not (created <= now + 60 and now < expires <= created + MAX_VALIDITY_S):
        raise PolicyRefused("POLICY_EXPIRED_OR_UNBOUNDED")
    approved = doc.get("approved")
    if not isinstance(approved, dict) or set(approved) != {"sha", "config_hash", "runtime_config_hash", "account",
                                                           "capital_ceiling_usdt", "exposure_ceiling_usdt"}:
        raise PolicyRefused("POLICY_APPROVED_FIELDS")
    if not (_hex(approved["sha"], 40) and _hex(approved["config_hash"], 64) and _hex(approved["runtime_config_hash"], 64)):
        raise PolicyRefused("POLICY_IDENTITY_MALFORMED")
    try:
        capital = CapitalPolicy(approved["capital_ceiling_usdt"], approved["exposure_ceiling_usdt"])
    except CapitalRefused as exc:
        raise PolicyRefused(f"POLICY_CAPITAL:{exc}") from exc
    artifacts = doc.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_GATES):
        raise PolicyRefused("POLICY_ARTIFACTS_INCOMPLETE")
    refs: dict[str, ArtifactRef] = {}
    for name in ARTIFACT_GATES:
        ref = artifacts[name]
        if not isinstance(ref, dict) or set(ref) != {"path", "sha256"} or not isinstance(ref["path"], str) or not ref["path"] \
                or not _hex(ref["sha256"], 64):
            raise PolicyRefused(f"POLICY_ARTIFACT_REF:{name}")
        refs[name] = ArtifactRef(Path(ref["path"]), ref["sha256"])
    return ReleasePolicy(typed_digest, approved["sha"], approved["config_hash"], approved["runtime_config_hash"], str(approved["account"]),
                         capital, refs, float(created), float(expires))
