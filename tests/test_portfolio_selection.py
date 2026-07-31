from __future__ import annotations

from candidate_lifecycle import deterministic_candidate_id, deterministic_plan_id
from clients.schemas import TradePlan
from execution.portfolio_selector import select_execution_winner
from tests.test_entry_path_audit import _service


def _plan(symbol: str, score: float = 80.0, *, candle_ms: int = 1_700_000_000_000) -> TradePlan:
    strategy = "low_vol_reclaim"
    candidate_id = deterministic_candidate_id(strategy, symbol, "LONG", candle_ms)
    return TradePlan(
        candidate_id=candidate_id,
        candidate_candle_open_timestamp_ms=candle_ms,
        plan_id=deterministic_plan_id(candidate_id),
        symbol=symbol,
        strategy=strategy,
        direction="LONG",
        verdict="EXECUTABLE",
        score=score,
        entry_prices=[100.0],
        stop_loss=95.0,
        take_profits=[110.0],
        risk_reward_ratio=2.0,
        account_risk_pct=1.0,
        leverage=5.0,
        position_notional_usdt=50.0,
        notes=[],
        reasons=[],
        geometry_entry=100.0,
    )


def test_sol_wins_when_btc_and_sol_are_executable_but_sol_scores_higher():
    btc, sol = _plan("BTCUSDT"), _plan("SOLUSDT")
    selected = select_execution_winner(
        [btc, sol],
        execution_scores={"BTCUSDT": 81.0, "SOLUSDT": 88.0},
    )
    assert selected.winner is sol


def test_input_order_never_changes_the_winner():
    plans = [_plan("BTCUSDT", 80), _plan("SOLUSDT", 90), _plan("ETHUSDT", 85)]
    assert select_execution_winner(plans).winner.symbol == "SOLUSDT"
    assert select_execution_winner(reversed(plans)).winner.symbol == "SOLUSDT"


def test_exact_tie_uses_alphabetical_symbol_as_final_market_tiebreak():
    sol, btc = _plan("SOLUSDT"), _plan("BTCUSDT")
    assert select_execution_winner([sol, btc]).winner is btc


def test_directional_expectancy_then_setup_quality_then_spread_break_ties():
    btc, sol = _plan("BTCUSDT"), _plan("SOLUSDT")
    btc.reasons = ["symbol expectancy source=live (BTCUSDT LONG, n=8, exp=0.10)"]
    sol.reasons = ["symbol expectancy source=live (SOLUSDT LONG, n=8, exp=0.20)"]
    assert select_execution_winner([btc, sol]).winner is sol

    btc.reasons = sol.reasons = []
    btc.notes = ["planner_entry_quality=82", "spread_bps_for_edge=3"]
    sol.notes = ["planner_entry_quality=85", "spread_bps_for_edge=8"]
    assert select_execution_winner([btc, sol]).winner is sol

    btc.notes = ["planner_entry_quality=85", "spread_bps_for_edge=3"]
    sol.notes = ["planner_entry_quality=85", "spread_bps_for_edge=8"]
    assert select_execution_winner([sol, btc]).winner is btc


def test_invalid_high_score_is_filtered_before_selection():
    invalid, valid = _plan("SOLUSDT", 99), _plan("BTCUSDT", 80)
    invalid.stop_loss = 0.0
    selection = select_execution_winner([invalid, valid])
    assert selection.winner is valid
    assert [(row.symbol, row.reason) for row in selection.rejected] == [
        ("SOLUSDT", "stop_loss_invalid")
    ]


def test_non_allowlisted_plan_cannot_win_even_with_highest_score():
    btc, rogue = _plan("BTCUSDT", 80), _plan("DOGEUSDT", 99)
    selection = select_execution_winner(
        [rogue, btc],
        allowed_symbols={"BTCUSDT", "SOLUSDT"},
    )
    assert selection.winner is btc
    assert selection.rejected[0].reason == "symbol_not_in_canonical_allowlist"


def test_execution_service_creates_state_only_for_the_single_winner(monkeypatch):
    service = _service(monkeypatch)
    btc, sol, eth = _plan("BTCUSDT", 80), _plan("SOLUSDT", 95), _plan("ETHUSDT", 90)
    service.client.get_all_positions.side_effect = [
        {"data": []},
        {"data": []},
        {
            "data": [{
                "symbol": "SOLUSDT",
                "holdSide": "long",
                "total": "0.5",
                "openPriceAvg": "100.0",
                "markPrice": "100.0",
            }]
        },
    ]

    reports = service.execute([btc, sol, eth])

    assert [report.symbol for report in reports] == ["SOLUSDT"]
    assert service.client.place_futures_market_order.call_count == 1
    positions = service.store.load(default=[])
    assert [position["symbol"] for position in positions] == ["SOLUSDT"]
    assert positions[0]["position_lifecycle_id"]
    assert service.intent_store.get(service.entry_submitter.client_oid_for(btc)) is None
    assert service.intent_store.get(service.entry_submitter.client_oid_for(eth)) is None


def test_open_exchange_position_blocks_winner_without_falling_through(monkeypatch):
    service = _service(monkeypatch)
    sol, btc = _plan("SOLUSDT", 95), _plan("BTCUSDT", 80)
    open_position = {
        "symbol": "ETHUSDT",
        "holdSide": "long",
        "total": "0.5",
        "openPriceAvg": "100.0",
    }
    service.client.get_all_positions.side_effect = [
        {"data": [open_position]},
        {"data": [open_position]},
    ]

    reports = service.execute([btc, sol])

    assert [report.symbol for report in reports] == ["SOLUSDT"]
    assert reports[0].status == "SKIPPED"
    assert "max open positions" in reports[0].message
    service.client.place_futures_market_order.assert_not_called()
    assert service.intent_store.all() == []
