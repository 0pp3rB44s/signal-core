"""Single-TP contract: het geplaatste target moet de bereikbare TP1 zijn.

Live 2026-07-07: niet-reclaim plannen gingen met single TP = tp2 (1.5-1.7R)
naar de exchange terwijl de markt mediaan 0.5-1.0R gaf — target vrijwel nooit
gevuld, elke omkeer rood. Dit test pint vast dat single-TP = tp1 voor alle
strategieën, zodat de TP-engine-geometrie ook echt op de exchange belandt.
"""

from unittest.mock import MagicMock

from planning.trade_planner import TradePlanner


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
    s.sweep_tp1_atr_mult = 1.2
    return s


def _candidate(strategy: str = "momentum_breakdown", direction: str = "SHORT") -> MagicMock:
    c = MagicMock()
    c.symbol = "DOGEUSDT"
    c.strategy = strategy
    c.direction = direction
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


# --- Momentum ATR stop-geometrie (2026-07-11) ---

def _atr_settings():
    from types import SimpleNamespace
    return SimpleNamespace(
        planner_stop_buffer_bps=8.0,
        momentum_atr_geometry_enabled=True,
        momentum_stop_atr_mult=1.0,
        momentum_stop_min_bps=15.0,
        momentum_stop_max_bps=80.0,
    )


def _atr_candidate(strategy, direction, entry, invalidation, atr_pct):
    from types import SimpleNamespace
    detection = SimpleNamespace(invalidation=invalidation, entry_hint=entry, reason_flags=[])
    primary = SimpleNamespace(latest_close=entry, atr_percent=atr_pct)
    market = SimpleNamespace(primary=primary, notes=[])
    return SimpleNamespace(strategy=strategy, direction=direction, detection=detection, market=market, notes=[])


def test_momentum_wide_structural_stop_capped_to_atr():
    # structural stop 0.60% wide, ATR 0.40% -> stop tightened to ~0.40% (40bps).
    planner = TradePlanner(settings=_atr_settings())
    c = _atr_candidate("momentum_breakout", "LONG", entry=100.0, invalidation=99.40, atr_pct=0.40)
    stop = planner._build_stop(c)
    dist_bps = (100.0 - stop) / 100.0 * 10000
    assert 39 <= dist_bps <= 41, f"stop {dist_bps:.1f}bps, verwacht ~40bps (ATR-cap)"


def test_momentum_already_tight_structural_stop_not_widened():
    # structural stop 0.20% (tighter than 0.40% ATR) must be kept (never widened).
    planner = TradePlanner(settings=_atr_settings())
    c = _atr_candidate("momentum_breakout", "LONG", entry=100.0, invalidation=99.80, atr_pct=0.40)
    stop = planner._build_stop(c)
    dist_bps = (100.0 - stop) / 100.0 * 10000
    assert dist_bps < 30, f"strakke structurele stop mag niet verbreed worden (kreeg {dist_bps:.1f}bps)"


def test_momentum_atr_cap_disabled_keeps_structural():
    s = _atr_settings(); s.momentum_atr_geometry_enabled = False
    planner = TradePlanner(settings=s)
    c = _atr_candidate("momentum_breakout", "LONG", entry=100.0, invalidation=99.40, atr_pct=0.40)
    stop = planner._build_stop(c)
    dist_bps = (100.0 - stop) / 100.0 * 10000
    assert dist_bps > 55, f"met flag uit moet de brede structurele stop blijven (kreeg {dist_bps:.1f}bps)"


def test_momentum_short_stop_capped_to_atr():
    planner = TradePlanner(settings=_atr_settings())
    c = _atr_candidate("momentum_breakdown", "SHORT", entry=100.0, invalidation=100.60, atr_pct=0.40)
    stop = planner._build_stop(c)
    dist_bps = (stop - 100.0) / 100.0 * 10000
    assert 39 <= dist_bps <= 41, f"short stop {dist_bps:.1f}bps, verwacht ~40bps"


def test_reclaim_unaffected_by_momentum_branch():
    # low_vol_reclaim keeps its own ATR clamp (30-85bps), not the momentum one.
    planner = TradePlanner(settings=_atr_settings())
    c = _atr_candidate("low_vol_reclaim", "LONG", entry=100.0, invalidation=99.40, atr_pct=0.40)
    stop = planner._build_stop(c)
    dist_bps = (100.0 - stop) / 100.0 * 10000
    assert dist_bps >= 30, f"reclaim gebruikt eigen clamp (min 30bps), kreeg {dist_bps:.1f}bps"


# --- liquidity_sweep reachability filter (2026-07-11) ---

def _sweep_candidate(direction, invalidation, atr_pct):
    c = _candidate("liquidity_sweep_reversal", direction)
    c.detection.invalidation = invalidation
    c.detection.sweep_extreme = invalidation
    c.market.primary.latest_close = 100.0
    c.market.primary.atr_percent = atr_pct
    return c


def test_sweep_deep_target_blocked_as_unreachable():
    # 1% wick stop with 0.3% ATR -> TP1 (~1R) lands far beyond 1.2xATR -> block.
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_sweep_candidate("LONG", invalidation=99.0, atr_pct=0.3), _score(), _risk())
    assert plan.verdict == "BLOCKED"
    assert any("sweep_target_unreachable" in str(n) for n in plan.notes)


def test_sweep_shallow_target_not_flagged_unreachable():
    # 0.3% wick with 0.5% ATR -> TP1 within 1.2xATR -> reachability guard passes.
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_sweep_candidate("LONG", invalidation=99.70, atr_pct=0.5), _score(), _risk())
    assert not any("sweep_target_unreachable" in str(n) for n in plan.notes)


def test_momentum_unaffected_by_sweep_reachability_guard():
    planner = TradePlanner(settings=_settings())
    plan = planner.build(_candidate("momentum_breakout", "LONG"), _score(), _risk())
    assert not any("sweep_target_unreachable" in str(n) for n in plan.notes)
