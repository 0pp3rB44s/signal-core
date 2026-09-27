from __future__ import annotations

import argparse
import json
from pathlib import Path

from candidate_lifecycle.reconstruct import reconstruct_candidate_lifecycles


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--funnel", type=Path, default=Path("data_store/funnel_events.jsonl"))
    parser.add_argument("--forward-events", type=Path, default=Path("data_store/forward_paper_events.jsonl"))
    parser.add_argument("--outcomes", type=Path, default=Path("data_store/forward_paper_outcomes.csv"))
    parser.add_argument("--output", type=Path, default=Path("reports/candidate_lifecycle.json"))
    args = parser.parse_args()
    report = reconstruct_candidate_lifecycles(args.funnel, args.forward_events, args.outcomes)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"candidate_lifecycle_hash={report['reconstruction_hash']}")


if __name__ == "__main__":
    main()
