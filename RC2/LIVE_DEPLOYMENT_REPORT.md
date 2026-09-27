# Live Deployment Report — 2026-07-28

# VERDICT: LIVE DEPLOYMENT BLOCKED

One precise, evidenced defect makes live execution unsafe today. No order was placed.

---

## THE BLOCKER — duplicate-order hazard on the live entry path

**A live market entry can be submitted twice, creating a second unmanaged position.**

Three facts, each verified in code:

| # | Fact | Evidence |
|---|---|---|
| 1 | The HTTP retry loop is **method-agnostic** — it wraps `requests.request()` for every method, including order-placing POSTs | `clients/bitget_base_client.py:151` `for attempt in range(1, self.max_request_retries + 1)` |
| 2 | Retried statuses include the **ambiguous** ones: `408` (timeout), `500`, `502`, `503`, `504` — exactly the cases where the exchange may have accepted the order but the response was lost | `bitget_base_client.py:171` |
| 3 | Neither order call site passes an idempotency key | `execution/execution_service.py` contains **zero** occurrences of `client_oid`/`clientOid`; entry at line 526, fail-safe close at line 1082 |

`clients/bitget_order_client.py` **supports** `client_oid` (lines 105, 144-145, 175,
211-212, 230, 242) and forwards it as `clientOid`. The execution service simply never
supplies it.

**Consequence with real money:** entry submitted → exchange fills it → response times
out (408) → transport retries the same POST with no `clientOid` → Bitget has no way to
deduplicate → **two positions**. That breaches the non-negotiable
`MAX_OPEN_POSITIONS=1` limit at the exchange, and the second position may carry no
stop-loss because the protection step runs once per logical entry.

**Phase 8 requirements failed:**

- #1 "an entry cannot be duplicated after timeout or retry" — **FAIL**
- #2 "every order receives a unique clientOid" — **FAIL**
- #3 "ambiguous exchange responses trigger reconciliation, not blind retry" — **FAIL** (it blind-retries)

This is the definition of unmanaged financial exposure. Per the mission's failure
policy, this halts live execution.

**Classification:** order validation failure (live execution path defect).

### Exact minimal fix (specified, deliberately not shipped)

1. Derive a stable idempotency key per logical entry — `plan.plan_id` is already in
   scope at the call site (`plan.symbol` is used on the adjacent line) — and pass it as
   `client_oid` to `place_futures_market_order`. Stable across retries of the *same*
   entry, unique across different entries, so the exchange rejects the duplicate.
2. Exclude order-placing POSTs from the blind retry loop, or on an ambiguous
   status query open orders/positions and reconcile instead of resubmitting.

**Why I did not implement it now:** this is never-executed code on the money path that
cannot be integration-tested without submitting real orders. Shipping an
under-verified change there — a malformed `clientOid` rejects every order, a wrong key
scope suppresses legitimate entries — is worse than reporting it precisely. It needs
implementing against a Bitget testnet or a supervised first order, not in the tail of
a session.

---

## What passed

| Area | Evidence |
|---|---|
| Repository state | `main` @ `4328ad7`, tag `rc2-hardened-platform-7`, tree clean |
| Test suite | 340 passed |
| Credentials present | `.env` has key/secret/passphrase (35/64/8 chars — values never read or printed) |
| Reduce-only protection | `bitget_order_client.py:251` `_verify_reduce_only_close_body()` asserts `tradeSide=close`, `reduceOnly=YES`, market type and non-empty size, logging `REDUCE_ONLY_VERIFY_OK/FAILED` — requirement #9 **PASS** |
| Position ceiling (logic) | enforced twice — `execution_service.py:132` against exchange open symbols and `:277` against local state |
| Mode isolation | `.env.forward`/`.env.live` separation, four live authorisation layers, verified aborting |
| Single entry point | supervisor restarts via `launch_forward.sh` (proven, 50 s recovery) |

## What blocked before the gate could complete

| Priority | Item | Status |
|---|---|---|
| 2 | Relocate outside TCC | **NOT DONE** — tooling ready and dry-run verified (4 332 MiB payload, 33 786 MiB free at `~/cgc`). One command: `bash deploy/migrate_out_of_tcc.sh --execute` |
| 4 | Host availability | **FAIL** — host is on **Battery Power**; `CGC_REQUIRE_AC=1` would correctly abort a live start |
| 5 | Alerting | **DEGRADED** — no provider; `scripts/alert_config.sh validate` then `test` |
| 6 | Live API read-only checks | **NOT RUN** — blocked behind the order-path defect; running them is safe and should precede any order |
| 8 | Order protection | **FAIL** — the blocker above |
| 10 | First live order | **NOT ATTEMPTED** |

## Two things that are mine-not-to-do, regardless of the above

1. **Submitting a real order.** Executing a cryptocurrency trade is a hard prohibition
   for me and does not relax when authorised. The order must be submitted by you.
2. **Entering API credentials.** Not needed here — `.env` already holds them — but I
   will not write credential values into `.env.live`.

Even with the defect fixed, Phase 10 ends as owner action.

## Remaining risks

R1 (boot persistence unverified), R2 (**host on battery now** — the 22.19 h suspension
mechanism is live), R3/R4 mitigated but alerting degraded, R5 unchanged, plus the
duplicate-order defect above. `docs/RISK_REGISTER.md`.

## Constraints still active

`EXECUTION_ENABLED=false`; no `.env.live`; no `state/LIVE_PILOT_AUTHORISATION`;
`launch_live.sh` aborts at layer 1 while Critical risks are open; engine frozen
(0 runtime files changed since `cda8187`).

## Owner actions, in order

1. `bash deploy/migrate_out_of_tcc.sh --execute`, rebuild `.venv`, run tests, install
   the launchd agent, **reboot and verify** (closes R1, unblocks TCC).
2. Connect **AC power**, lid open, `sudo pmset -a sleep 0 disksleep 0`; set
   `CGC_REQUIRE_AC=1` (closes R2).
3. Fix the duplicate-order defect — pass `client_oid` and stop blind-retrying order
   POSTs — with focused tests. **This is the hard gate for live.**
4. Run the read-only authenticated Bitget checks (server time, account, balances,
   position/margin mode, leverage, open positions/orders, symbol precision, min size).
5. Confirm the API key has read+trade only, no transfer/withdrawal.
6. Then, supervised, place the first minimum-size order yourself.
