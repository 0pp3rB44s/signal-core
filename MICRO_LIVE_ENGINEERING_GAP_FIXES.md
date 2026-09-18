# Micro-Live Engineering Gap Fixes — 2026-09-18

Worktree: `/Users/bryonkolkman/cgc/micro-live-deployment-readiness-v1` (branch
`research/micro-live-deployment-readiness-v1`, parent SHA `4d2fb7182afc49687cfa27cde3fcc7c4783f22ae`,
unchanged production HEAD). Commit `f20e9eb`. Isolated only — not merged, not deployed, production
checkout untouched.

## Gap #1 — AdaptiveTrend entry path bypasses RiskManager

**Reproduced independently, by direct source read** (not just cited from Agent C's report):

- `strategies/adaptive_trend_plan.py::build_trade_plan()` hardcodes `verdict="EXECUTABLE"` and
  contains zero references to `RiskManager`/`risk_manager`/`.evaluate(` anywhere in the function.
- `execution/execution_service.py::ExecutionService.execute()` — the single, strategy-agnostic
  order-submission entry point every `TradePlan` flows through — also contains zero calls to
  `RiskManager`/`.evaluate(`. Risk authorization today happens ONLY inside
  `planning/trade_planner.py`. `execute()` trusts whatever `TradePlan` it is handed; it has no way
  to tell whether the plan actually passed through `RiskManager.evaluate()` or not.
- **Current blast radius, also confirmed by direct source read:**
  `strategies/adaptive_trend_runtime.py::route_selected_candidate()` never calls
  `build_trade_plan()` or `execute()` — its own docstring: *"this always ends in a shadow record,
  never a real order... routed to shadow only, because that path does not exist yet."* **The
  defect is real and latent, but not currently reachable from any live order** — this is a more
  precise statement than "AdaptiveTrend is 1-position-max," which was true but described a
  different safety property.

**Fix:** `risk/mandatory_authorization.py` (new, isolated). `authorize()` is the only sanctioned
way to record a RiskManager verdict (wraps `RiskManager.evaluate()`, persists a content-bound
receipt keyed by `candidate_id`). `require_authorized()` is what `execute()` must call FIRST, for
every plan, unconditionally — strategy-agnostic (reads only `candidate_id`/economics, never
`strategy` name, so there is no name to special-case). Fails closed on: no record, corrupt store,
expired record (300s TTL), RiskManager exception, explicit deny, or plan economics that changed
after authorization (content-hash mismatch).

**Tests:** `tests/test_mandatory_risk_authorization.py`, 13/13 passing — single position, multiple
correlated positions (BTC/ETH/SOL, one denied), cluster-exposure-breach denial, global-exposure-
breach denial, RiskManager unavailable, malformed/corrupt state file, process restart (recovers a
valid record; blocks when nothing was ever written), duplicate signal, expired authorization,
plan-mutated-after-authorization, and a direct unit-level reproduction of the
`adaptive_trend_plan.py` defect (asserts the string `RiskManager` does not appear in
`build_trade_plan`'s source).

**Exact integration point (not applied by this mission):**
```python
# execution/execution_service.py, inside ExecutionService.execute(), immediately after the
# `if not self.settings.execution_enabled: return []` guard and BEFORE _ensure_entry_intent_recovery():
from risk.mandatory_authorization import require_authorized, RiskAuthorizationError
...
for plan in plans:
    try:
        require_authorized(plan, store=self._risk_authorization_store)
    except RiskAuthorizationError as exc:
        self.log.error("RISK_AUTHORIZATION_REFUSED | plan_id=%s | %s", plan.plan_id, exc)
        return []  # or per-plan filtering, depending on desired multi-plan semantics
```
And in `planning/trade_planner.py`, wherever `RiskManager.evaluate()` is currently called directly,
call `risk.mandatory_authorization.authorize()` instead (same signature plus `store=`) so the
receipt actually gets recorded for the plan `TradePlanner` is about to return.
`strategies/adaptive_trend_plan.py::build_trade_plan()` would need the same treatment before it
could ever be safely wired to `execute()`.

---

## Gap #2 — No clock-skew detection on the live order-signing path

**Reproduced independently, by direct source read:** `clients/bitget_auth.py::timestamp_ms()` is
`str(int(time.time() * 1000))` — local wall clock, no exchange-time comparison, no drift check, no
alerting, called from `build_headers()` for every authenticated request. `CLOCK_SKEW_DETECTION=NOT_FOUND`,
confirmed.

**Fix:** `clients/clock_safety.py` (new, isolated). Deliberately reuses the SAME design already
proven this week by the liquidation V2 collector's `clock_reference()` (see
`LIQUIDATION_V2_RECOVERY_2026-09-18.md`) — same frozen `CLOCK_OFFSET_LIMIT_MS=500`, same
coherent-wall/monotonic-pair requirement, same fail-closed contract — adapted to Bitget's public
`GET /api/v2/public/time` endpoint and the order path specifically (single bounded probe with a
short-TTL cache, not a continuous background collector). `require_safe_clock()` raises
`ClockUnsafe` — refuses to proceed — unless a fresh (≤60s old), in-bound, internally-coherent
reference exists.

**Tests:** `tests/test_clock_safety.py`, 12/12 passing — normal clock, positive skew (in-bound),
negative skew (in-bound), skew beyond the 500ms bound (blocks), bound-never-loosened assertion,
stale reference (blocks if renewal also fails; passes if renewal succeeds), endpoint failure,
network timeout, malformed response, clock jump (wall/monotonic incoherence mid-probe), process
restart (a fresh cache never trusts a carried-over state, forces a real probe).

**Exact integration point (not applied by this mission):**
```python
# clients/bitget_auth.py, inside build_headers() (or immediately before it's called from the
# order-submission client, clients/bitget_order_client.py), before signing:
from clients.clock_safety import require_safe_clock, ClockUnsafe
...
try:
    require_safe_clock(self._clock_cache)  # one ClockSafetyCache instance per client/process
except ClockUnsafe as exc:
    raise OrderSubmissionRefused(f"clock safety gate failed: {exc}") from exc  # NO ORDER
```

---

## Why neither fix was wired into execution_service.py / bitget_auth.py directly

`execution_service.py` is 2715 lines of safety-critical, already-complex, already-tested
production-pattern logic (selection, reconciliation, Bitget-truth syncing, cooldowns). A correct,
minimal-blast-radius edit to it — and verification that it doesn't regress any of its existing
behavior — is real, separate engineering work deserving its own focused, reviewed change, not a
rushed insertion bundled into this diagnostic/prep sprint under a Sunday deadline. The fix modules
themselves are complete, correct, and independently tested (25/25 tests passing, zero regressions
in 80 adjacent existing production tests spot-checked: `test_risk_manager.py`,
`test_adaptive_trend_plan.py`, `test_execution_safety.py`, `test_entry_order_idempotency.py`). The
exact integration diff is documented above precisely so wiring, when authorized, is a small,
reviewable, mechanical change rather than a design exercise.

---

## Honest checklist status

```
DEPLOYMENT_CHECKLIST_PASS_COUNT=20
DEPLOYMENT_CHECKLIST_TOTAL=22
```
Unchanged from the 2026-09-18 sprint report's own count — the production-equivalent code in this
worktree has NOT been edited (only two new, isolated, unimported files were added). This mission
did not claim 22/22 by changing what counts as "wired": the 2 remaining items require an actual
edit to `execution_service.py` and `bitget_auth.py`, which is real, separate, reviewable work now
fully specified and ready to execute, not performed here. The 20 other items are unchanged and
still verified as of this same commit (see the 2026-09-18 sprint report for their individual
file:line citations — not re-derived here to avoid duplicating that already-complete audit).

---

## NAV executability — freshly re-verified against LIVE Bitget contract metadata

```
NEARUSDT live fetch (this mission, 2026-09-18): minTradeUSDT=5, minTradeNum=1, sizeMultiplier=1
Reference: Turtle's actual real NEARUSDT signal (2026-09-17), risk-sized notional $1.3462 at
  reference NAV $27.443667453963 (0.75% risk/trade, 5x leverage cap -- frozen, unchanged)

NAV50:  risk-sized notional = $2.4527  -> BELOW $5.00 minimum -> SKIP (correct, not forced)
NAV100: risk-sized notional = $4.9053  -> BELOW $5.00 minimum -> SKIP (matches sprint report's
                                            $4.90 figure, independently re-derived)
NAV150: risk-sized notional = $7.3580  -> clears with real margin (47% headroom)
NAV200: risk-sized notional = $9.8106  -> clears with more margin (96% headroom)

MINIMUM_FEASIBLE_NAV (today's actual NEARUSDT signal, frozen sizing, exact) = $101.93
```
No risk was inflated, no threshold loosened, Turtle's own parameters were not touched. NAV100
remains PARTIAL — it is close (within ~2%) but does not clear today's actual smallest real signal.
NAV150 is the first level with genuine margin, confirming the sprint report's finding independently
against a fresh live fetch rather than re-citing the earlier number unchecked.

---

## Paper / Shadow lifecycle — confirmed, not rebuilt

`forward_paper/service.py` IS this project's paper/shadow engine, already used continuously in
production by both the Turtle shadow (PID 76660, running since 2026-09-14, zero gaps) and the
Sunday multi-strategy shadow (PID 62906, running since 2026-09-13). Its causal lifecycle was
independently, forensically verified across dozens of real trades in this engagement's own prior
sessions: `simulated_fill` prefers a fresh causal decision-time quote over a stale trigger-candle
close (commit `8cd67a1`, exchange-truth `net_pnl = gross_pnl - fees` verified with zero formula
failures), realistic slippage/fee/funding accounting, zero entry-candle-reuse or same-candle-
reprice violations found across every trade audited this engagement. No new code was needed to
satisfy Stage 6/11's paper/shadow requirements — the reusable engine already exists and is already
proven; rebuilding it would duplicate real, working infrastructure.

```
SHADOW_READY=YES
PAPER_READY=YES
```

---

## Micro-live safety envelope package (prepared, NOT activated)

Composed from what already exists in production plus the two new gates above — nothing here is
wired into any running process:

| Control | Source | Status |
|---|---|---|
| Risk per trade | `Settings.account_risk_per_trade_pct` (existing) + `risk/mandatory_authorization.py` (new, enforces it structurally) | Exists + gate ready |
| Max total exposure | `Settings.planner_max_notional_pct_of_equity` / `planner_max_notional_per_trade_usdt` (existing) | Exists |
| Max simultaneous exposure | `Settings.max_open_positions` (existing, default 2; recommend 1 for a first micro-live pilot) | Exists |
| Daily loss limits | `Settings.hard_daily_stop_pct` (2.0%, existing) / `weekly_freeze_loss_pct` (7.0%, existing) | Exists |
| Execution-error limit | `RiskManager._kill_switch_gate`'s consecutive-loss check (existing, hardcoded 3) | Exists |
| Stale-data stop | Market-data snapshot `.usable()` discard (existing, confirmed 2026-09-16 audit) | Exists |
| Clock stop | `clients/clock_safety.py` (new, this mission) | Built + tested, not wired |
| Reconciliation stop | `execution/order_intent_store.py` `STATE_UNKNOWN` blocks all new entries (existing) | Exists |
| API-uncertainty stop | `execution/order_intent_store.py` `STATE_AMBIGUOUS` recoverable-not-terminal handling (existing) | Exists |
| Duplicate-order guard | `execution/order_identity.py` + `order_intent_store.py` (existing, deterministic clientOid) | Exists |
| Kill switch | `scripts/stop_all.sh` (SIGTERM/SIGKILL) + `state/live.stop`/`state/supervisor.stop` (existing) | Exists |
| Central risk authorization | `risk/mandatory_authorization.py` (new, this mission) | Built + tested, not wired |
| Audit log | `execution/closed_trade_writer.py` / `closed_lifecycle_recorder.py` (existing, exchange-truth sourced) | Exists |

```
MICROLIVE_ENGINEERING_READY=NOT_YET — 20/22 wired, 2/22 built+tested+isolated pending a separate
  reviewed integration change (see exact diffs above); independent of the engineering question,
  no candidate currently has sufficient evidence to need this stage (unchanged from sprint report)
```

---

## Live arming ceremony (prepared, NOT performed) — see LIVE_ARMING_CEREMONY_CHECKLIST_2026-09-18.md
