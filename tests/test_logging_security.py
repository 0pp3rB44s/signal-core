from __future__ import annotations

import logging

import pytest
from unittest.mock import MagicMock

from app.logger import SensitiveDataFilter, setup_logging
from clients.bitget_base_client import BitgetBaseClient


@pytest.mark.parametrize(
    ("message", "secret"),
    [
        ('{"apiKey":"json-secret"}', "json-secret"),
        ("{'apiSecret': 'dict-secret'}", "dict-secret"),
        ("secretKey=kv-secret", "kv-secret"),
        ('passphrase="quoted secret"', "quoted secret"),
        ("password='password secret'", "password secret"),
        ("token=plain-token", "plain-token"),
        ('{"accessToken":"access-secret"}', "access-secret"),
        ('{"nested":{"refreshToken":"refresh-secret"}}', "refresh-secret"),
        ("Authorization: Bearer header-secret", "header-secret"),
        ("authorization=Basic basic-secret", "Basic basic-secret"),
        ("standalone Bearer standalone-secret", "standalone-secret"),
    ],
)
def test_logging_redacts_credentials_from_console_and_file(
    tmp_path, monkeypatch, capsys, message, secret
):
    monkeypatch.chdir(tmp_path)
    setup_logging("INFO")

    logging.getLogger("security-test").info("request %s", message)

    console_output = capsys.readouterr().out
    file_output = (tmp_path / "logs" / "agent.log").read_text(encoding="utf-8")

    for output in (console_output, file_output):
        assert secret not in output
        assert "[REDACTED]" in output


def test_private_response_error_uses_only_bounded_redacted_code_and_message():
    response = MagicMock()
    response.json.return_value = {
        "code": "40001",
        "msg": "invalid apiKey=private-value " + ("x" * 500),
        "data": {"apiSecret": "must-never-escape"},
    }

    code, message = BitgetBaseClient._safe_response_error(response, private=True)

    assert code == "40001"
    assert "private-value" not in message
    assert "must-never-escape" not in message
    assert "[REDACTED]" in message
    assert len(message) <= BitgetBaseClient._MAX_ERROR_MESSAGE_LENGTH


def test_redact_regex_pass_runs_exactly_once_per_record(tmp_path, monkeypatch):
    """SensitiveDataFilter.filter() runs on both the stream handler and the
    file handler for every record (correct: Logger-level filters do NOT
    apply to records from named child loggers during propagation, verified
    directly against the stdlib -- so per-handler filters are the only way
    to cover console output too). The expensive three-regex work inside
    `redact()` used to run twice per record regardless -- pure wasted CPU
    under this bot's log volume (proven root cause of a growing multi-hour
    write backlog on the live Runner, 2026-08-27). `filter()` itself may
    still be called twice (once per handler); `redact()` -- the actual
    cost -- must run at most once per logical event."""
    monkeypatch.chdir(tmp_path)
    setup_logging("INFO")

    redact_calls = []
    original_redact = SensitiveDataFilter.redact.__func__

    def counted(cls, message):
        redact_calls.append(message)
        return original_redact(cls, message)

    monkeypatch.setattr(SensitiveDataFilter, "redact", classmethod(counted))

    logging.getLogger("perf-test").info("token=plain-token")

    assert len(redact_calls) == 1, (
        f"expected exactly one redact() call per record, got {len(redact_calls)}"
    )


def test_private_non_json_response_body_is_never_copied():
    response = MagicMock()
    response.json.side_effect = ValueError("not json")
    response.text = "Authorization: Bearer must-never-escape"

    _, message = BitgetBaseClient._safe_response_error(response, private=True)

    assert message == "upstream error response"
    assert "must-never-escape" not in message
