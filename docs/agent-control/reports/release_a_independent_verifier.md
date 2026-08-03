# Release A Independent Verifier — Fresh Verification

## Verdict

- `SAFE_TO_REVIEW: yes`
- `SAFE_TO_DEPLOY_TECHNICALLY: yes`
- Base: `817bc72f5b7b85f12b23fd6d7b03b09542addaed`
- Verified tip: `c03c51786c59703504282e2320cf38cf45454f06`
- Branch: `codex/release-a-close-economics-recovery`
- Exact merge-base: `817bc72f5b7b85f12b23fd6d7b03b09542addaed`

Both blockers from the prior independent pass are closed. Release A is technically safe for owner code review and, from the code/wiring/test perspective, technically deployable. This is not authorization to merge, migrate data, deploy, or send exchange orders; those actions remain explicitly owner-gated.

## Prior blockers reverified

### M2 / unprotected UNKNOWN flatness — FIXED / PROVEN / WIRED

`execution/tp_sl_lifecycle.py::_close_unprotected_position()` now uses the shared `exchange_confirmed_flat()` predicate. It requires status `CLOSED` or `NO_POSITION`, literal `flatness=FLAT`, and `remaining_size=0.0`.

Evidence reaches the real PositionManager runtime path: `test_unprotected_close_unknown_flatness_keeps_local_position_open` drives `PositionManager.sync()` through failed protection repair with a malformed `{status:CLOSED, flatness:UNKNOWN, remaining_size:None}` result and proves local status remains OPEN. The narrower M2 test also proves the close helper returns false.

The earlier CAPITAL RISK defect is closed.

### M7 / emergency-flatten production caller — FIXED / PROVEN / WIRED

`scripts/emergency_flatten.py` is the explicit owner-operated production entrypoint. With `--confirm-emergency-flatten`, it constructs `PositionManager` and calls `PositionManager.emergency_flatten_all()`, which consumes every transport lifecycle identity, writes provisional records and invokes shared reconciliation for confirmed-flat outcomes.

The M7 test starts at the CLI `main()` function and proves the lifecycle-aware orchestrator is called and recording outcomes are surfaced. AST call-site audit now finds:

- `scripts/emergency_flatten.py:37` -> `PositionManager.emergency_flatten_all()`
- `execution/position_manager.py:68` -> transport `client.emergency_flatten_all()`

Direct operator behavior was also verified:

- `python3 scripts/emergency_flatten.py --help` succeeds from repo root.
- Default invocation without the confirmation flag exits `2` with `--confirm-emergency-flatten is required; no exchange action was taken` before Settings or PositionManager construction.
- No confirmed invocation was run during verification.

The later bootstrap commit also makes `scripts/audit_provisional_close_migration.py --help` directly runnable from repo root.

## Required Release A gates

### Scope, ancestry and drift

- PROVEN: exact declared base and merge-base.
- Final diff: 31 files, 3,529 insertions and 247 deletions; this includes the independent verifier report already committed earlier on the branch.
- PROVEN: no `.env`, application config, risk, strategy or planning file changed.
- PROVEN: no leverage, sizing, margin, max-position, TP/SL distance, protection parameter or entry-routing setting drift was found.
- PROVEN: no sensitive-path match or private-key marker appears in the diff.

### Close-path production wiring

- PROVEN / WIRED: dead-trade timeout -> confirmed full-close -> provisional row -> shared recorder.
- PROVEN / WIRED: residual cleanup -> confirmed full-close -> provisional row -> shared recorder.
- PROVEN / WIRED: TP3 close-all -> confirmed full-close -> provisional row -> shared recorder.
- PROVEN / WIRED: primary entry fail-safe -> `close_futures_position_full()` -> shared fail-safe recorder.
- PROVEN / WIRED: protection-repair/unprotected emergency -> strict typed-flatness helper -> provisional row -> shared recorder.
- PROVEN / WIRED: owner-confirmed emergency-flatten CLI -> PositionManager orchestrator -> transport identities -> provisional/economic recorder outcomes.

### Typed FLAT / REMAINS / UNKNOWN semantics

- PROVEN: transport errors, malformed lists/rows, invalid or ambiguous sizes remain UNKNOWN with no zero fabrication.
- PROVEN: accepted close acknowledgement is followed by a fresh readback before local close or protection cleanup.
- PROVEN: REMAINS/UNKNOWN retains protection and does not produce economic closure.
- PROVEN: all reviewed local-close gates now use the strict typed-flatness contract.

### Startup and periodic recovery

- PROVEN / WIRED: `StartupRunner._execute_selected_plans()` is the sole production call to `ExecutionService.execute()`.
- PROVEN: startup recovery precedes that call and unknown/blocked recovery prevents execution.
- PROVEN / WIRED: `PositionManager.sync()` performs periodic recovery.
- PROVEN: active plus every numeric rotated dataset is loaded; already-resolved rows are filtered before the limit; unresolved rows are sorted oldest-first; the batch is bounded.
- PROVEN: history UNKNOWN blocks the sweep instead of inventing economics.

### Economics and deduplication

- PROVEN: typed economics preserves explicit gross PnL, open fee, close fee, funding and literal exchange `netProfit`.
- PROVEN: required identity and money fields must be present and finite.
- PROVEN: Decimal formula `gross - abs(openFee) - abs(closeFee) + funding == netProfit` is checked with `0.0000001` tolerance and mismatch fails closed.
- PROVEN: the writer uses literal net profit and does not subtract fees twice.
- PROVEN: lifecycle matching uses exchange position ID first; fallback requires symbol, side, open time and size; ambiguity fails closed.
- PROVEN: dedup covers position ID, lifecycle ID, order ID and open-time/size composite across active and all numeric rotations.

### Migration

- PROVEN: `scripts/audit_provisional_close_migration.py` defaults to read-only.
- PROVEN: local appends require explicit `--apply`; the default-read-only test proves byte-for-byte unchanged input.
- PROVEN: direct `--help` works after repo-root bootstrap.
- Owner approval, backup and reviewed audit output remain required before any `--apply` use.

### M1–M15 credibility

All M1–M15 scenarios now bind to production boundaries or call-sites. M2 reaches real PositionManager sync state mutation; M7 reaches the real operator CLI main. The matrix is credible regression/mutation evidence. It is still not an executed third-party mutation-testing framework, so claims should remain limited to the named mutations.

## Commands and exact results

1. `git status --short --branch` — clean at start of fresh verification.
2. `git rev-parse HEAD` — `c03c51786c59703504282e2320cf38cf45454f06`.
3. `git merge-base HEAD 817bc72...` — exact base.
4. Focused Release A suite on the immediately preceding implementation tip — `129 passed in 88.58s` (the final tip adds only script import bootstrap).
5. Final-tip blocker selection — `4 passed in 0.13s`.
6. `python3 -m pytest -q` at final tip — `1032 passed, 1 skipped in 112.92s`.
7. `python3 scripts/emergency_flatten.py --help` — exit 0; confirmation flag documented.
8. `python3 scripts/audit_provisional_close_migration.py --help` — exit 0; default and `--apply` documented.
9. `python3 scripts/emergency_flatten.py` without confirmation — exit 2; no exchange action path reached.
10. `python3 -m compileall -q app clients execution telemetry scripts tests` — passed.
11. `git diff --check 817bc72...HEAD` — passed.
12. AST call-site audit — two emergency calls (CLI -> PositionManager; PositionManager -> client) and exactly one `execution_service.execute` call (`app/runner.py:379`).
13. Config/risk/strategy/planning/sensitive-path audit — no matching changed paths.
14. Private-key marker audit — `private_key_marker_found=no`.

No LIVE checkout, exchange call, order, cancellation, protection, process, remote, deployment, secret, implementation file, test, control plane or git history was modified by this fresh verification. Only this report is updated and committed as authorized.

## Non-blocking release boundaries

- The frozen 25-trade economic sample from the broader program remains an ECONOMIC EDGE DEFECT and does not justify strategy/risk expansion. Release A changes close safety/economic recording, not strategy parameters.
- Historical strategy backtesting remains an independent gate for strategy deployment decisions; it does not invalidate this close-integrity release's technical wiring.
- Emergency flatten is intentionally destructive and can only be owner-triggered with its explicit confirmation flag.
- Migration `--apply`, merge and deployment remain owner-authorized operations.

## Rollback

- Before merge: leave Release A unmerged.
- After merge but before deployment: revert the Release A implementation commits through normal reviewed history.
- Before any migration apply: preserve a dataset backup and reviewed dry-run totals. If apply results are rejected, restore the local dataset backup; do not infer or rewrite exchange truth.
- No push, merge, rebase, deploy or migration apply was performed.

## Final disposition

- `SAFE_TO_REVIEW: yes`
- `SAFE_TO_DEPLOY_TECHNICALLY: yes`
- `OWNER_AUTHORIZATION_TO_DEPLOY: not granted by this verification`
