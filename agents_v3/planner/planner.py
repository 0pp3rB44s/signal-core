from __future__ import annotations

from dataclasses import dataclass

from agents_v3.repository.repo_indexer import RepoIndex


@dataclass
class Plan:
    task: str
    risk_level: str
    relevant_files: list[str]
    relevant_tests: list[str]
    steps: list[str]


def create_plan(task: str, index: RepoIndex) -> Plan:
    task_l = task.lower()

    relevant_files = list(index.critical_files)
    relevant_tests = [
        f for f in index.test_files
        if any(k in f.lower() for k in ("tp", "risk", "position", "planner", "adaptive"))
    ]

    risk_level = "MEDIUM"
    if any(k in task_l for k in ("live", "leverage", "risk", "sl", "stop", "position")):
        risk_level = "HIGH"
    elif any(k in task_l for k in ("read", "index", "audit", "rapport")):
        risk_level = "LOW"

    steps = [
        "Index repository context",
        "Select relevant production files",
        "Select related tests",
        "Assess trading safety risk",
        "Return plan before making changes",
    ]

    return Plan(
        task=task,
        risk_level=risk_level,
        relevant_files=relevant_files,
        relevant_tests=relevant_tests,
        steps=steps,
    )
