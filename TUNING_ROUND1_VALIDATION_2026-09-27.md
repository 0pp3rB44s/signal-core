# Tuning Round 1 — data separation, validation run, and result (2026-09-27)

## Data separation

```
DEVELOPMENT  = state/executed_trades.json + state/live_trade_journal.json (May-Aug 2026, the last real-money
               session's own ledgers). This is the data AGENT_A_ECONOMIC_DIAGNOSTIC_DEEP_2026-09-27.md
               analyzed to produce the concentration hypothesis. Fully "spent": every trade in it has
               already been aggregated, broken down by symbol/family/hour, and used to reason about
               causes. It cannot be reused as proof the tuning change works.

VALIDATION   = freshly-fetched Bitget public candles + one order-book snapshot per symbol, pulled AFTER
               TUNING_ROUND1_FROZEN_SPEC_2026-09-27.json was written, hashed and committed (git history is
               the evidence of that ordering). The cap value was derived from arithmetic
               (TUNING_ROUND1_CAP_SELECTION_2026-09-27.md), not from looking at this or any other replay's
               PnL, so it was not fitted to this data. Disclosed honestly rather than overclaimed: this is
               NOT long-elapsed-time independent of Phase 2's earlier same-day fetch (this is all one
               working session) - real market history did not have days or weeks to diverge. It is the best
               available "not used to pick the value" data, not a rigorous sealed holdout.

SEALED HOLDOUT = genuinely not available as historical data: every historical ledger this project holds
               was already used in Phase 2's diagnostic. Per this mission's own instruction for exactly
               this situation, the substitute is prospective PAPER: funnel/tools/shadow_compare.py runs
               ORIGINAL and TUNED side by side on live data and logs every divergence (timestamp, symbol,
               strategy, baseline/tuned decision, reason, simulated outcome). Its logging is proven
               functional (verified against a synthetic case where a divergence must be detected - see the
               module's own test invocation), but a MEANINGFUL sealed holdout requires real elapsed time
               continuing to accumulate divergences beyond this session, which this report does not
               fabricate having already happened.

HOLDOUT_INDEPENDENT = NO, for the VALIDATION data specifically (same-day, disclosed above). The genuine
               holdout (the ongoing shadow log) has not yet accumulated meaningful history at the time of
               this report.
```

## Two harness bugs found and fixed before any result could be trusted — neither changed the tuning value

Both are disclosed here in full, in the order found, because a reader must be able to tell "the mechanism
was actually tested" from "the test never gave it a chance":

1. **Sequential-by-symbol replay could never expose cross-symbol concentration.** The first harness draft
   replayed each symbol's entire ~190-step history before starting the next symbol's. A cluster-exposure
   cap can only ever matter when two *different* same-cluster symbols are open *at the same time* - which a
   sequential replay structurally cannot produce (by the time symbol 2 starts, symbol 1's whole timeline,
   including every close, has already finished). Fixed: all 8 symbols' steps are now interleaved into one
   true chronological timeline (`funnel/tools/tuning_round1_ab.py::_build_timeline`).

2. **`risk/risk_manager.py::_load_open_positions()` reads a FILE** (`state/executed_trades.json`,
   `status=="OPEN"` rows), not this restoration's in-memory engine state. The first harness draft never
   wrote to that file, so the cluster-exposure check always saw zero existing exposure, for both variants,
   regardless of what was actually simulated open. Fixed: each variant now writes its OWN current open
   positions to that file immediately before its own decision (and the two variants' portfolios never share
   that file at the same instant - variant A runs to completion, then variant B does, each independent).

3. **A `Settings` construction bug** (found while investigating why results were still identical after
   fixes 1-2): `app/config.py::Settings` fields carry a pydantic alias (e.g. `MAX_CLUSTER_EXPOSURE_PCT`);
   `populate_by_name` is not enabled, so `Settings(max_cluster_exposure_pct=40.0)` **silently keeps the
   default** (120.0) instead of raising - variant B had never actually differed from variant A in any run
   before this was found. Fixed: constructed via the alias, `Settings(MAX_CLUSTER_EXPOSURE_PCT=40.0)`,
   confirmed by direct inspection (`s.max_cluster_exposure_pct == 40.0`) before re-running.

None of these three fixes touched `TUNING_VALUE` (40.0) or `TUNING_HASH`
(`d4b49da704c28f9c83b9c0b7b70efa32e81202ded0ad2acbcaa73ad6d4640fb0`) - they are corrections to how the
already-frozen change is *observed* in this test harness, made before generating the number reported below,
not adjustments made after seeing a disappointing number and then reversed.

## The result, after all three fixes: still no measurable effect - and why, disclosed rather than hidden

With both bugs fixed, `cluster_room=40.00` (variant B) and `cluster_room=120.00` (variant A) both genuinely
appear in the run log, and two same-cluster symbols (SUIUSDT, SEIUSDT, both `L1_BETA`) were simultaneously
open for real in this validation window (`max_simultaneous_same_cluster=2` for both variants). **Yet the
two variants' results remain bit-for-bit identical** (`TRADES_BLOCKED_BY_NEW_CAP=0`).

Traced to its root cause rather than left unexplained: SUIUSDT's entry was accepted at t=1790474400001;
SEIUSDT's candidate was evaluated exactly one 15-minute bar later, at t=1790475300001 - the SAME instant
SUIUSDT's entry order became eligible to fill. Symbols are processed in alphabetical order within a tied
timestamp, so at that exact millisecond, SEIUSDT's risk check runs *before* SUIUSDT's fill for that same
step is processed - meaning `_load_open_positions()` still shows SUIUSDT as not-yet-open at the moment
SEIUSDT's cluster exposure is evaluated, even though both are open moments later. This is a genuine,
narrower limitation of the *existing, unmodified* cluster-exposure mechanism itself (it can only ever act on
already-filled positions, never on a same-instant fill still being processed) - not a mechanism this round
invented, and not something this round is permitted to redesign (that would be a new mechanism, out of
scope for "one change only"). It is reported, not patched around: a fourth harness change chosen after
seeing this exact outcome would cross from "correcting how the change is observed" into "searching for a
result," which this mission's own rules forbid.

```
BASELINE_TRADES=30   TUNED_TRADES=30
BASELINE_NET_PNL=-7.0621   TUNED_NET_PNL=-7.0621
BASELINE_PROFIT_FACTOR=0.0846   TUNED_PROFIT_FACTOR=0.0846
BASELINE_MAX_DRAWDOWN=7.0621   TUNED_MAX_DRAWDOWN=7.0621
BASELINE_SYMBOL_CONCENTRATION(HHI)=0.1644   TUNED_SYMBOL_CONCENTRATION(HHI)=0.1644
BASELINE_STRATEGY_CONCENTRATION(HHI)=0.4822   TUNED_STRATEGY_CONCENTRATION(HHI)=0.4822
TRADES_BLOCKED_BY_NEW_CAP=0   PNL_OF_BLOCKED_TRADES=0.0 (diagnostic; no trades were blocked to diagnose)
```

Robustness diagnostics (identical between variants, since nothing differed): excluding BTCUSDT, net moves
from -7.06 to -6.77 (n 30→24); excluding the best symbol (SEIUSDT here, -0.08) barely changes it; excluding
the worst symbol (AVAXUSDT here, -2.44) leaves -4.63. Notably, **BTCUSDT was the *least* negative symbol in
this validation window** (-0.30 of -7.06), the opposite of its role in the historical August diagnostic -
reinforcing Phase 2's own conclusion that per-symbol "badness" is not a stable, persistent property, and
that a symbol-specific rule (rather than the general mechanism actually chosen) would have been the wrong
lesson to draw. Only one week bucket exists in this ~4-day validation window, so "excluding best/worst week"
is degenerate here (excluding the only week removes every trade) and is not reported as if it were
informative.

## Round 1 result

```
ROUND_1_RESULT=INCONCLUSIVE
```

Not PASS: no reduction in realized concentration or drawdown was demonstrated - the mechanism never bound
in this validation window. Not FAIL: nothing was destroyed either (the two variants are identical; no
expectancy was lost, no new failure was introduced) - and the reason it did not bind is a specific, identified,
narrow timing edge case in the existing mechanism, not evidence that tightening cluster exposure is the
wrong idea. Calling this PASS would overclaim; calling it FAIL would misrepresent a null result as a
negative one. INCONCLUSIVE is the accurate word for "correctly implemented, honestly tested, no signal
either way from this window."
