# MicroFlow Scalper v1 — frozen collection specification

Status: **COLLECTOR_ONLY / ZERO ORDERS**. `microflow_scalper_v1` is not an
entry-enabled strategy. Promotion requires at least 100 independent events and
all economic gates in the owner protocol to pass on chronological validation.

## Native feeds

- WebSocket: `wss://ws.bitget.com/v2/ws/public`
- product: `USDT-FUTURES`
- trades: `trade` (price, size, taker side, trade id, exchange timestamp)
- depth: `books5` (five-level snapshot, sequence and exchange timestamp)

No credential file is loaded. The collector imports no client, execution,
planning, risk or strategy module and exposes no order operation.

## Frozen universe

Frozen before MicroFlow outcomes were observed:

`BTCUSDT, ETHUSDT, SOLUSDT, XRPUSDT, DOGEUSDT, BNBUSDT, LINKUSDT,
AVAXUSDT, SUIUSDT, HYPEUSDT, ZECUSDT, NEARUSDT`

Selection used live 24h USDT-futures volume, touch spread, top-five depth and
minimum executable notional. BTC and SOL are controls. ADA was rejected at a
5.38 bps observed spread; TRX and BCH were rejected for low practical
volume/depth versus available alternatives. The largest observed minimum
notional in the retained set was ETH at approximately 19.13 USDT; its 20 bps
underlying stop loss is approximately 0.0383 USDT before costs and requires
approximately 3.83 USDT margin at 5x. These are timestamped observations, not
hardcoded runtime USD limits.

## Frozen candidate baseline

- spec id: `microflow-v1-baseline-20260812`
- spec hash: `34a0fd1efe1e6a7dd8b822e307f124c2a906facbe45185183df7ce6357db9483`
- primary OFI: 5 seconds, absolute threshold 0.25
- top-five notional book imbalance: absolute threshold 0.20
- microprice confirmation: same sign, threshold 0 bps
- persistence: 2 seconds
- stream freshness: at most 1 second; sequence must be valid
- maximum spread: 5 bps
- observed 60-second price range: at least 10 bps
- episode neutralization: 1 second
- post-episode cooldown: 60 seconds
- frozen label geometry: TP 40 bps, SL 20 bps, max hold 10 minutes

This baseline samples research events; it is not a claim of edge. Geometry
sensitivity and execution-method comparison occur only after the event-count
gate, with the bounded matrix from the owner protocol.

## Storage and safety

State, raw trades and candidate events are separate gzip JSONL segment streams.
Segments rotate every five minutes or 20 MB uncompressed, whichever comes
first. Each finalized segment is renamed atomically and recorded in an
append-only manifest with SHA-256, schema, time range, row count, symbols, gap
counts and sequence errors. Default retention is 14 days and writes fail closed
below 5 GB free disk.

The collector handles reconnect/backoff, explicit Bitget text ping/pong,
duplicate trades, out-of-order trade timestamps, non-monotonic book sequences,
stale streams and subscription errors. Any stale or invalid state is ineligible
for a candidate.
