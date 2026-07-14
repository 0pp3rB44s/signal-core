from __future__ import annotations

import logging

from app.logger import setup_logging


def test_logging_redacts_credentials_from_console_and_file(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    setup_logging("INFO")

    logging.getLogger("security-test").info(
        "request api_key=%s password=%s Authorization: Bearer %s",
        "test-api-value",
        "test-password-value",
        "test-bearer-value",
    )

    console_output = capsys.readouterr().out
    file_output = (tmp_path / "logs" / "agent.log").read_text(encoding="utf-8")

    for output in (console_output, file_output):
        assert "test-api-value" not in output
        assert "test-password-value" not in output
        assert "test-bearer-value" not in output
        assert output.count("[REDACTED]") == 3
