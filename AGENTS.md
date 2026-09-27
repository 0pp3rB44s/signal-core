# CGC TradingBot Agent Rules

## Core Safety Rules
- Never read, display, copy, commit, sync, search, modify, or expose API keys,
  secrets, `.env*` files, tokens, passwords, or private credentials, except for
  the narrowly controlled Runner `.env.live` operation below.
- Never increase leverage, risk limits, position size, daily loss limits, or max open positions without explicit human approval.
- Never remove or weaken stop-loss, take-profit, reduce-only, or order protection logic.
- Never bypass risk_manager.py or execution/position_manager.py safeguards.
- Never make live trading changes directly on main without a pull request.

## Workflow Rules
- Always work in a separate branch.
- Always make small, reviewable patches.
- Always explain what files were changed and why.
- Always prefer fixing root causes over adding quick hacks.
- Always run available tests or static checks before proposing a merge.
- If tests are missing, state that clearly and suggest the smallest useful test.

## TradingBot Priorities
1. Capital protection.
2. TP/SL reliability.
3. Clean dataset logging.
4. Duplicate-close prevention.
5. Rate-limit protection.
6. Dashboard accuracy.
7. Strategy improvement only after execution safety is stable.

## Project Context
This repository is a Bitget AI trading agent. The bot must follow CGC discipline:
- A+ setups only.
- No FOMO logic.
- No revenge trading.
- One controlled position flow.
- Risk first, profit second.

## Forbidden Files
Agents must not read, search, modify, or expose:
- `.env`
- `.env.*`, except the authoritative Runner `.env.live` under the controlled
  exception below
- API key files
- local credential files
- private logs containing secrets

## Controlled authoritative Runner `.env.live` exception

The default prohibition remains fail closed. An agent may inspect or modify
only the authoritative Runner Mac's authoritative repository-root `.env.live`
when every condition below is true:

1. The owner explicitly authorizes `.env.live` inspection or modification in
   the current task.
2. The operation is necessary for a production deployment, strategy cutover,
   execution-mode change, leverage/margin configuration, risk configuration,
   or recovery operation.
3. The Runner host and authoritative checkout are identified with certainty.
4. Secret values are never printed, quoted, logged, returned in chat,
   committed, uploaded, synced, searched, reversibly hashed, or copied into an
   artifact or to another host.
5. Credential fields may be reported only as `PRESENT`/`ABSENT`, or by an
   existing non-secret fingerprint already produced by the production system.
6. Only non-secret settings may be reported, such as execution mode and enable
   flags, strategy allowlists, symbol allowlists, leverage, margin mode,
   position limits, risk percentages, notional caps, and executor identity.
7. Before every write, a timestamped mode-0600 backup is created locally on
   the Runner, outside Git and within an ignored Runner-local backup directory.
8. The write preserves all unmodified credential values, is atomic, and is
   followed only by a redacted diff of changed non-secret keys and effective
   configuration verification.
9. Neither `.env.live` nor its backups may ever be committed, uploaded, pushed,
   synced, or copied to the Work Mac or research artifacts.
10. Credential replacement requires explicit credential-rotation authorization
    in that same task. Ordinary `.env.live` authorization is insufficient.

Allowed under this exception: inspect key names and credential presence; read
and change owner-authorized non-secret settings; preserve credentials unchanged
during an atomic rewrite; and verify effective runtime configuration.

Still forbidden: displaying API keys, API secrets, passphrases, dashboard
passwords, tokens, or other secret values; arbitrary `.env*` access; credential
replacement without explicit rotation authority; secret transport; and any
operation whose Runner identity or target path is uncertain.

Explicit owner authorization can permit this operational Runner write. It can
never authorize secret disclosure, committing or uploading secrets, or
weakening credential protections. Use the repository's controlled Runner env
tooling where available; do not improvise shell reads or rewrites.

## Required Output For Every Agent Task
Every agent must return:
- Summary
- Files changed
- Risk impact
- Tests/checks run
- Remaining concerns
