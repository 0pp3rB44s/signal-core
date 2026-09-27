from __future__ import annotations

import csv
import json
import multiprocessing
import threading

from telemetry.safe_io import append_csv_rows, atomic_write_json, locked_open


def _append_worker(path: str, worker: int, count: int) -> None:
    for index in range(count):
        append_csv_rows(
            path,
            fieldnames=["worker", "index"],
            rows=[{"worker": worker, "index": index}],
        )


def test_concurrent_process_csv_appends_have_one_header_and_no_corruption(tmp_path):
    path = tmp_path / "dataset.csv"
    processes = [
        multiprocessing.Process(target=_append_worker, args=(str(path), worker, 40))
        for worker in range(4)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines.count("worker,index") == 1
    rows = list(csv.DictReader(lines))
    assert len(rows) == 160
    assert all(set(row) == {"worker", "index"} for row in rows)


def test_atomic_json_never_exposes_partial_document(tmp_path):
    path = tmp_path / "learning.json"
    atomic_write_json(path, {"generation": 0, "values": list(range(50))})
    failures: list[Exception] = []

    def writer() -> None:
        for generation in range(1, 50):
            atomic_write_json(path, {"generation": generation, "values": list(range(50))})

    thread = threading.Thread(target=writer)
    thread.start()
    while thread.is_alive():
        try:
            with locked_open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            assert len(payload["values"]) == 50
        except Exception as exc:  # pragma: no cover - assertion payload below is clearer
            failures.append(exc)
    thread.join(timeout=5)

    assert failures == []
