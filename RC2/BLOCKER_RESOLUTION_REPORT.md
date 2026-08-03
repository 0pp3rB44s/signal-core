# Blocker Resolution Report

**Date:** 2026-07-28 · **Engine:** FROZEN (0 runtime files changed) · **Suite:** 340 passed

| # | Blocker | Status |
|---|---|---|
| 1 | macOS TCC blocks launchd | **OWNER ACTION REQUIRED** |
| 2 | Supervisor uses legacy launcher | **RESOLVED** |
| 3 | External alerting unproven | **OWNER ACTION REQUIRED** |
| 4 | Host sleep (R2) never investigated | **PARTIALLY RESOLVED** — root cause found, fix needs sudo |
| 5 | Live execution path unvalidated | **OWNER ACTION REQUIRED** |

---

## Phase 1 — what reading first changed

The previous acceptance run proved I had ignored existing documentation. Reading
first this time changed two conclusions materially:

1. **`scripts/com.cgc.tradingbot.plist.template`** records that a launchd checker
   which *backgrounds* the bot fails because **macOS process-coalition cleanup**
   kills the child when the spawning shell exits — *"regardless of `nohup` or
   `setsid` — confirmed directly."* Our `launch_detached.py` uses
   `start_new_session=True` (setsid), so it is subject to exactly this. The RC2
   design differs (a persistent `exec`'d loop that never exits, so the coalition
   survives) — but that distinction is **unproven** and must be tested after
   relocation, not assumed.
2. **`scripts/lib/power_assertion.sh:14-17`** already documented the R2 root
   cause in plain language. See Blocker 4.

Both were in the repository before this session. Neither had been acted on.

---

## Blocker 1 — macOS TCC · OWNER ACTION REQUIRED

**Evidence:** installing the agent from `~/Desktop/...` gives
`launchctl list → - 126 com.cgc.forward` with
`getcwd: cannot access parent directories: Operation not permitted`.

**Relocation is code-safe — verified, not assumed.** A repository-wide scan found
**zero hardcoded absolute project paths** in Python or shell. `risk_manager`
derives `BASE_PATH` from `__file__`; 18 scripts derive `PROJECT_DIR` from
`dirname $0`; the plist uses a `__PROJECT_DIR__` token substituted at install
time. Nothing needs rewriting after a move.

**Delivered:** `deploy/migrate_out_of_tcc.sh` — dry-run by default, **copies
rather than moves** so the original survives. It refuses a dirty tree, a running
bot or supervisor, a TCC-protected destination, and insufficient space; after
copying it verifies git history, HEAD parity, tree cleanliness, key files, `+x`
bits, script syntax and payload sizes.

Dry run passes: payload 4 332 MiB, 33 786 MiB free at `~/cgc/`.

**Not executed.** Relocating the owner's project directory is their decision: it
moves 4.3 GB, invalidates shell/editor state, and orphans tooling keyed to the
current path. Full Disk Access was deliberately *not* recommended — it weakens
security for every process involved.

**Why not RESOLVED:** boot persistence is still unproven. Even after relocation
the coalition-cleanup risk above must be tested, then verified by a real reboot.

## Blocker 2 — Supervisor · RESOLVED

`forward_paper_keepalive.sh:46` restarted through the legacy
`start_forward_paper.sh`, which enforced mode safety but read the ambient `.env`,
so automatic restarts ran at `MAX_SYMBOLS=40 / MAX_OPEN_POSITIONS=4` instead of
the pilot ceilings of 1/1.

Now restarts via `scripts/launch_forward.sh` — the single production entry point.

**Proof (live test):** killed the engine with SIGKILL; recovered in **50 s**;
`logs/forward_paper_keepalive.log` references `launch_forward.sh`;
`state/forward_paper_runtime.state` contains the `commit=` line that only
`launch_forward.sh` writes, and `env_file=.env.forward`.

Repository-wide sweep: the only remaining `start_forward_paper.sh` references are
documentation and two tests. Tests updated to require every keepalive invocation
to be `launch_forward.sh`, plus a new test that the entry point pins
`.env.forward` and never loads `.env.live`.

## Blocker 3 — Alerting · OWNER ACTION REQUIRED

`scripts/alert_config.sh` adds `validate` (names exactly which variables are
missing per provider, without printing values) and `test` (attempts a **real**
external send, printing `DELIVERED` only after the provider returns success).

**Verified it cannot fake success:** `validate` exits 1 —
*"external alerting is NOT operational"*; `test` **refuses to run** rather than
pretending. Owner action: supply provider credentials in `state/alerting.env`.
No agent can or should do that.

## Blocker 4 — Host sleep (R2) · PARTIALLY RESOLVED

**Root cause, from evidence:**

| Finding | Evidence |
|---|---|
| `caffeinate -s` is inert on battery | man page: *"valid only when system is running on AC power"* |
| Host is on battery | `pmset -g batt` → *"Now drawing from 'Battery Power'"*, MacBook Air |
| System-sleep assertion not held | `pmset -g assertions` → `PreventSystemSleep 0` |
| `-i` never blocks lid-close sleep | `power_assertion.sh:14-17`: *"Het dichtklappen van de deksel forceert clamshell-sleep die caffeinate niet kan tegenhouden"* |
| Idle sleep is 1 min on AC **and** battery | `pmset -g custom` |

**Conclusion:** the power assertion cannot keep this host awake on battery or with
the lid closed. This is not a bug in our code — it is the chosen mechanism used
outside its valid envelope. The repository documented the limitation before the
incident.

**Which trigger fired on 2026-07-26 (lid vs battery) is evidence not available** —
the unified log was rotated at the 07-27 boot. The mechanism is established; the
specific trigger is not.

**Delivered:** `guard_assert_sleep_safe()`, wired into `launch_forward.sh`,
reporting `on_battery` / `PreventSystemSleep` / `idle_sleep` and naming the fix.
It **warns by default** rather than aborting — defaulting to abort on a
battery-powered laptop would silently break every automatic restart, the same
failure class this work exists to remove. Production sets `CGC_REQUIRE_AC=1` to
make it a hard gate. Both paths tested.

**Not resolved:** the fix is `sudo pmset -a sleep 0 disksleep 0` plus AC power and
lid open — owner actions. The exposure is now visible instead of silent.

## Blocker 5 — Live execution · OWNER ACTION REQUIRED

Unchanged and untouched: 0 order calls, 0 private endpoints. Validating it
requires placing a real exchange order with real money, which I must not do.
Checklist: `Live_Release_v1/05_DEPLOYMENT_GUIDE.md` Stage 4 plus
`04_CONFIGURATION_GUIDE.md` exchange preparation.

---

## Remaining owner actions

1. **Relocate** — `bash deploy/migrate_out_of_tcc.sh --execute`, rebuild `.venv`,
   run tests, install the agent (expect no exit 126), **reboot and verify**.
2. **Power** — connect AC, keep the lid open, and/or `sudo pmset -a sleep 0
   disksleep 0`; then set `CGC_REQUIRE_AC=1`.
3. **Alerting** — populate `state/alerting.env`, then `scripts/alert_config.sh
   validate` and `test` until `DELIVERED`.
4. **Exchange** — API key with trade-only permission, withdrawal disabled,
   IP allow-list.
5. **Live pilot** — only after 1–4, then create
   `state/LIVE_PILOT_AUTHORISATION` and run `scripts/launch_live.sh`.

## Verdict

The repository is objectively closer to production: one blocker fully resolved
with live proof, one root-caused and made visible, three prepared to the limit of
what an agent may safely do. **Production remains NOT approved** — boot
persistence, alerting delivery and the live order path are all still unproven.
