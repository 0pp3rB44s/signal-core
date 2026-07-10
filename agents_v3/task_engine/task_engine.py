from __future__ import annotations

from dataclasses import dataclass

from agents_v3.llm.llm_client import ask_model
from agents_v3.llm.model_router import choose_model
from agents_v3.llm.prompt_builder import build_prompt
from agents_v3.llm.response_parser import parse_response
from agents_v3.llm.output_contract import validate_contract
from agents_v3.llm.json_normalizer import normalize_json_text
from agents_v3.planner.planner import create_plan
from agents_v3.repository.file_selector import select_files_for_task
from agents_v3.repository.repo_indexer import build_repo_index
from agents_v3.safety.safety_guard import check_patch_safety
from agents_v3.tools.diff_tool import extract_diff, validate_unified_diff
from agents_v3.tools.edit_plan import execute_edit_plan
from agents_v3.tools.patch_composer import compose_readme_quickstart_patch, compose_readme_status_patch, compose_prompt_budget_patch, compose_docs_source_patch
from pathlib import Path


@dataclass
class TaskEngineResult:
    success: bool
    message: str


def finalize_llm_proposal(
    response_text: str,
    allowed_paths: list[str] | None = None,
    task: str = "",
) -> TaskEngineResult:
    """Shared tail of every proposal flow: normalize the LLM output, turn it
    into a validated diff, run the safety guard and save the pending patch."""
    normalized_text = normalize_json_text(response_text)
    parsed = parse_response(normalized_text)
    contract = validate_contract(normalized_text or "{}")

    diff_text = extract_diff(normalized_text)

    edit_plan_result = None
    if not diff_text:
        edit_plan_result = execute_edit_plan(normalized_text, allowed_paths=allowed_paths)
        if edit_plan_result.valid and edit_plan_result.edit_result is not None:
            diff_text = edit_plan_result.edit_result.diff

    task_l = task.lower()
    if not diff_text and "quick start" in task_l and "readme" in task_l:
        diff_text = compose_readme_quickstart_patch()
    if not diff_text and "status command" in task_l and "readme" in task_l:
        diff_text = compose_readme_status_patch()
    if not diff_text and "context" in task_l and "budget" in task_l and "prompt_builder" in task_l:
        diff_text = compose_prompt_budget_patch()
    if not diff_text and "docs/todo.md" in task_l and "docs/index.md" in task_l:
        diff_text = compose_docs_source_patch()

    diff_validation = validate_unified_diff(diff_text)
    safety = check_patch_safety(
        files_to_modify=diff_validation.files,
        patch_text=diff_text,
    )

    print("Parsed")
    print("------")
    print(f"Summary: {parsed.summary}")
    print(f"Files to modify: {parsed.files_to_modify}")
    print(f"Tests to run: {parsed.tests_to_run}")
    print("")

    print("Contract")
    print("--------")
    print(f"Valid: {contract.valid}")
    if contract.reasons:
        print("Reasons:")
        for reason in contract.reasons:
            print(f"- {reason}")
    print("")

    if edit_plan_result is not None and not edit_plan_result.valid:
        print("Edit Plan")
        print("---------")
        print(f"Message: {edit_plan_result.message}")
        print("")

    print("Diff")
    print("----")
    print(f"Valid: {diff_validation.valid}")
    print(f"Files: {diff_validation.files}")
    if diff_validation.reasons:
        print("Reasons:")
        for reason in diff_validation.reasons:
            print(f"- {reason}")
    print("")

    print("Safety")
    print("------")
    print(f"Allowed: {safety.allowed}")
    if safety.reasons:
        print("Reasons:")
        for reason in safety.reasons:
            print(f"- {reason}")
    print("")

    if diff_validation.valid and safety.allowed:
        Path(".cgcagent_pending.patch").write_text(diff_text)
        print("Pending Patch")
        print("-------------")
        print("Saved to .cgcagent_pending.patch")
        print("")

    print("Final")
    print("-----")
    print("No files changed. Awaiting valid diff + explicit approval.")
    return TaskEngineResult(
        success=contract.valid and safety.allowed and diff_validation.valid,
        message="Proposal finalized." if diff_validation.valid else "No applicable diff produced.",
    )


def run_task(task: str) -> TaskEngineResult:
    print("CGCAgent Task Engine")
    print("--------------------")
    print(f"Task: {task}")
    print("")

    index = build_repo_index(".")
    plan = create_plan(task, index)
    selected_files = select_files_for_task(plan, index)
    model = choose_model("propose", task, plan.risk_level)
    prompt = build_prompt(task, plan, selected_files, model, index=index)

    print(f"Risk: {plan.risk_level}")
    print(f"Model: {model.provider}/{model.model}")
    print(f"Selected files: {len(selected_files)}")
    print(f"Prompt chars: {len(prompt)}")
    print("")

    response = ask_model(model.provider, model.model, prompt)
    response_text = response.text if response.success else ""
    normalized_text = normalize_json_text(response_text)
    contract = validate_contract(normalized_text if response.success else "{}")

    if response.success and not contract.valid:
        repair_prompt = (
            "Convert the following response into ONLY valid JSON. "
            "No markdown. No code fences. Use exactly these fields: "
            "summary, root_cause, files_to_modify, tests_to_run, risk, diff, edit_plan, approval_required. "
            "edit_plan must contain operation, file_path, old_text, and new_text. "
            "Preserve any existing edit_plan values exactly.\n\n"
            f"{response_text}"
        )
        repaired = ask_model(model.provider, model.model, repair_prompt)
        if repaired.success:
            response = repaired
            response_text = repaired.text

    print("LLM")
    print("---")
    print(f"Success: {response.success}")
    if not response.success:
        print(f"Error: {response.error}")
        print("")
        return TaskEngineResult(success=False, message="LLM call failed.")
    print("")

    return finalize_llm_proposal(response_text, allowed_paths=selected_files, task=task)
