"""Trading Core V1 -- a small, self-contained, deterministic backtesting core.

This package is strictly additive and isolated: it does not import from, modify,
or interact with the live production engine (`app/`, `execution/`, `risk/`, the
existing `strategies/` package, dashboards, or the watchdog). It exists to answer
one question: does a strategy have positive expectancy after realistic trading
costs? It deliberately does not try to find a profitable strategy.

See README_TRADING_CORE_V1.md for usage.
"""

__version__ = "1.0.0"
