"""Independent audit of an LSRL recording directory (read-only).

    python -m lsrl.tools.verify_recording data_store/lsrl/shadow/recorded

Checks, in order: hash chain + tail anchor of the INPUT stream and of the DECISION stream, header/schema/config,
then replays the raw inputs through a FRESH observer + engine and requires the decisions to equal the ones the
live process wrote. Exit code 0 only if every check passes.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from lsrl.io.recorder import anchor_path, decision_files, input_files, read_bodies, read_header, verify_chain
from lsrl.io.replay import decisions_match, replay


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("root", type=Path, help="recording directory (contains inputs-*.jsonl[.gz])")
    args = ap.parse_args(argv)
    root = args.root
    ins, outs = input_files(root), decision_files(root)
    if not ins:
        print(f"FAIL no input files under {root}")
        return 1
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= passed
        print(f"{'PASS' if passed else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")

    for label, files, prefix in (("inputs", ins, "inputs"), ("decisions", outs, "decisions")):
        good, why = verify_chain(files, anchor=anchor_path(root, prefix))
        check(f"{label} hash chain + tail anchor", good, why)
    header = read_header(ins[0])
    print(f"      header: strategy={header['strategy']} schema={header['schema_version']} git={header['git_sha']} "
          f"symbol={header['symbol']} exchange={header['exchange']} mode={header['mode']} session={header['session_id']}")
    print("      NOTE: decision equivalence is guaranteed only for the code version that recorded the log "
          "(header git); a newer engine may legitimately write different records for the same inputs.")
    try:
        result = replay(ins, anchor=anchor_path(root, "inputs"))
    except ValueError as exc:
        check("replay through a fresh engine", False, str(exc))
        return 1
    check("replay through a fresh engine", True,
          f"sessions={result.sessions} records={len(result.records)} observer={result.observer.state}")
    same, why = decisions_match(ins, outs)
    check("decision equivalence (replayed == recorded)", same, why)
    kinds = Counter(r.kind for r in result.records)
    reasons = Counter(r.data.get("reason") for r in result.records if r.kind == "setup_rejected")
    print(f"      records: {dict(kinds)}")
    print(f"      rejection reasons: {dict(reasons.most_common(8))}")
    print(f"      feed stats: {dict(result.observer.stats)}")
    print(f"      recovery events: {[b['reason'] for b in read_bodies(ins, ('recovery',))]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
