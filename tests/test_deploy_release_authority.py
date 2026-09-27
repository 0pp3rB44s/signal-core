from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TARGET_RELEASE = "ed7dbb4cb2de511cedaabca8a0550150a7ce109e"
PRODUCTION_REF = "origin/production/live-baseline-cd8671"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    subprocess.run(["git", "add", name], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=repo, check=True)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def release_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)

    for relative in ("scripts/deploy_runner.sh", "scripts/lib/platform_preflight.sh"):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text())
        target.chmod(0o755)
    (repo / ".python-version").write_text("3.12\n")
    (repo / "requirements.txt").write_text("")
    (repo / "runner.env.fixture").write_text("CGC_RUNTIME_MODE=runner\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "common"], cwd=repo, check=True)
    common = git(repo, "rev-parse", "HEAD")

    subprocess.run(["git", "switch", "-qc", "production/live-baseline-cd8671"], cwd=repo, check=True)
    production = commit_file(repo, "production.txt", "approved\n", "production")

    subprocess.run(["git", "switch", "-q", "main"], cwd=repo, check=True)
    main_only = commit_file(repo, "main-only.txt", "not released\n", "main only")

    subprocess.run(["git", "switch", "-qc", "feature/test", common], cwd=repo, check=True)
    feature_only = commit_file(repo, "feature.txt", "feature\n", "feature")

    subprocess.run(["git", "switch", "--orphan", "orphan"], cwd=repo, check=True, capture_output=True)
    (repo / "orphan.txt").write_text("orphan\n")
    subprocess.run(["git", "add", "orphan.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "orphan"], cwd=repo, check=True)
    orphan = git(repo, "rev-parse", "HEAD")

    subprocess.run(["git", "switch", "-q", "production/live-baseline-cd8671"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
    return repo, {
        "production": production,
        "main_only": main_only,
        "feature_only": feature_only,
        "orphan": orphan,
    }


def run_preflight(repo: Path, target: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    prefix = tmp_path / "brew"
    (prefix / "bin").mkdir(parents=True, exist_ok=True)
    brew = prefix / "bin/brew"
    brew.write_text("#!/usr/bin/env bash\nexit 0\n")
    brew.chmod(0o755)
    python = tmp_path / "python"
    python.write_text("#!/usr/bin/env bash\necho 3.12\n")
    python.chmod(0o755)
    return subprocess.run(
        [str(repo / "scripts/deploy_runner.sh"), "--preflight", target],
        cwd=repo,
        env={
            **os.environ,
            "CGC_UNAME_S": "Darwin",
            "CGC_UNAME_M": "x86_64",
            "CGC_HOMEBREW_PREFIX": str(prefix),
            "CGC_PYTHON_BIN": str(python),
            "CGC_ENV_FILE": "runner.env.fixture",
        },
        text=True,
        capture_output=True,
    )


def test_integrated_dynamic_grid_release_is_in_authoritative_history():
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", TARGET_RELEASE, PRODUCTION_REF],
        cwd=ROOT,
        check=True,
    )


def test_production_reachable_commit_passes(release_repo, tmp_path):
    repo, commits = release_repo
    result = run_preflight(repo, commits["production"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"target_commit={commits['production']}" in result.stdout


@pytest.mark.parametrize("commit_name", ["main_only", "feature_only", "orphan"])
def test_non_production_commits_fail_closed(release_repo, tmp_path, commit_name):
    repo, commits = release_repo
    result = run_preflight(repo, commits[commit_name], tmp_path)
    assert result.returncode != 0
    assert f"not reachable from {PRODUCTION_REF}" in result.stderr


def test_dirty_checkout_still_fails_before_deployment(release_repo, tmp_path):
    repo, commits = release_repo
    (repo / "dirty.txt").write_text("dirty\n")
    result = run_preflight(repo, commits["production"], tmp_path)
    assert result.returncode != 0
    assert "dirty runner checkout" in result.stderr
