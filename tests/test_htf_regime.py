"""HTF regime-laag: 1D/4H classificatie + risk gate gedrag."""

from unittest.mock import MagicMock

from market_data.htf_regime import classify_htf_regime, classify_trend, htf_opposition
from risk.risk_manager import RiskManager


def _trend_candles(start: float, step: float, n: int = 40) -> list[dict]:
    return [{"close": start + i * step} for i in range(n)]


def test_classify_trend_bullish_bearish_neutral():
    assert classify_trend(_trend_candles(100, 0.5)) == "bullish"
    assert classify_trend(_trend_candles(100, -0.5)) == "bearish"
    assert classify_trend([{"close": 100.0} for _ in range(40)]) == "neutral"
    assert classify_trend([]) == "neutral"  # geen data -> neutraal, geen crash


def test_classify_htf_regime_consensus_and_conflict():
    up = _trend_candles(100, 0.5)
    down = _trend_candles(100, -0.5)
    flat = [{"close": 100.0} for _ in range(40)]
    assert classify_htf_regime(up, up)["htf_regime"] == "bullish"
    assert classify_htf_regime(down, down)["htf_regime"] == "bearish"
    assert classify_htf_regime(up, down)["htf_regime"] == "conflicted"
    assert classify_htf_regime(up, flat)["htf_regime"] == "lean_bullish"


def test_htf_opposition_symmetry():
    regime = {"regime_1d": "bearish", "regime_4h": "bearish"}
    n_long, hits = htf_opposition("LONG", regime)
    assert n_long == 2
    n_short, _ = htf_opposition("SHORT", regime)
    assert n_short == 0


def _gate_probe_and_block(direction: str, notes: list[str]) -> tuple[bool, bool, list[str]]:
    """Draai evaluate() ver genoeg om de HTF-uitkomst te zien via reasons."""
    rm = RiskManager(settings=MagicMock(
        account_risk_per_trade_pct=0.5, default_leverage=3, max_leverage=3,
        max_open_positions=4, session_risk_reduction_windows_utc="", session_risk_multiplier=1.0,
    ))
    rm._latest_backtest_summary = lambda: {}
    rm._latest_strategy_expectancy = lambda: {}
    rm._latest_agent_decisions = lambda: {}
    rm._daily_defensive_status = lambda: {"report_readable": True}
    rm._weekly_realized_pnl = lambda: 0.0

    candidate = MagicMock()
    candidate.strategy = "momentum_breakout"
    candidate.symbol = "TESTUSDT"
    candidate.direction = direction
    candidate.notes = notes
    candidate.market.notes = []
    candidate.market.alignment = "mixed"
    candidate.market.primary.volume_ratio_20 = 2.0
    candidate.detection.bars_since_sweep = 0

    score = MagicMock(); score.total = 90.0; score.verdict = "GO"; score.reasons = []
    verdict = rm.evaluate(candidate, score)
    blocked_by_htf = any("HTF regime blocks" in r for r in verdict.reasons)
    probed_by_htf = any("HTF regime PROBE" in r for r in verdict.reasons)
    return blocked_by_htf, probed_by_htf, verdict.reasons


def test_gate_blocks_when_both_htf_oppose():
    blocked, probed, _ = _gate_probe_and_block("LONG", [
        "htf_regime_1d=bearish", "htf_regime_4h=bearish", "volume_ratio=2.0",
    ])
    assert blocked and not probed


def test_gate_probes_when_one_htf_opposes():
    blocked, probed, _ = _gate_probe_and_block("LONG", [
        "htf_regime_1d=bearish", "htf_regime_4h=neutral", "volume_ratio=2.0",
    ])
    assert not blocked and probed


def test_gate_neutral_without_htf_notes():
    blocked, probed, _ = _gate_probe_and_block("SHORT", ["volume_ratio=2.0"])
    assert not blocked and not probed
