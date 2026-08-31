# AgentC capital-growth research release — 2026-08-31

## Summary

The owner-authorized AgentC Phase B research gate is open in a dedicated
worktree on `research/capital-growth-v3`. The requested SHA `1fc9c11` was
verified but rejected as the research base because it predates the documented
Runner baseline and omits the later public-data collectors. The clean deployed
SHA `39e9221` is a descendant and contains those collectors, so it is the
verified research base.

## Files changed

- `docs/agent-control/CONTROL_PLANE.md`
- `docs/agent-control/reports/agentc_research_release_2026-08-31.md`

## Risk impact

Research-only authorization was recorded. Production modification and real
order authority remain prohibited. AdaptiveTrend, the dirty owner checkout,
LIVE runtime, risk settings, capital, and order state were not changed.

## Tests/checks run

- Verified both base objects as immutable Git commits.
- Verified `1fc9c11` is an ancestor of `39e9221`.
- Verified the selected base contains all three Binance research collectors.
- Created and checked the isolated branch/worktree at exactly `39e9221`.
- Searched the process table for the three collector entrypoints without
  stopping, restarting, or modifying any process; no matching process was
  observed.
- Verified the dirty owner checkout retained its pre-existing status.

## Remaining concerns

Collector ownership and runtime state are `UNKNOWN`; absence of an entrypoint
name in the process table does not prove that all observation infrastructure is
stopped. Research must remain public-data-only and must not access credentials
or private logs to resolve that uncertainty.
