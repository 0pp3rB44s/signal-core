from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_repository_hygiene.sh"
LIVE_BASELINE = "f0742de19d309a26e9b6a821fa3860c6bbbd3289"


def _run(*args: str, cwd: Path = ROOT, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=cwd,
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "GITHUB_BASE_REF": ""},
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "hygiene-test@example.invalid")
    _git(repo, "config", "user.name", "Hygiene Test")
    (repo / "safe.txt").write_text("safe\n", encoding="utf-8")
    _git(repo, "add", "safe.txt")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def test_path_classifier_rejects_environment_operational_dataset_cache_and_editor_paths():
    forbidden = [
        ".env",
        ".env.forward",
        "nested/.env.live.example",
        "logs/private.log",
        "nested/state/position.json",
        "data/trades.csv",
        "datasets/local.json",
        "cache/market.bin",
        "keys/operator.pem",
        "worker.pid",
        "pkg/__pycache__/module.pyc",
        ".pytest_cache/v/cache/nodeids",
        ".idea/workspace.xml",
        ".vscode/settings.json",
        ".DS_Store",
        "notes.txt~",
    ]
    for path in forbidden:
        result = _run("--check-paths", stdin=f"src/safe.py\n{path}\n")
        assert result.returncode == 1, path
        assert path in result.stderr


def test_path_classifier_accepts_source_under_data_and_normal_source_paths():
    result = _run(
        "--check-paths",
        stdin="app/config.py\ndata/__init__.py\ndata/repository.py\ntests/test_safety.py\n",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "repository_hygiene=PASS"


def test_tracked_mode_rejects_nested_forbidden_path(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "credentials" / "placeholder.txt"
    path.parent.mkdir()
    path.write_text("not a credential\n", encoding="utf-8")
    _git(repo, "add", "credentials/placeholder.txt")
    _git(repo, "commit", "-qm", "forbidden path")

    result = _run("--tracked", cwd=repo)

    assert result.returncode == 1
    assert "credentials/placeholder.txt" in result.stderr


def test_release_mode_checks_commit_staged_worktree_and_untracked_diffs(tmp_path):
    scenarios = (
        ("commit", "logs/session.log"),
        ("staged", "nested/state/position.json"),
        ("worktree", "runtime/worker.pid"),
        ("untracked", "data/cache.json"),
    )
    for index, (kind, relative) in enumerate(scenarios):
        repo = _repo(tmp_path / str(index))
        baseline = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        path = repo / relative
        path.parent.mkdir(parents=True)
        path.write_text("synthetic test data\n", encoding="utf-8")
        if kind in {"commit", "staged", "worktree"}:
            _git(repo, "add", relative)
        if kind == "commit":
            _git(repo, "commit", "-qm", "forbidden commit")
        elif kind == "worktree":
            path.write_text("modified synthetic test data\n", encoding="utf-8")

        result = _run("--base", baseline, cwd=repo)

        assert result.returncode == 1, kind
        assert relative in result.stderr


def test_release_mode_rejects_forbidden_deleted_path(tmp_path):
    repo = _repo(tmp_path)
    path = repo / "logs" / "historical.log"
    path.parent.mkdir()
    path.write_text("synthetic test data\n", encoding="utf-8")
    _git(repo, "add", "logs/historical.log")
    _git(repo, "commit", "-qm", "historical forbidden path")
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    path.unlink()
    _git(repo, "add", "logs/historical.log")

    result = _run("--base", baseline, cwd=repo)

    assert result.returncode == 1
    assert "logs/historical.log" in result.stderr


def test_release_mode_rejects_symlink_without_following_it(tmp_path):
    repo = _repo(tmp_path)
    target = tmp_path / "outside.txt"
    target.write_text("synthetic external content\n", encoding="utf-8")
    link = repo / "linked.txt"
    link.symlink_to(target)

    result = _run("--base", "HEAD", cwd=repo)

    assert result.returncode == 1
    assert "symbolic links" in result.stderr


def test_real_release_patch_is_hygienic_relative_to_approved_live_baseline():
    result = _run("--base", LIVE_BASELINE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "repository_hygiene=PASS"
