from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import Any

from telemetry.safe_io import atomic_write_json


class RuntimeDiagnostics:
    """Crash-safe, secret-free lifecycle breadcrumbs for the bot process."""

    def __init__(
        self,
        heartbeat_path: str | Path = "state/runtime_heartbeat.json",
        shutdown_path: str | Path = "state/last_shutdown.json",
    ) -> None:
        self.heartbeat_path = Path(heartbeat_path)
        self.shutdown_path = Path(shutdown_path)
        self.log = logging.getLogger("runtime_diagnostics")
        self._lock = threading.RLock()
        self._installed = False
        self._shutdown_written = False
        self._scan_started = 0
        self._scan_completed = 0
        self._previous_thread_hook = threading.excepthook

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _process_identity() -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "process_group": os.getpgrp(),
            "session_id": os.getsid(0),
        }

    def install(self) -> None:
        with self._lock:
            if self._installed:
                return
            self._installed = True
            threading.excepthook = self._thread_exception
            for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                signal.signal(signum, self._signal_handler)
            atexit.register(self._atexit)
        self.heartbeat("process_started")

    def heartbeat(self, stage: str, **fields: Any) -> None:
        with self._lock:
            if not self._installed:
                return
            if fields.pop("scan_started", False):
                self._scan_started += 1
            if fields.pop("scan_completed", False):
                self._scan_completed += 1
            payload = {
                "schema_version": 1,
                "timestamp": self._now(),
                "stage": str(stage),
                **self._process_identity(),
                "thread": threading.current_thread().name,
                "scan_cycles_started": self._scan_started,
                "scan_cycles_completed": self._scan_completed,
                "details": fields,
            }
            atomic_write_json(self.heartbeat_path, payload, indent=2, sort_keys=True)

    def record_shutdown(
        self,
        reason: str,
        *,
        exit_code: int | None,
        signal_name: str | None = None,
        force: bool = False,
    ) -> None:
        with self._lock:
            if self._shutdown_written and not force:
                return
            payload = {
                "schema_version": 1,
                "timestamp": self._now(),
                "reason": str(reason),
                "exit_code": exit_code,
                "signal": signal_name,
                **self._process_identity(),
                "scan_cycles_started": self._scan_started,
                "scan_cycles_completed": self._scan_completed,
            }
            atomic_write_json(self.shutdown_path, payload, indent=2, sort_keys=True)
            self._shutdown_written = True
            self._flush_logs()

    def _signal_handler(self, signum: int, _frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        exit_code = 128 + signum
        self.log.warning("RUNTIME_SIGNAL_RECEIVED | signal=%s | exit_code=%s", name, exit_code)
        self.record_shutdown(f"signal:{name}", exit_code=exit_code, signal_name=name, force=True)
        raise SystemExit(exit_code)

    def _thread_exception(self, args: threading.ExceptHookArgs) -> None:
        self.log.error(
            "RUNTIME_THREAD_EXCEPTION | thread=%s | type=%s",
            args.thread.name if args.thread else "UNKNOWN",
            args.exc_type.__name__,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        self.heartbeat(
            "thread_exception",
            thread_name=args.thread.name if args.thread else "UNKNOWN",
            exception_type=args.exc_type.__name__,
        )

    def _atexit(self) -> None:
        if not self._shutdown_written:
            self.record_shutdown("atexit_without_explicit_reason", exit_code=None)

    @staticmethod
    def _flush_logs() -> None:
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass


_runtime = RuntimeDiagnostics()


def get_runtime_diagnostics() -> RuntimeDiagnostics:
    return _runtime


def runtime_heartbeat(stage: str, **fields: Any) -> None:
    _runtime.heartbeat(stage, **fields)
