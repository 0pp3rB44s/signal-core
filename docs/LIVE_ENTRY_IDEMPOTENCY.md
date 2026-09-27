# Live entry orders: idempotency, ambiguity and recovery

Scope: the live entry-order submission path only. Strategy, sizing, leverage,
stop-loss and take-profit logic are unchanged by this design.

Written 2026-07-28, resolving the duplicate-order blocker recorded in `8cdf51e`.

---

## 1. The failure mode this replaces

`clients/bitget_base_client.py` retried requests for **every** HTTP method,
including the order POST, on 408/429/500/502/503/504 and on transport errors.
Neither entry call site supplied a `clientOid`.

With real money that combination fails like this:

```
bot                          Bitget
 |-- POST place-order  ------->|   order accepted, position opened
 |                             |
 |<-- (504 / read timeout) ----|   response lost
 |
 |-- POST place-order  ------->|   SECOND order accepted, SECOND position
```

Bitget had no key with which to deduplicate, so the retry created a second real
position — breaching `MAX_OPEN_POSITIONS`, potentially unprotected.

## 2. The design

### 2.1 One stable identity per logical entry

`execution/order_identity.py` derives the `clientOid`:

```
clientOid = "<bot>-<leg>-<sha256(namespace, bot, strategy, symbol, direction,
                                 leg, plan_id, candidate_id)[:26]>"
e.g. bgai-m-be10f61b427d89d63a8c976aa8      (33 chars, [A-Za-z0-9_-])
```

* **Deterministic** — no wall-clock time, no per-attempt UUID. The same logical
  entry always derives the same value, in-process and after a restart.
* **Distinct** — `candidate_id` is a hash of strategy, symbol, direction and the
  candidate candle-open timestamp, so different intended entries differ.
* **Validated, not assumed** — `validate_plan_identity()` proves `plan_id` is the
  canonical `deterministic_plan_id(candidate_id)` before it is used. If it is
  not, the entry is refused. There is no random fallback: an identity we cannot
  reproduce is an identity we cannot reconcile with.
* **Safe to log** — it is a digest; it carries no credential and no account data.
* **Two legs** — the post-only maker attempt (`-k-`) and the market entry
  (`-m-`) are genuinely different exchange orders, so they carry different ids.

### 2.2 Persist before submit

`execution/order_intent_store.py` writes the intent to
`state/order_intents.json` **before** the first byte goes out: plan id,
clientOid, symbol, side, quantity, order type, leg, strategy, creation
timestamp, submission state, session id.

The write is atomic (temp file → `fsync` → `os.replace`) under the interprocess
lock, and `prepare()` is idempotent by clientOid. A crash anywhere after this
point leaves a record that names the order we may have created.

State model:

| State | Meaning |
|---|---|
| `PREPARED` | persisted, nothing sent |
| `SUBMITTING` | a POST is in flight (or died in flight) |
| `SUBMITTED` | exchange accepted, orderId known |
| `AMBIGUOUS` | transport/5xx failure — the order may or may not exist |
| `ADOPTED` | reconciliation found the order; we took it over |
| `ABSENT` | exchange definitively confirms no such order |
| `REJECTED` | business rejection — no order created (terminal) |
| `NOT_SENT` | never reached the exchange (terminal for that attempt) |
| `FILLED` | resulting position confirmed |
| `PROTECTED` | SL/TP confirmed exactly once (terminal, happy path) |
| `UNKNOWN` | **CRITICAL** — state unresolvable, blocks all new entries |
| `ABANDONED` | resolved without a live order (terminal) |

### 2.3 No blind retries on order creation

`_request(..., allow_blind_retry=False)` marks the two entry endpoints. They are
attempted **once**; the outcome is classified instead of resent:

| Classification | Trigger | Meaning |
|---|---|---|
| `NOT_SENT` | connect timeout, DNS failure, connection refused | provably never reached Bitget |
| `AMBIGUOUS` | read timeout, mid-response failure, 408/429/500/502/503/504 | may or may not exist |
| `REJECTED` | business error code in a 200 response, or a non-ambiguous 4xx | exchange saw it and refused |
| accepted | 2xx with success code | order exists |

A timeout is never silently downgraded to "failed". Everything else — public
market data, safe authenticated reads, the reduce-only close path, TP/SL
placement — keeps its existing retry behaviour.

### 2.4 Reconciliation by clientOid

On `AMBIGUOUS` (and, conservatively, on `NOT_SENT`), the submitter asks the
exchange what happened, via `find_order_by_client_oid()`:

1. `GET /api/v2/mix/order/detail?clientOid=…`
2. `GET /api/v2/mix/order/orders-pending`
3. `GET /api/v2/mix/order/orders-history`

Verdicts:

* **FOUND** → adopt the exchange order: store its `orderId`, carry its fill
  quantity and average price forward, continue to protection. **Never submit
  again.** An order found in a dead state (cancelled/rejected/expired) with no
  position is terminal — a cancelled order is not a lost message.
* **ABSENT** → only when *every* route explicitly reported "no such order", and
  only after a position check confirms nothing was created anyway. This unlocks
  **exactly one** controlled resubmission, reusing the same clientOid.
* **UNKNOWN** → any inconclusive route, any lookup exception, or "no order but a
  live position exists". The intent moves to `UNKNOWN`, a CRITICAL log is
  emitted and **all new entries are blocked** until an owner reconciles. The bot
  never guesses.

Hard ceiling: `MAX_ENTRY_SUBMISSIONS = 2` POSTs per logical entry, and the
second is only reachable through a definitive ABSENT verdict.

### 2.5 Restart recovery

The first LIVE `execute()` of a process reconciles every unfinished intent
before the strategy may create anything (`_entry_guard_reason()` →
`recover_pending_intents()`):

* order **found** + live position **protected** → intent closed as `PROTECTED`;
* order **found** + live position **unprotected** → `UNKNOWN`, entries blocked,
  owner safe-protection workflow required;
* order **found**, no position → retired;
* order **absent** → retired. **The bot never submits a fresh order merely
  because it died before reading a response**, and it never mints a new identity;
* **unresolvable** → entries blocked.

`MAX_OPEN_POSITIONS` continues to be enforced against exchange truth
(`get_all_positions()`) per plan, not against local state.

### 2.6 Protection exactly once

Protection uses Bitget's **position-level** endpoint `place-pos-tpsl`, which
*sets* the position's TP/SL rather than stacking separate orders, so a repeated
attempt reconciles instead of duplicating. On top of that:

* an intent already marked `protection_state=CONFIRMED` skips placement entirely
  (relevant after adoption or restart);
* success marks the intent `PROTECTED`;
* the fail-safe close marks the intent `ABANDONED / CLOSED_OUT`.

**Fixed in passing:** `_fail_safe_close()` called
`place_futures_market_order(trade_side="close")`, but that function drops
`trade_side` and hardcodes `tradeSide: "open"` — so the "emergency close" of an
unprotected position actually **opened an opposite position**. It now uses the
verified reduce-only close path.

## 3. When new entries are blocked

The bot refuses every new entry, for the rest of the cycle and every later cycle,
while any of these hold:

1. an order intent is in `UNKNOWN` state;
2. startup recovery could not reconcile an intent with the exchange;
3. a recovered live position has no confirmed exchange-side protection;
4. the maker leg left the exchange state unknown (the market fallback is
   suppressed — it could duplicate the position).

Log line to grep for: `NEW_ENTRIES_BLOCKED` and
`ENTRY_STATE_UNKNOWN_NEW_ENTRIES_BLOCKED`.

## 4. Owner procedure when exchange state is unknown

1. **Do not restart the bot to clear it.** The block is persisted in
   `state/order_intents.json` and survives restarts by design.
2. Read the blocking record:
   `jq '.data[] | select(.state=="UNKNOWN")' state/order_intents.json`
   Note `client_oid`, `symbol`, `submit_attempts`.
3. In the Bitget UI (or a read-only API call), search order history for that
   `clientOid` on that symbol.
4. Determine ground truth:
   * **Order exists and filled** → confirm the position has SL/TP. If not, place
     protection manually, then set the record's `state` to `PROTECTED`.
   * **Order exists and is open** → cancel it or let it work; set `ADOPTED`.
   * **No such order and no position** → set the record's `state` to
     `ABANDONED`.
5. Edit `state/order_intents.json` only while the bot is stopped (the file is
   checksummed; a mismatched file is quarantined, not silently accepted).
6. Restart. `STARTUP_RECOVERY_COMPLETE | blocked=False` confirms the block is
   cleared.

## 5. Observability

Every stage emits a structured, credential-free line. One logical trade can be
proved to have caused zero, one or multiple exchange submissions by grepping a
single `client_oid`:

```
ORDER_INTENT_PERSISTED                     plan_id, client_oid, symbol, side, size, leg, session
ENTRY_SUBMISSION_ATTEMPT                   attempt=N/2
ENTRY_SUBMISSION_RESULT                    classification=ACCEPTED|AMBIGUOUS|REJECTED|NOT_SENT
ORDER_SUBMISSION_CLASSIFIED                transport-level classification
ENTRY_RECONCILIATION_STARTED
ORDER_LOOKUP_FOUND | _ABSENT | _UNKNOWN
ENTRY_ORDER_ADOPTED                        resubmission_suppressed=True
ENTRY_STATE_UNKNOWN_NEW_ENTRIES_BLOCKED
ENTRY_PROTECTION_RECONCILED
STARTUP_RECOVERY_STARTED | _COMPLETE | _INTENT_RETIRED | _UNPROTECTED_POSITION
```

Count of `ENTRY_SUBMISSION_ATTEMPT` lines for one `client_oid` **is** the number
of exchange submissions for that logical entry.

Never logged: API key, secret, passphrase, signed headers, or private response
bodies (`SensitiveDataFilter.redact` plus a 300-character bound on exchange
messages).

## 6. Evidence required during the first real order

Mocked tests prove code behaviour. They do not prove Bitget's behaviour. The
first real order is owner-operated final verification. Capture:

1. **Before:** `state/order_intents.json` is absent or contains no
   non-terminal record; Bitget shows no open position.
2. **The intent** — `ORDER_INTENT_PERSISTED` appears in the log **before**
   `BITGET_PLACE_MARKET_ORDER`, with the clientOid.
3. **Submission count** — exactly one `ENTRY_SUBMISSION_ATTEMPT` line for that
   clientOid, `attempt=1/2`.
4. **Exchange side** — the order in Bitget's order history carries exactly that
   `clientOid`, and the symbol has exactly **one** position.
5. **Protection** — `ENTRY_PROTECTION_RECONCILED` and the position showing SL
   and TP in the Bitget UI.
6. **Final state** — the intent record reaches `PROTECTED` with
   `submit_attempts: 1`.
7. **Restart proof (optional but recommended)** — stop and restart the bot with
   the position open; expect `STARTUP_RECOVERY_POSITION_PROTECTED` and no new
   order.

If step 3 or 4 shows more than one submission or more than one position, stop
trading immediately and reopen the blocker.
