from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def text(path: str) -> str:
    return (ROOT / path).read_text()


def test_deploy_requires_explicit_target_and_clean_tree():
    script = text("scripts/deploy_runner.sh")
    assert "[[ $# -ge 1 ]] || usage" in script
    assert 'git status --porcelain' in script
    assert "dirty runner checkout" in script


def test_deploy_accepts_only_annotated_tag_or_full_main_sha():
    script = text("scripts/deploy_runner.sh")
    assert "runner-v[0-9]" in script
    assert 'cat-file -t "refs/tags/$target"' in script
    assert "^[0-9a-f]{40}$" in script
    assert 'merge-base --is-ancestor "$commit" origin/main' in script


def test_deploy_preserves_backup_and_records_exact_sha():
    script = text("scripts/deploy_runner.sh")
    assert "refs/runner-backups/" in script
    assert 'git update-ref "$backup" "$previous"' in script
    assert "state/deployed_commit.txt.tmp" in script
    assert "state/deployed_commit.txt" in script


def test_deploy_never_starts_live_execution():
    script = text("scripts/deploy_runner.sh")
    forbidden = ("launchctl start", "systemctl start", "startup_runner.py", "run_bot.py")
    assert not any(value in script for value in forbidden)
    assert 'live_execution_started=no' in script


def test_operational_roots_are_documented_but_not_hardwired_into_trading():
    example = text(".env.example")
    for name in ("CGC_STATE_ROOT", "CGC_REPORTS_ROOT", "CGC_DATA_ROOT", "CGC_RUNTIME_MODE"):
        assert f"{name}=" in example
    changed_paths = {
        ".env.example", ".gitignore", ".python-version", ".github/workflows/ci.yml",
        "scripts/bootstrap_mac.sh", "scripts/deploy_runner.sh", "scripts/verify_checkout.sh",
        "scripts/verify_repository_hygiene.sh", "docs/RUNNER_MIGRATION.md",
    }
    assert not any(path.startswith(("strategies/", "execution/", "risk/", "planner/")) for path in changed_paths)


def test_ci_has_no_live_or_secret_dependency():
    workflow = text(".github/workflows/ci.yml")
    assert "python -m pytest -q" in workflow
    assert "verify_repository_hygiene.sh" in workflow
    assert "secrets." not in workflow
    assert "run_bot" not in workflow and "startup_runner" not in workflow
