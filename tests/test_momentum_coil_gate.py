"""Coil-na-expansie: probe i.p.v. hard block; chases blijven hard geblokkeerd.

Forward-return studie 2026-07-07 (12 symbolen, 331 entries): coil-na-expansie
was de enige netto-positieve bucket (+0.198R, 61.5% TP1); post-breakout chases
na expansie de slechtste (25.5% TP1). De gate moet dat onderscheid maken.
"""

from unittest.mock import MagicMock

from risk.risk_manager import RiskManager


def _candidate(notes: list[str], strategy: str = "momentum_breakout") -> MagicMock:
    c = MagicMock()
    c.strategy = strategy
    c.symbol = "TESTUSDT"
    c.direction = "LONG"
    c.notes = notes
    return c


def _rm() -> RiskManager:
    return RiskManager(settings=MagicMock())


BASE_NOTES = [
    "prearmed_breakout",
    "volume_ratio=1.30",
    "breakout_pct=0.00",
    "bars_since_breakout=0",
    "prearmed_pressure_score=60.00",
    "prearmed_expansion_prob=75.0",
    "breakout_context_ready=True",
]


def test_coil_after_expansion_probes_instead_of_blocking():
    notes = BASE_NOTES + [
        "entry_model=pre_breakout_coil",
        "coil_distance_pct=0.1200",
        "expansion_exhaustion_score=90.00",
        "move already expanded",
    ]
    allowed, reasons, probe = _rm()._momentum_quality_gate(_candidate(notes))
    assert allowed, f"coil hoort niet hard geblokkeerd te worden: {reasons}"
    assert probe, "coil-na-expansie moet op probe-size"
    assert any("coil after expansion" in r for r in reasons)


def test_chase_after_expansion_stays_hard_blocked():
    notes = BASE_NOTES + [
        "expansion_exhaustion_score=90.00",
        "move already expanded",
    ]
    allowed, reasons, probe = _rm()._momentum_quality_gate(_candidate(notes))
    assert not allowed, "post-breakout chase na expansie moet geblokkeerd blijven"
    assert any("exhaustion/expanded" in r for r in reasons)


def test_coil_without_exhaustion_is_normal_pass():
    notes = [n for n in BASE_NOTES if not n.startswith("volume_ratio=")] + [
        "volume_ratio=1.70",  # boven de prearmed-eis (1.60) -> geen volume-probe
        "entry_model=pre_breakout_coil",
        "coil_distance_pct=0.0800",
        "expansion_exhaustion_score=40.00",
    ]
    allowed, reasons, probe = _rm()._momentum_quality_gate(_candidate(notes))
    assert allowed
    assert not probe  # geen exhaustion, geen volume-tekort -> volle size
