from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path


LOG_PATH = Path("logs/bot.out")
REPORT_PATH = Path("reports/momentum_funnel_report.txt")

PATTERN = re.compile(
    r"MOMENTUM_FUNNEL \| (?P<symbol>[A-Z0-9]+USDT) \| "
    r"strategy=(?P<strategy>[^|]+) \| "
    r"direction=(?P<direction>[^|]+) \| "
    r"stage=(?P<stage>[^|]+) \| "
    r"result=(?P<result>PASS|FAIL)"
)


def main() -> int:
    if not LOG_PATH.exists():
        print(f"Missing log file: {LOG_PATH}")
        return 1

    lines = LOG_PATH.read_text(errors="ignore").splitlines()

    total = 0
    by_strategy_stage: dict[str, Counter[str]] = defaultdict(Counter)
    by_strategy_result: dict[str, Counter[str]] = defaultdict(Counter)
    by_symbol: dict[str, Counter[str]] = defaultdict(Counter)
    pass_events = []

    for line in lines:
        match = PATTERN.search(line)
        if not match:
            continue

        total += 1
        symbol = match.group("symbol")
        strategy = match.group("strategy").strip()
        direction = match.group("direction").strip()
        stage = match.group("stage").strip()
        result = match.group("result").strip()

        key = f"{strategy}:{direction}"
        by_strategy_stage[key][stage] += 1
        by_strategy_result[key][result] += 1
        by_symbol[symbol][f"{strategy}:{stage}:{result}"] += 1

        if result == "PASS":
            pass_events.append((symbol, strategy, direction, stage, line))

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)

    out: list[str] = []
    out.append("=== MOMENTUM FUNNEL REPORT ===")
    out.append(f"log_file: {LOG_PATH}")
    out.append(f"total_funnel_events: {total}")
    out.append("")

    out.append("=== BY STRATEGY / RESULT ===")
    for key in sorted(by_strategy_result):
        counts = by_strategy_result[key]
        out.append(f"{key}")
        out.append(f"  PASS: {counts.get('PASS', 0)}")
        out.append(f"  FAIL: {counts.get('FAIL', 0)}")
    out.append("")

    out.append("=== BY STRATEGY / STAGE ===")
    for key in sorted(by_strategy_stage):
        out.append(f"{key}")
        for stage, count in by_strategy_stage[key].most_common():
            out.append(f"  {stage}: {count}")
    out.append("")

    out.append("=== TOP SYMBOLS BY FUNNEL EVENTS ===")
    symbol_totals = Counter({symbol: sum(counter.values()) for symbol, counter in by_symbol.items()})
    for symbol, count in symbol_totals.most_common(25):
        out.append(f"{symbol}: {count}")
        for reason, reason_count in by_symbol[symbol].most_common(5):
            out.append(f"  {reason}: {reason_count}")
    out.append("")

    out.append("=== PASS EVENTS ===")
    if pass_events:
        for symbol, strategy, direction, stage, line in pass_events[-50:]:
            out.append(f"{symbol} | {strategy} | {direction} | {stage}")
            out.append(f"  {line}")
    else:
        out.append("No PASS events found.")

    REPORT_PATH.write_text("\n".join(out) + "\n")

    print(f"Wrote {REPORT_PATH}")
    print("\n".join(out[:80]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
