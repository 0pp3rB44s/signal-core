# Strict validation report — frozen speculative candidates

Validation started 2026-08-31 after spec freeze. No new architecture or
threshold search was performed.

## Decision

PRIMARY_STATUS=FAIL_SHADOW_GATE
SECONDARY_STATUS=FAIL_SHADOW_GATE
SHADOW_READY=NO
PRODUCTION_TOUCHED=NO
REAL_ORDERS_SENT=0

The frozen spec SHA-256 is
`cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.
Optimization after freeze is prohibited.

## Gate assessment

1. **Remove current-universe look-ahead — PARTIAL / NOT YET PASSED.** Bitget's
   current contract response does not provide usable historical membership for
   sampled contracts and excludes delisted failures. Historical results remain
   `DIAGNOSTIC_ONLY_CURRENT_SURVIVOR_UNIVERSE`. A hash-chained prospective
   point-in-time ledger began at `2026-08-31T18:03:08.134914Z` with 768 markets;
   future observations can satisfy this gate without backfill.
2. **Freeze both specs — PASSED.** Exact eligibility, signals, ranking, entry,
   24h hold, 10% stop, costs, and $100 portfolio rules are frozen in
   `FROZEN_SPECS.json`.
3. **Untouched/prospective validation — STARTED / INSUFFICIENT SAMPLE.** The
   append-only journal has zero closed events. The frozen gate requires at
   least 30 calendar days and 30 closed events per candidate.
4. **Stress costs/slippage — DIAGNOSTIC PASSED FOR EXPECTANCY.** Both historical
   portfolio proxies stay positive at 3x measured $10-notional costs. This is
   not event-time execution proof.
5. **Gap-through-stop — IMPLEMENTED / INSUFFICIENT SAMPLE.** Stops execute at a
   worse bar open when price gaps beyond the threshold. Primary history found
   three such candidate events (0.021%; maximum 136.24 bps beyond the stop),
   though none entered the capacity-limited portfolio. Secondary history found
   none; that is not proof of zero future gap risk.
6. **Portfolio daily growth — PASSED AS DIAGNOSTIC ONLY.** Results below use
   10% of equity per position, maximum two concurrent positions, 20% maximum
   gross exposure, 1x leverage, compounding, and next-open execution.
7. **Robustness/drawdown/capacity near $100 — FAILED FOR SHADOW.** Primary
   drawdown exceeds the 10% gate. Secondary narrowly exceeds it under required
   2x cost stress, and neither has prospective outcomes.

## $100 portfolio results

### Primary — HV_A volatility expansion

| Cost stress | Ending equity | Total return | Geometric daily growth | Conservative drawdown |
|---:|---:|---:|---:|---:|
| 1x | $117.78 | 17.78% | 0.0917% | -15.38% |
| 2x | $110.08 | 10.08% | 0.0538% | -17.29% |
| 3x | $102.88 | 2.88% | 0.0159% | -19.92% |

Accepted 413 events and skipped 14,062 because only two simultaneous positions
were allowed. Stop-hit rate over all diagnostic events was 19.07%. The result
does not support 1% daily growth and fails the drawdown requirement at every
cost level.

### Secondary — 24h funding-crowding continuation

| Cost stress | Ending equity | Total return | Geometric daily growth | Conservative drawdown |
|---:|---:|---:|---:|---:|
| 1x | $142.42 | 42.42% | 0.4177% | -9.56% |
| 2x | $139.89 | 39.89% | 0.3964% | -10.01% |
| 3x | $137.39 | 37.39% | 0.3751% | -10.46% |

Accepted 111 events and skipped 136 due to capacity/symbol overlap. Diagnostic
stop-hit rate was 29.96%. The 2x drawdown misses the frozen 10% limit by 0.012
percentage point; the rule is not rounded into a pass.

## Capacity and evidence limits

At ~$100 equity, each frozen position starts near $10 notional and at most $20
gross exposure is permitted. All cost calculations use measured $10 depth,
12 bps round-trip taker fees, and current spread/slippage, then stress the total
by 1x/2x/3x. Earlier snapshots showed book capacity through $1,000, but this
strict phase makes no capacity claim above $100 because event-time depth and
fill probability remain unavailable.

Historical portfolio growth is `SUGGESTIVE`, not `PROVEN`, because current
survivors and current execution snapshots are applied backward. Unknown values
were never replaced with zero.

## Required continuation

Do not change either spec. Continue append-only point-in-time collection until
the prospective minimum is met. Re-evaluate exactly once against the frozen
gates. No shadow build is authorized before then.

FINAL_STATUS=STRICT_VALIDATION_IN_PROGRESS_EXTERNAL_TIME_SAMPLE_BLOCKER
