from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class DocsMemory:
    index_exists: bool
    todo_exists: bool
    todo_open_items: list[str]
    linked_docs: list[str]


def read_docs_memory() -> DocsMemory:
    index_path = Path("docs/INDEX.md")
    todo_path = Path("docs/TODO.md")

    linked_docs: list[str] = []
    todo_open_items: list[str] = []

    if index_path.exists():
        for line in index_path.read_text().splitlines():
            if line.strip().startswith("- ["):
                linked_docs.append(line.strip())

    if todo_path.exists():
        for line in todo_path.read_text().splitlines():
            if line.strip().startswith("- [ ]"):
                todo_open_items.append(line.strip())

    return DocsMemory(
        index_exists=index_path.exists(),
        todo_exists=todo_path.exists(),
        todo_open_items=todo_open_items,
        linked_docs=linked_docs,
    )


def next_todo_task() -> str:
    memory = read_docs_memory()
    if not memory.todo_open_items:
        return ""

    first = memory.todo_open_items[0]
    task = first.replace("- [ ]", "").strip().rstrip(".")
    return task
