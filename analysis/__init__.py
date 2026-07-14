"""Read-only repository analysis tools.

The package deliberately has no imports from the bot runtime.  This keeps an
analysis invocation from constructing clients, loading Settings, or changing
runtime state.
"""

from .strategy_funnel import ACTIVE_STRATEGIES, StrategyFunnelAnalyzer

__all__ = ["ACTIVE_STRATEGIES", "StrategyFunnelAnalyzer"]
