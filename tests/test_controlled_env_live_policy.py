from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts import manage_runner_env_live as managed


def _env(path: Path) -> Path:
    path.write_text(
        "BITGET_API_KEY=key-secret\nBITGET_API_SECRET=api-secret\n"
        "BITGET_API_PASSPHRASE=pass-secret\nDASHBOARD_PASSWORD=dash-secret\n"
        "CUSTOM_ACCESS_TOKEN=custom-secret\n"
        "EXECUTION_MODE=DRY_RUN\nMAX_LEVERAGE=5\nMAX_OPEN_POSITIONS=2\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_unauthorized_and_wrong_host_or_path_fail_closed(tmp_path, monkeypatch):
    env = _env(tmp_path / ".env.live")
    monkeypatch.delenv(managed.AUTHORIZATION_ENV, raising=False)
    with pytest.raises(managed.EnvLivePolicyError, match="authorization"):
        managed._require_authorized_runner(env_path=env, repo_path=tmp_path,
                                           hostname=managed.AUTHORITATIVE_RUNNER_HOST)
    monkeypatch.setenv(managed.AUTHORIZATION_ENV, "true")
    with pytest.raises(managed.EnvLivePolicyError, match="authoritative Runner"):
        managed._require_authorized_runner(env_path=env, repo_path=tmp_path, hostname="work-mac")
    with pytest.raises(managed.EnvLivePolicyError, match="checkout"):
        managed._require_authorized_runner(env_path=env, repo_path=tmp_path,
                                           hostname=managed.AUTHORITATIVE_RUNNER_HOST)


def test_inspection_reports_secret_presence_but_never_values(tmp_path):
    env = _env(tmp_path / ".env.live")
    report = managed.inspect_redacted(env)
    rendered = repr(report)
    assert report["credential_presence"]["BITGET_API_KEY"] == "PRESENT"
    assert report["credential_presence"]["CUSTOM_ACCESS_TOKEN"] == "PRESENT"
    assert report["non_secret"]["EXECUTION_MODE"] == "DRY_RUN"
    for secret in ("key-secret", "api-secret", "pass-secret", "dash-secret", "custom-secret"):
        assert secret not in rendered


def test_write_is_atomic_backed_up_0600_and_preserves_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "AUTHORITATIVE_RUNNER_REPO", tmp_path)
    env = _env(tmp_path / ".env.live")
    original = env.read_bytes()
    backup_dir = tmp_path / "backups" / "env-live"
    report = managed.apply_updates(env, {"EXECUTION_MODE": "LIVE", "MAX_LEVERAGE": "3"},
                                   backup_dir=backup_dir)
    backups = list(backup_dir.glob("env.live.*.backup"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert env.stat().st_mode & 0o777 == 0o600
    text = env.read_text()
    assert "BITGET_API_KEY=key-secret" in text
    assert report["changed_non_secret_keys"] == {
        "EXECUTION_MODE": {"before": "DRY_RUN", "after": "LIVE"},
        "MAX_LEVERAGE": {"before": "5", "after": "3"},
    }
    assert "key-secret" not in repr(report)


@pytest.mark.parametrize("key", ["BITGET_API_KEY", "BITGET_API_SECRET", "DASHBOARD_PASSWORD",
                                 "UNLISTED_ACCESS_TOKEN"])
def test_secret_updates_are_always_rejected(tmp_path, key):
    env = _env(tmp_path / ".env.live")
    with pytest.raises(managed.EnvLivePolicyError, match="credential mutation forbidden"):
        managed.apply_updates(env, {key: "replacement"}, backup_dir=tmp_path / "backups")
    assert not (tmp_path / "backups").exists()


def test_generic_env_and_unknown_keys_cannot_be_targeted(tmp_path, monkeypatch):
    generic = _env(tmp_path / ".env")
    monkeypatch.setenv(managed.AUTHORIZATION_ENV, "true")
    with pytest.raises(managed.EnvLivePolicyError, match="not the authoritative Runner .env.live"):
        managed._require_authorized_runner(
            env_path=generic, repo_path=managed.AUTHORITATIVE_RUNNER_REPO,
            hostname=managed.AUTHORITATIVE_RUNNER_HOST,
        )
    with pytest.raises(managed.EnvLivePolicyError, match="not an approved non-secret"):
        managed.apply_updates(generic, {"ARBITRARY_KEY": "value"}, backup_dir=tmp_path / "backups")


def test_gitignore_and_policy_keep_env_and_backups_out_of_git():
    root = Path(__file__).resolve().parents[1]
    ignored = (root / ".gitignore").read_text()
    assert ".env.*" in ignored and "backups/" in ignored and "*.backup*" in ignored
    policy = (root / "AGENTS.md").read_text()
    assert "Neither `.env.live` nor its backups may ever be committed" in policy
    assert "arbitrary `.env*` access" in policy
    result = subprocess.run(
        ["bash", str(root / "scripts/verify_repository_hygiene.sh"), "--check-paths"],
        input=".env.live\nbackups/env-live/env.live.20260812T120000Z.backup\n",
        text=True,
        capture_output=True,
        cwd=root,
        check=False,
    )
    assert result.returncode == 1
    assert ".env.live" in result.stderr
    assert "backups/env-live" in result.stderr


def test_tool_contains_no_secret_transport_or_git_operations():
    source = Path(managed.__file__).read_text()
    for forbidden in ("subprocess", "requests", "socket.create_connection", "git add", "scp ", "rsync "):
        assert forbidden not in source
