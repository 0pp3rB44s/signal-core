from __future__ import annotations

import time
import json
import multiprocessing

from app.config import Settings
from clients.bitget_base_client import BitgetBaseClient
from clients.interprocess_rate_limiter import InterprocessRateLimiter
from telemetry.safe_io import file_lock


def _wait_worker(path: str, interval: float, queue: multiprocessing.Queue) -> None:
    InterprocessRateLimiter(path, interval).wait()
    queue.put(time.time())


def _crash_with_lock(path: str) -> None:
    with file_lock(path):
        multiprocessing.Event().wait(0.02)
        raise SystemExit(0)


def test_rate_limit_is_shared_across_client_instances(tmp_path):
    settings = Settings(
        _env_file=None,
        BITGET_RATE_LIMIT_MIN_INTERVAL_MS=20,
        BITGET_RATE_LIMIT_STATE_PATH=str(tmp_path / "rate.json"),
    )
    first = BitgetBaseClient(settings=settings)
    second = BitgetBaseClient(settings=settings)
    first._rate_limit_wait()
    started = time.perf_counter()
    second._rate_limit_wait()

    assert time.perf_counter() - started >= 0.015


def test_rate_limit_is_shared_across_processes(tmp_path):
    path = str(tmp_path / "rate.json")
    queue: multiprocessing.Queue = multiprocessing.Queue()
    processes = [multiprocessing.Process(target=_wait_worker, args=(path, 0.04, queue)) for _ in range(3)]
    for process in processes:
        process.start()
    timestamps = sorted(queue.get(timeout=5) for _ in processes)
    for process in processes:
        process.join(timeout=5)
        assert process.exitcode == 0

    assert all(right - left >= 0.03 for left, right in zip(timestamps, timestamps[1:]))


def test_corrupt_state_fails_closed_then_recovers(tmp_path):
    path = tmp_path / "rate.json"
    path.write_text("not-json", encoding="utf-8")
    limiter = InterprocessRateLimiter(path, 0.03)

    started = time.perf_counter()
    limiter.wait()

    assert time.perf_counter() - started >= 0.025
    assert isinstance(json.loads(path.read_text(encoding="utf-8"))["last_request_epoch"], float)


def test_process_exit_releases_stale_lock(tmp_path):
    path = str(tmp_path / "rate.json")
    process = multiprocessing.Process(target=_crash_with_lock, args=(path,))
    process.start()
    process.join(timeout=2)
    assert process.exitcode == 0

    started = time.perf_counter()
    InterprocessRateLimiter(path, 0.001).wait()
    assert time.perf_counter() - started < 1.0
