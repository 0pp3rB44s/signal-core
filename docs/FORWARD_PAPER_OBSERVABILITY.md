# Forward-paper observability

## Safety boundary

Forward paper is a separate dataset and lifecycle simulator. It consumes only
plans that have already passed the existing planner and risk gates. It does not
instantiate an exchange client, query account state, place orders, or write live
position state. `EXECUTION_ENABLED=false` remains the default.

The namespaces are immutable:

- paper: `data_store/forward_paper_events.jsonl` and
  `data_store/forward_paper_outcomes.csv`;
- live/exchange: existing execution, journal and exchange-truth files;
- backtest: `reports/backtests` and `data/backtests`.

No live or backtest record is imported into paper storage.

## Architecture

`ForwardPaperService` observes executable plans and fresh market snapshots. It
first advances existing paper positions, then opens new paper positions. This
prevents the signal candle from immediately closing a newly created trade.

`ForwardPaperEventStore` is the source of truth. It is append-only, uses one
interprocess lock, fsyncs every event, assigns a contiguous global sequence and
chains every record to the preceding SHA-256 hash. Duplicate `event_id` values
are ignored. Invalid JSON, a broken sequence, wrong dataset, schema mismatch or
checksum mismatch blocks further reads and writes.

`ForwardPaperReconstructor` deterministically derives the outcome CSV from the
eventlog. The CSV and quality report are written by atomic replacement. Trades
without both a valid open and close remain explicitly listed as incomplete and
are excluded from outcomes.

## Event schema version 1

Every JSONL line contains:

- `schema_version`, always `1`;
- `dataset`, always `forward_paper`;
- global `sequence`;
- unique `event_id`, `trade_id` and `plan_id`;
- `event_type` and ISO-8601 UTC `timestamp`;
- typed event `payload`;
- `previous_hash` and `event_hash`.

Supported lifecycle events include `TRADE_OPENED`, `MARK_DECISION`,
`MFE_UPDATE`, `MAE_UPDATE`, `TP_TOUCH`, `SL_TOUCH`, `PARTIAL_EXIT`,
`BREAK_EVEN_ACTIVATED`, `PROFIT_LOCK_ACTIVATED`, `STOP_UPDATED`, `FUNDING`,
`FAILED_CONTINUATION`, `EXIT_REASON_TRANSITION`, `TRADE_CLOSED` and
`PAPER_REJECTED`.

The open payload freezes identity, plan geometry, simulated fill, initial risk,
expected move/RR/costs, spread, liquidity assumption, volatility, score,
strategy features, safe configuration hash and git commit. It never contains
credentials.

## Outcome schema version 1

Each CSV row represents exactly one completed paper trade and includes:

- identity, provenance, timeframe, regime and session;
- planned and simulated entry, original stop and targets;
- initial price/currency risk, explicit `1.0R`, and expected RR/move/costs;
- exit price/time/reason, gross PnL, fees, signed funding, currency/percentage slippage, net PnL
  and result in R;
- holding duration, timestamped MFE/MAE and maximum profit giveback;
- TP/SL touches, BE/profit-lock/failed-continuation flags and partial count;
- deterministic `outcome_hash`.

## Lifecycle assumptions

- Simulated entry uses the fresh snapshot mark; planned entry is retained.
- The configured round-trip fee is split equally over entry and exits.
- Funding defaults to zero and is only non-zero when an explicit `FUNDING`
  event exists.
- Candle handling is pessimistic: current SL is evaluated before TP touches.
- TP partial percentages, fee-adjusted BE and profit-lock threshold reuse the
  existing typed settings. They do not alter live execution behavior.
- Missing market data or critical open fields produces a paper rejection and no
  outcome. Values are never invented.

## Data quality and migration

`reports/forward_paper_data_quality.json` records chain validity, event/trade
counts, incomplete records, duplicate count, deterministic dataset hash and
migration status.

No historical rows were imported: existing execution rows are labeled LIVE,
backtests are simulations with different fill semantics, and no explicit,
reliably attributable forward-paper outcome source exists.

## Operations

Rebuild outcomes and the quality report at any time:

```bash
PYTHONPATH=. .venv/bin/python scripts/rebuild_forward_paper_outcomes.py
```

Reconstruction is idempotent: unchanged events produce identical outcome and
dataset hashes.
