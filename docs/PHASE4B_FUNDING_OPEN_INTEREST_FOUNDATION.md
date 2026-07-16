# Phase 4B — funding and open-interest foundation

## Phase 4A freeze

Phase 4A is frozen at `878356a471fdb228e815638ce4f61e9103b79bf7` with
artifact hash `b3759b5ab034616b7b7e192caa5a593552633d56ed77dea90edd4d0172302286`.
Its conclusion remains `NO REPLICATED ECONOMIC EDGE FAMILY FOUND`. The tree was
clean before branching and Phase 4A changed no production, detector, selector,
risk, planner, execution, paper or live behavior.

## Source feasibility audit

Audit date: 2026-07-16. Primary references were the official Bitget API pages
for historical funding and open interest, followed by Tardis' Bitget Futures
exchange-native archive documentation and metadata endpoint.

| Dataset | Historical endpoint | Requested coverage | Granularity | Limitation | Verdict |
|---|---|---|---|---|---|
| Realised funding | Bitget `/api/v2/mix/market/history-fund-rate` | No | 8h; WIF 4h | Page size 100; only 270 records (WIF 540), approximately 89 days | PARTIALLY AVAILABLE |
| Open interest | Bitget `/api/v2/mix/market/open-interest` | No | Current snapshot | No historical time/range/pagination parameters | UNAVAILABLE FROM BITGET |
| Funding + OI | Tardis `bitget-futures` ticker archive | Partial | Exchange-native ticker updates | Starts 2024-11-08; continuous access requires subscription/API key | REQUIRES EXTERNAL SOURCE |

Bitget funding records are realised settlement values. Current/predicted
funding is a different endpoint and is never written into the realised field.
The Bitget OI response is base-coin size at the response timestamp; it is not a
historical series and was not reused as one.

Tardis captures Bitget's public ticker WebSocket in Tokyo and exposes funding,
OI, mark and index fields. Its metadata confirms all eight requested USDT
symbols from 2024-11-08 onward. The first day of each month is downloadable
without a key, but those isolated days cannot support continuous features or a
chronological replication study. A BTC sample confirmed the normalized schema;
it was not included as research data.

## Acquired funding coverage

The public Bitget endpoint was acquired with stable page ordering, bounded
retries, rate-limit handling, raw pages, canonical records, atomic writes,
hashes and a manifest. Requested coverage is 2024-07-15 through 2026-07-15.

| Symbols | Actual common window | Records per symbol | Interval | Requested coverage | Duplicates / missing intervals |
|---|---|---:|---:|---:|---:|
| ADA, AVAX, BTC, ETH, LINK, SOL, SUI | 2026-04-17 16:00–2026-07-14 16:00 UTC | 265 | 8h | 12.10% | 0 / 0 |
| WIF | 2026-04-17 12:00–2026-07-14 20:00 UTC | 531 | 4h | 12.12% | 0 / 0 |

The largest common funding window is 2026-04-17 16:00 through 2026-07-14
16:00 UTC. There is no locally accessible historical OI window, hence no valid
OHLCV + funding + OI common window. The potential external-source window is
2024-11-08 through 2026-07-15, which omits 116 requested development days.

## Canonical and synchronization contracts

Canonical funding stores exchange, market type, settlement timestamp, realised
rate, inferred interval, retrieval timestamp and raw reference. Predicted rate,
mark and index are separate optional fields. Canonical OI stores raw unit,
contract size/conversion basis, normalized USDT notional where supported,
retrieval timestamp and raw reference.

At synchronization, a funding or OI observation is eligible only at or before
the closed 15m candle close. OI beyond the explicit maximum age is marked stale
and removed from available feature values. No future observation or long-gap
forward fill is permitted.

## Frozen feature and hypothesis contracts

The feature dictionary is frozen before outcomes: funding level/change and
cumulative windows; normalized OI level and 15m/1h/4h/8h/24h changes; price ×
OI, funding × OI, crowding and unwind proxies. Interpretive labels such as
liquidation, new longs and new shorts are prohibited without direct evidence.

Eight primary hypotheses are preregistered with one direction and horizon each:
positive/negative funding mean reversion; price-up/down with rising OI
continuation; price-up/down with falling OI exhaustion; extreme funding with OI
acceleration reversal; and funding normalization with falling-OI unwind. Their
minimum sample is 500 and sign reversal or development/replication magnitude
ratio below 0.25 is a contradiction.

## Analysis gate and conclusion

Outcome analysis, event study, FDR calculation, economic classification and
stability analysis were not opened. Doing so with current OI snapshots,
isolated monthly sample days or fabricated fills would violate the contract.
All corresponding artifacts explicitly say `BLOCKED_DATA_FOUNDATION`.

**NO REPLICATED FUNDING/OI EDGE FAMILY FOUND**

This is a data-availability stop, not evidence that positioning has no edge.
The single next research direction is **add basis/mark-index divergence**,
because Bitget exposes historical mark/index candles directly and this avoids
silently substituting non-Bitget or discontinuous OI data.

## Reproducibility

The canonical funding manifest hash is
`329e9f4bcce403bca38ab66ab6dfda5089f79811f753289983d4dbd90011721a`.
Two feasibility-artifact builds are byte-identical with hash
`5d579c800126fd3f8268e8e5f4a45667f030f461ce137bb0a2103e4108588bc2`.
No credential, `.env`, runtime process or runtime state was accessed.
