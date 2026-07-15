from scripts.phase2c_shadow_analysis import performance


def test_gate_value_performance_is_deterministic_and_uses_only_closed_fills():
    rows = [
        {"fill_status": "FILLED", "final_exit_reason": "FINAL_TARGET", "net_pnl": "2"},
        {"fill_status": "FILLED", "final_exit_reason": "STOP_LOSS", "net_pnl": "-1"},
        {"fill_status": "UNFILLED", "final_exit_reason": "", "net_pnl": "99"},
    ]
    assert performance(rows) == {
        "trades": 2, "net_pnl": 1.0, "profit_factor": 2.0,
        "expectancy": .5, "max_drawdown": 1.0,
    }
