#!/usr/bin/env python3
"""Operator CLI for the file-based emergency stop.

  status                         show state (exit 3 when engaged)
  engage --reason R --by NAME    block ALL new entries (positions keep being managed)
  clear  --by NAME [--note N]    remove the block (explicit, recorded)
  flatten --by NAME --i-understand-this-sends-real-close-orders
                                 engage, then close every open position on the exchange
                                 through the bot's existing emergency_flatten_all().
                                 Never runs without the long flag; prints a plan first.
"""
from __future__ import annotations

import argparse
import json
import sys

sys.dont_write_bytecode = True
from execution.emergency_stop import EmergencyStop  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    e = sub.add_parser("engage"); e.add_argument("--reason", required=True); e.add_argument("--by", default="operator")
    c = sub.add_parser("clear"); c.add_argument("--by", required=True); c.add_argument("--note", default="")
    f = sub.add_parser("flatten"); f.add_argument("--by", required=True)
    f.add_argument("--i-understand-this-sends-real-close-orders", action="store_true", dest="confirmed")
    a = ap.parse_args(argv)
    es = EmergencyStop()
    if a.cmd == "status":
        s = es.status(); print(json.dumps(s, indent=1)); return 3 if s["active"] else 0
    if a.cmd == "engage":
        print(json.dumps(es.engage(a.reason, a.by), indent=1)); return 0
    if a.cmd == "clear":
        print(json.dumps(es.clear(a.by, a.note), indent=1)); return 0
    if a.cmd == "flatten":
        es.engage("operator requested FLATTEN", a.by)
        if not a.confirmed:
            print("Emergency stop ENGAGED. FLATTEN NOT executed (missing --i-understand-this-sends-real-close-orders).")
            return 2
        from app.config import Settings
        from clients.bitget_rest import BitgetRestClient
        res = BitgetRestClient(Settings()).emergency_flatten_all()
        print(json.dumps({"flatten_result": res}, indent=1, default=str)[:4000])
        print("Verify flat on the exchange (positions + plan orders) before clearing the stop.")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
