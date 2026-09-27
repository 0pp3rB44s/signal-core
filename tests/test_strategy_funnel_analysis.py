from __future__ import annotations

import ast
import csv
import json
from pathlib import Path

from analysis.strategy_funnel import ACTIVE_STRATEGIES, SourcePaths, StrategyFunnelAnalyzer
from analysis.validate_strategy_funnel import validate_report


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _json(path: Path, value) -> None:
    _write(path, json.dumps(value))


def _fixture_root(tmp_path: Path) -> Path:
    strategies = {
        strategy: {
            "candidates": 10,
            "candidates_go": 7,
            "plans": 6,
            "plans_executable": 2,
            "executions_executed": 0,
            "executions_skipped": 0,
        }
        for strategy in ACTIVE_STRATEGIES[:-1]
    }
    _json(tmp_path / "reports/backtests/strategy_funnel.json", {"strategies": strategies})
    _json(tmp_path / "data_store/trades/trade_funnel_report.json", {
        "plan_rejects": {
            "PLAN_REJECT | BTCUSDT | strategy=momentum_breakout | direction=LONG | reasons=blocked: orderbook risk-off | score 60 below 72": 3,
        }
    })
    _json(tmp_path / "data_store/decisions/latest_decisions.json", [
        {
            "timestamp": "2026-01-01T00:00:00+00:00", "symbol": "BTCUSDT",
            "strategy": "momentum_breakout", "direction": "LONG", "verdict": "EXECUTABLE",
            "score": "80", "decision_snapshot": "planner_risk_status=EXECUTABLE",
        },
        {
            "timestamp": "2026-01-01T00:00:00+00:00", "symbol": "ignored",
            "strategy": "BTCUSDT", "direction": "momentum_breakout", "verdict": "LONG",
            "score": "80", "decision_snapshot": "shifted",
        },
    ])
    _json(tmp_path / "data_store/trades/latest_real_closed_trades.json", [
        {
            "strategy": "momentum_breakout", "symbol": "BTCUSDT", "direction": "LONG",
            "opened_at": "a", "closed_at": "b", "net_pnl": 1.0, "exchange_order_id": "one",
        },
        {
            "strategy": "momentum_breakout", "symbol": "ETHUSDT", "direction": "LONG",
            "opened_at": "c", "closed_at": "d", "net_pnl": -1.0, "exchange_order_id": "two",
        },
        {
            "strategy": "recovered_exchange_position", "symbol": "X", "direction": "LONG",
            "opened_at": "e", "closed_at": "f", "net_pnl": 0.0, "exchange_order_id": "three",
        },
    ])
    events = [
        {
            "event_id": "e1", "trade_id": "t1", "plan_id": "p1", "event_type": "TRADE_OPENED",
            "timestamp": "2026-01-01T00:00:00+00:00",
            "payload": {"strategy": "momentum_breakout"},
        },
        {
            "event_id": "e2", "trade_id": "t1", "plan_id": "p1", "event_type": "TRADE_CLOSED",
            "timestamp": "2026-01-01T01:00:00+00:00", "payload": {},
        },
    ]
    _write(
        tmp_path / "data_store/forward_paper_events.jsonl",
        "".join(json.dumps(event) + "\n" for event in events),
    )
    outcome_path = tmp_path / "data_store/forward_paper_outcomes.csv"
    outcome_path.parent.mkdir(parents=True, exist_ok=True)
    with outcome_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["dataset", "trade_id", "strategy", "result_r", "final_exit_reason"]
        )
        writer.writeheader()
        writer.writerow({
            "dataset": "forward_paper", "trade_id": "t1", "strategy": "momentum_breakout",
            "result_r": "1.2", "final_exit_reason": "TP1",
        })
    _json(tmp_path / "reports/forward_paper_data_quality.json", {
        "event_chain_valid": True, "duplicate_event_ids": 0, "incomplete_trades": [],
        "outcome_dataset_hash": "hash",
    })
    return tmp_path


def test_analyzer_never_imports_runtime_modules() -> None:
    source = (Path(__file__).parents[1] / "analysis/strategy_funnel.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported.isdisjoint({
        "app", "clients", "execution", "forward_paper", "market_data", "planning", "risk", "strategies"
    })


def test_dataset_views_stay_separate_and_unknown_stages_are_null(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    report = StrategyFunnelAnalyzer(root).analyze()
    breakout = next(item for item in report["strategies"] if item["name"] == "momentum_breakout")
    assert breakout["metrics"]["detected"] == 10
    assert breakout["metrics"]["selector_pass"] is None
    assert breakout["metrics"]["risk_pass"] is None
    assert breakout["metrics"]["planner_fail"] == 4
    assert breakout["metrics"]["forward_open"] == 1
    assert breakout["metrics"]["forward_closed"] == 1
    assert breakout["metrics"]["exchange_trades"] == 2
    assert breakout["metrics"]["wins"] == 1
    assert breakout["metrics"]["losses"] == 1
    assert breakout["not_a_single_cohort"] is True
    assert {row["dataset_scope"] for row in report["dataset_views"]} == {
        "backtest_funnel_undated", "forward_paper_current", "exchange_internal_attribution"
    }


def test_rejects_are_counted_by_strategy_symbol_and_normalized_reason(tmp_path: Path) -> None:
    report = StrategyFunnelAnalyzer(_fixture_root(tmp_path)).analyze()
    rejects = [item for item in report["reject_analysis"] if item["strategy"] == "momentum_breakout"]
    assert {item["reason"]: item["count"] for item in rejects} == {
        "blocked: orderbook risk-off": 3,
        "score <N> below <N>": 3,
    }
    assert all(item["symbols"] == {"BTCUSDT": 3} for item in rejects)
    assert all(item["sessions"] == ["UNKNOWN"] for item in rejects)
    assert all(item["timeframes"] == ["UNKNOWN"] for item in rejects)
    assert all(item["classification"] == "BLOCKING_OR_ADVERSE" for item in rejects)


def test_semantically_shifted_decisions_are_excluded_and_reported(tmp_path: Path) -> None:
    report = StrategyFunnelAnalyzer(_fixture_root(tmp_path)).analyze()
    quality = report["data_quality"]["decision_snapshots"]
    assert quality == {"rows": 2, "valid_rows": 1, "shifted_rows": 1, "invalid_rows": 0}
    issue = next(item for item in report["data_quality"]["issues"] if item["code"] == "SEMANTICALLY_SHIFTED_DECISION_ROWS")
    assert issue["count"] == 1
    assert issue["action"] == "excluded from funnel counts"


def test_forward_lifecycle_and_dataset_mixing_fail_closed(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    outcome_path = root / "data_store/forward_paper_outcomes.csv"
    with outcome_path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["live", "bad", "momentum_breakout", "2.0", "TP1"])
    report = StrategyFunnelAnalyzer(root).analyze()
    breakout = next(
        row for row in report["dataset_views"]
        if row["dataset_scope"] == "forward_paper_current" and row["strategy"] == "momentum_breakout"
    )
    assert breakout["forward_closed"] == 1
    assert any(item["code"] == "FORWARD_DATASET_MIXING" for item in report["data_quality"]["issues"])


def test_overlap_uses_exact_timestamp_symbol_direction_and_is_deterministic(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    candidate = root / "candidates.csv"
    with candidate.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "direction", "strategy"])
        writer.writeheader()
        writer.writerows([
            {"timestamp": "t", "symbol": "BTCUSDT", "direction": "LONG", "strategy": "momentum_breakout"},
            {"timestamp": "t", "symbol": "BTCUSDT", "direction": "LONG", "strategy": "trend_continuation"},
            {"timestamp": "t", "symbol": "BTCUSDT", "direction": "SHORT", "strategy": "momentum_breakdown"},
        ])
    analyzer = StrategyFunnelAnalyzer(root, SourcePaths(candidate_csv=candidate))
    first = analyzer.analyze()
    second = analyzer.analyze()
    pair = next(
        item for item in first["overlap_analysis"]["pairs"]
        if item["strategy_a"] == "momentum_breakout" and item["strategy_b"] == "trend_continuation"
    )
    assert pair["same_candle_count"] == 1
    assert first["analysis_hash"] == second["analysis_hash"]


def test_outputs_have_required_columns_and_valid_json(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    analyzer = StrategyFunnelAnalyzer(root)
    report = analyzer.analyze()
    json_path = root / "output/report.json"
    csv_path = root / "output/report.csv"
    analyzer.write_json(report, json_path)
    analyzer.write_csv(report, csv_path)
    assert json.loads(json_path.read_text())["analysis_hash"] == report["analysis_hash"]
    rows = list(csv.DictReader(csv_path.open()))
    assert len(rows) == len(ACTIVE_STRATEGIES) * 3
    assert {row["strategy"] for row in rows} == set(ACTIVE_STRATEGIES)
    for field in (
        "strategy", "detected", "selector_pass", "selector_fail", "score_pass", "score_fail",
        "risk_pass", "risk_fail", "planner_pass", "planner_fail", "executable", "forward_open",
        "forward_closed", "exchange_trades", "wins", "losses", "be", "reject_reasons",
    ):
        assert field in rows[0]
    assert validate_report(json_path, csv_path) == []


def test_malformed_utf8_jsonl_fails_closed(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path)
    (root / "data_store/forward_paper_events.jsonl").write_bytes(b"\xff\xfe\n")
    report = StrategyFunnelAnalyzer(root).analyze()
    assert any(
        issue["code"] == "INVALID_JSONL_ENCODING"
        for issue in report["data_quality"]["issues"]
    )
    assert all(
        row["forward_open"] == 0
        for row in report["dataset_views"]
        if row["dataset_scope"] == "forward_paper_current"
    )


def test_validator_rejects_report_with_no_read_sources(tmp_path: Path) -> None:
    report = StrategyFunnelAnalyzer(tmp_path).analyze()
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"
    StrategyFunnelAnalyzer.write_json(report, json_path)
    StrategyFunnelAnalyzer.write_csv(report, csv_path)
    assert "no analyzer source was successfully read" in validate_report(json_path, csv_path)
