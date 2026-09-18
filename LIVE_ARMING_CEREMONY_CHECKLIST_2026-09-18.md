# Live Arming Ceremony — Future Checklist (prepared, NOT performed)

Not executed by this mission. No credentials enabled, no orders placed, owner explicit
authorization remains mandatory after evidence approval, independent of everything below.

Production (`scripts/launch_live.sh` + `scripts/lib/env_guard.sh`) already implements a real,
tested 4-layer ceremony (audited directly, 2026-09-16 and reconfirmed unchanged this mission):

- **LAYER 1** — every CRITICAL risk in `docs/RISK_REGISTER.md` RESOLVED or ACCEPTED (parsed by
  `guard_assert_critical_risks_cleared`, fails closed on any OPEN/PARTIALLY-RESOLVED critical risk).
- **LAYER 2** — `state/LIVE_PILOT_AUTHORISATION` file must exist (owner-created, dated, records
  `commit=<sha>`).
- **LAYER 3** — repo-clean / venv / no-duplicate-process / disk / live-mode / pilot-limits /
  continuous-power invariant checks.
- **LAYER 4** — interactive human must type `START LIVE PILOT` verbatim.

## Mapping onto the mission's 13-step sequence

| # | Step | Existing equivalent | Status |
|---|---|---|---|
| 1 | Strategy evidence PASS | Agent C's evidence gates (external to this script) | Owned elsewhere |
| 2 | Prospective shadow PASS | `forward_paper` shadow mode | Proven, reusable |
| 3 | Paper PASS | `forward_paper` (same engine) | Proven, reusable |
| 4 | Owner explicit live authorization | LAYER 2 | Exists, **see GAP 1** |
| 5 | Verify NAV | Not automated | **Manual step required** |
| 6 | Verify API scope (READ=yes, TRADE=yes, WITHDRAW=no) | Not automated | **Manual step required** |
| 7 | Verify withdrawal permission disabled | Not automated | **Manual step required, exchange-side** |
| 8 | Verify kill switches | LAYER 3 + manual file inspection | Partial |
| 9 | Verify position/order state is zero (local AND exchange) | Not automated | **GAP 2 — see below** |
| 10 | Set strategy hash | Not automated | **GAP 1 — see below** |
| 11 | Set max order notional | `EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT` (printed at LAYER 4) | Exists |
| 12 | Explicit LIVE_ARM action | LAYER 4 typed phrase | Exists |
| 13 | Independent post-arm verification | `launch_live.sh`'s own post-start check is NOT independent (same process); `scripts/watchdog_live.sh` is separate but **alert-only, not a hard gate** | Partial |

## Two gaps found (re-confirmed this mission, not previously fixed)

**GAP 1 — commit binding not enforced — FIXED IN THE ISOLATED WORKTREE, 2026-09-19, NOT YET
APPLIED TO PRODUCTION.** `scripts/lib/env_guard.sh::guard_assert_authorization_binding()` now
binds Layer 2 to commit + risk-config hash + runtime/supervisor hash + a non-secret account
fingerprint; 15 tests prove it rejects the exact real stale production file. See
`AGENT_A_CONTROLLED_SAFETY_INTEGRATION_2026-09-18.md` for full detail. `scripts/launch_live.sh`
itself is not yet edited to call the new function -- that remains separate, owner-facing work.
Original finding preserved below, superseded by the above, not deleted.

**GAP 1 — commit binding not enforced (original finding).** LAYER 2 only checks the authorization file's
*existence* (`[ -f "$AUTH_FILE" ]`). The file records `commit=<sha>`, but nothing compares that
to the CURRENT checkout's HEAD. Re-verified this mission: the on-disk authorization is still
pinned to commit `aa42c3a`, while production HEAD is now `4d2fb71` — a different commit — and
Layer 2 would still pass today. There is no coded analogue of step 10 ("set strategy hash") that
actually blocks arming on a mismatch.

**GAP 2 — no automated zero-position/order-state check before arming.** `execution/position_reconciler.py`
and `execution/close_dedup.py` can answer "is local/exchange state actually zero" but nothing in
the launch sequence calls them before Layer 4's confirmation.

Both gaps are named for the owner to close at their discretion in the production checkout — not
fixed by this mission (see `MICRO_LIVE_ENGINEERING_GAP_FIXES.md` for why: real, reviewable
integration work, not a rushed bundled edit).

## What owner authorization means here

Steps 1–3 (evidence, shadow, paper) are Agent C's domain and are, per the 2026-09-18 sprint
report, **not met by any candidate today**. Steps 4–13 are infrastructure/ceremony and are what
this checklist covers. Clearing every row above is necessary but not sufficient: **live trading
requires both** a candidate that has actually earned steps 1–3 **and** an owner who explicitly,
separately authorizes activation once it has. Neither condition is met as of this sprint.
