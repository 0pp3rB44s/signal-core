"""Capacity + heartbeat monitor for a running LSRL PAPER/SHADOW collection. Read-only on the exchange (it never touches one).

    python -m lsrl.tools.capacity_monitor --data-dir <lsrl data dir>/paper --log-dir <monitor dir> [--notify] [--stop-on-alarm]

WHY. Agent C ruled `CAPACITY_GATE=PASS` for a PAPER collection window on two conditions: (i) it restarts UNDER MEASUREMENT with a free-space
alarm and a stop rule if the drain exceeds 100 MB/h for 30 minutes, and (ii) the recorder floor stays 1.0 GB. The first LSRL epoch also
showed a second gap: a recorder that hit its floor left the process alive and silent for 5 h 47 min because nothing watched it.

WHAT IT WATCHES (every `--interval` seconds, default 60):
  * FREE SPACE of the volume; ALARM below `--free-alarm-gb` (default 8), STOP below `--free-stop-gb` (default 3): the process is stopped
    gracefully (SIGTERM: recorder closed on a clean boundary) long before the recorder's own 1.0 GB floor would poison it;
  * DRAIN: the volume's free-space decrease over a sliding `--drain-window-min` (default 30) minutes, MINUS the growth of LSRL's own data
    directory, per hour. Above `--drain-limit-mb-h` (default 100) is the ALARM and, with `--stop-on-alarm`, the STOP. Endpoints are the mean of
    three samples so one transient (a compressed day, a temp file) is not mistaken for a trend;
  * HEARTBEAT: the process is alive; `status.json` is fresh; `recorder_error` and `engine_halted` are empty; the feed is not stuck unsafe.
    A poisoned recorder can never recover inside its process, so it is an alarm AND (with `--stop-on-alarm`) a stop.

WHAT COMES OUT: `ALARM.jsonl` (append-only), `ALARM.json` (currently active alarms), `monitor_status.json` (its own heartbeat: if THIS file is
stale, the monitor itself is dead), one line per sample in `samples.jsonl`, and optionally a macOS notification. It changes nothing else.
Monitor and collection are separate processes: a dead monitor never stops the collection, so its own heartbeat file must be watched.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

GB = 1e9
MB = 1e6


def allocated_bytes(root: Path) -> int:
    """Bytes ALLOCATED under `root` (sparse files count what they hold, not their apparent size)."""
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                st = os.lstat(os.path.join(base, name))
            except OSError:
                continue
            total += getattr(st, "st_blocks", 0) * 512 or st.st_size
    return total


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


class Monitor:
    def __init__(self, *, data_dir: Path, log_dir: Path, volume: str = "/", pidfile: Path | None = None,
                 interval_s: float = 60.0, free_alarm_gb: float = 8.0, free_stop_gb: float = 3.0,
                 drain_limit_mb_h: float = 100.0, drain_window_min: float = 30.0, drain_confirmations: int = 5,
                 stale_status_s: float = 120.0,
                 unsafe_feed_alarm_s: float = 600.0, stop_on_alarm: bool = False, notify: bool = False,
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep,
                 disk_free: Callable[[str], int] | None = None, dir_bytes: Callable[[Path], int] = allocated_bytes,
                 pid_alive: Callable[[int], bool] | None = None, kill: Callable[[int, int], None] = os.kill,
                 notifier: Callable[[str], None] | None = None) -> None:
        self.data_dir, self.log_dir, self.volume = Path(data_dir), Path(log_dir), volume
        self.pidfile = Path(pidfile) if pidfile else self.data_dir / "prospective.pid"
        self.interval_s, self.free_alarm_gb, self.free_stop_gb = interval_s, free_alarm_gb, free_stop_gb
        self.drain_limit_mb_h, self.drain_window_s = drain_limit_mb_h, drain_window_min * 60.0
        self.drain_confirmations, self._drain_streak = max(1, int(drain_confirmations)), 0
        self.stale_status_s, self.unsafe_feed_alarm_s = stale_status_s, unsafe_feed_alarm_s
        self.stop_on_alarm, self.notify = stop_on_alarm, notify
        self._clock, self._sleep, self._kill, self._dir_bytes = clock, sleep, kill, dir_bytes
        self._disk_free = disk_free or (lambda v: shutil.disk_usage(v).free)
        self._pid_alive = pid_alive or self._default_pid_alive
        self._notifier = notifier or self._osascript
        self.samples: deque[tuple[float, int, int]] = deque()          # (t, free_bytes, lsrl_dir_bytes)
        self.active: dict[str, dict[str, Any]] = {}
        self.stopped_pid: int | None = None
        self._unsafe_since: float | None = None
        self.log_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ sampling
    @staticmethod
    def _default_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _pid(self) -> int | None:
        try:
            return int(self.pidfile.read_text().strip())
        except (OSError, ValueError):
            return None

    def _status(self) -> tuple[dict[str, Any] | None, float | None]:
        path = self.data_dir / "status.json"
        try:
            doc = json.loads(path.read_text())
            data = doc.get("data") or {}
            saved = (doc.get("_state_metadata") or {}).get("saved_at")
            age = None
            if saved:
                from datetime import datetime
                age = self._clock() - datetime.fromisoformat(str(saved)).timestamp()
            return data, age
        except (OSError, ValueError, TypeError):
            return None, None

    def drain_rate_mb_h(self) -> float | None:
        """External drain over the window: (free decrease - LSRL's own growth) per hour, or None until the window is covered."""
        if not self.samples or self.samples[-1][0] - self.samples[0][0] < self.drain_window_s * 0.98 or len(self.samples) < 6:
            return None
        head, tail = list(self.samples)[:3], list(self.samples)[-3:]
        free_drop = _mean([s[1] for s in head]) - _mean([s[1] for s in tail])
        lsrl_growth = _mean([s[2] for s in tail]) - _mean([s[2] for s in head])
        hours = (_mean([s[0] for s in tail]) - _mean([s[0] for s in head])) / 3600.0
        return (free_drop - lsrl_growth) / MB / hours if hours > 0 else None

    # ------------------------------------------------------------------ one look
    def check(self) -> dict[str, Any]:
        now = self._clock()
        free, lsrl = int(self._disk_free(self.volume)), int(self._dir_bytes(self.data_dir))
        self.samples.append((now, free, lsrl))
        while self.samples and now - self.samples[0][0] > self.drain_window_s * 1.5:
            self.samples.popleft()
        alarms: dict[str, str] = {}
        stop_reasons: list[str] = []

        if free < self.free_stop_gb * GB:
            alarms["FREE_SPACE_STOP_LEVEL"] = f"free {free / GB:.2f} GB < stop level {self.free_stop_gb} GB"
            stop_reasons.append("FREE_SPACE_STOP_LEVEL")
        elif free < self.free_alarm_gb * GB:
            alarms["FREE_SPACE_LOW"] = f"free {free / GB:.2f} GB < alarm level {self.free_alarm_gb} GB"
        rate = self.drain_rate_mb_h()
        over = rate is not None and rate > self.drain_limit_mb_h
        self._drain_streak = self._drain_streak + 1 if over else 0
        if over:
            alarms["DRAIN_RATE"] = f"external drain {rate:.0f} MB/h over {self.drain_window_s / 60:.0f} min > limit {self.drain_limit_mb_h:.0f}"
            if self._drain_streak >= self.drain_confirmations:        # sustained, not one transient at the edge of the window
                stop_reasons.append("DRAIN_RATE")

        pid = self._pid()
        data, age = self._status()
        if pid is None:
            alarms["NO_PIDFILE"] = f"no readable pid file at {self.pidfile}"
        elif not self._pid_alive(pid):
            alarms["PROCESS_DEAD"] = f"process {pid} is not running"
        if data is None:
            alarms["STATUS_UNREADABLE"] = "status.json missing or unreadable"
        else:
            if age is not None and age > self.stale_status_s:
                alarms["STATUS_STALE"] = f"status.json last written {age:.0f} s ago (limit {self.stale_status_s:.0f})"
            if data.get("recorder_error"):
                alarms["RECORDER_ERROR"] = str(data["recorder_error"])[:200]
                stop_reasons.append("RECORDER_ERROR")            # a poisoned recorder can never recover inside its process
            if data.get("engine_halted"):
                alarms["ENGINE_HALTED"] = str(data["engine_halted"])[:200]
            state = ((data.get("feed") or {}).get("state") or "").upper()
            if state and state != "OK":
                self._unsafe_since = self._unsafe_since or now
                if now - self._unsafe_since > self.unsafe_feed_alarm_s:
                    alarms["FEED_UNSAFE"] = f"feed state {state} for {now - self._unsafe_since:.0f} s"
            else:
                self._unsafe_since = None

        self._publish(now, free, lsrl, rate, alarms, pid)
        if stop_reasons and self.stop_on_alarm:
            self._stop(pid, stop_reasons, now)
        return {"t": now, "free_gb": round(free / GB, 3), "drain_mb_h": None if rate is None else round(rate, 1),
                "alarms": dict(alarms), "stop_reasons": stop_reasons}

    # ------------------------------------------------------------------ outputs
    def _append(self, name: str, row: dict[str, Any]) -> None:
        with open(self.log_dir / name, "a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def _write_atomic(self, name: str, doc: dict[str, Any]) -> None:
        tmp = self.log_dir / (name + ".tmp")
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True))
        os.replace(tmp, self.log_dir / name)

    def _publish(self, now: float, free: int, lsrl: int, rate: float | None, alarms: dict[str, str], pid: int | None) -> None:
        self._append("samples.jsonl", {"t": now, "free_bytes": free, "lsrl_dir_bytes": lsrl,
                                       "drain_mb_h": None if rate is None else round(rate, 2), "pid": pid,
                                       "alarms": sorted(alarms)})
        for key, detail in alarms.items():
            if key not in self.active:                              # an alarm is written when it RAISES, not every minute
                self.active[key] = {"since": now, "detail": detail}
                self._append("ALARM.jsonl", {"t": now, "event": "RAISED", "alarm": key, "detail": detail})
                self._notify(f"LSRL monitor: {key} - {detail}")
        for key in [k for k in self.active if k not in alarms]:
            self._append("ALARM.jsonl", {"t": now, "event": "CLEARED", "alarm": key})
            del self.active[key]
        self._write_atomic("ALARM.json", {"t": now, "active": self.active})
        self._write_atomic("monitor_status.json", {"t": now, "alive": True, "interval_s": self.interval_s,
                                                   "free_gb": round(free / GB, 3), "active_alarms": sorted(self.active),
                                                   "stopped_pid": self.stopped_pid})

    def _stop(self, pid: int | None, reasons: list[str], now: float) -> None:
        if pid is None or self.stopped_pid == pid or not self._pid_alive(pid):
            return
        try:
            self._kill(pid, signal.SIGTERM)                         # graceful: the launcher joins its thread and closes the recorder
            self.stopped_pid = pid
            self._append("ALARM.jsonl", {"t": now, "event": "STOP_SENT", "pid": pid, "signal": "SIGTERM", "reasons": reasons})
            self._notify(f"LSRL monitor: STOP sent to {pid}: {', '.join(reasons)}")
        except OSError as exc:
            self._append("ALARM.jsonl", {"t": now, "event": "STOP_FAILED", "pid": pid, "error": str(exc)})

    def _notify(self, message: str) -> None:
        if self.notify:
            try:
                self._notifier(message)
            except Exception:                                       # noqa: BLE001 - a notification must never break the monitor
                pass

    @staticmethod
    def _osascript(message: str) -> None:
        text = message.replace('"', "'")[:200]
        subprocess.run(["osascript", "-e", f'display notification "{text}" with title "LSRL capacity monitor"'],
                       timeout=10, check=False, capture_output=True)

    def run(self, *, max_checks: int | None = None, until_process_exits: bool = False) -> int:
        n = 0
        while max_checks is None or n < max_checks:
            result = self.check()
            n += 1
            if until_process_exits and "PROCESS_DEAD" in result["alarms"]:
                return 3
            if max_checks is None or n < max_checks:
                self._sleep(self.interval_s)
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", type=Path, required=True, help="the LSRL data dir containing status.json (e.g. <root>/paper)")
    ap.add_argument("--log-dir", type=Path, required=True)
    ap.add_argument("--volume", default="/System/Volumes/Data" if sys.platform == "darwin" else "/")
    ap.add_argument("--pidfile", type=Path)
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--free-alarm-gb", type=float, default=8.0)
    ap.add_argument("--free-stop-gb", type=float, default=3.0)
    ap.add_argument("--drain-limit-mb-h", type=float, default=100.0)
    ap.add_argument("--drain-window-min", type=float, default=30.0)
    ap.add_argument("--drain-confirmations", type=int, default=5, help="consecutive over-limit checks before a STOP (the alarm is immediate)")
    ap.add_argument("--stale-status-s", type=float, default=120.0)
    ap.add_argument("--stop-on-alarm", action="store_true", help="SIGTERM the collection on a stop-level condition")
    ap.add_argument("--notify", action="store_true", help="macOS notification on a raised alarm")
    ap.add_argument("--once", action="store_true", help="one check, print it, exit (1 if any alarm)")
    args = ap.parse_args(argv)
    mon = Monitor(data_dir=args.data_dir, log_dir=args.log_dir, volume=args.volume, pidfile=args.pidfile,
                  interval_s=args.interval, free_alarm_gb=args.free_alarm_gb, free_stop_gb=args.free_stop_gb,
                  drain_limit_mb_h=args.drain_limit_mb_h, drain_window_min=args.drain_window_min,
                  drain_confirmations=args.drain_confirmations,
                  stale_status_s=args.stale_status_s, stop_on_alarm=args.stop_on_alarm, notify=args.notify)
    if args.once:
        result = mon.check()
        print(json.dumps(result, indent=1, sort_keys=True))
        return 1 if result["alarms"] else 0
    return mon.run(until_process_exits=False)


if __name__ == "__main__":
    sys.exit(main())
