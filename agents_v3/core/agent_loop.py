from __future__ import annotations

import json
from dataclasses import dataclass, field

from agents_v3.core.tools_registry import describe_tools, execute_tool
from agents_v3.llm.json_normalizer import normalize_json_text
from agents_v3.llm.llm_client import ask_model
from agents_v3.llm.model_router import choose_model
from agents_v3.llm.prompt_builder import build_repo_map
from agents_v3.repository.repo_indexer import RepoIndex


MAX_STEPS = 12
MAX_TRANSCRIPT_CHARS = 20000


SYSTEM_PROMPT = """You are CGCAgent, an autonomous engineering agent for a Bitget trading bot.
You work in steps. Each step you reply with ONLY one JSON object, no markdown, no code fences.

To use a tool:
{"thought": "why", "action": "tool", "tool": "tool_name", "args": {"arg": "value"}}

To finish with your final answer/proposal:
{"thought": "why", "action": "final", "result": {
  "summary": "short summary",
  "root_cause": "root cause hypothesis",
  "files_to_modify": ["path/to/file.py"],
  "tests_to_run": ["pytest tests/test_file.py"],
  "risk": "LOW|MEDIUM|HIGH",
  "diff": "",
  "edit_plan": {"operation": "replace_once", "file_path": "path", "old_text": "exact unique text", "new_text": "replacement"},
  "approval_required": true
}}

Rules:
- Explore before proposing: read the relevant files first with tools.
- files_to_modify and edit_plan.file_path must be exact paths you confirmed exist in THIS session via list_files, search_code or read_file. Never guess a path.
- old_text must be copied EXACTLY from read_file output (without the line-number prefixes) and occur only once in the file.
- Never touch .env or secrets. Never weaken stop-loss/take-profit protection. Never increase leverage or position size.
- Strategy tightening (stricter filters, fewer low-quality entries) is allowed; risk-increasing changes require approval_required=true.
- If the task is analysis-only, return action=final with empty diff and empty edit_plan fields.
- Use trade_stats to ground strategy decisions in real performance data.
"""


@dataclass
class AgentLoopResult:
    success: bool
    final_json: str
    steps_used: int
    transcript: list[str] = field(default_factory=list)


def _parse_step(text: str) -> dict | None:
    normalized = normalize_json_text(text)
    try:
        payload = json.loads(normalized)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _trim_transcript(parts: list[str]) -> list[str]:
    # Keep the header (task + tools) and the most recent exchanges within budget.
    if not parts:
        return parts
    head, tail = parts[0], parts[1:]
    total = len(head)
    kept: list[str] = []
    for part in reversed(tail):
        if total + len(part) > MAX_TRANSCRIPT_CHARS:
            break
        kept.append(part)
        total += len(part)
    return [head] + list(reversed(kept))


def run_agent_loop(task: str, index: RepoIndex, verbose: bool = True) -> AgentLoopResult:
    model = choose_model("do", task, "MEDIUM")
    header = "\n".join([
        SYSTEM_PROMPT,
        "Available tools:",
        describe_tools(),
        "",
        build_repo_map(index),
        "",
        f"Task: {task}",
    ])
    transcript: list[str] = [header]

    for step in range(1, MAX_STEPS + 1):
        prompt = "\n\n".join(_trim_transcript(transcript)) + "\n\nYour next step (JSON only):"
        response = ask_model(model.provider, model.model, prompt)
        if not response.success:
            return AgentLoopResult(False, "", step, transcript + [f"LLM error: {response.error}"])

        payload = _parse_step(response.text)
        if payload is None:
            transcript.append(
                "Assistant (invalid): " + response.text[:500]
                + "\nObservation: ERROR - reply was not a single valid JSON object. Reply with ONLY one JSON object."
            )
            continue

        action = str(payload.get("action") or "")
        if verbose:
            thought = str(payload.get("thought") or "")[:200]
            print(f"[step {step}] action={action or '?'} {('| ' + thought) if thought else ''}")

        if action == "final":
            result = payload.get("result")
            if not isinstance(result, dict):
                transcript.append(
                    "Assistant: " + json.dumps(payload)[:800]
                    + "\nObservation: ERROR - action=final requires a 'result' object."
                )
                continue
            return AgentLoopResult(True, json.dumps(result), step, transcript)

        if action == "tool":
            tool_name = str(payload.get("tool") or "")
            args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
            observation = execute_tool(tool_name, args)
            if verbose:
                print(f"[step {step}] {tool_name}({args}) -> {len(observation)} chars")
            transcript.append(
                "Assistant: " + json.dumps({"action": "tool", "tool": tool_name, "args": args})
                + "\nObservation: " + observation
            )
            continue

        transcript.append(
            "Assistant: " + json.dumps(payload)[:800]
            + "\nObservation: ERROR - action must be 'tool' or 'final'."
        )

    return AgentLoopResult(False, "", MAX_STEPS, transcript + ["Step budget exhausted without a final answer."])
