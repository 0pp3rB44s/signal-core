"""CLI for the read-only Strategy Funnel Analyzer."""

from __future__ import annotations

import argparse
from pathlib import Path

from analysis.strategy_funnel import SourcePaths, StrategyFunnelAnalyzer


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build dataset-separated strategy funnel reports")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--candidate-csv", type=Path, default=None)
    parser.add_argument("--output-json", type=Path, default=Path("reports/strategy_funnel_report.json"))
    parser.add_argument("--output-csv", type=Path, default=Path("reports/strategy_funnel.csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    paths = SourcePaths(candidate_csv=args.candidate_csv)
    analyzer = StrategyFunnelAnalyzer(root, paths)
    report = analyzer.analyze()
    output_json = args.output_json if args.output_json.is_absolute() else root / args.output_json
    output_csv = args.output_csv if args.output_csv.is_absolute() else root / args.output_csv
    analyzer.write_json(report, output_json)
    analyzer.write_csv(report, output_csv)
    print(f"strategy_funnel_json={output_json}")
    print(f"strategy_funnel_csv={output_csv}")
    print(f"strategies={len(report['strategies'])}")
    print(f"quality_issues={len(report['data_quality']['issues'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
