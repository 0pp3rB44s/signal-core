#!/usr/bin/env python3
from __future__ import annotations

from forward_paper.store import ForwardPaperEventStore, ForwardPaperReconstructor
from telemetry.safe_io import locked_open


def main() -> None:
    store = ForwardPaperEventStore("data_store/forward_paper_events.jsonl")
    with locked_open(store.path, "a", encoding="utf-8"):
        pass
    outcomes, quality = ForwardPaperReconstructor(store).reconstruct()
    print(f"forward-paper outcomes={len(outcomes)} events={quality['event_count']}")


if __name__ == "__main__":
    main()
