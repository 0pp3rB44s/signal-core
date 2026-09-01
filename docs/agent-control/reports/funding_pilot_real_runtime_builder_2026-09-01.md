# Funding Pilot Real Runtime — Builder Report (2026-09-01)

## Summary

Built a schedulable `CanonicalFundingPilotRunner`, concrete
`BitgetPilotExchangePort`, persistent isolated ledger wiring, deterministic
`cgc-fcp-` entry ownership, exchange-history economics ingestion, restart state
repair, and production `app.runner` construction behind
`FUNDING_PILOT_RUNTIME_ENABLED`. The runtime hardcodes `armed_live=False` at its
production construction site. No real order was sent.

Builder verdict: **READY FOR INDEPENDENT AUDIT**, not self-approved release.

## Files changed

- `app/config.py`
- `app/runner.py`
- `execution/entry_submitter.py`
- `funding_pilot/bitget_exchange.py`
- `funding_pilot/runner.py`
- `funding_pilot/canonical.py`
- `tests/test_funding_pilot_real_runtime.py`
- `tests/test_entry_path_audit.py`
- this report

## Risk impact

- Production construction is opt-in and always disarmed. Adapter mutation
  methods independently reject with `REAL_ORDER_ARMED=FALSE`.
- Pilot entry client OIDs use the deterministic `cgc-fcp-` namespace.
- Positions are pilot-owned only when matched to the durable ownership map;
  working orders require the namespace; stops require namespace or the exact
  persisted stop ID. Foreign AdaptiveTrend rows are excluded from pilot truth.
- Actual closed-position PnL, open/close fees and funding are ingested from
  exchange history with persistent deduplication. Missing economic fields fail
  closed rather than becoming zero.
- Post-ack stop mismatch invokes an immediate owned close and verifies no pilot
  position or working order remains.
- Restart reconciliation repairs missing/partial PositionManager rows from
  exchange truth and halts on missing schedules or stops.

## Tests/checks run

- Production module compilation passed.
- 198 selected tests ran in the broad suite; 197 passed and one test-fixture
  setup failed because it had omitted PositionManager's store. After correcting
  that fixture, 59 focused tests passed, including the failed integration test.
- Real concrete adapter/runner tests place mocked transport beneath the adapter,
  covering read truth, ownership isolation, actual economics ingestion,
  disarmed writes, scheduler dry-run and owned close behavior.
- Frozen spec SHA256 remained
  `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.
- `git diff --check` passed.

## Read-only Bitget rehearsal

An authenticated read-only rehearsal was attempted with `_env_file=None` and
without reading or exposing any credential file. Result:
`CREDENTIALS_AVAILABLE=False`. No network write was attempted. Consequently,
real-account read verification is **BLOCKED BY MISSING CREDENTIALS**; the real
adapter was instead exercised through mocked transport below its API boundary.

## Remaining concerns

- Independent verification is mandatory.
- Authenticated real Bitget account/position/order reads remain externally
  blocked in this isolated environment.
- Real fills and exchange timing remain UNKNOWN until the owner separately arms
  and launches the capped pilot.
