"""MicroFlow must not be re-capped by the flat per-trade notional ceiling.

MicroFlow sizes from the account balance
(`microflow/live.py::size_microflow_position`): usable margin split across the
position slots, bounded by `MICROFLOW_MAX_NOTIONAL_PCT_EQUITY` and refused above
`MICROFLOW_MAX_LOSS_PCT_EQUITY`. The balance precheck in `execution_service`
then applied `EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT` on top, which silently
undid the whole change.

Observed on the Runner on 2026-08-15 at `e32ab70`:

```
EXECUTABLE_TRADE_CAPS            | LINKUSDT | notional=209.29 | leverage=10.00
BALANCE_PRECHECK_NOTIONAL_CAPPED | LINKUSDT | requested=209.29 | capped=35.00
```

Legacy strategies keep the flat cap — it is their only ceiling — so the exemption
is asserted to be strategy-specific rather than removed outright.
"""

from __future__ import annotations

import pytest

from execution.execution_service import is_microflow_scalper_v1


def cap(requested: float, hard_cap: float, flat_cap: float, strategy: str) -> float:
    """The decision under test, mirroring execution_service's balance precheck."""
    if is_microflow_scalper_v1(strategy):
        return min(requested, hard_cap)
    return min(requested, hard_cap, flat_cap)


# The situation that was actually observed on the Runner.
REQUESTED, HARD_CAP, FLAT_CAP = 209.29, 465.09, 35.00


def test_microflow_keeps_its_equity_based_notional():
    assert cap(REQUESTED, HARD_CAP, FLAT_CAP, "microflow_scalper_v1") == pytest.approx(209.29)


def test_the_leverage_hard_cap_still_binds_for_microflow():
    """Exempt from the flat cap is not exempt from every cap."""
    assert cap(1000.0, HARD_CAP, FLAT_CAP, "microflow_scalper_v1") == pytest.approx(HARD_CAP)


@pytest.mark.parametrize("strategy", [
    "momentum_breakout", "low_vol_reclaim_v2", "trend_continuation",
    "liquidity_sweep_reversal", "momentum_breakdown", "", "unknown_strategy",
])
def test_legacy_strategies_still_get_the_flat_cap(strategy):
    assert cap(REQUESTED, HARD_CAP, FLAT_CAP, strategy) == pytest.approx(FLAT_CAP)


def test_a_smaller_request_is_never_inflated():
    assert cap(12.0, HARD_CAP, FLAT_CAP, "microflow_scalper_v1") == pytest.approx(12.0)
    assert cap(12.0, HARD_CAP, FLAT_CAP, "momentum_breakout") == pytest.approx(12.0)


def test_the_exemption_is_wired_into_the_real_precheck():
    """Guards against the branch being removed or the helper being swapped out."""
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1]
              / "execution" / "execution_service.py").read_text()
    marker = "live_notional = min(requested_notional, hard_cap_notional)"
    assert marker in source, "MicroFlow exemption missing from the balance precheck"
    assert "live_notional = min(requested_notional, hard_cap_notional, max_live_notional)" in source, \
        "legacy strategies lost the flat cap"
