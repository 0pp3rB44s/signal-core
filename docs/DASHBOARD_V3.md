# Dashboard v3 — architecture, sources and what is deliberately not supported

Read-only observability. The dashboard has **no control surface**: no start, stop,
order or position mutation endpoint exists, and `tests/test_dashboard_security.py`
enforces that by construction. It never writes trading state.

## Data sources — what is authoritative

| UI field | Source | Authoritative? | Freshness | Failure mode |
|---|---|---|---|---|
| Weekly realized PnL / loss % / freeze | `logs/trade_dataset_v2.csv{,.1}` via `dashboard_v3/core/risk_truth.py` | **YES** — mirrors `RiskManager._weekly_realized_pnl` exactly | 7-day rolling | no file ⇒ `UNKNOWN`, never `0.0` |
| AdaptiveTrend signal per symbol | `data_store/adaptive_trend/shadow_decisions.jsonl` | YES — written by the strategy itself | 8 h warn / 24 h stale | missing ⇒ `UNKNOWN` per symbol |
| AdaptiveTrend scan state | `state/adaptive_trend_scan_state.json` | YES | 8 h / 24 h | missing ⇒ `UNKNOWN` |
| Engine heartbeat | `state/runtime_heartbeat.json` | YES | 10 min / 1 h | missing ⇒ `OFFLINE` |
| Account equity | `state/account_equity.json` | snapshot, not live exchange | 1 h | stale ⇒ `STALE` |
| Positions / orders | exchange panel | live query | on load | timeout ⇒ `UNKNOWN` |
| Daily PnL, breaker | `data_store/trades/daily_learning_report.json`, `state/portfolio_equity_guard.json` | YES | 1 h / 15 min | missing ⇒ `UNKNOWN` |
| Funnel / plans | `data_store/funnel_events.jsonl`, `logs/trade_plans.csv` | YES | 15 min / 1 h | missing ⇒ `UNKNOWN` |

## Weekly risk — why there is exactly one implementation

The panel used to recompute the weekly loss with `is_displayable_close` over
whatever journal rows it held. The kill-switch uses `is_economic_close`, reads the
rotated segment as well as the active file, and falls back to a synthetic identity
when `position_lifecycle_id` is empty. Three concrete divergences followed:

* `is_displayable_close` is a **denylist** — an empty `sync_source` passes. `is_economic_close`
  is an **allowlist** — an empty `sync_source` fails. Displayable is strictly wider, so the
  panel counted rows the kill-switch ignores.
* Skipping `trade_dataset_v2.csv.1` dropped every close that had rotated out.
* Deduplicating on lifecycle id alone double-counted legacy rows that have none.

The errors did not even share a sign, so the operator saw a risk figure that was
not the figure gating the account. `dashboard_v3/core/risk_truth.py` now mirrors the
authoritative path field for field, and the old helper was deleted rather than left
in place — a second implementation is how the drift happened.

## AdaptiveTrend panel

`dashboard_v3/panels/adaptive_trend.py` renders only what the strategy wrote. It
never recomputes momentum or ATR from candles: if the shadow log has no value, the
panel says `UNKNOWN` rather than deriving a number the engine never saw. Frozen spec
values are displayed beside the engine's own `mom` so "distance from threshold" is
meaningful, and `test_spec_values_match_the_frozen_strategy` fails if the strategy is
retuned without the panel following — the panel cannot silently lie about the spec.

The 6H boundary is computed from UTC quarter-days (00/06/12/18).

## Status vocabulary

`HEALTHY` · `STALE` · `DEGRADED` · `BLOCKED` · `OFFLINE` · `UNKNOWN`.

`UNKNOWN` means *no authoritative source was readable*. It is never rendered as a
zero, an empty list or a dash that could read as "fine". A missing weekly dataset
shows `UNKNOWN`, not `0.0%`, because `0.0%` reads as "no loss".

## Deliberately not supported

- **No control actions.** Start/stop/close/cancel are out of scope by design.
- **No risk resets.** Risk is read-only; there is no button that clears a freeze.
- **No derived indicators.** The dashboard visualises values the strategy actually
  uses. It does not invent decoration.
- **No aggressive exchange polling.** Snapshots are preferred; direct queries are
  used only where a snapshot cannot answer the question.
- **No currency conversion.** Values render in the unit present in the source.
