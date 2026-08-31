# Funding pilot independent verification — 2026-08-31

## Scope

Independent verification of deployment artifact commit
`9cbcbecab7cbb41654942883b45ef5f75c9923b0`. This review did not modify the
builder's files, production runtime, exchange state, or AdaptiveTrend.

## Verdict

`FROZEN_SPEC_MATCH=YES`

`STARTING_EQUITY=27.44_USDT`

`DYNAMIC_COMPOUNDING=VERIFIED_IN_ISOLATED_CORE_ONLY`

`MAX_POSITION=10_PERCENT_OF_PILOT_NAV_CURRENTLY_2.744_USDT`

`MAX_GROSS=20_PERCENT_OF_PILOT_NAV`

`MAX_POSITIONS=2`

`LEVERAGE=1X`

`NATIVE_STOP_VERIFIED=SIMULATED_FAKE_EXCHANGE_ONLY_NOT_PRODUCTION`

`RESTART_RECOVERY_VERIFIED=SQLITE_CORE_AND_FAKE_EXCHANGE_ONLY_NOT_PRODUCTION`

`KILL_SWITCH_VERIFIED=SIMULATED_FAKE_EXCHANGE_ONLY_NOT_PRODUCTION`

`TELEMETRY_VERIFIED=SCHEMA_AND_SQLITE_PERSISTENCE_ONLY_NOT_PRODUCTION_CAPTURE`

`STRATEGY_LEDGER_ISOLATED=YES_AT_COMPONENT_LEVEL_NOT_RUNTIME_DEPLOYED`

`ADAPTIVETREND_UNTOUCHED=YES`

`READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`

`FINAL_STATUS=BLOCKED_OPERATIONAL_RISK`

## Evidence and classification

- **PROVEN:** `research/validation/FROZEN_SPECS.json` hashes to the required
  SHA-256 `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.
- **PROVEN:** `PilotConfig` freezes starting equity at 27.44 USDT, position
  sizing at 10% of pilot NAV, gross exposure at 20%, two positions, 1x
  leverage, a 10% native-stop distance, and a 5% drawdown kill threshold.
- **PROVEN:** sizing is dynamic from the isolated pilot ledger's calculated
  NAV. At starting NAV the cap is 2.744 USDT, not approximately 10 USDT.
- **PROVEN:** commit scope adds only `funding_pilot/` and its unit tests;
  AdaptiveTrend is unchanged.
- **PROVEN:** the default and only wired mode has `orders_enabled=False`.
  `process_signal()` returns `NO_ORDER` in that mode and deliberately raises
  `FailClosed("live adapter not independently verified")` if enabled.
- **DEFECT / OPERATIONAL RISK:** no production module imports the package. No
  runner, scheduler, signal producer, Bitget adapter, `RiskManager`, canonical
  `ExecutionService`, or `PositionManager` call-site invokes `PilotRuntime`.
  Therefore the artifact cannot submit or own real orders.
- **INSUFFICIENT SAMPLE:** native stop placement/verification, restart
  reconciliation, live kill flattening, and telemetry were tested against
  `FakeExchange`. These tests establish helper behavior, not production
  exchange wiring or actual execution evidence.
- **OBSERVABILITY GAP:** execution telemetry has a required-field schema and
  SQLite persistence test, but there is no production source populating actual
  fills, fees, funding, spread, slippage, stop execution, equity, or drawdown.

## Tests and checks

- `python3 -m pytest -q tests/test_funding_pilot.py tests/test_position_lifecycle_safety.py tests/test_forward_paper_only_runtime.py` — 13 tests passed.
- `python3 -m py_compile funding_pilot/core.py tests/test_funding_pilot.py` — passed.
- `git diff --check 52cd4a1b298c26389c82b25e62971ec81b93eb5f..9cbcbecab7cbb41654942883b45ef5f75c9923b0` — passed.
- Repository-wide production-call-site search found `funding_pilot` imports
  only in `tests/test_funding_pilot.py`.
- Artifact diff inspection confirmed four added files only: package README,
  package initializer, safety core, and tests.

## Risk impact

No order was placed, canceled, or changed. No exchange or production runtime
was contacted. No leverage, risk, margin, stop, strategy, or AdaptiveTrend
configuration was changed. The verifier added only this report.

## Remaining concerns

Release requires a separately reviewed adapter and real production wiring
through the canonical risk/execution/position-ownership path, including
exchange-truth reconciliation, native-stop acknowledgement, restart recovery,
kill-switch flatten verification, and actual execution telemetry. Fake-exchange
unit tests cannot satisfy that gate. A subsequent owner launch decision should
only be requested after those paths exist and are independently verified.
