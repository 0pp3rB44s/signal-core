"""Standalone, research-only Bitget market-data collector process. RESEARCH ONLY -- no keys,
no orders, no execution.

This is a thin process wrapper around the existing, already-hardened MicroflowCollector
(microflow/collector.py). It does not duplicate any collector logic -- everything below is
process plumbing (signal handling, a PID file, logging, a configurable data directory) around
a class that already does all the market-data work and is already covered by its own tests.

Why this file exists: MicroflowCollector previously only ever ran embedded inside the
production trading engine (app.main, via microflow/live.py) for live signal generation. That
embedding is completely unaffected by this file and continues to run in production exactly as
before -- this process is a SEPARATE, additional, independently-restartable subscriber to the
same public Bitget market data, so cross-venue research collection no longer depends on
production's process lifecycle. Duplicate Bitget market-data subscriptions (this process next
to production's embedded one) are an accepted, deliberate consequence of that isolation.

Nothing here imports execution, risk, order-routing, or app.main -- and cannot, by
construction: MicroflowCollector's own import graph (microflow.candidates, microflow.near_miss,
microflow.segments, microflow.state) is pure standard library plus this repo's microflow
package, with zero trading-path dependencies. That boundary is enforced by test, not by
convention, exactly as for the Binance research collector
(research/collectors/binance_collector.py), whose module-level structure this file mirrors.

Required launch interpreter: .venv/bin/python3 (or its resolved absolute path). Do not launch
with a bare `python`/`python3`/`/usr/bin/python3` -- see the cross-process monotonic-clock
comparability finding from the release-gate phase, which is specific to the Python >=3.10
build this repo's venv provides.
"""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path

from microflow.collector import DEFAULT_SYMBOLS, MicroflowCollector

log = logging.getLogger("research.bitget_microflow")

#: The exact command this process must be launched with. Documented here (not just in the
#: docstring above) so a test can assert the contract doesn't silently drift.
REQUIRED_LAUNCH_COMMAND = ".venv/bin/python3 -m research.collectors.bitget_microflow_collector"


def run(symbols: tuple[str, ...], data_dir: Path) -> None:  # pragma: no cover - process entry
    """Own a MicroflowCollector for the lifetime of this process. Never returns until the
    collector's own run() loop exits (SIGTERM/SIGINT via the handlers below, or an
    unrecoverable error inside the collector itself)."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(data_dir / "collector.log")],
    )
    collector = MicroflowCollector(symbols=symbols, data_dir=data_dir)
    signal.signal(signal.SIGTERM, lambda *_: collector.stop())
    signal.signal(signal.SIGINT, lambda *_: collector.stop())
    (data_dir / "collector.pid").write_text(str(os.getpid()), encoding="utf-8")
    try:
        collector.run()
    finally:
        (data_dir / "collector.pid").unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover
    syms = tuple((os.environ.get("BITGET_MICROFLOW_SYMBOLS") or
                  ",".join(DEFAULT_SYMBOLS)).split(","))
    run(syms, Path(os.environ.get("BITGET_MICROFLOW_DATA_DIR") or "data_store/research_bitget_v2"))
