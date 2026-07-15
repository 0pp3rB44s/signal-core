"""Single-TP contract: het geplaatste target moet de bereikbare TP1 zijn.

Live 2026-07-07: niet-reclaim plannen gingen met single TP = tp2 (1.5-1.7R)
naar de exchange terwijl de markt mediaan 0.5-1.0R gaf — target vrijwel nooit
gevuld, elke omkeer rood. Dit test pint vast dat single-TP = tp1 voor alle
strategieën, zodat de TP-engine-geometrie ook echt op de exchange belandt.
"""

from unittest.mock import MagicMock

from planning.trade_planner import TradePlanner
from candidate_lifecycle import deterministic_candidate_id


def _settings() -> MagicMock:
    s = MagicMock()
    s.planner_stop_buffer_bps = 8.0
    s.planner_ladder_steps = 3
    s.planner_min_rr = 1.2
    s.planner_min_rr_to_tp1 = 1.0
    s.planner_estimated_roundtrip_fee_bps = 12.0
    s.planner_minimum_net_edge_buffer_bps = 4.0
    s.planner_largest_loss_guard_bps = 85.0
    s.planner_min_live_notional_usdt = 10.0
    s.planner_max_notional_pct_of_equity = 35.0
    s.planner_max_notional_per_trade_usdt = 35.0
    s.account_equity_usdt = 100.0
    s.planner_adaptive_fallback_min_rr_to_tp1 = 0.70
    s.planner_strong_continuation_min_rr_to_tp1 = 0.75
    return s


def _candidate(strategy: str = "momentum_breakdown", direction: str = "SHORT") -> MagicMock:
    c = MagicMock()
    c.symbol = "DOGEUSDT"
    c.strategy = strategy
    c.direction = direction
    c.candidate_candle_open_timestamp_ms = 1_700_000_000_000
    c.candidate_id = deterministic_candidate_id(strategy, c.symbol, direction, c.candidate_candle_open_timestamp_ms)
    c.notes = []
    c.detection.entry_hint = 100.0
    c.detection.invalidation = 100.6 if direction == "SHORT" else 99.4
    c.detection.reclaim_level = 100.0
    c.detection.sweep_extreme = 100.3 if direction == "SHORT" else 99.7
    c.detection.reason_flags = []
    c.market.alignment = "aligned_bearish" if direction == "SHORT" else "aligned_bullish"
    c.market.volatility_rank = 10.0
    c.market.notes = []
    c.market.primary.atr_percent = 0.5
    c.market.primary.volume_ratio_20 = 1.2
    c.market.primary.trend = "bearish" if direction == "SHORT" else "bullish"
    return c


def _score(total: float = 80.0) -> MagicMock:
    score = MagicMock()
    score.total = total
    score.verdict = "GO"
    score.reasons = []
    return score


def _risk() -> MagicMock:
    risk = MagicMock()
    risk.allowed = True
    risk.status = "GO"
    risk.reasons = []
    risk.account_risk_pct = 0.5
    risk.leverage = 3.0
    return risk


def test_single_tp_is_the_reachable_tp1_for_momentum():
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_candidate("momentum_breakdown", "SHORT"), _score(), _risk())

    assert len(plan.take_profits) == 1
    entry = plan.entry_prices[0]
    risk_distance = abs(entry - plan.stop_loss)
    tp_distance = abs(plan.take_profits[0] - entry)
    rr_of_placed_tp = tp_distance / risk_distance
    # tp1 wordt door de engine op >= 1.05R gebouwd; tp2 zit op >= 1.6R.
    # Het geplaatste target moet tp1 zijn, dus ruim onder de oude 1.5-1.7R.
    assert 0.9 <= rr_of_placed_tp <= 1.35, f"single TP staat op {rr_of_placed_tp:.2f}R — dat is tp2, niet tp1"
    assert any("single_tp_source=tp1" in str(n) for n in plan.notes)


def test_single_tp_is_tp1_for_reclaim():
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_candidate("low_vol_reclaim", "LONG"), _score(), _risk())
    assert len(plan.take_profits) == 1
    assert any("single_tp_source=tp1_reclaim_profile" in str(n) for n in plan.notes)
