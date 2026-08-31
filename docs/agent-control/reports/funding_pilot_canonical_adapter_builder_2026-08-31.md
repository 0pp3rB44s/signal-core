# Funding Pilot Canonical Adapter — Builder Report (2026-08-31)

## Summary

Implemented an additive `STOP_ONLY_TIME_EXIT` lifecycle for the frozen
`funding_crowding_continuation_24h` pilot through the canonical
`RiskManager -> ExecutionService -> PositionManager` ownership path. Existing
strategies retain the prior mandatory SL+TP behavior. No exchange was contacted
and no real order was sent.

Builder conclusion: **READY FOR INDEPENDENT VERIFICATION**, not release approval.

## Evidence classification

- **PROVEN (fixture/sandbox canonical path):** RiskManager enforces the frozen
  hash, explicit authorization, 1x leverage, dynamic 10%/20% NAV limits, two
  positions, free margin, native-stop availability, and latched kill switch.
- **PROVEN (fixture/sandbox canonical path):** ExecutionService permits absent
  TP only for the exact hash-bound mode, routes entry through the normal entry
  submitter, verifies an exchange-native catastrophic stop, persists its ID and
  refuses unverified protection through the existing fail-safe close chain.
- **PROVEN (fixture/sandbox canonical path):** a restarted PositionManager loads
  the persisted time exit, closes through `close_futures_position_full`, cancels
  stop plans, verifies no residual stop, and leaves AdaptiveTrend rows untouched.
- **PROVEN:** frozen spec SHA256 remains
  `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.
- **UNKNOWN:** real Bitget acknowledgement, fill, stop latency, cancellation and
  residual exposure. No real orders were authorized for verification.

## Files changed

- `clients/schemas.py`
- `risk/risk_manager.py`
- `execution/portfolio_selector.py`
- `execution/execution_service.py`
- `execution/position_manager.py`
- `tests/test_entry_path_audit.py`
- `tests/test_funding_pilot_canonical.py`
- this report

## Risk impact

Capital risk is reduced relative to the prior blocked adapter: TP absence is no
longer worked around, while the exceptional mode is constrained by strategy ID,
frozen hash, owner authorization, dynamic NAV state, a verified native stop and
a scheduled exit. Default schema values fail closed for pilot admission. Other
strategies still require TP protection and continue through the pre-existing
TP/SL/BE lifecycle.

## Tests/checks run

- `python3 -m py_compile` on changed production modules
- canonical funding-pilot entry/risk/restart/time-exit tests
- funding pilot safety-core tests
- entry-path, position lifecycle, position migration and portfolio-selection
  regressions
- `git diff --check`
- SHA256 verification of `research/validation/FROZEN_SPECS.json`

## Remaining concerns

- Independent verification is mandatory; the builder cannot approve release.
- The live exchange edge remains deliberately untested. Exchange behavior must
  be treated as UNKNOWN until the separately authorized capped pilot runs.
- The control plane must be updated only by the Master Orchestrator after the
  independent verdict.
