from __future__ import annotations

import time

from app.config import Settings
from clients.bitget_base_client import BitgetBaseClient


def test_rate_limit_is_shared_across_client_instances():
    settings = Settings(
        _env_file=None,
        BITGET_RATE_LIMIT_MIN_INTERVAL_MS=20,
    )
    first = BitgetBaseClient(settings=settings)
    second = BitgetBaseClient(settings=settings)
    BitgetBaseClient._global_last_request_ts = 0.0

    first._rate_limit_wait()
    started = time.perf_counter()
    second._rate_limit_wait()

    assert time.perf_counter() - started >= 0.015
