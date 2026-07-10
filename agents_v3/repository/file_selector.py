from __future__ import annotations

from pathlib import Path

from agents_v3.planner.planner import Plan
from agents_v3.repository.repo_indexer import RepoIndex


MAX_SELECTED_FILES = 8


def _score_file(task: str, file_path: str) -> int:
    task_l = task.lower()
    file_l = file_path.lower()
    name_l = Path(file_path).name.lower()

    score = 0

    for token in task_l.replace("_", " ").replace("-", " ").split():
        if len(token) >= 3 and token in file_l:
            score += 10

    if "readme" in task_l and name_l == "readme.md":
        score += 100

    if "tp" in task_l and ("tp" in file_l or "trade_planner" in file_l or "adaptive_tp" in file_l):
        score += 80

    if "risk" in task_l and "risk" in file_l:
        score += 80

    if "execution" in task_l and "execution" in file_l:
        score += 60

    if file_path in ("agents_v3/README.md", "README.md"):
        score += 30

    if file_path.startswith("tests/"):
        score -= 15

    # The agent's own code only matters when the task is about the agent;
    # for bot tasks these files crowd out real bot context.
    if file_path.startswith("agents_v3/") and not any(
        k in task_l for k in ("agent", "cgcagent", "agents_v3")
    ):
        score -= 40

    return score


def select_files_for_task(plan: Plan, index: RepoIndex, limit: int = MAX_SELECTED_FILES) -> list[str]:
    candidates = list(dict.fromkeys(plan.relevant_files + plan.relevant_tests + index.production_files))

    ranked = sorted(
        candidates,
        key=lambda f: _score_file(plan.task, f),
        reverse=True,
    )

    selected = [f for f in ranked if _score_file(plan.task, f) > 0]

    if not selected:
        selected = plan.relevant_files[:limit]

    return selected[:limit]
