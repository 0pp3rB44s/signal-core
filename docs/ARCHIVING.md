# Microstructuur-archivering (observe-only)

Doel: gestructureerde live-archivering van orderbook-, funding- en
liquidatiedata zodat de geblokkeerde onderzoeksfamilies (journal fase-4D
triage: executie-algo's, MM-inventory, orderflow, liquidatie-dynamiek) na
4-8 weken dataverzameling toetsbaar worden. Dit subsysteem plaatst nooit
orders, importeert geen executie-/strategie-code (afgedwongen door
`tests/test_archiving.py`) en gebruikt uitsluitend publieke endpoints —
geen .env of API-sleutels nodig.

## Bronnen en formaten

Alle bestanden: JSONL per UTC-dag, `{ARCHIVE_DIR}/{bron}/{bron}-YYYY-MM-DD.jsonl`,
afgesloten dagen worden ge-gzipt. Elke rij bevat `_k` (dedupe-sleutel) en een
ISO-8601 UTC-timestamp.

### orderbook — Bitget REST merge-depth (top-50), cadans 10 s/symbool
Per record: `ts_utc`, `recv_ts_ms`, `exchange=BITGET`, `product_type`,
`symbol`, `exchange_ts_ms` (update-identificatie; REST levert geen
sequence-id → `seq_available=false`), top-of-book (best bid/ask + sizes),
`spread`/`spread_bps`/`mid_price`, top-15 depth-levels per zijde (ruw),
notional depth per zijde + `imbalance`, band-notionals (±10/±25/±50 bps),
wall-features (`largest_bid_wall`/`largest_ask_wall` met ratio), pressure,
`spread_regime`, en `quality` (OK/DEGRADED/EMPTY, crossed-book, stale-lag,
levelaantallen).

### funding — Bitget REST, poll 5 min + settlements 1×/uur
`funding`: actuele funding rate per poll (dedupe 1 rij/symbool/minuut).
`funding_settlements`: gesettelde waarden via history-fund-rate, dedupe op
(symbool, fundingTime) — authoritative reeks.

### liquidations — Bybit v5 allLiquidation WebSocket (12 symbolen)
**Bitget biedt geen publiek liquidatiekanaal** (v2 public channels:
tickers/candlesticks/order book/trades; gecontroleerd 2026-07-18).
Default-provider is **Bybit** (`ARCHIVE_LIQ_PROVIDER=bybit`): publieke
v5-topic `allLiquidation.{SYM}` voor de 12 onderzoekssymbolen, volledige
feed, per record expliciet `exchange=BYBIT` gelabeld. Client stuurt elke
20 s een tekst-ping (Bybit-vereiste); elk inkomend frame (ook pong/ack)
telt als verbindingsleven voor de healthstatus.

Alternatief `binance` (!forceOrder@arr, marktbreed) is aanwezig maar niet
default: op dit netwerk bleken Binance-WS-pushes uit te blijven (REST
werkt en subscribe-ack slaagt, maar 0 frames op een controle-stream die
elders vele frames/s levert; getest 2026-07-18). Bitget- en Binance-native
liquidaties blijven dus ontbrekende bronnen; Bybit is het gearchiveerde,
eerlijk gelabelde liquidatiesignaal. Velden: symbool, side (Sell = long
geliquideerd), qty, prijs, notional, event-/trade-ts.

## Betrouwbaarheid
- **Reconnects/backoff**: WS exponentieel 1→60 s (reset na 5 min stabiel);
  REST-poller backoff 2→300 s bij aanhoudende fouten.
- **Duplicaten**: dedupe-sleutels per bron; herstart herlaadt de sleutels van
  vandaag uit het dagbestand (crash-herstel zonder dubbele rijen).
- **Missing data**: supervisor logt `SOURCE_STALE` en zet status DEGRADED
  wanneer een bron > 3× interval geen succes heeft.
- **Rotatie/retentie**: dagbestanden; gzip van afgesloten dagen; verwijdering
  na `ARCHIVE_RETENTION_DAYS` (default 90).
- **Disk-guard**: schrijven stopt hard onder `ARCHIVE_MIN_FREE_GB` (default 2).
- **Health**: `{ARCHIVE_DIR}/status.json` elke 30 s (per bron: laatste succes,
  lag, rijen, fouten; plus vrije schijfruimte en pid).

## Configuratie (env, gevalideerd bij start)
| Variabele | Default | Betekenis |
|---|---|---|
| `ARCHIVE_DIR` | `data/archive` | doelmap |
| `ARCHIVE_SYMBOLS` | 12 onderzoekssymbolen | vast universum (H-4D-2/3) |
| `ARCHIVE_ORDERBOOK_INTERVAL_S` | 10 | seconden per volledige ronde |
| `ARCHIVE_FUNDING_INTERVAL_S` | 300 | funding-pollcadans |
| `ARCHIVE_RETENTION_DAYS` | 90 | retentie |
| `ARCHIVE_MIN_FREE_GB` | 2.0 | disk-guard |
| `ARCHIVE_WS_LIQUIDATIONS` | true | WS-bron aan/uit |
| `ARCHIVE_DEPTH_LEVELS` | 15 | opgeslagen levels per zijde |

API-druk: 12 symbolen / 10 s = 1,2 req/s (limiet 20 req/s public); de
archiver gebruikt een eigen rate-limit-statebestand zodat de bot-limiter
onaangetast blijft.

## Verwachte opslag (12 symbolen)
- orderbook: ~110k rijen/dag ≈ 130-160 MB raw ≈ 15-25 MB/dag gz;
- funding: ~3,5k rijen/dag ≈ < 1 MB/dag;
- liquidations: marktafhankelijk, typisch 2-10 MB/dag.
Bij 90 dagen retentie: ~2-3 GB totaal.

## Bediening
```bash
scripts/start_archiver.sh          # start (weigert dubbele start via pid-file)
cat data/archive/status.json       # health
scripts/stop_archiver.sh           # nette stop (SIGTERM)
python3 -m pytest tests/test_archiving.py -q
```
Na merge naar main: archiver stoppen in de tijdelijke werkboom en herstarten
vanuit de hoofdcheckout (zelfde `ARCHIVE_DIR`, data sluit naadloos aan door
dagbestand-dedupe).
