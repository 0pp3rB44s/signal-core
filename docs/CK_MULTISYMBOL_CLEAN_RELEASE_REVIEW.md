# Clean C–K multi-symbol release review

Status: **CODE BLOCKERS FIXED — DEPLOYMENT NOT APPROVED**

This release is based directly on the authorised LIVE/rollback baseline
`cd8671c09df56b238e2c52727f6f54731ab5fac1`. It contains the 14 intended C–K
commits plus small, reviewable remediation commits. It contains no stale `main`
history. The rejected tip `8d538670ffb93b8fd6a6af506580f57b71bf5bdb` is not a
deployment target.

No LIVE stop, restart, checkout, supervisor-pin change, configuration mutation,
exchange order action, merge, or deployment was performed while preparing this
release.

## Owner-approved production scope

The production-LIVE schema, config gate, and exchange gate all require this
exact ordered allowlist:

1. `BTCUSDT`
2. `SOLUSDT`
3. `SUIUSDT`
4. `XLMUSDT`
5. `AVAXUSDT`
6. `DOGEUSDT`
7. `WIFUSDT`
8. `SEIUSDT`
9. `TRXUSDT`

Production LIVE also remains hard-pinned to one open position and one execution
winner per cycle. Risk, leverage, notional cap, TP/SL, scoring, expectancy,
entry criteria, and short-side strategy behavior are not raised or relaxed by
this remediation.

## Second-review blocker verdicts

| Original blocker | Verdict | Evidence |
|---|---|---|
| PR scope and hygiene | **FIXED** | Clean baseline; release-diff scanner covers committed, staged, worktree, untracked, deleted, nested, cache/editor, secret-pattern, large-file and symlink paths. |
| Directional expectancy fall-through | **FIXED** | Only a single symbol+direction provenance record is accepted; missing, `n/a`, malformed, non-finite or ambiguous input is neutral `0.0`; strategy expectancy cannot leak into ranking. |
| Config attestation incomplete | **FIXED** | Exact release SHA, checksum, full Settings schema, exact nine symbols/count, one-position invariants, isolated mode, leverage/risk/notional, BTC-only override absence, explicit BE inputs and redacted output all gate with only `PASS` or `FAIL`. |
| Exchange attestation not fail-closed | **FIXED** | Account flatness, regular and trigger orders, orphan SL/TP, unresolved local intents/checksum and quarantine state are checked; all nine symbols require active metadata, mark, book, spread, precision/minima, isolated long+short leverage, and GET-only plan support. `CONDITIONAL` cannot deploy. |
| BE+fees semantics implicit | **FIXED** | Exchange-confirmed fee has precedence, including a confirmed zero; fallback rates and named spread/slippage/extra components are explicit for production LIVE; Decimal tick rounding proves non-negative expected net for LONG and SHORT. |
| Hygiene expression missed environment variants | **FIXED** | The path classifier rejects every environment-file variant and nested occurrence rather than relying on the old suffix-sensitive expression. |

## BE+fees semantic attestation at entry 100

With the explicitly attested release values (quantity `1`, tick `0.01`):

| Model | LONG target | SHORT target |
|---|---:|---:|
| Historical legacy-only buffer | `100.12` | `99.88` |
| Owner-approved itemised BE+fees | `100.19` | `99.82` |

The itemised result contains opening fee, expected closing fee, spread
allowance, slippage allowance, and extra safety allowance exactly once. Tick
rounding is cost-covering for both directions. The config attestation emits the
same example dynamically from the candidate configuration and fails if either
direction is not cost-covering.

## Validation contract

Required before review handoff:

- full Python test suite;
- Python compilation;
- shell syntax validation;
- whitespace/diff validation;
- release hygiene relative to the exact LIVE baseline;
- clean worktree and exact ancestry check.

Required later, in an explicitly authorised deployment window:

- real secret-safe config attestation against the exact reviewed release SHA
  and owner-approved checksum;
- real credentialed read-only exchange attestation;
- all account checks true and all nine symbol classifications `APPROVED`;
- explicit human approval to merge and deploy.

Until those later gates pass, the correct operational action is no merge, no
deployment, no supervisor change, and no LIVE restart.
