# RESEARCH JOURNAL — permanent grootboek van hypothese-families

Doel: een statistisch valide marktinefficiëntie vinden die onafhankelijke
replicatie overleeft. Verwerpen is een geldig en verwacht resultaat.
Regels: pre-registratie vóór testen; geen optimalisatie na uitkomsten; geen
cherry-picking; economische significantie > statistische significantie.

Dit bestand is git-tracked (permanent). Ruwe run-artefacten staan in
`reports/analysis/` (gitignored; per-run reproduceerbaar via scripts).

---

## GROOTBOEK — afgesloten families

| Fase | Familie | Verdict | Bewijs |
|---|---|---|---|
| 2-3 | strategie-niveau tests (scalp, momentum, continuation, reclaim) | VERWORPEN | 367 live trades, exchange-truth; reports/analysis/phase2-3 |
| 4A | OHLCV-directioneel (breakouts, continuation, momentum, vol/ATR-percentiel, range-escape, trend/MR onconditioneel) | VERWORPEN | dev-resultaten verdwenen/keerden om in replicatie; runs 1-6 |
| 4B | Funding / Open Interest | GEBLOKKEERD/VERWORPEN | historische OI onbeschikbaar; funding partieel; geen betrouwbare positioning-edge |
| 4C | Basis / mark-index divergentie | VERWORPEN | 2 jaar gesynchroniseerd, BH-gecorrigeerd, geen economisch betekenisvolle edge |
| — | Sweep-and-reverse gemechaniseerd (1H/4H, met/zonder HTF-filter) | VERWORPEN | consistent ≈ −0,3R over alle doorsnedes (sessie 2026-07-13) |

Verworpen families worden niet gerecycled zonder aantoonbare methodologische
fout in het oorspronkelijke onderzoek.

---

## FASE 4D — DATA-TRIAGE van de prioriteitenlijst (2026-07-16)

Beschikbare activa:
- **Orderbook-snapshots (in-house)**: logs/market_context.csv + 10 rotaties,
  ~82k snapshots, 2026-07-08 → heden, ~2,5 min cadans per watchlist-symbool
  (28→40 symbolen). Velden o.a. spread_bps, orderbook_imbalance, wall-ratios.
  Definitie imbalance (clients/bitget_market_client.py): notional-gewogen
  (bid−ask)/totaal over top-50 merge-depth levels.
- **OHLCV**: onbeperkt vers op te halen (15m/1H/4H/1D, gepagineerd).
- **Funding**: partieel (4B). **Historische OI**: niet. **Historische L2/ticks**: niet.
- **Liquidaties historisch**: niet beschikbaar via Bitget public API.

| Prioriteit | Familie | Status |
|---|---|---|
| 1 | Microstructure imbalance | **TOETSBAAR NU** (in-house snapshots) → H-4D-1 |
| 2 | Liquidity voids | deels (candle-gaps proxy); L2-historie ontbreekt |
| 3 | Liquidity sweeps | strategie-vorm verworpen; event-study-vorm mogelijk, lage prior |
| 4-5 | Auction theory / volume profile | grof benaderbaar (1m OHLCV), beperkte historie |
| 6 | VWAP-gedrag | toetsbaar (1m/15m data) |
| 7-9 | Executie-algo's / MM-inventory / orderflow | GEBLOKKEERD: vereist tick/L2-historie → start live archivering |
| 10 | Cross-exchange lead-lag | deels: 1m klines Binance↔Bitget publiek; echte effect leeft sub-seconde |
| 13 | Liquidatie-dynamiek | GEBLOKKEERD historisch; live WS-archivering mogelijk |
| 15-16 | Time-of-day / sessie-overgangen | TOETSBAAR (OHLCV) → kandidaat H-4D-2 |
| 17-19 | Regime/HMM/Bayesiaans | toetsbaar op OHLCV; hoog overfit-risico, strenge protocollen |

Aanbeveling parallel aan al het onderzoek: **live archivering starten** van
orderbook (dieper + hogere cadans), liquidaties (WS) en funding, zodat de
geblokkeerde families over 4-8 weken toetsbaar worden.

---

## H-4D-1 — Orderbook-imbalance → forward return (PRE-REGISTRATIE)

**Status: GEREGISTREERD 2026-07-16, vóór enige test. Resultaten: nog geen.**

### Theorie & mechanisme
Persistente notional-imbalance in het zichtbare boek weerspiegelt netto
inventory-/informatiedruk. Als quote-aanpassing traag is t.o.v. staande druk,
voorspelt bid-zware imbalance positieve korte-horizon drift (klassiek
microstructuur-resultaat in equities/futures; onbekend of het op 2,5-min
snapshots in crypto-perps overleeft). Richting (pre-registered): POSITIEF
(bid-zwaar → omhoog). Contrariaanse uitkomst telt als falsificatie van dit
mechanisme, niet als "ook goed".

### Data & features
- Snapshots: alle market_context-rotaties, 2026-07-08 → 2026-07-16.
- Signaal: `orderbook_imbalance` ∈ [−1,1] (definitie hierboven, top-50).
- Forward returns: uit verse 15m-candles (API), entry = OPEN van de eerste
  15m-candle die volledig NA het snapshot-tijdstip opent (geen look-ahead);
  horizonnen 15m / 1h / 4h (close van candle N vs entry-open), log-returns.

### Protocol
1. Thinning: per symbool max 1 snapshot per horizon-venster (non-overlapping).
2. Kwintielen van imbalance bepaald op DEV-verdeling; Q5−Q1 spread per
   horizon; per-timestamp clustering (cross-sectionele afhankelijkheid).
3. DEV = 2026-07-08 t/m 2026-07-11; REP = 2026-07-12 t/m 2026-07-16
   (chronologisch, vooraf vastgelegd, geen hersplitsing).
4. Primaire tests: 3 (één per horizon). BH-correctie binnen deze familie.
   Secundaire cuts (spread<mediaan; wall-ratio; per-symbool) alléén ter
   robuustheid als primair slaagt — nooit als redding.

### Succescriteria (vooraf)
- Statistisch: BH-gecorrigeerde p < 0,05 in DEV én zelfde teken in REP met
  |t| ≥ 2 (cluster-robuust).
- Economisch: Q5−Q1 ≥ 15 bps bruto @1h, of ≥ 8 bps @15m mét teken-
  consistentie ≥ 70% van de dagen. Daaronder: economisch dood, verwerpen.
- Stabiliteit: teken consistent in ≥ 6 van 8 dagen; niet gedreven door ≤ 2
  symbolen (leave-two-out) of één dag.

### Bekende bias-bronnen (vooraf gedocumenteerd)
- Watchlist-selectie: symbolen staan in de log ómdat ze volume/move hadden
  (conditionering). Resultaat geldt dan ook alleen conditioneel op de
  watchlist — dat is óók de populatie waarop gehandeld zou worden.
- Bot-downtime-gaten (niet-random: crashes/restarts/reboot 07-13).
- Eén marktregime (chop/bearish grind): zelfs bij PASS is dit een pilot;
  volledige acceptatie vereist hertest in ≥ 2 andere regimes (verzamelen
  loopt door). Dit is expliciet GEEN vrijbrief voor implementatie.
- Snapshot-cadans 2,5 min ≠ tick; effecten die sneller leven zijn onzichtbaar.

### Power (vooraf)
~82k snapshots → na thinning ~15-20k obs @15m; ~770 tijdclusters.
Detecteerbaar effect bij 80% power: ~2-3 bps — ruim onder de economische
drempel, dus een "geen effect"-uitkomst is informatief, geen power-probleem.

### Faalcondities → verwerp permanent
Tekenwissel DEV→REP; spread onder economische drempel; concentratie in ≤2
symbolen of 1 dag; effect verdwijnt na uitsluiten spread>mediaan-snapshots
(dan is het een illiquiditeits-artefact, geen edge).

### Executie-aannames
Taker roundtrip 12 bps + 2 bps slippage-buffer. Signaal op 2,5-min snapshot
is uitvoerbaar (geen tick-latency vereist). Turnover @15m-horizon is hoog →
economische drempel daar navenant streng.
