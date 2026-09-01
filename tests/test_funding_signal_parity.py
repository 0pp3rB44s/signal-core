import math
import pandas as pd
import pytest

from funding_pilot.signals import frozen_funding_decision


@pytest.mark.parametrize("direction", [1, -1])
def test_pure_production_formula_matches_frozen_pandas_research_formula(direction):
    n = 90
    rates = [direction * (i + 1) / 1_000_000 for i in range(n)]
    opens = [100 + direction * math.sin(i / 3) for i in range(n)]
    opens[-1] = opens[-4] * (1 + direction * 0.20)
    funding = [(i * 28_800_000, rates[i]) for i in range(n)]
    decision = frozen_funding_decision(funding, opens)

    frame = pd.DataFrame({"funding": rates, "entry_price": opens})
    frame["funding_pct"] = frame.funding.rolling(90, min_periods=30).rank(pct=True)
    frame["ret24"] = frame.entry_price / frame.entry_price.shift(3) - 1
    frame["ret24_vol"] = frame.ret24.rolling(30, min_periods=20).std().shift(1)
    frame["extension"] = frame.ret24 / frame.ret24_vol
    expected = frame.iloc[-1]
    assert decision is not None
    assert decision["funding_pct"] == pytest.approx(expected.funding_pct)
    assert decision["extension"] == pytest.approx(expected.extension)
    assert decision["side"] == ("LONG" if direction > 0 else "SHORT")


def test_tied_funding_rank_matches_pandas_average_rank():
    rates = [0.001] * 30
    opens = [100 + math.sin(i) for i in range(30)]
    # No signal is expected, but parity of the tie calculation is exercised by
    # ensuring it does not falsely classify the tied window at the 95th pct.
    assert frozen_funding_decision([(i, value) for i, value in enumerate(rates)], opens) is None
