"""New-strategy home for Trading Core V1.

Named `strategies_v1` (not `strategies`) deliberately: this repo already has a
top-level `strategies/` package used by the live production engine
(AdaptiveTrend and friends). Reusing that name for Trading Core V1's baseline
strategy would risk an import collision and would blur the line between "code
the live engine imports" and "code that only exists for this isolated
backtesting core." `strategies_v1` lives under `trading_core_v1/` so it is
obviously scoped to this package and cannot be picked up by anything that
still imports the top-level `strategies` package.
"""
