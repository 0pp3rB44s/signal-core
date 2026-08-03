# 72-hour strict forward-paper validation configuration.
# Sourced by validation_72h/supervise.sh so every supervisor restart inherits
# an identical configuration. Safety-critical values (FORWARD_PAPER_ONLY,
# EXECUTION_ENABLED, blank credentials) are enforced by
# scripts/start_forward_paper.sh and are deliberately NOT set here.

# One symbol, one timeframe, one position. LTCUSDT chosen on evidence: it had the
# highest executable-plan frequency (18 over 7 days = 2.57/day) of any symbol.
export WATCHLIST=LTCUSDT
export MAX_SYMBOLS=1
export MAX_OPEN_POSITIONS=1
export ALLOW_AUTO_WATCHLIST_REFRESH=false

# One timeframe pair, matching how the 147 executable plans were produced.
export BITGET_DEFAULT_GRANULARITY=15m
export BITGET_CONFIRMATION_GRANULARITY=1h

# Real strategy, not the smoke harness. low_vol_reclaim produced 114 of the 147
# executable plans, so it is the only one with enough frequency for a 72h window.
export FORWARD_PAPER_SMOKE_STRATEGY_ENABLED=false

# Deterministic exits, fixed bounded risk, no compounding, no leverage escalation.
export TP1_CLOSE_PCT=100
export TP2_CLOSE_PCT=0
export TP3_CLOSE_PCT=0
export FAST_LANE_ENABLED=false
export DASHBOARD_ENABLED=false

# Scan cadence for the run.
export SCAN_INTERVAL=60
