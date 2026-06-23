# CGC TradingBot Agent Rules

## Core Safety Rules
- Never touch, print, copy, commit, or expose API keys, secrets, .env files, tokens, passwords, or private credentials.
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
Agents must not modify or expose:
- .env
- .env.*
- API key files
- local credential files
- private logs containing secrets

## Required Output For Every Agent Task
Every agent must return:
- Summary
- Files changed
- Risk impact
- Tests/checks run
- Remaining concerns
