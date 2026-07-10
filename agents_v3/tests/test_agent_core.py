from __future__ import annotations

from agents_v3.llm.model_router import choose_model
from agents_v3.llm.prompt_builder import MAX_CONTEXT_CHARS, build_prompt, build_repo_map
from agents_v3.planner.planner import create_plan
from agents_v3.repository.file_selector import MAX_SELECTED_FILES, select_files_for_task
from agents_v3.repository.repo_indexer import build_repo_index
from agents_v3.safety.safety_guard import check_patch_safety


def test_safety_blocks_env_files():
    result = check_patch_safety([".env", ".env.production", "configs/prod.env"])
    assert not result.allowed
    assert len(result.reasons) == 3


def test_safety_blocks_dangerous_added_lines():
    diff = (
        "--- a/app/config.py\n"
        "+++ b/app/config.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-max_leverage = 5\n"
        "+max_leverage = 50\n"
    )
    result = check_patch_safety(["app/config.py"], diff)
    assert not result.allowed


def test_safety_allows_context_lines_with_risky_words():
    # Context lines (unchanged code) legitimately mention leverage;
    # only added lines may trigger the guard.
    diff = (
        "--- a/execution/position_manager.py\n"
        "+++ b/execution/position_manager.py\n"
        "@@ -10,3 +10,3 @@\n"
        " size = notional * leverage\n"
        "-log.info('close')\n"
        "+log.info('position close requested')\n"
    )
    result = check_patch_safety(["execution/position_manager.py"], diff)
    assert result.allowed, result.reasons


def test_safety_scans_plain_text_fully():
    result = check_patch_safety([], "please disable_sl for faster trades")
    assert not result.allowed


def test_model_router_routes_patch_modes_to_strong_model(monkeypatch):
    import agents_v3.llm.model_router as router

    monkeypatch.setattr(router, "FAST_CODE_MODEL", "fast-model")
    monkeypatch.setattr(router, "STRONG_CODE_MODEL", "strong-model")
    assert router.choose_model("do", "fix bug", "MEDIUM").model == "strong-model"
    assert router.choose_model("audit", "read", "HIGH").model == "strong-model"
    assert router.choose_model("audit", "read", "LOW").model == "fast-model"


def test_file_selector_returns_multiple_files():
    index = build_repo_index(".")
    plan = create_plan("fix position manager execution bug", index)
    selected = select_files_for_task(plan, index)
    assert 1 <= len(selected) <= MAX_SELECTED_FILES
    assert len(selected) > 2  # old ceiling was 2 files


def test_prompt_contains_repo_map_and_respects_budget():
    index = build_repo_index(".")
    plan = create_plan("fix position manager execution bug", index)
    selected = select_files_for_task(plan, index)
    model = choose_model("do", plan.task, plan.risk_level)
    prompt = build_prompt(plan.task, plan, selected, model, index=index)
    assert "Repository map" in prompt
    assert len(prompt) <= MAX_CONTEXT_CHARS + 100
    assert "execution/" in build_repo_map(index)


def test_registry_works_without_openai_package():
    from agents_v3.llm.provider_registry import ProviderRegistry

    registry = ProviderRegistry()
    assert registry.get("ollama") is not None
