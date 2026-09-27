# 03 — Risk Engine

> **The risk engine blocked nothing during the validation run** (`RISK_DECISION`
> PASS 5 / FAIL 0). Its blocking behaviour is proven by unit tests only, never by a
> running system. Risk R7.

## Sizing

| Control | Setting | Default |
|---|---|---|
| Risk per trade | `ACCOUNT_RISK_PER_TRADE_PCT` | 0.75 % of equity |
| Default leverage | `DEFAULT_LEVERAGE` | 5.0 |
| Max leverage | `MAX_LEVERAGE` | 5.0 |
| Max concurrent positions | `MAX_OPEN_POSITIONS` | 2 (validation used 1) |
| Notional cap per live trade | `EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT` | 35 USDT |
| Notional floor | `EXECUTION_MIN_LIVE_NOTIONAL_USDT` | 10 USDT |

Position size derives from risk distance to the stop, then is capped by the notional
ceiling. `PLANNER_NOTIONAL_CAPPED` warnings record every capping event — 15 occurred
during the validation run.

**No compounding and no leverage escalation** are implemented. Leverage is fixed.

## Kill switches

`risk/risk_manager.py:_kill_switch_gate()`:

| Switch | Setting | Default | Effect |
|---|---|---|---|
| Daily soft loss | `MAX_DAILY_LOSS_PCT` | 1.5 % | mode degradation |
| Daily hard stop | `HARD_DAILY_STOP_PCT` | 2.0 % | blocks new entries |
| Weekly freeze | `WEEKLY_FREEZE_LOSS_PCT` | 7.0 % | freezes new entries for the week |
| Symbol cooldown | `SYMBOL_COOLDOWN_MINUTES` | 30 | blocks re-entry per symbol |

Day mode is logged each cycle: `DAY_MODE | mode=GREEN | daily_pnl=... |
consecutive_losses=... | weekly_pnl=...`.

## Gates beyond loss limits

| Gate | Source | Notes |
|---|---|---|
| Expectancy / strategy weighting | `reports/backtests/strategy_expectancy.json` | reduces size or pauses a weak strategy |
| Kill-switch by symbol | expectancy data | pauses a symbol |
| AI-agent coach | `agents_v2/reports/coach_decisions.json` | advisory exposure reduction |
| Mandatory stop | planner | a plan without a stop cannot become executable |

**Known defect (R8-adjacent):** these gates read absolute module-level paths
(`BASE_PATH`, `REPORTS_PATH`, `AGENT_DECISIONS_PATH` in `risk/risk_manager.py`), so the
risk decision depends on mutable files outside the deployed commit. Tests patch them;
production does not. This makes risk behaviour **not reproducible from the commit
alone**.

## Evidence from the validation run

- 5 `RISK_DECISION` PASS, **0 FAIL** — no blocking path was exercised.
- 3 plans blocked at the planner stage (`PLAN_BLOCKED`), not by risk.
- 19 `PLAN_REJECT` and 15 `PLANNER_NOTIONAL_CAPPED` warnings.
- Max 1 open position respected: 3 trades, strictly sequential, never concurrent.
- Mandatory stop present on 3/3 trades.

## What is not proven

- Any kill switch firing in a live system.
- Behaviour when equity moves materially (equity was static at 56.64 throughout).
- Multi-position exposure interaction (`MAX_OPEN_POSITIONS` was 1).
- Recovery of risk state after a mid-day restart.
