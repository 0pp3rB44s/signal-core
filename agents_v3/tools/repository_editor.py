from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class EditResult:
    success: bool
    changed: bool
    file_path: str
    diff: str
    message: str


def _make_diff(file_path: str, old_text: str, new_text: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
        )
    )


def replace_once(
    file_path: str,
    old_text: str,
    new_text: str,
    *,
    apply: bool = False,
) -> EditResult:
    path = Path(file_path)

    if not path.exists():
        return EditResult(
            success=False,
            changed=False,
            file_path=file_path,
            diff="",
            message="Target file does not exist.",
        )

    current = path.read_text()

    count = current.count(old_text)
    if count == 0:
        return EditResult(
            success=False,
            changed=False,
            file_path=file_path,
            diff="",
            message="Target text was not found.",
        )

    if count > 1:
        return EditResult(
            success=False,
            changed=False,
            file_path=file_path,
            diff="",
            message=f"Target text is ambiguous: found {count} matches.",
        )

    updated = current.replace(old_text, new_text, 1)

    if updated == current:
        return EditResult(
            success=True,
            changed=False,
            file_path=file_path,
            diff="",
            message="No change required.",
        )

    diff = _make_diff(file_path, current, updated)

    if apply:
        path.write_text(updated)

    return EditResult(
        success=True,
        changed=True,
        file_path=file_path,
        diff=diff,
        message="Edit applied." if apply else "Edit preview generated.",
    )


def append_once(
    file_path: str,
    marker: str,
    addition: str,
    *,
    apply: bool = False,
) -> EditResult:
    path = Path(file_path)

    if not path.exists():
        return EditResult(
            success=False,
            changed=False,
            file_path=file_path,
            diff="",
            message="Target file does not exist.",
        )

    current = path.read_text()

    if marker in current:
        return EditResult(
            success=True,
            changed=False,
            file_path=file_path,
            diff="",
            message="Marker already exists; duplicate prevented.",
        )

    updated = current.rstrip() + "\n\n" + addition.strip() + "\n"
    diff = _make_diff(file_path, current, updated)

    if apply:
        path.write_text(updated)

    return EditResult(
        success=True,
        changed=True,
        file_path=file_path,
        diff=diff,
        message="Edit applied." if apply else "Edit preview generated.",
    )
