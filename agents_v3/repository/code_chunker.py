from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MAX_CHUNK_LINES = 40


@dataclass
class CodeChunk:
    file_path: str
    start_line: int
    end_line: int
    text: str


def chunk_file(file_path: str, max_lines: int = MAX_CHUNK_LINES) -> list[CodeChunk]:
    path = Path(file_path)

    if not path.exists() or not path.is_file():
        return [
            CodeChunk(
                file_path=file_path,
                start_line=0,
                end_line=0,
                text="[MISSING FILE]",
            )
        ]

    lines = path.read_text(errors="replace").splitlines()
    chunks: list[CodeChunk] = []

    for i in range(0, len(lines), max_lines):
        chunk_lines = lines[i:i + max_lines]
        chunks.append(
            CodeChunk(
                file_path=file_path,
                start_line=i + 1,
                end_line=i + len(chunk_lines),
                text="\n".join(chunk_lines),
            )
        )

    return chunks


def chunk_files(file_paths: list[str], max_lines: int = MAX_CHUNK_LINES) -> list[CodeChunk]:
    chunks: list[CodeChunk] = []

    for file_path in file_paths:
        chunks.extend(chunk_file(file_path, max_lines=max_lines))

    return chunks
