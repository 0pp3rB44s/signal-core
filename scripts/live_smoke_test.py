#!/usr/bin/env python3
"""Controlled LIVE smoke test: ONE minimum-size trade, fully confirmed, fully journaled.

NOTHING IN THIS FILE RUNS UNTIL THE OWNER DOES IT. Default mode is `preflight`:
read-only checks + the exact order that WOULD be sent. No order is possible
without ALL of:
  1. --mode execute  AND  --i-understand-this-places-a-real-order
  2. env LIVE_SMOKE_AUTH_SHA256 == sha256(spec)         (owner computes it from the printed spec)
  3. file state/LIVE_SMOKE_AUTHORISATION containing  spec_sha256=<hash>  and  authorised_by=<name>
  4. typed confirmation phrase equal to the one printed by `preflight`
  5. emergency stop NOT engaged; account flat; leverage/margin/posMode already match the spec
This tool never changes leverage, margin mode or position mode.

  python scripts/live_smoke_test.py --spec smoke_spec.json --env-file .env.live --mode preflight
  python scripts/live_smoke_test.py --spec smoke_spec.json --env-file .env.live --mode execute --i-understand-this-places-a-real-order
  python scripts/live_smoke_test.py --spec smoke_spec.json --env-file .env.live --mode resume
"""
from __future__ import annotations

import argparse
import json
import sys

sys.dont_write_bytecode = True
from app.config import Settings  # noqa: E402
from clients.bitget_rest import BitgetRestClient  # noqa: E402
from execution.smoke_lifecycle import AUTH_ENV, AUTH_FILE, BitgetPort, SmokeRun, SmokeSpec, authorise  # noqa: E402

STATE = "state/live_smoke_state.json"
JOURNAL = "state/live_smoke_journal.jsonl"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--mode", choices=["preflight", "execute", "resume"], default="preflight")
    ap.add_argument("--i-understand-this-places-a-real-order", action="store_true", dest="confirmed")
    a = ap.parse_args(argv)
    spec = SmokeSpec(**json.load(open(a.spec)))
    print("SPEC", spec.canonical())
    print("SPEC_SHA256", spec.sha256())
    print("CONFIRM_PHRASE", spec.confirm_phrase())
    # FORWARD_PAPER_ONLY must be explicitly off for private reads; the spec/gates decide everything else.
    settings = Settings(_env_file=a.env_file, FORWARD_PAPER_ONLY=False) if a.env_file else Settings(FORWARD_PAPER_ONLY=False)
    port = BitgetPort(BitgetRestClient(settings), settings.bitget_product_type)

    if a.mode == "resume":
        res = SmokeRun.resume(port, state_path=STATE, journal_path=JOURNAL)
    elif a.mode == "preflight":
        res = SmokeRun(port, spec, state_path=STATE, journal_path=JOURNAL).run(execute=False)
    else:
        if not a.confirmed:
            print("REFUSED: missing --i-understand-this-places-a-real-order"); return 2
        phrase = input(f"Type exactly: {spec.confirm_phrase()}\n> ").strip()
        why = authorise(spec, typed_phrase=phrase)
        if why:
            print("REFUSED:"); [print(" -", w) for w in why]; return 2
        if not (settings.execution_enabled and settings.execution_mode.upper() == "LIVE"):
            print("REFUSED: EXECUTION_ENABLED/EXECUTION_MODE=LIVE not set in the env file"); return 2
        res = SmokeRun(port, spec, state_path=STATE, journal_path=JOURNAL).run(execute=True, authorised=True)
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("outcome") in {"DRY_RUN_COMPLETE", "DONE", "FAILSAFE_CLOSED"} else 1


if __name__ == "__main__":
    sys.exit(main())
