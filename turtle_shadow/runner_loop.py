"""Persistent loop wrapper around turtle_shadow.runner.run_one_scan().

Usage: python -m turtle_shadow.runner_loop [interval_seconds]

Daily-bar strategy: a much longer interval than the 15m production shadow's
60s is appropriate (new candles close once per day per symbol), but this
still checks periodically so a freshly-completed candle is picked up
promptly rather than once a day at a fixed clock time -- simpler and more
robust than cron-like scheduling, and consistent with the polling-loop
pattern already used by scripts/start_sunday_shadow.sh.
"""

from __future__ import annotations

import json
import logging
import sys
import time

from turtle_shadow.runner import run_one_scan

logger = logging.getLogger("turtle_shadow.loop")


def main() -> None:
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 1800
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    logger.info("TURTLE_SHADOW_LOOP_START | interval=%ss", interval)
    while True:
        try:
            result = run_one_scan()
            logger.info("TURTLE_SCAN_COMPLETE | %s", json.dumps(result, default=str))
        except Exception:
            logger.exception("TURTLE_SCAN_FAILED")
        time.sleep(interval)


if __name__ == "__main__":
    main()
