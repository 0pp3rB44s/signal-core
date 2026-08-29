# Trading Core V1

A small, deterministic, cost-aware, lookahead-safe backtesting core. It answers
one question: **does a strategy have positive expectancy after realistic
trading costs?** It does not try to find a profitable strategy -- it proves the
machine works honestly with one simple, not-necessarily-profitable baseline.

This package is strictly additive and isolated. It does not import from, run
alongside, or interact with the live production engine (`app/`, `execution/`,
`risk/`, the existing `strategies/` package, `dashboard_v3/`, or the watchdog).
It reuses a few of the production repo's existing, well-tested components
deliberately (listed below) as an external toolbox, and nothing else.

## Layout

```
trading_core_v1/
    config.py            # loads config/trading_core_v1.yaml; optional .env.live reuse
    models.py             # Signal
    data.py                # historical candles, integrity checks, sequential feed
    strategy.py            # abstract Strategy interface
    strategies_v1/           # see "Why strategies_v1, not strategies/" below
        baseline_strategy.py
    backtest.py              # event-driven backtester + metrics + basic edge gate
    risk.py                   # position sizing, separate from strategy logic
    executor.py                # BACKTEST / PAPER / LIVE; LIVE hard-disabled
    journal.py                  # trade journal CSV/JSONL export
    main.py                      # CLI entrypoint
config/
    trading_core_v1.yaml          # parameters only, never secrets
tests/
    trading_core_v1/
research_results/
    <run_id>/                       # config.yaml, metrics.json, trades.csv, metadata.json
```

## Why `strategies_v1/`, not `strategies/`

This repo already has a top-level `strategies/` package used by the live
engine (AdaptiveTrend and others). Putting the new baseline strategy there
would collide with production code and blur the line between "the live engine
imports this" and "this only exists for the isolated backtester." The baseline
lives at `trading_core_v1/strategies_v1/baseline_strategy.py` instead --
clearly scoped under the new package, not reachable from anything that still
imports the top-level `strategies` package.

## Components reused from the existing repo (deliberately, nothing else)

- `app.config.Settings` -- existing pydantic Settings loader, used only to
  construct a Bitget REST client for public candle data. No new settings
  class was written.
- `clients.bitget_rest.BitgetRestClient` / `clients.bitget_market_client` --
  the existing, rate-limited, retrying Bitget REST transport and its
  `api_granularity()` normalizer. `trading_core_v1/data.py` calls the shared
  `_request()` method directly (rather than the higher-level `get_candles()`
  wrapper) only because that wrapper does not expose `startTime`/`endTime`
  pagination -- it only returns the latest N bars. Same auth-free, public,
  rate-limited transport either way (`private=False`; no API keys touched).
- Cost assumptions (`maker_fee_bps=2`, `taker_fee_bps=6`) come from this
  repo's own prior research (`docs/RESEARCH_JOURNAL.md`: *"maker 2 bps and
  taker 6 bps per side"*), not invented here.

Nothing else was imported from the production tree. No strategy, execution,
risk, dashboard, or watchdog code was read into this package, modified, or run.

## How costs are modeled

- `maker_fee_bps`, `taker_fee_bps`, `slippage_bps` are all explicit, config-
  driven values (see `config/trading_core_v1.yaml`).
- Entries and stop-loss exits are modeled as **taker** (market) fills.
  Take-profit exits are modeled as **maker** (limit) fills. This is a stated
  modeling assumption, not a fill-behavior guarantee.
- Slippage always works against the trader (a long entry slips up, a long
  exit slips down; mirrored for shorts).
- Every trade and every metrics report always shows GROSS_PNL alongside
  NET_PNL -- costs are never hidden.

## Execution convention (no lookahead)

A strategy signal is generated using bar *i*'s close
(`Strategy.evaluate(history=candles[:i], current_bar=candles[i])`). The
earliest possible fill is bar *i+1*'s **open** -- never the same bar's close,
since that information would not yet have been tradeable. `trading_core_v1/
data.sequential_feed()` is the only supported way to walk candles, and
guarantees `history` never contains the current or any future bar.

**Same-bar stop/TP ambiguity**: when one candle's high/low range touches both
the stop and the take-profit, OHLC data alone cannot tell you which happened
first. Trading Core V1 always assumes the **stop was hit first** -- the
conservative choice, never whichever is more profitable.

## How risk sizing works

`trading_core_v1/risk.py` is independent of strategy logic:

```
RISK_AMOUNT = EQUITY * RISK_PCT
position_size = RISK_AMOUNT / abs(entry_price - stop_price)
```

If the resulting notional is below `min_notional`, the trade is rejected
outright (`TRADE_REJECTED_MIN_NOTIONAL`) -- risk is never inflated to force a
trade through. V1 scope: `MAX_OPEN_POSITIONS=1` (the backtester only ever
tracks a single open position), no hedging, no pyramiding, no martingale, no
recovery sizing. Leverage is not a risk-module parameter.

## How to run a backtest

```bash
cd /Users/bryonkolkman/cgc/bitget_ai_agent_phase7
.venv/bin/python -m trading_core_v1.main --config config/trading_core_v1.yaml --split ALL
```

`--split` accepts `ALL` (default), `TRAIN`, `VALIDATION`, or `TEST` --
chronological (never shuffled) splits defined in the config's `split:` block,
via `trading_core_v1.data.chronological_split`. This is structure only for
future walk-forward-style work; V1 does not run any optimization across
splits.

Output lands in `research_results/<run_id>/`:
`config.yaml` (exact config snapshot), `metrics.json`, `trades.csv` /
`trades.jsonl`, `metadata.json` (strategy name/version, data range, cost
assumptions). Every result is reproducible from its own folder: re-running
`main.py` with the copied `config.yaml` and the same cached candle data
reproduces the same trades.

## How to add a strategy

Implement `trading_core_v1.strategy.Strategy`:

```python
class MyStrategy(Strategy):
    name = "my_strategy"
    def evaluate(self, history, current_bar) -> Signal:
        ...
        return Signal(timestamp=current_bar.timestamp, symbol=..., side=Side.LONG,
                       stop_reference=..., strategy_name=self.name)
```

No order placement, no exchange calls, no risk sizing, no Telegram calls
inside a strategy -- ever. The SAME implementation is usable for both
backtesting and (future, currently disabled) live execution.

## How `.env.live` is reused

`.env.live` remains the ONLY place for secrets. `trading_core_v1/config.py`
can parse it (`load_env_live` / `apply_env_live_to_environ`) using the same
KEY=VALUE semantics as `scripts/lib/env_guard.sh`'s `guard_load_env`, but no
secret is duplicated into any new file, and none is required for a historical
backtest (Bitget's public candle endpoint needs no API key). It is only
consulted, best-effort, for the optional Telegram lifecycle messages
(`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`) when `telegram.enabled: true` in
`config/trading_core_v1.yaml` -- and a missing/failing Telegram call never
blocks or fails the backtest.

## Why live trading is disabled

`trading_core_v1/executor.py`'s `ExecutionMode.LIVE` path raises
`LiveTradingNotAuthorized` unconditionally -- there is no order-submission
code path in this module at all, by design. Trading Core V1 does not place
orders under any configuration.

## Running the tests

```bash
cd /Users/bryonkolkman/cgc/bitget_ai_agent_phase7
.venv/bin/python -m pytest tests/trading_core_v1 -v
```
