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

### Amendement databron (2026-07-16, vóór enige testrun)
market_context.csv bleek de orderbook-kolommen nooit te vullen (0 van 79.719
rijen — logger-defect, apart gemeld). Identieke metingen staan als tekst in
de notes van strategy_performance.csv-scanrijen (zelfde producent:
bitget_market_client top-50 merge-depth, zelfde cadans). Bron vervangen,
protocol ongewijzigd. Resultaat: 71.725 gededuplicedeerde snapshots,
40 symbolen, 07-08 15:37 → 07-15 07:48 UTC.

### RESULTATEN (run 2026-07-16, research/h4d1_imbalance_study.py)

| Horizon | DEV n/clusters | DEV bps | t | BH-p | REP n/clusters | REP bps | t |
|---|---|---|---|---|---|---|---|
| 15m | 6523/248 | +2,45 | 2,26 | 0,072 | 6731/207 | +1,15 | 1,24 |
| 1h | 1819/85 | −4,16 | −1,00 | 0,476 | 1883/71 | −2,82 | −0,76 |
| 4h | 504/24 | +4,62 | 0,25 | 0,805 | 567/23 | −19,0 | −1,84 |

### Verdict: **VERWORPEN** (alle drie de vooraf vastgelegde poorten falen)
- Statistisch: geen enkele horizon haalt BH-p < 0,05 in DEV; REP-|t| < 2 overal.
- Replicatie: 4h wisselt van teken; 1h heeft het verkeerde (niet-geregistreerde)
  teken in beide periodes.
- Economisch: het enige teken-consistente signaal (15m: +2,45 → +1,15 bps) is
  ~5× kleiner dan de drempel (8 bps) en ~6× kleiner dan de kosten (14 bps).

### Falsificatie-analyse
Het zwakke positieve 15m-signaal is richting-consistent met de theorie maar
economisch dood; bij deze power (detecteerbaar ~2-3 bps) is dit een
informatieve verwerping, geen power-probleem. Waarschijnlijkste duiding: de
informatie in top-50 notional-imbalance is op 2,5-min cadans vrijwel volledig
ingeprijsd; wat rest is een fluistering onder het kostenniveau.

**Bevroren. Niet recyclen** op deze data-resolutie. Her-opening vereist
fundamenteel andere data (tick/L2-historie of hogere snapshot-cadans) én een
nieuw mechanisme — geen parametervariatie.

Confidence in verwerping: hoog (adequate power, schone pre-registratie).
Restonzekerheid: één marktregime (chop, 8 dagen); effect zou in trend-regimes
kunnen bestaan — her-toetsbaar zodra de archivering meer regimes dekt.

### Onafhankelijke reproductie-audit (2026-07-17)

Herrun van research/h4d1_imbalance_study.py, ongewijzigd, tegen dezelfde
immutabele inputs (logs/strategy_performance.csv*, laatste rij 07-15 07:48:45,
sindsdien geen nieuwe rijen; 15m-candles opnieuw via API — historisch immutabel).
Commando: `python3 research/h4d1_imbalance_study.py`.

| Metric | Gerapporteerd | Gereproduceerd | Δ | Oordeel |
|---|---|---|---|---|
| snapshots / symbolen / bereik | 71.725 / 40 / 07-08 15:37→07-15 07:48 | identiek | 0 | PASS |
| 15m DEV n/cl, bps, t, BH-p | 6523/248, +2,45, 2,26, 0,072 | 6523/248, +2,45, 2,26, 0,0720 | 0 | PASS |
| 15m REP n/cl, bps, t | 6731/207, +1,15, 1,24 | identiek | 0 | PASS |
| 1h DEV n/cl, bps, t, BH-p | 1819/85, −4,16, −1,00, 0,476 | 1819/85, −4,16, −1,00, 0,4764 | 0 | PASS |
| 1h REP n/cl, bps, t | 1883/71, −2,82, −0,76 | identiek | 0 | PASS |
| 4h DEV n/cl, bps, t, BH-p | 504/24, +4,62, 0,25, 0,805 | 504/24, +4,62, 0,25, 0,8046 | afronding | PASS |
| 4h REP n/cl, bps, t | 567/23, −19,0, −1,84 | identiek | 0 | PASS |

BH-rekenwerk onafhankelijk geverifieerd vanuit de t-stats (0,0238×3/1;
0,317×3/2; 0,803×3/3) — klopt, monotonie niet geschonden. Methodologische
checks: entry via bisect_right (strikt ná snapshot, geen look-ahead); exit
alleen op gesloten candles (i+nfwd < len); kwintielgrenzen alleen op DEV;
thinning per symbool non-overlappend; dedupe (symbool, seconde).
Kanttekening: pre-registratiecommit (27537ea, 10:17) ligt slechts 5 min vóór
resultatencommit (0ca49f3, 10:22) — commit-discipline dun, maar de inhoud van
27537ea bevat protocol+succescriteria zonder resultaten, en de exacte
reproductie vanaf immutabele inputs draagt het bewijs.
**Audit-verdict: VERWERPING H-4D-1 BEVESTIGD; geen discrepanties.**

---

## H-4D-2 — Time-of-day / sessie-structuur (PRE-REGISTRATIE)

**Status: GEREGISTREERD 2026-07-16. Nog niet getest.**

- **Theorie**: crypto-perps hebben een vaste dagcyclus (Azië/EU/US-sessies,
  US-equity-open, funding-settlements 00/08/16 UTC). Als liquiditeits- en
  flowcycli systematische drift veroorzaken, is die zichtbaar als
  uur-van-de-dag-conditionele returns.
- **Data**: 1H OHLCV, ≥ 400 dagen, 12 vaste symbolen (BTC, ETH, SOL, BNB,
  XRP, DOGE, LINK, AVAX, ADA, SUI, LTC, DOT — vooraf vastgelegd, geen
  watchlist-conditionering). Gepagineerd op te halen; dekt meerdere regimes
  (bull/bear/chop) — sterker dan H-4D-1 op dit punt.
- **Features**: per-timestamp cross-sectioneel gemiddelde 1H-return (doodt
  cross-correlatie), gebucket naar 24 UTC-uren + 6 vooraf benoemde vensters
  (Azië-open 00-02, EU-open 07-09, US-open 13-15, US-close 20-22, en ±1h
  rond funding 00/08/16).
- **Protocol**: DEV = eerste helft van de periode, REP = tweede helft
  (kalender-split, vooraf). Primaire tests: 24+6 = 30 → BH. Cluster op dag.
- **Succes**: BH-p<0,05 in DEV; zelfde teken + |t|≥2 in REP; |effect| ≥ 4 bps
  per uur bruto (een uur-effect is 1×/dag verhandelbaar met 1 roundtrip →
  economische lat: > 14 bps per trade betekent venster-effecten optellen of
  verwerpen); maand-tekenconsistentie ≥ 65%.
- **Faal**: anders → verwerpen; geen post-hoc venster-shopping.
- **Bias-bronnen**: seizoenaliteit van het sample; DST-verschuivingen (UTC
  gebruiken, equity-open venster ruim nemen); autocorrelatie (dag-clusters).

### Interpretatie-amendement (2026-07-17, vastgelegd en gecommit VÓÓR enige testrun)

De registratie hierboven laat details open; die worden hier ex ante gefixeerd.
Uitvoering: research/h4d2_data.py (databouw+audit), research/h4d2_session_study.py.

1. **Periode vast**: [2024-07-17T00:00Z, 2026-07-17T00:00Z) = 730 dagen.
   DEV = [2024-07-17, 2025-07-17), REP = [2025-07-17, 2026-07-17) (kalenderhelften).
2. **Return**: log(close/open) van de 1H-candle die op uur h opent
   (entry = open uur h, exit = close uur h). Cross-sectioneel gelijkgewogen
   gemiddelde per timestamp; timestamp telt alleen mee bij ≥ 8/12 symbolen
   aanwezig; nooit forward-fill.
3. **De 6 vensters** (elk 2 candle-open-uren): asia_open {0,1};
   eu_open_funding08 {7,8}; us_open {13,14}; us_close {20,21};
   funding_00 {23,0}; funding_16 {15,16}. De registratie telt 24+6=30:
   EU-open (07-09) en funding-08 (±1h rond 08) vallen samen → één venster.
4. **Statistiek**: cluster = UTC-dag van candle-open, CR0-SE, tweezijdige p
   (normale benadering; ≥300 clusters), BH step-up (monotoon) over 30 DEV-tests.
5. **Economische poort**: zelfde teken DEV/REP én min(|DEV|,|REP|) ≥ 4 bps/uur.
   Kostenpoort: verhandelbare constructie = het venster zelf, 1 roundtrip;
   |uursom per trade| > 14 bps (12 taker + 2 slippage). Stress: ×1,5 = 21 bps.
6. **Maand-tekenconsistentie**: ≥ 65% van de kalendermaanden (volle periode)
   heeft maandgemiddelde met hetzelfde teken als het volle-periode-effect.
7. **Falsificatie** (kan alleen verwerpen, nooit redden): leave-one-out en
   leave-two-out op best-bijdragende symbolen; beste maand eruit; regimes
   (bull/bear via BTC-90d-trend, hoog/laag-vol via mediaan 30d realized vol);
   placebo ±3h; entry +1h vertraagd; 10%-trimmed mean; subperiode-verval (4
   kwartalen); DST-split voor US-vensters. NB ex ante: voor klok-effecten is
   het signaal oneindig ver vooraf bekend (klok, geen berekening); de
   +1h-vertraagde test meet randscherpte/placebo, geen signaallatentie.
8. **Datavolgorde**: databouw + kwaliteitsaudit draaien vóór de studie; de
   studie-uitvoer wordt pas daarna berekend. Dit amendement is gecommit
   voordat resultaten bestonden (zie commit-historie).
