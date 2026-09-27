# Known limitations — Release Candidate 1

What RC1 does **not** demonstrate. Every entry is evidence-backed; where evidence is
absent this is stated explicitly rather than inferred.

Sources: `validation_72h/archive/` (`reports/CLOSEOUT_REPORT.md`),
`docs/FORWARD_PAPER_VALIDATION.md`, `docs/RISK_REGISTER.md`.

Append-only.

---

## L1 — The 72-hour reliability criterion was not met

Effective scanning time was **20.56 h — 28.5 %** of the requirement. Process lifetime
42.75 h, of which 22.19 h suspended; the run then ended by host reboot and lay dead
7.82 h. **No 72-hour reliability claim can be made from this evidence.**

## L2 — The live order path has never been executed

0 order calls and 0 private endpoints across the entire campaign, by design. Order
placement, rejection handling, partial fills, real slippage and exchange error codes are
**completely unvalidated**. Everything proven about execution concerns the *simulated*
path only.

## L3 — Take-profit and partial-exit paths unexercised in live conditions

No take-profit was reached. `TP_TOUCH`, `PARTIAL_EXIT` and `TRAIL_UPDATE` did not occur
during the run. They are covered by integration tests but not by live evidence.

## L4 — Trade sample has no statistical meaning

2 closed trades. Aggregate gross +0.04365000, fees 0.05240619, net **−0.00875619**.
Both were profit-lock stops. This says nothing about expectancy and must never be cited
as performance. The platform position remains: **no proven edge** (see
`docs/RESEARCH_JOURNAL.md`).

## L5 — Risk-engine blocking never triggered

`RISK_DECISION` PASS 5 / FAIL 0 in the window. The gate's blocking behaviour is proven
only by unit tests, never in a running system.

## L6 — Host-level reliability is unproven and known-defective

Sleep prevention failed (22.19 h suspension despite an active power assertion) and no
boot-persistent supervision exists. Both are CRITICAL entries R1/R2.

## L7 — Monitoring can fail silently

The observation loop stopped 22.7 h before the run ended and nothing detected it.
Absence of alerts is not evidence of health.

## L8 — Configuration identity is not traceable from the manifest

`manifest.json` records `config_version_hash 17524444…`; trades are stamped
`d6f53802…`. The git commit does match (`cda8187`), so code identity is sound but
configuration identity is not reproducible from the manifest alone.

## L9 — Funnel counters disagree with the event store

`FORWARD_PAPER_LINK` PASS = 3 versus `EXECUTABLE_DECISION` PASS = 2. Unexplained. The
event store is internally consistent; the funnel counter is not trustworthy for exact
reconciliation.

## L10 — Evidence not available

Stated explicitly rather than estimated:

- **CPU and memory usage** for the run — never sampled; process table lost at reboot.
- **OS sleep/wake records** for the 22.19 h gap — unified log rotated at the
  2026-07-27T10:54:07Z boot.
- **Whether the `caffeinate` assertion process was alive** during the suspension — no
  liveness logging exists for it.
- **Fine-grained strategy rejection categories** (expectancy / AI-gate / pressure /
  momentum / continuation) for the run window — only `NO_DETECTION`, `NOT_SELECTED` and
  `PLAN_BLOCKED` reason codes were emitted.

## L11 — One position was left unresolved

`paper_2b760b2f13c268cf9af7` remained open at freeze
(`unresolved_open_trade_count: 1`). Its lifecycle cannot be closed or reconciled.

## L12 — Single symbol, single strategy, single timeframe

LTCUSDT / `low_vol_reclaim` / 15m-1h only. Nothing is demonstrated about multi-symbol
scheduling, cross-symbol exposure or other detectors under load.

## L13 — Archiver throughput threshold undefined

Pre-flight health-check case G was never implemented; archive degradation would not be
flagged. The archiver also stopped at the reboot and did not restart.
