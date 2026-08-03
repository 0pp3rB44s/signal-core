# Release A Independent Verifier

## Verdict

- `SAFE_TO_REVIEW: no`
- `SAFE_TO_DEPLOY_TECHNICALLY: no`
- Base: `817bc72f5b7b85f12b23fd6d7b03b09542addaed`
- Verified tip: `dba04d9fbd02e9d25557e2d554d24807a195af1c`
- Branch: `codex/release-a-close-economics-recovery`
- Merge-base: `817bc72f5b7b85f12b23fd6d7b03b09542addaed` (exact)

Release A is not ready for owner review or technical deployment. Two production-wiring defects remain despite a green full suite: the unprotected-emergency helper can accept a typed UNKNOWN response as closed, and the identity-consuming emergency-flatten orchestrator has no production caller. Consequently M2 and M7 do not credibly guard the claimed production behavior.

## Blocking findings

### 1. CAPITAL RISK / DEFECT — unprotected emergency accepts `status=CLOSED` without typed flatness proof

`execution/tp_sl_lifecycle.py::_close_unprotected_position()` accepts any result whose status is `CLOSED` or `NO_POSITION`. It does not require `flatness == FLAT` and `remaining_size == 0.0` through `exchange_confirmed_flat()`.

The caller then marks the local position `CLOSED`, sets remaining size to zero and proceeds with provisional/economic reconciliation. The recorder later rejects malformed flatness, but that is too late: local lifecycle state has already forgotten a potentially live, unprotected position.

Independent reproduction against the real helper:

`{'status':'CLOSED','flatness':'UNKNOWN','remaining_size':None}` produced `malformed_closed_accepted=True`.

Required fix: make `_close_unprotected_position()` use the same strict typed flatness predicate as every recorder/orchestrator, and add a real call-path negative test proving UNKNOWN/bare CLOSED cannot mutate local state to closed.

### 2. PIPELINE INCOMPLETE / OPERATIONAL RISK — emergency-flatten recorder has no production caller

`PositionManager.emergency_flatten_all()` correctly consumes each transport identity and reconciles confirmed-flat outcomes. However, AST and repository call-site audit found only one non-test `emergency_flatten_all()` call: the method's internal call to `self.client.emergency_flatten_all()` at `execution/position_manager.py:68`. No app, command, dashboard, operator or safety path calls `PositionManager.emergency_flatten_all()`.

Therefore the identity-consuming recorder path is implemented but not WIRED. A production caller invoking `BitgetRestClient.emergency_flatten_all()` directly would close positions without the promised provisional/economic lifecycle bookkeeping.

M7 is mislabeled: `test_m7_real_emergency_production_caller_consumes_each_identity` directly invokes `manager.emergency_flatten_all()` in a unit harness. It proves method internals, not a real production caller.

Required fix: route the actual authorized emergency-flatten entrypoint through `PositionManager.emergency_flatten_all()` and add a call-site test starting from that production entrypoint. Do not add or exercise any LIVE/order mutation during testing.

## Verified runtime wiring

- PROVEN / WIRED: dead-trade timeout writes a provisional row and invokes `reconcile_closed_lifecycle()` after full-close success.
- PROVEN / WIRED: residual cleanup writes provisional economics and invokes the shared recorder after its full-close gate.
- PROVEN / WIRED: TP3 close-all invokes the shared recorder after confirmed full close.
- PROVEN / WIRED: primary entry fail-safe uses `close_futures_position_full()` and the shared fail-safe recorder.
- PROVEN / WIRED: protection-repair/unprotected emergency invokes the close helper, provisional writer and shared recorder, but its local-state flatness gate is defective as described above.
- PIPELINE INCOMPLETE: emergency-flatten recorder exists but has no upstream production caller.

## Typed flatness contract

- PROVEN: `PositionReadbackState` distinguishes `FLAT`, `REMAINS` and `UNKNOWN`; transport errors and malformed rows remain UNKNOWN with `size=None`.
- PROVEN: `close_futures_position_full()` performs a post-close readback before cleanup, retains protection for UNKNOWN/REMAINS, and only emits CLOSED after FLAT.
- PROVEN: `exchange_confirmed_flat()` requires status CLOSED/NO_POSITION, literal `flatness=FLAT`, and `remaining_size=0.0`.
- DEFECT: `_close_unprotected_position()` bypasses that predicate and checks status alone.

## Startup and periodic recovery

- PROVEN / WIRED: `StartupRunner._execute_selected_plans()` is the sole production call to `ExecutionService.execute()` and gates it behind `_ensure_startup_close_recovery()`.
- PROVEN: startup history/recovery failure blocks execution rather than assuming success.
- PROVEN / WIRED: `PositionManager.sync()` performs periodic recovery.
- PROVEN: recovery loads the active dataset and all numeric rotations, removes already-resolved economics before applying the limit, sorts unresolved rows oldest-first, and processes a bounded batch.
- PROVEN: a single history fetch is reused for the batch; unknown history marks the sweep blocked.

## Economics, deduplication and migration

- PROVEN: `ExchangeCloseEconomics` carries explicit gross PnL, open fee, close fee, funding and literal `net_profit`/`netProfit` semantics.
- PROVEN: required identity/money fields must be finite and present; UNKNOWN is not defaulted to zero.
- PROVEN: `gross - abs(openFee) - abs(closeFee) + funding == netProfit` is checked with Decimal tolerance `0.0000001`; mismatch fails closed.
- PROVEN: the real writer preserves literal exchange net profit and does not subtract fees twice.
- PROVEN: lifecycle matching prefers exchange `positionId`; fallback requires symbol, side, open time within tolerance and size within tolerance; equal-distance ambiguity fails closed.
- PROVEN: dedup checks exchange position ID, lifecycle ID, order ID, and open-time/size composite across the active dataset and every numeric rotation.
- PROVEN: duplicate/ambiguous composite economics are blocked.
- PROVEN: `scripts/audit_provisional_close_migration.py` defaults to read-only; writes require explicit `--apply`; its test confirms byte-for-byte unchanged input without apply.
- OPERATIONAL RISK: `--apply` is a local append operation and remains owner-controlled; it was not executed during verification.

## Scope and drift

- PROVEN: base ancestry is exact; the branch is four commits ahead of the declared base.
- Diff scope: 29 files, 3,304 insertions and 247 deletions.
- PROVEN: no `.env`, secret, credential, token or private-key path is changed; no private-key marker appears in the diff.
- PROVEN: no `app/config.py`, risk, strategy or planning file is changed.
- PROVEN: no leverage, sizing, margin, max-position, TP/SL distance, protection parameter or entry-routing setting drift was found.
- The changes are confined to close/readback transport, close lifecycle/economics, recovery/migration, runtime gating, documentation and tests.

## M1–M15 credibility

- Credible production-boundary evidence: M1, M3, M4, M5, M6, M8, M9, M10, M11, M12, M13, M14 and M15.
- M2 is incomplete: it tests the shared recorder predicate but misses `_close_unprotected_position()`'s status-only production gate.
- M7 is insufficient: it directly calls the new orchestrator method and does not prove any production entrypoint invokes it.
- Classification: mutation matrix overall is SUGGESTIVE, not PROVEN, until M2 and M7 are corrected. These are regression-style mutation scenarios rather than an executed mutation-testing framework.

## Commands and results

1. `git status --short --branch` — clean branch before report creation.
2. `git rev-parse HEAD` — `dba04d9fbd02e9d25557e2d554d24807a195af1c`.
3. `git merge-base HEAD 817bc72...` — exact declared base.
4. `git diff --stat 817bc72...HEAD` — 29 files, 3,304 insertions, 247 deletions.
5. Repository `rg` and Python AST call-site audits — one non-test emergency-flatten call, internal to the uncalled PositionManager orchestrator.
6. Focused Release A pytest command covering mutation, reconciliation, recorder, PositionManager, fail-safe, provisional money and lifecycle safety tests — `127 passed in 88.64s`.
7. `python3 -m pytest -q` — `1030 passed, 1 skipped in 113.87s`.
8. `python3 -m compileall -q app clients execution telemetry scripts tests` — passed.
9. `git diff --check 817bc72...HEAD` — passed.
10. Config/risk/strategy/planning and sensitive-path diff audit — no matching changed paths.
11. Private-key marker scan of the diff — `private_key_marker_found=no`.
12. Read-only malformed-flatness reproduction — `malformed_closed_accepted=True`.

No LIVE checkout, process, exchange order, protection, remote, deployment, secret, implementation file, test, control plane or git history was modified by this verification.

## Rollback

- Before merge: leave Release A unmerged.
- After merge but before deployment: revert the four Release A commits through normal reviewed history.
- Dataset migration is separately owner-triggered; do not run `--apply` until audit output, backup and rollback handling are approved.
- No push, merge, rebase, deployment or branch cleanup is authorized or performed.

## Required disposition

Return Release A to the builder. Fix both blockers, add real negative/call-site evidence, and request a fresh independent verification. Until then:

- `SAFE_TO_REVIEW: no`
- `SAFE_TO_DEPLOY_TECHNICALLY: no`
