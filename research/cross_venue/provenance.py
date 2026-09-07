"""Objective 13 — provenance stamping for derived research artifacts.

Any file this package (or a future consumer of it) writes as a derived
output must be stamped with this record. This module never writes into
``data_store/`` and never touches a clean-epoch raw file — it only produces
the metadata dict a caller should attach to whatever *separate* derived
artifact it is producing.
"""

from __future__ import annotations

import platform
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .contract import NORMALIZATION_CONTRACT_VERSION


@dataclass(frozen=True)
class Provenance:
    clean_epoch_id: str
    source_files: tuple[str, ...]
    source_schema: tuple[str, ...]
    normalization_contract_version: str
    research_commit_sha: str
    python_version: str
    generated_utc: str

    def as_dict(self) -> dict:
        return {
            "CLEAN_EPOCH_ID": self.clean_epoch_id,
            "source_files": list(self.source_files),
            "source_schema": list(self.source_schema),
            "normalization_contract_version": self.normalization_contract_version,
            "research_commit_sha": self.research_commit_sha,
            "python_version": self.python_version,
            "generated_utc": self.generated_utc,
        }


def _git_commit_sha(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return "UNKNOWN"


def build_provenance(
    *,
    clean_epoch_id: str,
    source_files: tuple[str, ...],
    source_schema: tuple[str, ...],
    repo_root: Path,
) -> Provenance:
    return Provenance(
        clean_epoch_id=clean_epoch_id,
        source_files=tuple(source_files),
        source_schema=tuple(source_schema),
        normalization_contract_version=NORMALIZATION_CONTRACT_VERSION,
        research_commit_sha=_git_commit_sha(repo_root),
        python_version=platform.python_version(),
        generated_utc=datetime.now(timezone.utc).isoformat(),
    )
