# C–K / LIVE multi-symbol integration review

> **SUPERSEDED / REJECTED RELEASE:** this historical review belongs to the
> rejected integration tip `8d538670ffb93b8fd6a6af506580f57b71bf5bdb` and is
> not deployment approval. The clean replacement review is
> `docs/CK_MULTISYMBOL_CLEAN_RELEASE_REVIEW.md`.

Status: **IMPLEMENTATION READY FOR REVIEW**. This is a development-only handoff.
No deployment, restart, order action, supervisor-pin update, LIVE-clone checkout, or
configuration-file change was performed.

## Branch identity

- Worktree: `/Users/bryonprivee/Desktop/bitget_ai_agent/c-k-multisymbol-live`
- Branch: `integration/c-k-multisymbol-live`
- Exact base: `cd8671c09df56b238e2c52727f6f54731ab5fac1`
- Base relation: `git merge-base cd8671c HEAD` equals the exact base above.
- Implementation tip before this review document: `13c0fee`
- Worktree state at validation: clean before report authoring.

## Integration commits

1. `adda9b7` — Add exchange-authoritative position model
2. `7fd61d4` — Migrate live position management to exchange truth
3. `cf22422` — Add position migration reporting and forensic tests
4. `0ccebca` — Preserve LIVE entry guards across position migration
5. `859bda5` — Replace BTC pilot scope with canonical owner allowlist
6. `8035e45` — Select one deterministic portfolio winner per cycle
7. `0803d46` — Refresh exchange truth on protection retries
8. `0141f68` — Make close economics lifecycle authoritative
9. `5f9a885` — Expose exchange-authoritative position state in dashboard v3
10. `9024036` — Prove single-position cross-symbol isolation
11. `046c74e` — Add read-only deployment attestation gates
12. `320d8bf` — Make expectancy integration tests hermetic
13. `13c0fee` — Keep portfolio selection compatible with planner fixtures

## Semantic conflict matrix

| Area | Current LIVE line | C–K line | Integrated result |
|---|---|---|---|
| Position entry contract | Legacy planning aliases still present in consumers | `planned_avg_entry` versus immutable exchange-confirmed entry | C–K contract is authoritative; `avg_entry` and `actual_entry` remain telemetry-only |
| Precision and contract metadata | Newer exchange metadata cache and validation | Earlier execution assumptions | LIVE metadata behavior retained; retries can force a fresh contract lookup |
| Minimum size and notional | Exchange `minTradeNum` / `minTradeUSDT` enforcement | Migration touched execution flow | LIVE enforcement retained without generic minimum fallback |
| Quantity normalization | Decimal, aligned and rounded down | Migration touched order sizing | LIVE down-only normalization retained; exposure cannot be rounded upward |
| Entry retry/idempotency | Persisted clientOid intent, no blind POST retry, restart reconciliation | Migration changes around entry confirmation | Both retained; pending exchange entries now add a second read-only hard block |
| Directional expectancy and shorts | Newer directional live-data gate and shorts invariant | Position migration did not replace the strategy gate | LIVE gate retained; hermetic tests no longer depend on an untracked dataset |
| Heartbeat, DNS and scan resilience | Newer heartbeat, retry and incomplete-cycle visibility | Runner overlap in C–K | LIVE resilience retained around integrated winner selection |
| Supervisor and recovery | Commit pin, authorisation token, power checks and recovery | C–K touched recovered position state | Supervisor behavior retained; recovery creates exchange-derived symbol/side lifecycle state |
| Protection and break-even | Existing exchange placement and reduce-only behavior | Monetary Decimal BE, provenance, lifecycle checks | C–K economics integrated; old stop remains until replacement is verified; every retry refreshes exchange truth |
| Reconciliation | LIVE close/history and stale-state recovery fixes | Exchange-entry and lifecycle authority | Symbol/side/lifecycle matching is fail-closed; planned entry never repairs execution truth |
| Duplicate close prevention | Existing close safeguards | Dataset schema/provenance changes | Dedupe is lifecycle-first, legacy-key fallback only; restart dedupe retained |
| Dashboard | Production is dashboard-v3; v2 is retired | C–K initially migrated v2 | v3 now shows exchange entry, planning separately, provenance, lifecycle, protection and distinct economics |
| Symbol scope | BTC pilot enforced by `MAX_SYMBOLS<=1` and independent env lists | C–K did not remove BTC-only | One mandatory owner-controlled allowlist derives every LIVE symbol consumer |
| Portfolio selection | Candidate reporting sorted, execution effectively received first plan | No cross-symbol winner policy | One deterministic, state-free winner is selected before any execution state |

The only direct file overlaps between the newer LIVE changes and the original C–K
series were `app/runner.py`, `execution/execution_service.py`, and
`tests/test_entry_path_audit.py`. Those were semantically merged and then covered
by full-path tests rather than accepted as blind cherry-picks.

## Preserved LIVE safety behavior

- Exchange-derived precision, minimum size and minimum notional.
- Down-only quantity normalization.
- ClientOid write-ahead intent and ambiguous-order adoption.
- No blind entry retry, and no second entry while intent state is unknown.
- Exchange open-position truth before entry.
- Directional expectancy and explicit short-side enforcement.
- Scan-loop DNS/retry resilience, heartbeat visibility and watchdog behavior.
- Interprocess scan lock and trading-state lock.
- Supervisor authorisation, exact-commit pinning and host power preconditions.
- Existing reduce-only close path and TP/SL fail-safe behavior.
- Duplicate-close and close-history safeguards.

No leverage, risk percentage, notional ceiling, TP distance, entry condition, or
strategy score formula was changed.

## Integrated C–K position model

Every executed position carries:

- `planned_avg_entry`
- `exchange_avg_entry`
- `exchange_avg_entry_source`
- `exchange_avg_entry_confirmed_at`
- `position_lifecycle_id`
- `exchange_entry_order_id`
- `exchange_entry_client_oid`

Critical economics, break-even, protection, PnL, reconciliation, recovery,
dataset and dashboard paths use exchange-confirmed entry/size. If exchange entry
cannot be confirmed for the same symbol, side and lifecycle, protection changes
fail closed and the prior confirmed stop remains intact.

The break-even model uses Decimal monetary inputs: opening fee, expected closing
fee, spread allowance, slippage allowance, extra allowance, size, tick rounding
and current mark legality. Protection retries refetch mark, size, active stops,
entry, lifecycle and contract tick metadata before recomputing the target.

## BTC-only removal and canonical allowlist

The explicit BTC-only constraint removed was:

- `scripts/lib/env_guard.sh`: `MAX_SYMBOLS must be <=1`.

Independent symbol-list behavior was also removed in the following paths:

- `Settings.watchlist_symbols` derives the LIVE scanner list from
  `PRODUCTION_SYMBOL_ALLOWLIST`.
- `Settings.execution_confirm_symbol_set` derives execution confirmation from the
  same source.
- `scripts/lib/env_guard.sh` derives `WATCHLIST`, `EXECUTION_CONFIRM_SYMBOLS`,
  `MAX_SYMBOLS`, and disables auto-refresh in process memory.
- `scripts/launch_live.sh` and `deploy/launchd/live_agent.sh` run that same
  canonicalization and fail-closed validation before a future start/recovery.
- Runner and execution winner selection validate against the canonical set.
- Dashboard exposes the non-secret canonical source; it does not maintain a
  separate market list.

There were no separate hard-coded BTC filters in planner, scanner, runner,
recovery, dashboard, or expectancy code to delete. Inventing removals in those
areas would have weakened scope control.

`PRODUCTION_SYMBOL_ALLOWLIST` is deliberately empty by default. LIVE validation
requires an explicit non-empty value; no broad development list or dynamic
expansion is allowed.

### Owner-confirmation gate

The following is a proposal only and is not activated by source defaults:

`BTCUSDT,SOLUSDT,SUIUSDT,XLMUSDT,AVAXUSDT,DOGEUSDT,WIFUSDT,SEIUSDT,TRXUSDT`

The owner must explicitly confirm the exact ordered list before deployment.
Until then, deployment is blocked.

## Deterministic ranking

All valid executable plans from a full selection cycle are ranked by existing
fields only:

1. Highest existing execution-aware score.
2. Highest existing directional live expectancy.
3. Highest existing planner setup quality.
4. Lowest existing spread (represented as higher liquidity quality).
5. Alphabetical symbol, then direction, strategy and immutable plan ID.

The runner passes only the winner to execution. `ExecutionService` repeats the
state-free one-winner selection as defense in depth. Invalid and non-allowlisted
plans are filtered before ranking. Losers create no intent, lifecycle, position,
cooldown, or execution record. A late legality/protection failure does not fall
through to the runner-up.

## One-position and cross-symbol proof

The hard portfolio invariants are enforced at configuration, shell guard,
selection and execution layers:

- LIVE requires `MAX_OPEN_POSITIONS=1` and `EXECUTION_MAX_PER_CYCLE=1`.
- A pending non-reduce-only exchange entry blocks all new entries.
- An open exchange position blocks the winner with no fallback.
- Unknown/recoverable entry intents are reconciled on restart and fail closed.
- The interprocess scan lock prevents two runner cycles from entering together.
- The trading-state lock serializes execution and reconciliation.
- A latency race test with two overlapping execution calls creates one order and
  one local/exchange position only.

The three-symbol integration test proves:

- A wins; B and C receive no intent or lifecycle.
- After A closes, B can win independently.
- B inherits none of A's fee, retry, protection or expectancy provenance.
- A later A trade receives a new lifecycle and clean protection state.
- Cooldowns remain symbol-scoped.
- Recovery creates independent state for distinct symbol/side pairs.
- Dataset close identity and dedupe are lifecycle-scoped across restart.
- Dashboard local metadata is attached only after symbol, side and exchange-entry
  match; a stale lifecycle is not displayed as current.

## Dataset and dashboard economics

- `exchange_truth_pnl` or canonical `net_pnl` is monetary.
- Percentage-only rows are rejected as a monetary close source.
- Price return and margin ROI have separate fields.
- Exchange exit, confirmed size and exchange fees are preferred where available.
- Lifecycle ID is part of close identity and persisted dataset context.
- Dashboard-v3 never presents `actual_entry` or `avg_entry` as an exchange fill.
- Open-position exchange PnL, price return and margin ROI remain distinct.
- Directional expectancy remains displayed per `(symbol, direction)`.

## Read-only deployment gates

`deployment.exchange_attestation` uses only GET/read interfaces for:

- open positions;
- pending entries;
- active stop-loss and take-profit orders;
- orphan protection orders;
- contract metadata;
- current margin-mode evidence and leverage capability;
- minimum size, minimum notional, tick size and size multiplier.

It cannot place, close, cancel or modify an order and cannot set leverage.
Production readiness in code is distinct from the credentialed result. The
credentialed result has not been run in this task and must return `PASS` later.

`deployment.config_attestation` reads a configuration file only when an
authorised operator invokes it. It returns only allowlisted non-secret values,
redacted secret-key presence, the full-file SHA-256 checksum and exact comparison
results for leverage, risk, notional, portfolio cap and owner-confirmed symbols.
The repository's real configuration files were not read or changed here.

## Validation evidence

- Full suite: `779 passed, 1 skipped`.
- Original C–K migration files plus added retry/entry cases: `78 passed`.
- Core position model and lifecycle: `65 passed`.
- Ranking, multi-symbol, allowlist, dashboard, dataset, entry, scan and
  attestation set: `114 passed`.
- Compilation: `python -m compileall` passed for app, clients, execution, risk,
  telemetry, dashboards and deployment tooling.
- Shell syntax: `bash -n` passed for the changed launch/guard/supervisor scripts.
- Repository hygiene: `repository_hygiene=PASS`.
- No lint configuration is present in this repository; no separate configured
  lint command was available.

## Files changed

Production/application:

- `app/config.py`, `app/runner.py`, `app/symbol_allowlist.py`
- `clients/bitget_account_client.py`, `clients/bitget_precision.py`,
  `clients/bitget_tpsl_client.py`, `clients/schemas.py`
- `execution/closed_trade_writer.py`, `execution/execution_service.py`,
  `execution/portfolio_selector.py`, `execution/position_manager.py`,
  `execution/position_model.py`, `execution/position_reconciler.py`,
  `execution/tp_sl_lifecycle.py`
- `telemetry/trade_logger.py`

Operations/read-only tooling:

- `scripts/lib/env_guard.sh`, `scripts/launch_live.sh`,
  `deploy/launchd/live_agent.sh`
- `deployment/__init__.py`, `deployment/config_attestation.py`,
  `deployment/exchange_attestation.py`

Dashboard/reporting:

- `dashboard_v2/data_provider.py`, `dashboard_v2/static/dashboard.js`,
  `dashboard_v2/templates/Components`
- `dashboard_v3/app.py`, `dashboard_v3/core/sources.py`,
  `dashboard_v3/panels/exchange.py`, `dashboard_v3/panels/history.py`,
  `dashboard_v3/templates/performance.html`,
  `dashboard_v3/templates/positions.html`
- `docs/POSITION_MODEL_MIGRATION_CK.md`, this review document

Tests/fixtures:

- `tests/fixtures/position_model_forensic_replay.json`
- `tests/test_bitget_minimum_size.py`, `tests/test_dashboard_v3.py`,
  `tests/test_dataset_economics.py`, `tests/test_deployment_attestation.py`,
  `tests/test_entry_path_audit.py`, `tests/test_forward_paper_smoke_safety.py`,
  `tests/test_multisymbol_isolation.py`, `tests/test_portfolio_selection.py`,
  `tests/test_position_lifecycle_safety.py`,
  `tests/test_position_model_migration.py`,
  `tests/test_production_symbol_allowlist.py`,
  `tests/test_scan_loop_resilience.py`, `tests/test_shorts_enforcement.py`,
  `tests/test_symbol_expectancy_provenance.py`

No `.env*`, runtime state, log, credential, key or supervisor-pin file is part of
the patch.

## Risk impact and remaining gates

Risk impact is protective: the patch narrows LIVE configuration ambiguity,
selects one portfolio winner, strengthens exchange-truth and retry validation,
and separates monetary and percentage economics. It does not expand the maximum
simultaneous position count or any capital/risk limit.

Deployment remains blocked on all of the following:

1. Owner approval of the exact symbol allowlist.
2. Pull-request review and approval of this branch.
3. An owner-pinned, secret-safe configuration attestation with the approved
   existing leverage, risk, notional and checksum values.
4. A credentialed read-only exchange attestation returning `PASS` while flat,
   with no pending entries and no orphan protection.
5. Confirmation of Bitget contract/account response fields for every approved
   symbol in the real account.
6. Explicit owner authorisation for the later stop/deploy/pin/start sequence.

## Exact later deployment plan

Do not execute these steps as part of this review task.

1. Open a pull request from `integration/c-k-multisymbol-live`; review every
   commit and merge only the approved exact tip.
2. Owner confirms the ordered `PRODUCTION_SYMBOL_ALLOWLIST` and pins expected
   leverage, risk percentage, notional cap and current configuration SHA-256.
3. In a separately authorised maintenance window, require the bot/account to be
   flat. Do not proceed with an open position, pending entry or orphan TP/SL.
4. Run the secret-safe config gate with the owner-approved expected values:

   ```text
   .venv/bin/python -m deployment.config_attestation \
     --env-file .env.live \
     --expected-sha256 <OWNER_APPROVED_SHA256> \
     --expected-default-leverage <OWNER_APPROVED_VALUE> \
     --expected-max-leverage <OWNER_APPROVED_VALUE> \
     --expected-risk-pct <OWNER_APPROVED_VALUE> \
     --expected-notional-cap <OWNER_APPROVED_VALUE> \
     --expected-symbols <OWNER_CONFIRMED_CANONICAL_CSV>
   ```

5. With credentials supplied in the authorised operator environment (not by
   command-line arguments), run:

   ```text
   .venv/bin/python -m deployment.exchange_attestation \
     --symbols <OWNER_CONFIRMED_CANONICAL_CSV> \
     --required-leverage <OWNER_APPROVED_VALUE>
   ```

6. Require both tools to return `deployment_gate=PASS`. Archive only their
   sanitized output and approved commit/checksum identifiers.
7. Use the repository's controlled stop procedure. Verify the engine is stopped
   and the exchange is still flat before changing the checkout.
8. Update the deployment checkout to the exact reviewed commit. Verify clean
   status, exact commit, full tests/build evidence and no configuration drift.
9. Update the supervisor pin only to that exact owner-approved commit, as a
   separately reviewed deployment change.
10. Start through `scripts/launch_live.sh`; satisfy all authorisation layers and
    the interactive owner confirmation. Do not bypass the launcher.
11. Verify one process, canonical symbols, heartbeat, account reachability,
    zero unexpected orders/positions and dashboard-v3 provenance before enabling
    normal observation.

## Rollback plan

Rollback target: `cd8671c09df56b238e2c52727f6f54731ab5fac1`.

1. Do not roll back code while a non-BTC or otherwise unmanaged position is open.
   Keep exchange protection intact and escalate to the owner instead.
2. In an authorised flat maintenance window, stop through the controlled stop
   procedure and verify the process is gone.
3. Restore the deployment checkout and supervisor pin to the rollback target.
4. Do not restore or rewrite `.env.live`; this integration never changed it.
5. Verify clean checkout, exact rollback commit, flat exchange state, no pending
   entry and no orphan protection.
6. Restart only through the normal owner-authorised launcher and verify heartbeat,
   supervisor identity and dashboard state.

## Verdict

**IMPLEMENTATION READY FOR REVIEW**

This verdict is code-level only. It is not deployment approval.
