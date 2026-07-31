# Off-host dead-man switch — design (NOT DEPLOYED)

## The gap this closes

Every monitor in this system runs on the Mac it monitors. That is structurally
incapable of reporting the one failure mode that has actually occurred:

> 2026-07-29, 03:25Z → 06:26Z. The host entered `Low Power Sleep` at 1% battery.
> The engine process survived but was suspended for **3 h 00 m**. `launchd`
> interval jobs do not fire while asleep, so `com.cgc.watchdog` would not have
> run. Nothing was recorded and nobody was told. Recovery required a human to
> plug in the charger.

A host-resident watchdog cannot alert while the host is asleep, powered off, or
off the network. Detecting *absence* requires something outside the host that
expects to hear from it.

## Principle

Invert the direction. Instead of the Mac pushing an alert when something is
wrong, the Mac pushes a heartbeat when things are **right**, and an external
service alerts when that heartbeat **stops**. Silence becomes the signal.

```
  Mac (cgc)                          External service
  ─────────                          ────────────────
  every 60s ── HTTPS GET/POST ──►    record ping, reset grace timer
                                     │
  (host sleeps / dies / offline)     │  grace period elapses
       ✕  no ping                    ▼
                                     alert → Discord   "cgc stopped reporting"
  (host returns)  ── ping ──►        alert → Discord   "cgc recovered"
```

## Payload contract — deliberately minimal

Only these fields ever leave the Mac:

```json
{
  "bot": "cgc-01",                    // anonymous identifier, not an account id
  "ts":  "2026-07-29T12:00:00Z",
  "mode": "LIVE",
  "status": "OK",                     // OK | DEGRADED
  "commit": "27f5c69"                 // optional, short hash
}
```

**Never transmitted:** API keys, passphrases, webhook URLs, balances, equity,
positions, order ids, PnL, symbols, strategy names, or any trading decision.
The external service learns only *that* the bot is alive, never *what it holds*.
HTTPS only; the ping is idempotent and carries no authentication material beyond
the opaque endpoint URL, which is itself the only secret and lives in
`state/alerting.env` alongside the Discord webhook.

## Options compared

| Option | Setup | Recurring cost | External deps | Ops burden | Fails how? |
|---|---|---|---|---|---|
| **Healthchecks.io** (or self-hosted equivalent) | one URL, `curl` in the existing watchdog | free tier covers 20 checks | one SaaS | ~zero | SaaS outage = false alarm |
| **Uptime Kuma** on an external host | deploy container, configure push monitor | VPS ~€4/mo | VPS + container | patching, backups | you now monitor the monitor |
| **Cloud function** (Lambda/Cloud Run + scheduler + store) | write function, schedule, state store, Discord integration | ~free at this volume | 3 cloud services | IAM, deploys, cold starts | most moving parts |
| **Self-hosted VPS endpoint** | write service, TLS, systemd, alerting | VPS ~€4/mo | VPS | full stack ownership | highest |

## Recommendation: Healthchecks.io-style push dead-man

Smallest reliable option by a wide margin. It requires **one line** added to the
watchdog that already runs every 60 seconds:

```bash
# only ping when the engine is genuinely healthy — a ping must mean "all good",
# never merely "this script executed"
if [ "$FINDINGS" -eq 0 ] && [ -n "${DEADMAN_URL:-}" ]; then
  curl -fsS -m 10 --retry 2 -o /dev/null "$DEADMAN_URL" || true
fi
```

Configuration lives in `state/alerting.env` (mode 600, gitignored):

```
DEADMAN_URL=https://hc-ping.com/<uuid>
DEADMAN_GRACE_SECONDS=300
```

Grace period **300 s** — five missed 60 s pings. Long enough to absorb a slow
scan or a brief network blip, short enough that a sleeping host is reported in
under six minutes rather than three hours. The service sends its own recovery
notification when pings resume, satisfying the recovery requirement without any
extra code here.

**Deliberate design choice:** ping only on `FINDINGS == 0`. A dead-man that
fires merely because a script ran would stay green while the engine was dead.
Coupling the ping to a clean bill of health means a degraded-but-awake host also
raises the external alarm.

### Residual risks of this option

- A Healthchecks.io outage produces a false "bot down" alert. Acceptable: false
  positives on a liveness channel are far cheaper than the false negative that
  already cost 3 hours.
- The Mac's own network can fail while the engine is fine — also a false
  positive, and also acceptable for the same reason.
- The endpoint UUID is a bearer secret. Anyone holding it can suppress the alarm
  by pinging on your behalf. It must live only in `state/alerting.env` at 600.

## Not deployed

No account has been created, no URL configured, no code added to the watchdog.
This requires an owner decision and an owner-held credential. When you want it,
the change is the four lines above plus two config keys.
