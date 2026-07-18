from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "scripts/lib/platform_preflight.sh"


def shell(command: str, *, env: dict[str, str] | None = None, cwd: Path = ROOT):
    return subprocess.run(["bash", "-c", command], cwd=cwd, env={**os.environ, **(env or {})}, text=True, capture_output=True)


def source(expression: str, **env: str):
    return shell(f'source "{LIB}"; {expression}', env=env)


def test_01_bootstrap_accepts_arm64():
    result = source("detect_architecture", CGC_UNAME_M="arm64")
    assert result.returncode == 0 and result.stdout.strip() == "arm64"


def test_02_bootstrap_accepts_x86_64():
    result = source("detect_architecture", CGC_UNAME_M="x86_64")
    assert result.returncode == 0 and result.stdout.strip() == "x86_64"


def test_03_unsupported_architecture_fails():
    assert source("detect_architecture", CGC_UNAME_M="sparc").returncode != 0


def test_04_homebrew_prefix_selection():
    assert source("homebrew_prefix_for_architecture arm64").stdout.strip() == "/opt/homebrew"
    assert source("homebrew_prefix_for_architecture x86_64").stdout.strip() == "/usr/local"


def fake_python(tmp_path: Path, version: str) -> Path:
    path = tmp_path / "python"
    path.write_text(f"#!/usr/bin/env bash\necho {version}\n")
    path.chmod(0o755)
    return path


def test_05_system_python_39_is_rejected(tmp_path: Path):
    python = fake_python(tmp_path, "3.9")
    result = source("find_compatible_python 3.12", CGC_UNAME_M="x86_64", CGC_PYTHON_BIN=str(python))
    assert result.returncode != 0 and "found 3.9" in result.stderr


def test_06_correct_python_major_minor_is_accepted(tmp_path: Path):
    python = fake_python(tmp_path, "3.12")
    result = source("find_compatible_python 3.12", CGC_UNAME_M="arm64", CGC_PYTHON_BIN=str(python))
    assert result.returncode == 0 and result.stdout.strip() == str(python)


def test_07_wrong_venv_is_not_silently_reused():
    script = (ROOT / "scripts/bootstrap_mac.sh").read_text()
    assert "existing .venv uses Python" in script and "--recreate-venv" in script


def test_08_recreate_preserves_old_venv():
    script = (ROOT / "scripts/bootstrap_mac.sh").read_text()
    assert '.venv.backup.$(date -u +%Y%m%dT%H%M%SZ)' in script
    assert 'mv .venv "$backup"' in script
    assert "rm -rf .venv" not in script


def test_09_deploy_preflight_has_no_checkout_or_state_write_path(tmp_path: Path):
    repo = tmp_path / "repo"; repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    for relative in ("scripts/deploy_runner.sh", "scripts/lib/platform_preflight.sh"):
        target = repo / relative; target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((ROOT / relative).read_text()); target.chmod(0o755)
    (repo / ".python-version").write_text("3.12\n")
    (repo / "requirements.txt").write_text("")
    (repo / "runner.env.fixture").write_text("CGC_RUNTIME_MODE=runner\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True)
    target_commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "remote", "add", "origin", str(repo)], cwd=repo, check=True)
    prefix = tmp_path / "brew"; (prefix / "bin").mkdir(parents=True)
    brew = prefix / "bin/brew"; brew.write_text("#!/usr/bin/env bash\nexit 0\n"); brew.chmod(0o755)
    python = fake_python(tmp_path, "3.12")
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True)
    result = subprocess.run(
        [str(repo / "scripts/deploy_runner.sh"), "--preflight", target_commit], cwd=repo,
        env={**os.environ, "CGC_UNAME_S": "Darwin", "CGC_UNAME_M": "x86_64", "CGC_HOMEBREW_PREFIX": str(prefix), "CGC_PYTHON_BIN": str(python), "CGC_ENV_FILE": "runner.env.fixture"},
        text=True, capture_output=True,
    )
    after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True)
    assert result.returncode == 0, result.stderr
    assert before == after and "checkout_changed=no" in result.stdout
    assert not (repo / "state").exists()


def test_10_deploy_preflight_does_not_start_processes():
    script = (ROOT / "scripts/deploy_runner.sh").read_text()
    assert "processes_started=no" in script
    assert not any(value in script for value in ("launchctl start", "systemctl start", "run_bot.py"))


def test_11_env_is_preserved():
    script = (ROOT / "scripts/deploy_runner.sh").read_text()
    assert "CGC_ENV_FILE:-.env" in script and "rm .env" not in script and "mv .env" not in script


def test_12_state_and_reports_are_preserved():
    script = (ROOT / "scripts/deploy_runner.sh").read_text()
    assert "rm -rf state" not in script and "rm -rf reports" not in script


def test_13_environment_template_contains_no_secret_values(tmp_path: Path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".env.example").write_text("# secret\nBITGET_API_KEY=example-value\nCGC_RUNTIME_MODE=production\nCGC_STATE_ROOT=elsewhere\n")
    script = tmp_path / "scripts/create_runner_env_template.sh"
    script.write_text((ROOT / "scripts/create_runner_env_template.sh").read_text()); script.chmod(0o755)
    subprocess.run([str(script)], cwd=tmp_path, check=True, capture_output=True, text=True)
    generated = (tmp_path / ".env.runner.template").read_text()
    assert "example-value" not in generated
    assert "BITGET_API_KEY=\n" in generated and "CGC_RUNTIME_MODE=runner" in generated


def test_14_presence_report_never_prints_values(tmp_path: Path):
    example, work, runner = (tmp_path / name for name in ("example", "work", "runner"))
    example.write_text("KEY=placeholder\nMISSING=x\n"); work.write_text("KEY=work-secret\n"); runner.write_text("KEY=runner-secret\n")
    result = subprocess.run([str(ROOT / "scripts/compare_env_presence.sh"), str(example), str(work), str(runner)], text=True, capture_output=True, check=True)
    assert "work-secret" not in result.stdout and "runner-secret" not in result.stdout
    assert "KEY\tyes\tpresent\tpresent" in result.stdout


def test_15_explicit_tag_or_commit_remains_required():
    script = (ROOT / "scripts/deploy_runner.sh").read_text()
    assert "[[ $# -ge 1 ]] || usage" in script and "^[0-9a-f]{40}$" in script


def test_16_research_branches_are_undeployable():
    script = (ROOT / "scripts/deploy_runner.sh").read_text()
    assert 'merge-base --is-ancestor "$commit" origin/main' in script
    assert "research/" not in script


def test_17_rollback_contract_remains_intact():
    script = (ROOT / "scripts/deploy_runner.sh").read_text()
    assert "--rollback" in script and "refs/runner-backups/" in script and 'git update-ref "$backup" "$previous"' in script


def test_18_scripts_pass_bash_syntax():
    scripts = [ROOT / path for path in ("scripts/bootstrap_mac.sh", "scripts/deploy_runner.sh", "scripts/verify_checkout.sh", "scripts/verify_repository_hygiene.sh", "scripts/create_runner_env_template.sh", "scripts/compare_env_presence.sh", "scripts/lib/platform_preflight.sh")]
    subprocess.run(["bash", "-n", *map(str, scripts)], check=True)
