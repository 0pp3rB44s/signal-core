from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from agents_v3.llm.model_router import ModelDecision
from agents_v3.planner.planner import Plan
from agents_v3.repository.code_chunker import chunk_files
from agents_v3.repository.repo_indexer import RepoIndex


# Budgets sized for qwen2.5-coder with num_ctx=16384 (see ollama_provider):
# ~24k chars of prompt is roughly 8k tokens, leaving headroom for the response.
MAX_CONTEXT_CHARS = 24000
MAX_CHARS_PER_FILE = 6000


def build_repo_map(index: RepoIndex, max_dirs: int = 40) -> str:
    """Compact package overview so the model always sees the whole bot layout."""
    by_dir: dict[str, int] = defaultdict(int)
    for file in index.production_files:
        parts = Path(file).parts
        top = parts[0] if len(parts) > 1 else "."
        by_dir[top] += 1

    lines = ["Repository map (production python files per package):"]
    for directory, count in sorted(by_dir.items())[:max_dirs]:
        lines.append(f"- {directory}/ ({count} files)")
    lines.append("Critical files: " + ", ".join(index.critical_files))
    lines.append("Risk files: " + ", ".join(index.risk_files))
    return "\n".join(lines)


def build_prompt(
    task: str,
    plan: Plan,
    selected_files: list[str],
    model: ModelDecision,
    index: RepoIndex | None = None,
) -> str:
    parts: list[str] = []

    parts.append("You are CGCAgent, a disciplined software engineering agent for a Bitget trading bot.")
    parts.append("")
    parts.append("CRITICAL OUTPUT RULE:")
    parts.append("Return ONLY valid JSON. No markdown. No explanation outside JSON. No code fences.")
    parts.append("")
    parts.append("JSON schema:")
    parts.append("{")
    parts.append('  "summary": "short summary",')
    parts.append('  "root_cause": "root cause hypothesis",')
    parts.append('  "files_to_modify": ["path/to/file.py"],')
    parts.append('  "tests_to_run": ["pytest tests/test_file.py"],')
    parts.append('  "risk": "LOW|MEDIUM|HIGH",')
    parts.append('  "diff": "unified diff here or empty string",')
    parts.append('  "edit_plan": {')
    parts.append('    "operation": "replace_once",')
    parts.append('    "file_path": "exact existing repository path",')
    parts.append('    "old_text": "exact unique text copied from code context",')
    parts.append('    "new_text": "complete replacement text"')
    parts.append('  },')
    parts.append('  "approval_required": true')
    parts.append("}")
    parts.append("")
    parts.append("Safety rules:")
    parts.append("- Never touch .env or secrets.")
    parts.append("- Never increase leverage or live risk without approval.")
    parts.append("- Never disable SL/TP protection.")
    parts.append("- Prefer minimal, testable changes.")
    parts.append("- Use exact repository paths from Selected files.")
    parts.append("- When no unified diff is supplied, provide one replace_once edit_plan.")
    parts.append("- old_text must be copied exactly from Code context and occur only once.")
    parts.append("- If the task is audit/index/plan only, set diff to empty string and edit_plan fields to empty strings.")
    parts.append("")
    parts.append(f"Task: {task}")
    parts.append(f"Risk level: {plan.risk_level}")
    parts.append(f"Model decision: {model.provider}/{model.model}: {model.reason}")
    parts.append("")

    if index is not None:
        parts.append(build_repo_map(index))
        parts.append("")

    parts.append("Plan steps:")
    for step in plan.steps:
        parts.append(f"- {step}")
    parts.append("")
    parts.append("Selected files:")
    for file in selected_files:
        parts.append(f"- {file}")
    parts.append("")

    parts.append("Code context:")
    header = "\n".join(parts)
    remaining = MAX_CONTEXT_CHARS - len(header)

    for file_path in selected_files:
        if remaining <= 0:
            break
        file_budget = min(MAX_CHARS_PER_FILE, remaining)
        file_text_parts: list[str] = []
        for chunk in chunk_files([file_path]):
            file_text_parts.append(f"\nFILE: {chunk.file_path}:{chunk.start_line}-{chunk.end_line}")
            file_text_parts.append(chunk.text)
        file_text = "\n".join(file_text_parts)
        if len(file_text) > file_budget:
            file_text = file_text[:file_budget] + "\n[File truncated by CGCAgent budget]"
        parts.append(file_text)
        remaining -= len(file_text)

    prompt = "\n".join(parts)
    if len(prompt) > MAX_CONTEXT_CHARS:
        return prompt[:MAX_CONTEXT_CHARS] + "\n\n[Context truncated by CGCAgent budget]"
    return prompt
