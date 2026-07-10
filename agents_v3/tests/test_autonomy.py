from __future__ import annotations

from pathlib import Path

from agents_v3.core.agent_loop import _parse_step, _trim_transcript, MAX_TRANSCRIPT_CHARS
from agents_v3.core.tools_registry import execute_tool, tool_read_file, tool_list_files
from agents_v3.improvement.improvement_planner import build_performance_items
from agents_v3.safety.safety_guard import files_requiring_human_approval
from agents_v3.tools.trade_analyzer import analyze_trades, format_report


def _write_dataset(tmp_path: Path, rows: list[str]) -> Path:
    header = "event_type,timestamp,symbol,direction,strategy,status,result,opened_at,closed_at,net_pnl,fees"
    csv_path = tmp_path / "trades.csv"
    csv_path.write_text("\n".join([header, *rows]) + "\n")
    return csv_path


def test_trade_analyzer_groups_and_fees(tmp_path):
    rows = [
        "CLOSE,2099-01-01T10:00:00+00:00,BTCUSDT,LONG,alpha,CLOSED,x,2099-01-01T08:00:00+00:00,2099-01-01T10:00:00+00:00,1.0,0.1",
        "CLOSE,2099-01-01T11:00:00+00:00,ETHUSDT,SHORT,beta,CLOSED,x,2099-01-01T10:50:00+00:00,2099-01-01T11:00:00+00:00,-0.5,0.1",
        "OPEN,2099-01-01T11:00:00+00:00,ETHUSDT,SHORT,beta,OPEN,x,,,0,0",
    ]
    csv_path = _write_dataset(tmp_path, rows)
    analysis = analyze_trades(csv_path=csv_path, days=100000)

    assert analysis.trades == 2
    assert analysis.wins == 1
    assert abs(analysis.net_pnl - 0.5) < 1e-9
    assert abs(analysis.fees - 0.2) < 1e-9
    assert abs(analysis.gross_edge_before_fees - 0.7) < 1e-9
    assert analysis.by_strategy["alpha"].wins == 1
    assert analysis.by_direction["SHORT"].net_pnl < 0
    assert analysis.by_duration[">=1h"].trades == 1
    assert analysis.by_duration["<1h"].trades == 1
    assert "Trade performance" in format_report(analysis)


def test_tools_registry_blocks_env_and_unknown():
    assert "forbidden" in tool_read_file(".env").lower()
    assert "forbidden" in tool_read_file("configs/../.env").lower()
    assert "unknown tool" in execute_tool("rm_rf", {})
    assert "ERROR" in execute_tool("read_file", {"bogus_arg": 1})


def test_tools_registry_reads_repo_files():
    output = tool_read_file("agents_v3/README.md", start_line=1, max_lines=5)
    assert "CGC Agent V3" in output
    listing = tool_list_files("agents_v3")
    assert "core/" in listing


def test_agent_loop_parse_and_trim():
    assert _parse_step('{"action": "final", "result": {}}')["action"] == "final"
    assert _parse_step("not json") is None
    assert _parse_step('```json\n{"action": "tool"}\n```')["action"] == "tool"

    head = "HEADER"
    huge = ["x" * 9000 for _ in range(5)]
    trimmed = _trim_transcript([head, *huge])
    assert trimmed[0] == head
    assert sum(len(p) for p in trimmed) <= MAX_TRANSCRIPT_CHARS + len(head)


def test_human_approval_path_policy():
    gated = files_requiring_human_approval([
        "strategies/strategies/low_vol_reclaim.py",
        "planning/trade_planner.py",
        "risk/risk_manager.py",
        "execution/position_manager.py",
        "app/config.py",
        "docs/TODO.md",
    ])
    assert gated == ["risk/risk_manager.py", "execution/position_manager.py", "app/config.py"]


def test_performance_items_flag_bleeding_strategy(monkeypatch, tmp_path):
    rows = []
    # 40 losing trades for strategy 'leaky', 10 winners for 'solid'
    for i in range(40):
        rows.append(
            f"CLOSE,2099-01-01T10:{i % 60:02d}:00+00:00,AUSDT,SHORT,leaky,CLOSED,x,2099-01-01T09:00:00+00:00,2099-01-01T09:30:00+00:00,-0.1,0.05"
        )
    for i in range(10):
        rows.append(
            f"CLOSE,2099-01-01T11:{i % 60:02d}:00+00:00,BUSDT,LONG,solid,CLOSED,x,2099-01-01T08:00:00+00:00,2099-01-01T10:00:00+00:00,0.3,0.05"
        )
    csv_path = _write_dataset(tmp_path, rows)

    import agents_v3.improvement.improvement_planner as planner
    monkeypatch.setattr(
        planner, "analyze_trades",
        lambda days=14: analyze_trades(csv_path=csv_path, days=100000),
    )

    items = build_performance_items()
    titles = " | ".join(item.title for item in items)
    assert "leaky" in titles
    assert "SHORT" in titles
    assert "churn" in titles.lower()
