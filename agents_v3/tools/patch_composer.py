from __future__ import annotations

import difflib
from pathlib import Path

def build_unified_diff(file_path: str, new_text: str) -> str:
    path = Path(file_path)
    old_text = path.read_text() if path.exists() else ""
    return "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
    ))

def compose_readme_status_patch() -> str:
    file_path = "agents_v3/README.md"
    path = Path(file_path)
    current = path.read_text() if path.exists() else "# CGCAgent\n"
    if "## Status Command" in current:
        return ""
    addition = "\n\n## Status Command\n\nUse `status` to inspect current repository changes before applying patches.\n"
    return build_unified_diff(file_path, current.rstrip() + addition)


def compose_readme_quickstart_patch() -> str:
    file_path = "agents_v3/README.md"
    path = Path(file_path)
    current = path.read_text() if path.exists() else "# CGCAgent\n"
    if "## Quick Start" in current:
        return ""
    addition = "\n\n## Quick Start\n\n```bash\npython -m agents_v3.cli audit \"indexeer de repo\"\npython -m agents_v3.cli plan \"onderzoek TP planner bug\"\npython -m agents_v3.cli status \"show current repo changes\"\n```\n"
    return build_unified_diff(file_path, current.rstrip() + addition)

def compose_prompt_budget_patch() -> str:
    file_path = "agents_v3/llm/prompt_builder.py"
    path = Path(file_path)
    current = path.read_text() if path.exists() else ""

    if "MAX_CONTEXT_CHARS" in current:
        return ""

    new_text = current.replace(
        "MAX_CHUNKS = 2",
        "MAX_CHUNKS = 2\nMAX_CONTEXT_CHARS = 4000",
    )

    old_block = '    return "\\n".join(parts)'

    new_block = '''    prompt = "\\n".join(parts)
    if len(prompt) > MAX_CONTEXT_CHARS:
        return prompt[:MAX_CONTEXT_CHARS] + "\\n\\n[Context truncated by CGCAgent budget]"
    return prompt'''

    new_text = new_text.replace(old_block, new_block)

    return build_unified_diff(file_path, new_text)
def compose_docs_source_patch() -> str:
    file_path = "agents_v3/improvement/docs_memory.py"
    path = Path(file_path)
    current = path.read_text() if path.exists() else ""

    if "def next_todo_task" in current:
        return ""

    addition = """
def next_todo_task() -> str:
    memory = read_docs_memory()
    if not memory.todo_open_items:
        return ""

    first = memory.todo_open_items[0]
    task = first.replace("- [ ]", "").strip().rstrip(".")
    return task
"""
    new_text = current.rstrip() + "\n\n" + addition
    return build_unified_diff(file_path, new_text)
