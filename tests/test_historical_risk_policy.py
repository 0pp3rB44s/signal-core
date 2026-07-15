from dataclasses import replace

import pytest

from app.config import Settings
from backtesting.execution_contract import BacktestExecutionConfig, BacktestExecutionContract
from candidate_lifecycle import deterministic_candidate_id
from clients.schemas import (
    Candle,
    ContractSpec,
    MarketSnapshot,
    StrategyCandidate,
    StrategyScore,
    SweepDetection,
    TimeframeSnapshot,
)
from risk.historical_policy import (
    HistoricalProxyConfig,
    ResearchRiskMode,
    configured_round_trip_cost_bps,
    evaluate_conservative_proxy,
    historical_candidate,
)
from risk.risk_manager import RiskManager


def _candidate(*, timestamp=1_700_000_000_000, volume_ratio=1.0, range_pct=1.0, volatility=50.0):
    candles = [Candle(timestamp, 100, 101, 99, 100, 1_000)]
    tf = TimeframeSnapshot("BTCUSDT", "15m", 100, 1, range_pct, volume_ratio, 99, 98, "bullish", candles, timestamp, timestamp + 900_000)
    confirmation = replace(tf, granularity="1h")
    contract = ContractSpec("BTCUSDT", "USDT-FUTURES", "USDT", "BTC", "online", .001, .001, 2, None, None, {})
    market = MarketSnapshot(
        "BTCUSDT", contract, tf, confirmation, "aligned_bullish", 90,
        ["orderbook_available=false", "orderbook_risk_off=true", "risk_off_reason=orderbook_context_unavailable", "spread_bps=4", "entry_quality long=90 short=90 close_pos=0.5"],
        volatility_rank=volatility,
    )
    detection = SweepDetection("sell", 99, 98.5, 100, 100, 99, 1, 0, 1, 1, [])
    strategy = "liquidity_sweep_reversal"
    direction = "LONG"
    return StrategyCandidate(
        deterministic_candidate_id(strategy, "BTCUSDT", direction, timestamp), timestamp,
        "BTCUSDT", strategy, direction, "15m", "1h", market, detection, [],
    )


def _score():
    return StrategyScore(100, {}, "GO", [])


def test_production_remains_fail_closed_without_orderbook():
    verdict = RiskManager(Settings(_env_file=None)).evaluate(_candidate(), _score())
    assert not verdict.allowed
    assert verdict.reasons == ["blocked: orderbook risk-off"]


def test_historical_mode_is_explicit_and_not_a_settings_or_string_flag():
    with pytest.raises(TypeError):
        RiskManager(Settings(_env_file=None)).evaluate(_candidate(), _score(), research_mode="HISTORICAL_STRUCTURAL_ONLY")


def test_historical_boundary_removes_only_unavailable_orderbook_markers():
    transformed = historical_candidate(_candidate())
    text = " ".join(transformed.market.notes)
    assert "orderbook_available=false" not in text
    assert "orderbook_risk_off=true" not in text
    assert "spread_bps=4" in text
    assert "historical_orderbook_unavailable=true" in text


def test_structural_mode_retains_intrinsic_alignment_gate():
    candidate = historical_candidate(_candidate())
    candidate.market.alignment = "aligned_bearish"
    verdict = RiskManager(Settings(_env_file=None)).evaluate(
        candidate, _score(), research_mode=ResearchRiskMode.HISTORICAL_STRUCTURAL_ONLY,
    )
    assert not verdict.allowed
    assert any("alignment" in reason.lower() or "opposes" in reason.lower() for reason in verdict.reasons)


def test_proxy_formula_and_frozen_configuration_hash():
    cfg = BacktestExecutionConfig()
    proxy = HistoricalProxyConfig()
    decision = evaluate_conservative_proxy(_candidate(), cfg, proxy)
    assert configured_round_trip_cost_bps(cfg) == 24.0
    assert decision.proxy_values["tp1_reward_bps"] == pytest.approx(80.0)
    assert decision.proxy_values["required_reward_bps"] == 48.0
    assert decision.allowed
    assert proxy.configuration_hash == "722bb6962e575931e5d4b2ee58ce175413729c587f9eed5a796b69930a349cbc"


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"volume_ratio": .49}, "volume"),
        ({"range_pct": 5.01}, "range"),
        ({"volatility": 90.01}, "volatility"),
    ],
)
def test_proxy_frozen_execution_quality_rejections(changes, reason):
    decision = evaluate_conservative_proxy(_candidate(**changes), BacktestExecutionConfig(), HistoricalProxyConfig())
    assert not decision.allowed
    assert any(reason in item for item in decision.reasons)


def test_proxy_uses_only_candidate_time_snapshot_not_future_candles():
    candidate = _candidate()
    before = evaluate_conservative_proxy(candidate, BacktestExecutionConfig(), HistoricalProxyConfig())
    unrelated_future = Candle(candidate.candidate_candle_open_timestamp_ms + 900_000, 100, 1000, 1, 500, 0)
    assert unrelated_future not in candidate.market.primary.candles
    after = evaluate_conservative_proxy(candidate, BacktestExecutionConfig(), HistoricalProxyConfig())
    assert before == after


def test_execution_costs_constraints_and_policy_are_preserved_in_historical_record():
    cfg = BacktestExecutionConfig(maximum_notional=35, spread_bps=4, entry_slippage_bps=2, exit_slippage_bps=2, taker_fee_bps=6)
    record = BacktestExecutionContract(cfg).execute(
        strategy="fixture", symbol="BTCUSDT", timeframe="15m", direction="LONG",
        signal_timestamp=1, requested_entry=100, stop=99, targets=[100.8, 101.5],
        candles=[
            Candle(2, 100, 101, 99, 100, 100),
            Candle(3, 100, 101, 99.5, 100.8, 100),
            Candle(4, 101, 102, 100.5, 101.5, 100),
        ],
        equity=1000, risk_policy=ResearchRiskMode.HISTORICAL_STRUCTURAL_ONLY.value,
    )
    assert record.risk_policy == ResearchRiskMode.HISTORICAL_STRUCTURAL_ONLY.value
    assert record.notional <= 35
    assert record.spread_cost > 0
    assert record.entry_slippage > 0
    assert record.total_fees > 0
