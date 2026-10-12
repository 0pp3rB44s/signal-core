#!/usr/bin/env python3
"""Write `execution_status_v1.json` for Mission Control (read-only producer).

Default: LOCAL ONLY (no network). `--probe-exchange` adds GET-only exchange reads
via the bot's existing client; credentials come from the normal Settings/env and
are never printed. Output dir default: export/mission_control/.
"""
from __future__ import annotations

import argparse
import json
import sys

sys.dont_write_bytecode = True
from app.config import Settings  # noqa: E402
from execution.status_contract import ExchangeReadOnlyProbe, JSON_SCHEMA, build_execution_status, write_execution_status  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe-exchange", action="store_true")
    ap.add_argument("--out-dir", default="export/mission_control")
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--print-schema", action="store_true")
    a = ap.parse_args(argv)
    if a.print_schema:
        print(json.dumps(JSON_SCHEMA, indent=1)); return 0
    settings = Settings(_env_file=a.env_file) if a.env_file else Settings()
    exchange = None
    if a.probe_exchange:
        from clients.bitget_rest import BitgetRestClient
        exchange = ExchangeReadOnlyProbe(BitgetRestClient(settings), settings.bitget_product_type).snapshot()
    doc = build_execution_status(settings=settings, root=".", exchange=exchange)
    path = write_execution_status(doc, a.out_dir)
    print(f"wrote {path} risk={doc['risk']['state']} connection={doc['connection']['status']} mode={doc['mode']['execution_mode']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
