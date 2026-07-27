# 04 — Configuration Guide & Exchange Preparation

All settings live in `app/config.py` (`Settings`, pydantic) and are supplied by
environment or `.env`. **The strict launcher overrides `.env` for safety-critical
values** — the safety boundary is the launcher, not the file.

## Mode-critical settings

| Setting | Live v1 value | Notes |
|---|---|---|
| `APP_ENV` | `production` | |
| `FORWARD_PAPER_ONLY` | `false` for live | `true` forces execution off |
| `FORWARD_PAPER_ENABLED` | `true` | paper ledger runs alongside |
| `EXECUTION_ENABLED` | **`false` today** | the single switch that permits orders |
| `EXECUTION_MODE` | `DRY_RUN` until approved | `LIVE` only after sign-off |
| `POSITION_MANAGER_ENABLED` | `true` for live | forced off in strict paper |

`Settings.enforce_forward_paper_only()` guarantees: if `FORWARD_PAPER_ONLY=true` then
`execution_enabled=False` and `execution_mode=DRY_RUN`. Verified 16/16 combinations.

## Risk settings

| Setting | Default |
|---|---|
| `ACCOUNT_RISK_PER_TRADE_PCT` | 0.75 |
| `DEFAULT_LEVERAGE` / `MAX_LEVERAGE` | 5.0 / 5.0 |
| `MAX_OPEN_POSITIONS` | 2 |
| `MAX_DAILY_LOSS_PCT` | 1.5 |
| `HARD_DAILY_STOP_PCT` | 2.0 |
| `WEEKLY_FREEZE_LOSS_PCT` | 7.0 |
| `SYMBOL_COOLDOWN_MINUTES` | 30 |

## Execution settings

| Setting | Default | Purpose |
|---|---|---|
| `EXECUTION_REQUIRE_CONFIRMATION` | `true` | symbol allow-list enforced |
| `EXECUTION_CONFIRM_SYMBOLS` | list | only these may be traded |
| `EXECUTION_MAX_PER_CYCLE` | 1 | orders per scan |
| `EXECUTION_PLAN_LIMIT` | 2 | plans considered per scan |
| `EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT` | 35 | hard ceiling |
| `EXECUTION_MIN_LIVE_NOTIONAL_USDT` | 10 | exchange minimum |

## Market data settings

| Setting | Validation value |
|---|---|
| `WATCHLIST` | `LTCUSDT` |
| `MAX_SYMBOLS` | 1 |
| `BITGET_DEFAULT_GRANULARITY` | `15m` |
| `BITGET_CONFIRMATION_GRANULARITY` | `1h` |
| `SCAN_INTERVAL_SEC` | 60 |

**Timeframe rule.** Bitget accepts minutes lowercase but hours/days/weeks/months
UPPERCASE. Internal values stay lowercase; `api_granularity()` translates at the single
boundary. `1M` (month) is case-sensitively distinct from `1m` (minute). Never add a
second translation table — a partial one caused 742 rejected requests and a wasted run.

## Credentials

| Variable | Live | Strict paper |
|---|---|---|
| `BITGET_API_KEY` / `_SECRET` / `_PASSPHRASE` | required | **blanked by the launcher** |

Requirements: supply via a secret store, never commit, `.env` is gitignored, and no
credential may appear in logs (verified: 0 occurrences across the campaign).

## Exchange preparation — before live v1

Not performed. Each item is a prerequisite, not a completed step:

- [ ] API key with **trade** permission and **withdrawal disabled**
- [ ] IP allow-list configured and verified from the production host
- [ ] Margin mode (isolated), position mode and leverage set per symbol on the account
- [ ] Symbol whitelist matches `EXECUTION_CONFIRM_SYMBOLS`
- [ ] Minimum notional and price/size precision confirmed per symbol
- [ ] Rate-limit budget measured with order + position polling added
- [ ] A throwaway key used first to confirm private-endpoint reachability

## Configuration traceability — known defect

The validation manifest recorded `config_version_hash 17524444…` while the trades were
stamped `d6f53802…`. Git commit matched (`cda8187`), so **code** identity is sound but
**configuration** identity is not reproducible from the manifest. Risk R8. Additionally
the risk engine reads absolute paths outside the commit, so risk behaviour depends on
mutable files.
