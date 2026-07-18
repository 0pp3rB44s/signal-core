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


def test_keepalive_only_uses_strict_forward_paper_launcher() -> None:
    text = (SCRIPTS / "forward_paper_keepalive.sh").read_text(encoding="utf-8")
    assert "start_forward_paper.sh" in text
    assert "start_bot.sh" not in text, "keepalive mag nooit de gewone (env-gestuurde) startroute gebruiken"
    assert "EXECUTION_ENABLED=true" not in text
    assert "FAIL-CLOSED" in text  # snelle-crashbegrenzer aanwezig


def test_ops_scripts_contain_no_order_or_secret_words() -> None:
    for name in OPS:
        text = (SCRIPTS / name).read_text(encoding="utf-8").lower()
        for token in ("place_order", "api_key=", "api_secret", "passphrase"):
            assert token not in text, f"{name} bevat {token}"
