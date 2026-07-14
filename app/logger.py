import logging
from logging.handlers import RotatingFileHandler
import re
import sys
from pathlib import Path


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


class SensitiveDataFilter(logging.Filter):
    """Redact common credential fields before any handler emits a record."""

    _credential_pattern = re.compile(
        r"(?i)(api[_-]?(?:key|secret)|passphrase|password|token)"
        r"(\s*[=:]\s*)([^,\s|]+)"
    )
    _bearer_pattern = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")

    @classmethod
    def redact(cls, message: str) -> str:
        message = cls._credential_pattern.sub(r"\1\2[REDACTED]", message)
        return cls._bearer_pattern.sub("Bearer [REDACTED]", message)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = ()
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
