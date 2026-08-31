# Funding-crowding live-pilot authorization — 2026-08-31

## Summary

Recorded the owner's conditional real-order authority for only the frozen 24h
funding-crowding continuation pilot. Release is blocked: exact pilot capital
and hard position ceiling are unspecified; no isolated production-wired pilot
implementation exists; exchange-native protection, kill-switch behavior,
exchange-truth logging, and independent release verification are unproven.

## Files changed

- `docs/agent-control/CONTROL_PLANE.md`
- `docs/agent-control/reports/live_pilot_authorization_2026-08-31.md`

## Risk impact

Authorization is recorded without deployment. No production file, runtime,
position, order, stop, risk setting, capital allocation, or AdaptiveTrend path
changed. Real orders sent: zero.

## Tests/checks run

- Verified frozen spec SHA-256 exactly matches the owner-authorized hash.
- Verified the research branch/worktree is physically separate.
- Searched tracked production/research wiring for this strategy and found no
  live-pilot implementation.
- Confirmed repository launch governance requires owner authorization state and
  independent safety gates; no launch action was invoked.

## Remaining concerns

Exact `PILOT_CAPITAL`, an exact maximum notional, protected execution wiring,
5% exchange-truth drawdown enforcement, reconciliation/telemetry, independent
verification, and a final artifact-specific owner launch decision remain open.
