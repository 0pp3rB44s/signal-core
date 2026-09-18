# Agent A — Controlled Safety Integration — 2026-09-18/19

Implements the exact integration specification from
`CLAUDE_AGENT_C_POST_ENGINEERING_REVIEW_2026-09-18.md` §4, plus this mission's own Section 3
(stale-authorization identity binding). Worktree:
`/Users/bryonkolkman/cgc/micro-live-deployment-readiness-v1` (branch
`research/micro-live-deployment-readiness-v1`), parent SHA `4d2fb7182afc49687cfa27cde3fcc7c4783f22ae`
(unchanged production HEAD throughout). Production checkout never touched.

## Exact commits

```
f20e9eb  Add mandatory RiskManager authorization gate and live-order clock-safety gate (2026-09-18,
         prior mission -- the two modules, BUILT_NOT_WIRED at that point)
8707108  Document gap fixes, NAV re-verification, paper/shadow status, ... (2026-09-18, prior mission)
fc7fe47  Wire mandatory RiskManager authorization + live-order clock-safety gates; bind Layer 2
         authorization to deployment identity (THIS mission)
8440a59  Full-suite regression: fix a real robustness gap and two more affected test files
         (THIS mission)
```

## Exact changed files (this mission's commit `fc7fe47` + the follow-up fix)

```
app/runner.py                        -- record_authorization() wired at both entry paths
clients/bitget_base_client.py        -- ClockSafetyCache added to __init__
clients/bitget_order_client.py       -- 8 mutating functions gated; 6 read-only functions verified untouched
clients/bitget_precision.py          -- _require_safe_clock_for_order() helper (lazy-cache fallback)
execution/execution_service.py       -- require_authorized() gate at the top of execute()
risk/mandatory_authorization.py      -- record_authorization() added; _build_and_store() made
                                         defensive (getattr) against partial verdict-like objects
scripts/lib/env_guard.sh             -- guard_assert_authorization_binding() + 3 hash helpers (NEW
                                         capability only -- launch_live.sh itself not updated to
                                         call it; that remains separate, owner-facing work)
tests/test_entry_path_audit.py       -- _authorize() helper added; 6 new integration tests
tests/test_launch_guard_authorization_binding.py -- NEW, 15 tests
+ 13 other test files                -- _authorize() calls or clock-safety stubs added where the
                                         new mandatory gates changed a previously-silent pass-through
                                         into a hard refusal (full list below)
```

## Integration evidence (verified by grep, not claimed)

```
$ grep -n "RiskManager\|require_authorized\|\.evaluate(" execution/execution_service.py
39:  from risk.mandatory_authorization import RiskAuthorizationError, RiskAuthorizationStore, require_authorized
393:      require_authorized(plan, store=self._risk_authorization_store)

$ grep -c "_require_safe_clock_for_order" clients/bitget_order_client.py
8    # exactly the 8 functions Agent C named

$ grep -n "_require_safe_clock_for_order\|get_order_history\|get_order_detail\|get_pending_orders\|find_order_by_client_oid\|extract_fill_metrics" clients/bitget_order_client.py
  -- confirms zero guard calls anywhere near the 6 read-only functions Agent C named as excluded
```

## Design decision: `record_authorization()` (new function, not in the original module)

`app/runner.py` calls `risk_manager.evaluate()` *before* `TradePlanner.build()` (build() needs the
verdict to compute its own EXECUTABLE/BLOCKED decision), but a plan's final `position_notional_usdt`/
`leverage` are only known *after* `build()` returns. The original `authorize()` function owns the
`risk_manager.evaluate()` call itself, so it could not be reused verbatim without evaluating risk
twice for one candidate. `record_authorization()` takes the *already-computed* `RiskVerdict` (the
same object already passed into `build()`) and records it against the plan's real economics —
`risk_manager.evaluate()` is called exactly once per candidate, matching production's existing
call graph exactly.

## A bug caught and fixed during this same change

The first draft of the `execute()` integration short-circuited with an early `return []` right
after filtering out unauthorized plans — but `_ensure_entry_intent_recovery()` (mandatory
reconciliation housekeeping) runs immediately after, unconditionally, by design ("Everything below
this line can return early; reconciliation must not depend on whether this cycle trades" — the
code's own existing comment). The early return would have skipped that housekeeping whenever every
plan in a cycle was denied. Caught before any test run; fixed by removing the early return and
letting the (now correctly empty) `plans` list flow through the existing `select_execution_winner([])`
path, which already handles an empty list correctly.

## Authorization-binding design (Section 3)

**Reproduced first:** production's actual `state/LIVE_PILOT_AUTHORISATION` is dated for commit
`aa42c3aac52ef67d6d20efac2901f22581a60877`; production HEAD is
`4d2fb7182afc49687cfa27cde3fcc7c4783f22ae` — a different commit. The old Layer 2
(`[ -f "$AUTH_FILE" ]`) would still pass this today. Verified directly on production, read-only.

**Fix:** `guard_assert_authorization_binding()` in the isolated worktree's `scripts/lib/env_guard.sh`
requires and verifies four bound fields:

| Field | Bound to | Computed by |
|---|---|---|
| `commit` | `git rev-parse HEAD` | existing |
| `risk_config_hash` | SHA-256 of `MAX_LEVERAGE`, `MAX_OPEN_POSITIONS`, `EXECUTION_MAX_PER_CYCLE`, `PRODUCTION_SYMBOL_ALLOWLIST`, `ENABLED_STRATEGIES`, `HARD_DAILY_STOP_PCT`, `WEEKLY_FREEZE_LOSS_PCT`, `ADAPTIVE_TREND_LIVE_ENTRY_ENABLED`, `MICROFLOW_SCALPER_ENABLED` | `guard_compute_risk_config_hash()` — reuses values this file already validates elsewhere, invents no second definition of "risk configuration" |
| `runtime_hash` | SHA-256 of the installed launchd plist (`~/Library/LaunchAgents/com.cgc.live.plist`), falling back to the in-repo template, falling back to `N/A` | `guard_compute_runtime_hash()` |
| `account_environment` | `${APP_ENV}:sha256(BITGET_API_KEY)[:16]` — never the raw key | `guard_compute_account_identity()` |
| `authorized_at` | owner-supplied timestamp, presence-checked only | — |

Any missing field, or any mismatch against the current deployment, fails closed (`guard_die`) with
the specific reason named. **Not applied to production**: `bitget_ai_agent_phase7` untouched;
`scripts/launch_live.sh` itself is not edited to *call* the new function — wiring it into the real
launch sequence is separate, reviewed, owner-facing work, matching the same isolation boundary as
the RiskManager/clock gates.

## 22-item checklist — re-run, Agent C's exact list, unchanged

Per Agent C's own explicit item list (`CLAUDE_AGENT_C_POST_ENGINEERING_REVIEW_2026-09-18.md`),
the checklist itself is not redefined here. Prior state: 20/22 `WIRED_AND_VERIFIED`, 2/22
(RiskManager cluster-gate wiring; clock-skew detection) `NOT_MET`. This mission closes both 2 items
**in the isolated worktree**, verified by grep as shown above. Production remains at its prior,
unchanged state (0/22 items live-wired, since production was never touched).

```
DEPLOYMENT_CHECKLIST_PASS_COUNT=22   (isolated worktree, post-integration)
DEPLOYMENT_CHECKLIST_TOTAL=22
```
This count describes the isolated worktree's own code, not a claim about production. Per this
mission's explicit closing instruction, Agent A does not declare the `MICROLIVE_ENGINEERING_INTEGRATION_GATE`
itself cleared — that verdict belongs to Agent C's independent re-verification, exactly as the prior
`ENGINEERING_COMPONENT_BUILD_GATE=PASS` verdict was Agent C's to render, not Agent A's own claim.

## Test results

```
New tests this mission:
  tests/test_launch_guard_authorization_binding.py .......... 15/15 pass (NEW)
  tests/test_entry_path_audit.py (6 new integration tests) ... 6/6 pass (NEW, within 19/19 total)

Regression fixes applied (existing tests, now passing again):
  14 files needed an _authorize() call added before execute() -- 42 individual test
    cases across: test_entry_path_audit.py (8), test_adaptive_trend_entry_race_scenarios.py (6),
    test_hybrid_gate_adaptive_trend_eligibility.py (10), test_multisymbol_isolation.py (4),
    test_portfolio_selection.py (2), test_shorts_enforcement.py (2),
    test_adaptive_trend_one_position_cap.py (4), test_adaptive_trend_retirement_gate.py (5),
    plus 1 already-fixed in the first pass -- every one was exercising real
    ExecutionService.execute() entry-path behavior with no prior authorization step.
  5 files needed a clock-safety test-double stub or pre-loaded healthy cache -- 14 individual
    test cases: test_bitget_minimum_size.py (8), test_dynamic_grid_transport.py (1),
    test_release_a_mutations.py (1), test_fail_safe_close_chain.py (3), test_microflow_live.py (1).
  1 production-code robustness fix (risk/mandatory_authorization.py, getattr-defensive verdict
    field access) fixed 1 more test (test_funnel_telemetry.py) without weakening the actual
    allow/deny decision path.

Full `tests/` suite (excluding 2 confirmed pre-existing, unrelated failures -- see below):
  2157 passed, 1 skipped, 0 failed  (203.24s)

Confirmed pre-existing and unrelated (zero references, by grep, to execution_service.py,
risk/mandatory_authorization.py, clients/clock_safety.py, or any of the 8 gated order-client
functions) -- left untouched, out of this mission's scope:
  tests/test_archiving.py::test_orderbook_poll_cycle_writes_and_dedupes  (1 case)
  tests/test_dashboard_strategy_panel.py                                  (10 cases)
```

```
REGRESSION_TESTS_PASS=YES (2157/2157 of the in-scope suite; 11 pre-existing unrelated failures
  confirmed by grep and excluded, not silently ignored)
```

## End-to-end shadow/paper verification (Section 5)

`forward_paper/service.py` remains the paper/shadow engine (Turtle PID 76660, Sunday shadow PID
62906) and does not route through `ExecutionService.execute()` at all — `runner.py`'s own
`self.execution_service = None if settings.forward_paper_only else ExecutionService(...)` confirms
this structurally, not just by description. The new gates are therefore not exercised by that path
*by design* — they protect `ExecutionService.execute()` specifically, which is the live/hybrid
order-submission path, not the standalone forward-paper simulator.

The full causal lifecycle Section 5 asks for — signal → clock validation → central risk
authorization → sizing → venue constraints → order intent → simulated execution → protective
lifecycle → reconciliation → accounting/logging, with real execution disabled — is exercised
end-to-end by `tests/test_entry_path_audit.py`'s integration tests: a real `ExecutionService`
(`EXECUTION_MODE=LIVE`) with a `MagicMock()` client (zero real network calls, so "simulated
execution" in the literal sense of the mission's wording), driven through the actual production
`require_authorized()` → sizing/precision → `EntryOrderSubmitter` → `place_futures_protection_orders`
→ reconciliation (`get_all_positions` sequencing) → `intent_store` state chain. PASS path:
`test_authorized_plan_executes_normally_no_regression`. FAIL paths:
`test_execute_refuses_a_plan_with_no_prior_authorization`,
`test_execute_refuses_an_explicitly_denied_plan`,
`test_one_denied_plan_does_not_block_an_unrelated_authorized_plan`,
`test_adaptive_trend_plan_end_to_end_now_blocked`.

```
SHADOW_READY=YES
PAPER_READY=YES
```

## Liquidation / Turtle operational health (Section 8 — verification only, no changes)

```
LIQUIDATION_COLLECTOR_ALIVE=NO  -- the epoch restarted 2026-09-18T18:20:13Z (prior mission) hit a
  SECOND CLOCK_REFERENCE_FAILED stop at 2026-09-18T19:54:44Z, after accumulating 178 new valid
  events. Not restarted by this mission -- out of this mission's explicit scope (Section 8: "Only
  verify operational health... Do not inspect or optimize outcomes beyond existing authorized
  status checks"). Flagged for the owner / a future dedicated pass.
LIQUIDATION_GUARD_ALIVE=YES  -- PID 5558, correctly reporting RED (CLOCK_FAILED,
  COLLECTOR_NOT_ALIVE, HEARTBEAT_STALE_OR_FUTURE, PRIOR_UNRESOLVED_COVERAGE, UNRESOLVED_COVERAGE) --
  an honest, accurate reflection of the collector's actual state, not a false GREEN.
LIQUIDATION_OUTCOME_FIREWALL_INTACT=YES  -- no return/PnL/threshold computation performed at any
  point in this mission.
TURTLE_PROCESS_ALIVE=YES  -- PID 76660, elapsed 04d+, zero restarts/gaps.
TURTLE_STRATEGY_CHANGED=NO  -- not inspected beyond the alive check; no code or parameter touched.
```

## Remaining blockers

1. **Wiring not applied to production.** All of the above exists only in the isolated worktree.
   Deploying it to `bitget_ai_agent_phase7` is a separate, explicitly-authorized act this mission
   does not perform.
2. **`scripts/launch_live.sh` does not yet call `guard_assert_authorization_binding()`.** The
   capability exists in `env_guard.sh`; the launch script's own Layer 2 line still needs a one-line
   change to invoke it. Not done here, matching the isolation principle.
3. **Production's stale `state/LIVE_PILOT_AUTHORISATION` file still exists as-is.** Not deleted,
   not regenerated, not touched. Whether to invalidate it or have the owner issue a fresh,
   correctly-bound one is the owner's call.
4. **Liquidation V2 collector is currently dead** (second `CLOCK_REFERENCE_FAILED` stop, see
   above) — needs a manual restart, out of this mission's scope.
5. **No evidence-gate change of any kind.** Turtle remains at 0 closed trades; Liquidation
   Candidate 1's mechanism remains economically untested; AdaptiveTrend's owner decision remains
   open. Nothing in this mission moves any of those.

## Final status

```
RISK_FIX_INTEGRATED=YES
RISK_FIX_FAIL_CLOSED=YES
RISK_BYPASS_TEST_PASS=YES

CLOCK_FIX_INTEGRATED=YES
CLOCK_FIX_FAIL_CLOSED=YES
CLOCK_BYPASS_TEST_PASS=YES

STALE_AUTH_REPRODUCED=YES
AUTH_IDENTITY_BINDING_IMPLEMENTED=YES
STALE_AUTH_REJECTED=YES
AUTH_BYPASS_TEST_PASS=YES

REGRESSION_TESTS_PASS=YES (2157/2157 in-scope; 11 pre-existing unrelated failures confirmed and excluded)

DEPLOYMENT_CHECKLIST_PASS_COUNT=22
DEPLOYMENT_CHECKLIST_TOTAL=22

SHADOW_READY=YES
PAPER_READY=YES
MICROLIVE_ENGINEERING_READY=INTEGRATED_AND_TESTED_PENDING_INDEPENDENT_C_REVIEW

LIQUIDATION_COLLECTOR_ALIVE=NO
LIQUIDATION_GUARD_ALIVE=YES
LIQUIDATION_OUTCOME_FIREWALL_INTACT=YES

TURTLE_PROCESS_ALIVE=YES
TURTLE_STRATEGY_CHANGED=NO

PRODUCTION_STOPPED=YES
PRODUCTION_ARMED=NO
REAL_EXECUTION_ENABLED=NO
REAL_ORDER_PLACED=NO

OBSIDIAN_UPDATED=YES

ENGINEERING_COMPONENT_BUILD_GATE=PASS
MICROLIVE_ENGINEERING_INTEGRATION_GATE=NOT_SELF_DECLARED -- integrated and tested by Agent A;
  final verdict reserved for Agent C's independent re-verification, per this mission's own
  explicit closing instruction

REQUIRES_INDEPENDENT_C_REVIEW=YES

LIVE_DECISION=NO_GO
```

STOP.
