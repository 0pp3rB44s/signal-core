from unittest.mock import MagicMock, patch

from risk.risk_manager import RiskManager
from strategies.strategies.low_vol_reclaim import LowVolReclaimStrategy


def _candidate(
    *,
    strategy: str = "low_vol_reclaim_v2",
    direction: str = "LONG",
    alignment: str = "mixed",
    primary: str = "bullish",
    confirmation: str = "mixed",
):
    candidate = MagicMock()
    candidate.strategy = strategy
    candidate.symbol = "TESTUSDT"
    candidate.direction = direction
    candidate.notes = []
    candidate.market.notes = []
    candidate.market.alignment = alignment
    candidate.market.primary.trend = primary
    candidate.market.confirmation.trend = confirmation
    return candidate


def _score(verdict: str = "GO"):
    score = MagicMock()
    score.verdict = verdict
    return score


def test_v2_1_allows_mixed_confirmation_when_primary_matches_long():
    with patch("risk.risk_manager.logger.info") as log_info:
        allowed, reasons = RiskManager(settings=MagicMock())._alignment_gate(
            _candidate(), _score()
        )

    assert allowed is True
    assert any("v2.1 mixed confirmation allowed" in reason for reason in reasons)
    template = log_info.call_args.args[0]
    values = log_info.call_args.args[1:]
    rendered = template % values
    assert "old_gate_result=fail" in rendered
    assert "new_gate_result=pass" in rendered
    assert "strategy_version=low_vol_reclaim_v2_1" in rendered


def test_v2_1_allows_symmetric_short_case():
    allowed, _ = RiskManager(settings=MagicMock())._alignment_gate(
        _candidate(direction="SHORT", primary="bearish"), _score()
    )
    assert allowed is True


def test_v2_1_still_blocks_mixed_alignment_when_primary_opposes_direction():
    allowed, reasons = RiskManager(settings=MagicMock())._alignment_gate(
        _candidate(primary="bearish"), _score()
    )
    assert allowed is False
    assert "blocked: market alignment mixed without MTF confirmation" in reasons


def test_legacy_reclaim_does_not_receive_v2_1_exception():
    allowed, reasons = RiskManager(settings=MagicMock())._alignment_gate(
        _candidate(strategy="low_vol_reclaim"), _score()
    )
    assert allowed is False
    assert "blocked: market alignment mixed without MTF confirmation" in reasons


def test_unknown_strategy_fails_closed_on_mixed_alignment():
    allowed, reasons = RiskManager(settings=MagicMock())._alignment_gate(
        _candidate(strategy="unknown_strategy"), _score()
    )
    assert allowed is False
    assert "blocked: market alignment mixed without MTF confirmation" in reasons


def test_v2_1_does_not_override_conflicted_alignment_or_bad_score():
    conflicted, _ = RiskManager(settings=MagicMock())._alignment_gate(
        _candidate(alignment="conflicted"), _score()
    )
    bad_score, reasons = RiskManager(settings=MagicMock())._alignment_gate(
        _candidate(), _score("BLOCKED")
    )
    assert conflicted is False
    assert bad_score is False
    assert "blocked: score verdict blocked" in reasons


def test_historical_strategy_label_and_v2_1_version_are_both_preserved():
    assert LowVolReclaimStrategy.name == "low_vol_reclaim_v2"
    assert LowVolReclaimStrategy.version == "low_vol_reclaim_v2_1"
