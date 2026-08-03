# Independent Release Verifier — Final Verification

## Verdict

- `SAFE_TO_REVIEW: yes`
- `SAFE_TO_DEPLOY: no`
- Base verified: `817bc72f5b7b85f12b23fd6d7b03b09542addaed`
- Final verified tip: `f36647d2ab4369e10857cb30eb7d19cadba977fc`
- Branch: `codex/exchange-truth-integrity-release`
- Exact merge-base: `817bc72f5b7b85f12b23fd6d7b03b09542addaed`

All code-review blockers found in the two prior independent passes are closed at this tip. The branch is safe for owner code review. It is not safe to deploy because the independent economic and backtest gates fail; owner code review must not be represented as deployment approval.

## Final serializer-to-restart verification

The final change converts the authoritative fail-safe `opened_at_ms` to a timezone-aware `opened_at` value before the real `TradeDatasetV2Logger` writes the provisional CSV row.

The full production-shaped chain was independently reproduced:

1. `ExecutionService._record_fail_safe_close_economics()` invoked its real provisional writer.
2. `TradeDatasetV2Logger` wrote `CLOSE_PROVISIONAL` to a temporary CSV.
3. `read_provisional_rows()` reread the row as after a restart.
4. `recover_provisional_closes()` received two same-symbol, same-side and same-size exchange history rows.
5. The persisted open timestamp selected the exact intended lifecycle, `positionId=P1`, rather than the row one hour away.

Observed evidence:

`opened_at=2026-08-02T19:46:40+00:00; seen=1; recovered=1; matched_position_id=P1`

The new production test also exercises the real fail-safe serializer and asserts millisecond-equivalent open-time preservation. The previous serializer/restart blocker is FIXED / PROVEN.

## Prior blockers rechecked

1. **UNKNOWN funding coercion — FIXED / PROVEN.** `totalFunding` is required; missing or invalid funding leaves the close unreconciled. UNKNOWN is never converted to zero.
2. **Size mismatch fallback — FIXED / PROVEN.** A supplied expected size with no in-tolerance exchange candidate fails closed.
3. **Missing production recovery wiring — FIXED / WIRED.** `PositionManager.sync()` invokes dataset-backed recovery even when local position state is empty. The real ledger reader and bounded/idempotent recovery helper are on the production call path.
4. **Fail-safe bypass of remaining-size proof — FIXED / WIRED.** The fail-safe uses `close_futures_position_full()`, invokes the shared recorder and treats only verified `CLOSED` as success. Partial or nonzero remainder paths write no economics and retain protection.
5. **launchd power assertion and broad PID adoption — FIXED / PROVEN.** The helper is sourced before adoption; the recorded bot PID is validated and adopted rather than an arbitrary host-wide match.
6. **Composite-size dedup formatting — FIXED / PROVEN.** Equivalent numeric size representations normalize to one Decimal-based key.

## Production wiring and safety

- PROVEN: residual cleanup, TP3 close-all, dead-trade timeout and entry fail-safe routes invoke the shared close recorder after confirmed-flat full-close results.
- PROVEN: bare API acknowledgement, partial remainder, transport failure and missing lifecycle identity cannot create an economic close.
- PROVEN: exchange gross PnL, open fee, close fee, funding and net PnL are required and preserved; unavailable fields remain UNKNOWN/non-economic.
- PROVEN: economic dedup covers the active ledger plus three rotated segments and ignores provisional rows as economic proof.
- PROVEN: maker intents become terminal only after the successful cancel/absent-position path. No maker, fallback or routing setting changed.
- PROVEN: no `.env`, application configuration, strategy, planning or risk file changed relative to base.
- PROVEN: no leverage, sizing, margin, max-position, risk, TP/SL, protection-distance or strategy parameter drift was found.

## Non-blocking remaining concerns

- OBSERVABILITY GAP / FUTURE PATCH: `read_provisional_rows()` reads the active ledger only, whereas economic dedup also checks rotated segments. Recovering provisional rows from rotated ledgers would harden rare schema-rotation timing.
- DATA QUALITY / FUTURE PATCH: production does not retire provisional rows after the economic row is appended. Dedup prevents monetary double-counting, but the append-only supersession model should be documented accurately or safely compacted later.
- TEST QUALITY: the named mutation tests are regression examples rather than an executed mutation-testing framework. Treat mutation evidence as SUGGESTIVE, though the full negative-path suite is strong.
- PRE-EXISTING / FUTURE PATCH: emergency-flatten economics remains explicitly unwired and is not claimed PIPELINE COMPLETE for that route.
- OBSERVABILITY GAP / FUTURE PATCH: portfolio-ranking and decision-time quote capture remain excluded.

These concerns do not block code review because the release does not claim the excluded emergency/observability paths complete, and the implemented economic paths remain fail-closed and deduplicated.

## Economic and backtest gates

- `ECONOMIC_GATE: FAIL`
- `BACKTEST_GATE: INVALID`
- `SAFE_TO_DEPLOY: no`

The frozen 25-trade exchange-truth sample remains net `-0.97513810` USDT and does not demonstrate positive economic edge. The available backtest evidence remains mixed historical log analysis rather than a reproducible current-code simulation covering maker/fallback behavior, funding, intrabar ordering, portfolio caps, out-of-sample and walk-forward evaluation.

No strategy/risk parameter approval or deployment is justified. The code may proceed to owner review strictly as a safety, data-integrity and observability patch.

## Tests and checks run

- Focused final verifier suite: `80 passed`.
- Builder's requested focused evidence was consistent with `62 passed` for its narrower selection.
- Full repository suite independently confirmed: `1027 passed, 1 skipped` in `91.18s`.
- Real serializer -> CSV -> reread -> two-candidate recovery reproduction: passed and matched `positionId=P1`.
- `python3 -m compileall -q clients execution telemetry tests`: passed.
- `git diff --check <base>...HEAD`: passed.
- `bash -n deploy/launchd/live_agent.sh`: passed.
- Base, ancestry, full 21-file diff, production call-sites, negative tests, config/risk drift, protection behavior, backtest provenance and rollback were inspected read-only.
- No exchange call, LIVE mutation, order action, secret read, process change, code/test/control-plane change, git history change, commit, push, merge or deployment was performed.

## Rollback and release boundary

- Before merge: leave the branch or draft PR unmerged.
- After merge but before deployment: revert the release commits through normal reviewed history.
- Deployment remains separately owner-gated.
- Builder and verifier roles remain separate; this verifier approves code review only, not release deployment.
