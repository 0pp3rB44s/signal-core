# Bitget AI Agent — Phase 7 Release Candidate

Institutional-style Bitget futures trading engine focused on:

- liquidity sweep reversals
- momentum breakout continuation
- momentum breakdown continuation
- strict risk management
- automatic SL/TP protection
- TP1 → break-even automation
- adaptive compounding position sizing
- live dashboard monitoring
- backtesting + optimization

---

# Active Strategies

## 1. Liquidity Sweep LONG
Bullish liquidity sweep reversal with reclaim confirmation.

## 2. Liquidity Sweep SHORT
Bearish liquidity sweep reversal with rejection confirmation.

## 3. Momentum Breakout LONG
Trend-aligned breakout continuation with:
- volume expansion
- pullback hold
- strong continuation candle close

## 4. Momentum Breakdown SHORT
Trend-aligned bearish continuation with:
- breakdown confirmation
- reclaim failure
- strong bearish continuation close

---

# Current Core Features

## Execution Engine

- live Bitget execution
- automatic SL placement
- automatic TP1 / TP2 / TP3 placement
- TP1 → SL to break-even
- TP2 → SL to TP1
- fail-safe protection repair
- duplicate position protection
- dynamic precision formatting
- reduce-only protection handling

## Risk Management

- max open position guard
- leverage caps
- A+ setup filtering
- alignment confirmation
- momentum continuation filtering
- dynamic compounding sizing
- configurable risk-per-trade

## Dashboard

Local live dashboard includes:

- execution feed
- open positions
- protection status
- latest scans
- strategy plans
- position journal
- bot logs
- market snapshots

The dashboard fails closed unless `DASHBOARD_PASSWORD` is configured. Its
safe default bind address is `127.0.0.1`; exposing it on another interface
requires an explicit `DASHBOARD_HOST` setting and appropriate network controls.

---

# Current Watchlist Focus

Primary volatility focus:

- SOLUSDT
- SUIUSDT
- AVAXUSDT
- LINKUSDT
- WIFUSDT
- ETHUSDT
- INJUSDT
- NEARUSDT
- ARBUSDT
- FETUSDT
- AAVEUSDT
- OPUSDT
- SEIUSDT

---

# Safe Startup

# Quick Bootstrap (New Mac / Fresh Install)

Run full environment bootstrap:

```bash
./scripts/bootstrap.sh
```

This automatically:

- creates `.venv`
- installs dependencies
- creates `.env`
- prepares runtime folders
- makes scripts executable

---

# Runtime Cleanup

Clean local runtime/cache artifacts safely:

```bash
./scripts/clean_runtime.sh
```

This will:

- remove `__pycache__`
- remove `.pyc` artifacts
- remove `.bak/.tmp/.orig` files
- backup runtime state/logs first to `$HOME/bitget_ai_agent_runtime_backups`

# Healthcheck

Check bot, dashboard, dashboard port, HTTP status and recent critical logs:

```bash
./scripts/healthcheck.sh
```

Expected healthy output:

```text
bot: running
dashboard http: up
health status: OK
protection status: OK
desync status: OK
no recent critical agent events
```

---

# Official Operations Runbook

## Official Start Procedure

```bash
./scripts/start_bot.sh official_start
./scripts/start_dashboard.sh official_start
./scripts/healthcheck.sh
```

## Official Stop Procedure

```bash
./scripts/stop_all.sh official_stop
./scripts/healthcheck.sh
```

## Morning Recovery Flow

```bash
./scripts/healthcheck.sh
```

If bot or dashboard is down:

```bash
./scripts/stop_all.sh morning_recovery
./scripts/start_bot.sh morning_recovery
./scripts/start_dashboard.sh morning_recovery
./scripts/healthcheck.sh
```

## Night Restart Flow

```bash
python3 -m py_compile app/runner.py execution/execution_service.py execution/position_manager.py clients/bitget_rest.py risk/risk_manager.py planning/trade_planner.py strategies/scoring.py
python3 scripts/run_backtest.py --validation-only
python3 -m pytest tests -q
./scripts/stop_all.sh night_restart
./scripts/start_bot.sh night_restart
./scripts/start_dashboard.sh night_restart
./scripts/healthcheck.sh
```

## Protection Incident Flow

Trigger markers:

```text
UNPROTECTED
TP_PROTECTION_VERIFY_FAILED
VERIFY_STOP_LOSS_FAILED
ENTRY_PROTECTION_VERIFY_FAILED
FAIL_SAFE_CLOSE_FAILED
```

Immediate rule: check Bitget manually first. If a position has no SL, protect or close manually before restarting anything.

## Exchange Desync Recovery Flow

Trigger markers:

```text
STATE_MISMATCH
POSITION_SYNC_UNCERTAIN
LOCAL_OPEN_NOT_ON_EXCHANGE_SYNCED
RESIDUAL_POSITION_DETECTED
EXCHANGE_CLOSED_TPSL_CLEANUP_FAILED
```

Immediate rule: trust exchange reality first. If Bitget and local state disagree, stop new execution and recover state before continuing.

## Patch Notes / Runtime Lifecycle

Runtime lifecycle events are written to:

```text
logs/runtime.log
```

---

# P1.5 — Bash Command Runbook

Use these commands instead of long ad-hoc terminal pastes. Keep patches small, validate immediately, and never restart the bot after a failed compile.

## 1. Full Healthcheck

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
./scripts/healthcheck.sh
```

Healthy output must include:

```text
bot: running
dashboard http: up
health status: OK
protection status: OK
desync status: OK
no recent protection/desync markers
```

## 2. Core Compile Check

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
python3 -m py_compile \
app/config.py \
app/logger.py \
clients/bitget_rest.py \
execution/state_store.py \
execution/execution_service.py \
execution/position_manager.py \
risk/risk_manager.py \
risk/cooldown_manager.py \
planning/trade_planner.py \
strategies/scoring.py
```

If this fails: do not start or restart the bot. Fix the exact file and compile again.

## 3. Bitget Client Compile Check

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
python3 -m py_compile \
clients/bitget_rest.py \
clients/bitget_base_client.py \
clients/bitget_market_client.py \
clients/bitget_precision.py \
clients/bitget_account_client.py \
clients/bitget_order_client.py \
clients/bitget_tpsl_client.py
```

Use this after any Bitget REST/client split work.

## 4. Night Check / Safe Overnight Start

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
python3 -m py_compile \
app/config.py \
clients/bitget_rest.py \
execution/state_store.py \
execution/execution_service.py \
execution/position_manager.py \
risk/risk_manager.py \
risk/cooldown_manager.py \
planning/trade_planner.py && \
./scripts/stop_all.sh night_check && \
./scripts/start_bot.sh night_check && \
./scripts/start_dashboard.sh night_check && \
sleep 5 && \
./scripts/healthcheck.sh
```

Only leave the bot running overnight if health, protection, and desync are all `OK`.

## 5. Morning Recovery Check

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
./scripts/healthcheck.sh
```

If bot or dashboard is down:

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
./scripts/stop_all.sh morning_recovery && \
./scripts/start_bot.sh morning_recovery && \
./scripts/start_dashboard.sh morning_recovery && \
sleep 5 && \
./scripts/healthcheck.sh
```

## 6. Emergency Stop

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
./scripts/stop_all.sh emergency_stop && \
./scripts/healthcheck.sh
```

Use this when compile fails, state looks corrupt, Bitget desync appears, or protection markers show risk.

## 7. Code Integrity Check After Risky Patch

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
python3 -m py_compile \
execution/position_manager.py \
risk/risk_manager.py \
planning/trade_planner.py \
clients/bitget_rest.py && \
grep -n "class PositionManager\|class RiskManager\|class TradePlanner\|class BitgetRestClient" \
execution/position_manager.py \
risk/risk_manager.py \
planning/trade_planner.py \
clients/bitget_rest.py
```

Expected class mapping:

```text
execution/position_manager.py: class PositionManager
risk/risk_manager.py: class RiskManager
planning/trade_planner.py: class TradePlanner
clients/bitget_rest.py: class BitgetRestClient
```

## 8. State Store / Cooldown Check

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
python3 -m py_compile execution/state_store.py risk/cooldown_manager.py execution/position_manager.py && \
ls -la state && \
ls -la state/snapshots 2>/dev/null || true
```

Use this after P1.4 state/cooldown changes.

## 9. Runtime Lifecycle Log

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
tail -40 logs/runtime.log
```

Use this to verify start/stop reasons such as `night_check`, `morning_recovery`, or `official_start`.

## 10. Recent Protection / Desync Markers

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
tail -500 logs/agent.log | grep -Ei "UNPROTECTED|TP_PROTECTION|VERIFY_STOP_LOSS|ENTRY_PROTECTION|STATE_MISMATCH|POSITION_SYNC_UNCERTAIN|LOCAL_OPEN_NOT_ON_EXCHANGE_SYNCED|RESIDUAL_POSITION|EXCHANGE_CLOSED_TPSL" || echo "no recent protection/desync markers"
```

If any marker appears, do not blindly restart. Check Bitget manually first.

## 11. Recent Candidate / Block Reason Check

```bash
cd ~/Desktop/bitget_ai_agent/bitget_ai_agent_phase7 && \
tail -800 logs/agent.log | grep -Ei "CANDIDATE|TRADEPLAN|PLAN_SUMMARY|BLOCKED|NO_SETUP|CONTINUATION_GATE_SNAPSHOT" | tail -80
```

Use this when the bot is stable but has not entered trades.

## 12. Backup Rule Before Manual Patch

Before manual edits:

```bash
cp path/to/file.py path/to/file.py.bak_$(date +%Y%m%d_%H%M%S)
```

After manual edits:

```bash
python3 -m py_compile path/to/file.py
```

No compile clean means no restart.

---

## 1. Create virtual environment
