import logging
from logging.handlers import RotatingFileHandler
import sys
from pathlib import Path


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


def setup_logging(level: str = "INFO") -> None:
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    file_handler = RotatingFileHandler(
        log_dir / "agent.log",
        maxBytes=5_000_000,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))

    root.addHandler(stream_handler)
    root.addHandler(file_handler)


def log_operation(logger: logging.Logger, marker: str, **fields: object) -> None:
    """Write consistent key=value operational log markers."""
    payload = " | ".join(f"{key}={value}" for key, value in fields.items())
    if payload:
        logger.info("%s | %s", marker, payload)
    else:
        logger.info("%s", marker)
