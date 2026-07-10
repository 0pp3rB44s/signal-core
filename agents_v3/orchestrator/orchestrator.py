from __future__ import annotations

from pathlib import Path

from agents_v3.repository.repo_indexer import build_repo_index
from agents_v3.repository.file_selector import select_files_for_task
from agents_v3.planner.planner import create_plan
from agents_v3.llm.model_router import choose_model
from agents_v3.llm.prompt_builder import build_prompt
from agents_v3.llm.llm_client import ask_model
from agents_v3.llm.response_parser import parse_response
from agents_v3.tools.test_runner import run_tests
from agents_v3.tools.git_tool import git_status, git_diff_stat, git_diff_name_only
from agents_v3.safety.safety_guard import check_patch_safety
from agents_v3.tools.patch_tool import dry_run_patch
from agents_v3.tools.approval import require_approval
from agents_v3.tools.diff_tool import extract_diff, validate_unified_diff
from agents_v3.improvement.improvement_planner import build_improvement_backlog
from agents_v3.improvement.docs_memory import read_docs_memory
from agents_v3.lifecycle.patch_lifecycle import run_pending_patch_lifecycle
from agents_v3.core.agent_loop import run_agent_loop
from agents_v3.tools.trade_analyzer import analyze_trades, format_report
from agents_v3.task_engine.task_engine import finalize_llm_proposal, run_task


PENDING_PATCH = Path(".cgcagent_pending.patch")


def run(mode: str, task: str, approved: bool = False) -> int:
    print("CGCAgent v0.1")
    print(f"Mode: {mode}")
    print(f"Task: {task}")
    print("")

    index = build_repo_index(".")

    if mode == "analyze":
        days = 14
        for token in task.split():
            if token.isdigit():
                days = int(token)
                break
        analysis = analyze_trades(days=days)
        print(format_report(analysis))
        return 0

    if mode == "agent":
        loop_result = run_agent_loop(task, index)
        print("")
        print("Agent Loop")
        print("----------")
        print(f"Success: {loop_result.success}")
        print(f"Steps used: {loop_result.steps_used}")
        if not loop_result.success:
            print("Last transcript entries:")
            for entry in loop_result.transcript[-2:]:
                print(entry[:800])
            return 1
        print("")
        parsed_final = parse_response(loop_result.final_json)
        print("Analysis")
        print("--------")
        print(f"Summary: {parsed_final.summary}")
        print("")
        result = finalize_llm_proposal(loop_result.final_json, allowed_paths=None, task=task)
        print(f"Proposal success: {result.success}")
        print(f"Message: {result.message}")
        analysis_only = not result.success and result.message == "No applicable diff produced."
        if analysis_only:
            # A completed analysis task legitimately produces no patch.
            return 0
        if approved and result.success:
            lifecycle = run_pending_patch_lifecycle(human_approved=False)
            print("")
            print("Apply Lifecycle")
            print("---------------")
            print(f"Applied: {lifecycle.applied}")
            print(f"Tests passed: {lifecycle.tests_passed}")
            print(f"Runtime restarted: {lifecycle.runtime_restarted}")
            print(f"Message: {lifecycle.message}")
            return 0 if lifecycle.success else 1
        return 0 if result.success else 1

    if mode == "cycle":
        analysis = analyze_trades(days=14)
        print("Performance")
        print("-----------")
        print(format_report(analysis))
        print("")

        changed_files = [line.strip() for line in git_diff_name_only().output.splitlines() if line.strip()]
        docs_memory = read_docs_memory()
        backlog = build_improvement_backlog(index=index, git_changed_files=changed_files, docs_memory=docs_memory)
        if not backlog:
            print("No improvement items found; nothing to do.")
            return 0

        next_item = backlog[0]
        print("Selected Improvement")
        print("--------------------")
        print(f"Title: {next_item.title}")
        print(f"Reason: {next_item.reason}")
        print(f"Task: {next_item.suggested_task}")
        print("")

        loop_result = run_agent_loop(next_item.suggested_task, index)
        print("")
        print(f"Agent loop success: {loop_result.success} (steps: {loop_result.steps_used})")
        if not loop_result.success:
            return 1

        result = finalize_llm_proposal(loop_result.final_json, allowed_paths=None, task=next_item.suggested_task)
        if not result.success:
            print("Cycle stopped: no valid safe patch produced.")
            return 1

        if not approved:
            print("Cycle complete in proposal mode. Review .cgcagent_pending.patch and rerun with --approve to apply.")
            return 0

        lifecycle = run_pending_patch_lifecycle(human_approved=False)
        print("")
        print("Apply Lifecycle")
        print("---------------")
        print(f"Applied: {lifecycle.applied}")
        print(f"Tests passed: {lifecycle.tests_passed}")
        print(f"Rollback performed: {lifecycle.rollback_performed}")
        print(f"Runtime restarted: {lifecycle.runtime_restarted}")
        print(f"Patched files: {lifecycle.patched_files}")
        print(f"Message: {lifecycle.message}")
        return 0 if lifecycle.success else 1

    if mode == "audit":
        print("Repository Index")
        print("----------------")
        print(f"Root: {index.root}")
        print(f"Python files: {index.python_file_count}")
        print(f"Test files: {index.test_file_count}")
        print("")
        print("Critical files:")
        for file in index.critical_files[:20]:
            print(f"- {file}")
        print("")
        print("Risk files:")
        for file in index.risk_files[:20]:
            print(f"- {file}")
        return 0

    if mode == "safety":
        result = check_patch_safety([".env"], "increase leverage and disable_sl")
        print("Safety Check")
        print("------------")
        print(f"Allowed: {result.allowed}")
        print("Reasons:")
        for reason in result.reasons:
            print(f"- {reason}")
        return 0

    if mode == "status":
        status = git_status()
        diff_stat = git_diff_stat()
        diff_names = git_diff_name_only()

        print("Git Status")
        print("----------")
        print(status.output or "[clean]")
        print("")
        print("Diff Stat")
        print("---------")
        print(diff_stat.output or "[no diff]")
        print("")
        print("Changed Files")
        print("-------------")
        print(diff_names.output or "[none]")
        return 0

    if mode == "test":
        result = run_tests(["tests/test_adaptive_tp_engine.py", "tests/test_position_lifecycle_safety.py"])
        print("Test Run")
        print("--------")
        print(f"Command: {result.command}")
        print(f"Success: {result.success}")
        print(f"Return code: {result.return_code}")
        print("")
        print(result.output)
        return 0

    if mode == "plan":
        plan = create_plan(task, index)
        selected_files = select_files_for_task(plan, index)
        model = choose_model(mode, task, plan.risk_level)
        prompt = build_prompt(task, plan, selected_files, model, index=index)
        response = ask_model(model.provider, model.model, prompt)

        print("Execution Plan")
        print("--------------")
        print(f"Risk level: {plan.risk_level}")
        print("")
        print("Model decision:")
        print(f"- Provider: {model.provider}")
        print(f"- Model: {model.model}")
        print(f"- Reason: {model.reason}")
        print("")
        print("Selected context files:")
        for file in selected_files:
            print(f"- {file}")
        print("")
        print(f"Prompt chars: {len(prompt)}")
        print("")
        print("LLM Response")
        print("------------")

        if response.success:
            print(response.text)
        else:
            print("ERROR")
            print("-----")
            print(response.error)

        parsed = parse_response(response.text if response.success else "")
        print("")
        print("Parsed Response")
        print("---------------")
        print(f"Summary: {parsed.summary}")
        print(f"Files to modify: {parsed.files_to_modify}")
        print(f"Tests to run: {parsed.tests_to_run}")
        print(f"Approval required: {parsed.approval_required}")
        print("")
        print("Status: plan only. No files changed.")
        return 0

    if mode == "auto":
        changed_files = [line.strip() for line in git_diff_name_only().output.splitlines() if line.strip()]
        docs_memory = read_docs_memory()
        backlog = build_improvement_backlog(index=index, git_changed_files=changed_files, docs_memory=docs_memory)

        print("Auto Mode")
        print("---------")

        if not backlog:
            print("No improvement items found.")
            return 0

        next_item = backlog[0]
        print(f"Selected: {next_item.title}")
        print(f"Reason: {next_item.reason}")
        print(f"Task: {next_item.suggested_task}")
        print("")
        if not approved:
            print("Run this next:")
            print(f"python -m agents_v3.cli do \"{next_item.suggested_task}\"")
            return 0

        print("Auto approved. Running selected task...")
        result = run_task(next_item.suggested_task)

        if not result.success:
            print("Auto stopped: task did not produce a valid safe patch.")
            return 1

        lifecycle = run_pending_patch_lifecycle()
        print("")
        print("Auto Apply Lifecycle")
        print("--------------------")
        print(f"Applied: {lifecycle.applied}")
        print(f"Tests passed: {lifecycle.tests_passed}")
        print(f"Rollback performed: {lifecycle.rollback_performed}")
        print(f"Runtime restarted: {lifecycle.runtime_restarted}")
        print(f"Patched files: {lifecycle.patched_files}")
        print(f"Message: {lifecycle.message}")
        return 0 if lifecycle.success else 1

    if mode == "improve":
        changed_files = [line.strip() for line in git_diff_name_only().output.splitlines() if line.strip()]
        docs_memory = read_docs_memory()
        backlog = build_improvement_backlog(index=index, git_changed_files=changed_files, docs_memory=docs_memory)
        print("Improvement Backlog")
        print("-------------------")
        for item in backlog:
            print(f"{item.priority}. {item.title}")
            print(f"   Reason: {item.reason}")
            print(f"   Task: {item.suggested_task}")

        if backlog:
            next_item = backlog[0]
            print("")
            print("Next Suggested Action")
            print("---------------------")
            print(f"python -m agents_v3.cli do \"{next_item.suggested_task}\"")
        return 0

    if mode == "do":
        result = run_task(task)
        print("")
        print(f"Task engine success: {result.success}")
        print(f"Message: {result.message}")
        return 0

    if mode == "propose":
        plan = create_plan(task, index)
        selected_files = select_files_for_task(plan, index)
        model = choose_model(mode, task, plan.risk_level)
        prompt = build_prompt(task, plan, selected_files, model, index=index)
        response = ask_model(model.provider, model.model, prompt)

        diff_text = extract_diff(response.text if response.success else "")
        validation = validate_unified_diff(diff_text)

        print("Patch Proposal")
        print("--------------")
        print(f"LLM success: {response.success}")
        print(f"Diff valid: {validation.valid}")
        print(f"Files: {validation.files}")
        if validation.reasons:
            print("Reasons:")
            for reason in validation.reasons:
                print(f"- {reason}")
        print("")
        print("Status: proposal only. No files changed.")
        return 0

    if mode == "patch":
        approval = require_approval(approved)

        if approved:
            print("Apply Engine")
            print("------------")
            if not PENDING_PATCH.exists():
                print("No pending patch found.")
                return 0

            lifecycle = run_pending_patch_lifecycle(human_approved=True)
            print(f"Applied: {lifecycle.applied}")
            print(f"Tests passed: {lifecycle.tests_passed}")
            print(f"Rollback performed: {lifecycle.rollback_performed}")
            print(f"Runtime restarted: {lifecycle.runtime_restarted}")
            print(f"Patched files: {lifecycle.patched_files}")
            print(f"Message: {lifecycle.message}")
            return 0 if lifecycle.success else 1

        result = dry_run_patch(
            files_to_modify=["planning/trade_planner.py"],
            patch_text="safe dry run patch proposal",
        )
        print("Patch Dry Run")
        print("-------------")
        print(f"Success: {result.success}")
        print(f"Applied: {result.applied}")
        print(f"Message: {result.message}")
        print(f"Approval: {approval.approved} - {approval.reason}")
        if result.safety_reasons:
            print("Safety reasons:")
            for reason in result.safety_reasons:
                print(f"- {reason}")
        return 0

    print("Status: mode not implemented yet")
    return 0
