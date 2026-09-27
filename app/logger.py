import logging
from logging.handlers import RotatingFileHandler
import re
import sys
from pathlib import Path


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


class SensitiveDataFilter(logging.Filter):
    """Redact common credential fields before any handler emits a record."""

    _credential_pattern = re.compile(
        r"(?ix)"
        r"(?P<prefix>"
        r"(?P<keyquote>['\"]?)"
        r"(?:api[_-]?(?:key|secret)|secret[_-]?key|passphrase|password|"
        r"access[_-]?token|refresh[_-]?token|token)"
        r"(?P=keyquote)\s*[:=]\s*"
        r")"
        r"(?P<value>"
        r"\"(?:\\.|[^\"])*\"|"
        r"'(?:\\.|[^'])*'|"
        r"[^\s,;}\]|]+"
        r")"
    )
    _authorization_pattern = re.compile(
        r"(?i)(['\"]?authorization['\"]?\s*[:=]\s*)"
        r"(?:bearer|basic)?\s*[^\s,;}\]]+"
    )
    _bearer_pattern = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")

    @classmethod
    def redact(cls, message: str) -> str:
        message = cls._authorization_pattern.sub(r"\1[REDACTED]", str(message))
        message = cls._credential_pattern.sub(
            lambda match: f"{match.group('prefix')}[REDACTED]",
            message,
        )
        return cls._bearer_pattern.sub("Bearer [REDACTED]", message)

    def filter(self, record: logging.LogRecord) -> bool:
        # Every propagated record reaches each handler's own filter instance
        # in turn (Logger-level filters, by contrast, do NOT apply to records
        # from named child loggers during propagation -- verified directly
        # against the stdlib, not assumed). With two handlers (console + file)
        # each carrying their own SensitiveDataFilter, the three-regex
        # redaction pass used to run twice per record -- pure wasted CPU that
        # compounded into a multi-hour write backlog under this bot's log
        # volume (proven root cause, live Runner, 2026-08-27). The same
        # LogRecord instance is shared across every handler in one
        # `Logger.callHandlers()` sweep, so a marker set here is visible to
        # the second call for the same event without leaking across events
        # (each `.info()`/`.warning()` call constructs a fresh record).
        if getattr(record, "_credentials_redacted", False):
            return True
        record.msg = self.redact(record.getMessage())
        record.args = ()
        record._credentials_redacted = True
        return True


def setup_logging(level: str = "INFO") -> None:
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    stream_handler.addFilter(SensitiveDataFilter())

    file_handler = RotatingFileHandler(
        log_dir / "agent.log",
        maxBytes=5_000_000,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler.addFilter(SensitiveDataFilter())

    root.addHandler(stream_handler)
    root.addHandler(file_handler)


def log_operation(logger: logging.Logger, marker: str, **fields: object) -> None:
    """Write consistent key=value operational log markers."""
    payload = " | ".join(f"{key}={value}" for key, value in fields.items())
    if payload:
        logger.info("%s | %s", marker, payload)
    else:
        logger.info("%s", marker)
