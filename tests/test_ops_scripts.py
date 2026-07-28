from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPTS = Path(__file__).parents[1] / "scripts"
OPS = ("daily_ops_check.sh", "forward_paper_keepalive.sh")


def test_ops_scripts_parse_and_are_executable() -> None:
    for name in OPS:
        path = SCRIPTS / name
        assert path.exists(), name
        subprocess.run(["bash", "-n", str(path)], check=True)


def test_keepalive_only_uses_the_single_production_entry_point() -> None:
    """The keepalive must restart through launch_forward.sh and nothing else.

    Until 2026-07-28 it restarted via start_forward_paper.sh, which enforced mode
    safety but read the ambient .env. Automatic restarts therefore ran at the
    ambient scope (MAX_SYMBOLS=40) instead of the pilot ceilings in .env.forward
    -- production acceptance defect D2. There must be exactly one entry point.
    """
    text = (SCRIPTS / "forward_paper_keepalive.sh").read_text(encoding="utf-8")
    invocations = [
        line.strip() for line in text.splitlines()
        if "./scripts/" in line and ".sh" in line and not line.strip().startswith("#")
    ]
    assert invocations, "keepalive invokes no launcher at all"
    for line in invocations:
        assert "launch_forward.sh" in line, f"must restart via launch_forward.sh, got: {line}"
    assert "start_bot.sh" not in text, "keepalive mag nooit de gewone (env-gestuurde) startroute gebruiken"
    assert "EXECUTION_ENABLED=true" not in text
    assert "FAIL-CLOSED" in text  # snelle-crashbegrenzer aanwezig


def test_single_production_entry_point_pins_the_forward_env() -> None:
    """launch_forward.sh must pin .env.forward and never reference the live config."""
    text = (SCRIPTS / "launch_forward.sh").read_text(encoding="utf-8")
    assert 'guard_load_env ".env.forward"' in text
    assert "guard_assert_forward_mode" in text
    assert "guard_assert_pilot_limits" in text
    assert ".env.live" not in text, "the forward launcher must never reference the live config"


def test_ops_scripts_contain_no_order_or_secret_words() -> None:
    for name in OPS:
        text = (SCRIPTS / name).read_text(encoding="utf-8").lower()
        for token in ("place_order", "api_key=", "api_secret", "passphrase"):
            assert token not in text, f"{name} bevat {token}"
