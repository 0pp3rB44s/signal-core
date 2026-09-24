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

---

## H-AB1 — Agent B weekend alpha sprint (2026-09-24)

**Herkomst**: losse onderzoeksopdracht ("Agent B weekend alpha sprint"), buiten de
officiële H-4D-nummering/prioriteitenlijst van MASTER_PLAN.md. Apart gelabeld
(`H-AB1`, niet `H-4D-*`) zodat het grootboek herleidbaar blijft naar welk
proces dit genereerde. De opdracht noemde voorafgaand "LSRL_V1" (bevroren)
en "Turtle-L" als bestaande context — geen van beide komt voor in deze
repository (geen bestanden, geen journal-vermeldingen); vermoedelijk elders
bijgehouden (bv. Obsidian, buiten deze checkout). Niet aangeraakt, niet
gereconstrueerd, niet aangenomen.

**Databeslissing (eigenaar, 2026-09-24)**: alleen lokale OHLCV
(`data/history/*.json`), geen microstructuur/orderbook/funding/liquidatie-
data beschikbaar in deze checkout. Dekking geverifieerd: 12 symbolen
(ADA, AVAX, BNB, BTC, DOGE, ETH, FIL, LINK, SOL, TRX, UNI, XLM), elk 8800
bars @15m, 2026-04-06 15:15 → 2026-07-07 17:45 UTC, 43 gaps/symbool
(identiek aantal over alle symbolen → vermoedelijk exchange-brede
API-onderbrekingen, niet symbool-specifiek datakwaliteitsprobleem).

**Consequentie voor prior**: dit is exact het datatype (publieke OHLCV) dat
fases 4A-4C al uitputtend heeft getoetst en verworpen. Elke nieuwe kandidaat
op dezelfde databron begint met een verlaagde prior. Dit rechtvaardigt geen
verzwakking van de poorten — het is de reden om extra streng te zijn op
mechanisme-onderscheid t.o.v. de reeds-verworpen families hieronder.

### Kandidatentrechter (outcome-blind — mechanisme-screening vóór enige toets)

| ID | Mechanisme | Payer (wie betaalt de edge) | Fase-1-verdict | Reden |
|---|---|---|---|---|
| **A** | Cross-sectionele relatieve-sterkte-reversal: SOL extreem t.o.v. 11-symbool-basket over 4u keert korte termijn terug richting basket | tragere rotatie-/rebalance-/cross-asset-momentumflow die op relatieve rangorde reageert, niet op absolute prijs | **DOOR** naar pre-registratie | Nieuwe as (cross-sectioneel i.p.v. tijdreeks); geen overlap met verworpen 4A (absolute/tijdreeks-momentum, breakout, continuation, trend/MR onconditioneel) of 4D (orderbook-imbalance, tijd-van-dag, VWAP) |
| B | OHLCV-proxy voor liquidatie-cascade-uitputting (extreme range+volume-z-score → reversal) | gedwongen liquidatie-/marge-call-flow | **VERWORPEN (mechanisme, vóór test)** | Precies het mechanisme dat MASTER_PLAN §Fase C als prioriteit 1 noemt — maar zonder echte liquidatiedata kan een OHLCV-proxy niet onderscheiden worden van het reeds-verworpen "trend/MR onconditioneel" (4A). Hoog risico op impliciete redding van een verworpen familie onder nieuwe naam. Correcte toetsing vereist de Bybit-liquidatiefeed (nog niet in deze checkout) |
| C | Volume-bevestigde range-breakout | doorbraakvolgers | **VERWORPEN (mechanisme, vóór test)** | Vrijwel identiek aan verworpen 4A "breakouts" — geen nieuw mechanisme-argument, zou recycling zijn |
| D | Vol-compressie/expansie ("squeeze") als premium-proxy | — | **VERWORPEN (mechanisme, vóór test)** | Herformulering van verworpen 4A "vol/ATR-percentiel"; geen implied-vol-bron om een echte premium (i.p.v. gewoon regime-persistentie) te onderbouwen |
| F | BTC-beweging leidt SOL met vertraging (cross-asset lead-lag, zelfde exchange) | tragere alt-marktdeelnemers (liquiditeits-/aandachtsasymmetrie t.o.v. BTC) | Overleeft mechanisme-screen; **niet pre-geregistreerd deze cyclus** (tijdbudget) | Plausibel mechanisme maar op 2026-niveau zwaar ge-arbitreerd door cross-asset statarb op sub-bar-latency — reële kans dat 15m-resolutie het effect al mist. Volgende voorgedeclareerde kandidaat als A faalt of tijd overblijft — geen ander vervangt A stilzwijgend |

Kandidaat **A** geselecteerd voor pre-registratie: sterkste combinatie van
mechanisme-onderscheid t.o.v. verworpen families, volledige databeschikbaarheid,
en plausibele persistentie op 15m-resolutie (rotatieflow is trager dan
single-asset microstructuur-arbitrage).

---

## H-AB1-A — Cross-sectionele relatieve-sterkte-reversal SOLUSDT (PRE-REGISTRATIE)

**Status: GEREGISTREERD 2026-09-24, vóór enige uitkomstinspectie.**
**Commit-bewijs: deze pre-registratie wordt gecommit vóór het testscript wordt
uitgevoerd — commit-hash van dit moment is het tijdsanker voor gate A1.**

### Theorie & mechanisme
Correlatie tussen crypto-perps is hoog maar niet instantaan: rotatie-/
rebalance-flow (systematische relative-value-strategieën, cross-asset-
momentumvolgers die op ranking reageren) verwerkt relatieve-sterktesignalen
trager dan directe prijsactie. Wanneer SOL statistisch extreem uitschiet
t.o.v. een basket van 11 gecorreleerde alts over een kort venster, is dat
een over-extensie t.o.v. de trage rotatieflow, niet t.o.v. SOL's eigen
trend — vandaar **REVERSAL**, niet continuation, als geregistreerde richting.
Dit is bewust tegengesteld aan de klassieke (weken/maanden-horizon) academische
cross-sectionele-momentum-literatuur: op intraday-horizon (15m-4u) domineert
in de microstructuurliteratuur short-horizon reversal (liquiditeit/order-
flow-effecten), niet continuation. Contraire uitkomst (continuation wint)
telt als falsificatie van dit mechanisme, niet als "ook goed" — geen
richtingswissel achteraf.

### Universum & instrument
- Verhandeld instrument: **SOLUSDT perpetual (Bitget)** — enige instrument
  waarop wordt gehandeld.
- Basket (voor het signaal, niet verhandeld): de overige 11 symbolen
  (ADA, AVAX, BNB, BTC, DOGE, ETH, FIL, LINK, TRX, UNI, XLM), gelijk gewogen.
  Alle beschikbare symbolen gebruikt (geen selectie/uitsluiting) om
  cherry-picking van de basketsamenstelling uit te sluiten.

### Exacte inputs & signaal
- Timeframe: 15m OHLCV, close-to-close log-returns.
- Lookback W = 16 bars (4 uur), vast, niet geoptimaliseerd.
- Per symbool i op bar t: r_i,t = log(close_t / close_{t-W}).
- z_t = (r_SOL,t − mean(r_basket,t)) / std(r_basket,t)  [std over de 11
  basket-symbolen op hetzelfde tijdstip, cross-sectioneel].
- Drempels: P20 en P80 van de z_t-verdeling, **berekend uitsluitend op
  TRAIN** (zie hieronder) — vaste constanten, niet herberekend in VAL/TEST.

### Causale tijdstempels (geen look-ahead)
Signaal z_t gebruikt uitsluitend closes t/m bar t (sluitingstijd). Entry op
de OPEN van bar t+1 (eerste bar die volledig ná het signaalmoment opent).

### Entry / exit / stop / sizing
- **Entry**: z_t ≤ P20(TRAIN) → LONG SOL @ open(t+1). z_t ≥ P80(TRAIN) →
  SHORT SOL @ open(t+1).
- **Stop**: 1,5 × ATR(14, 15m, SOLUSDT op bar t) tegen de positie.
- **Target**: 2R.
- **Time-stop**: 16 bars (4u) na entry indien stop/target niet geraakt →
  exit op close van die bar.
- **Intrabar-tiebreak**: als stop én target binnen dezelfde bar liggen
  (alleen OHLC beschikbaar, geen ticks) → stop telt als eerst geraakt
  (conservatieve aanname, vooraf vastgelegd).
- **Eén positie tegelijk**: nieuwe signalen tijdens een open positie worden
  genegeerd (one controlled position flow — AGENTS.md-conventie). Dit
  verlaagt N maar voorkomt overlappende, niet-onafhankelijke trades.
- **Sizing**: genormaliseerd naar R (risicofractie is voor deze toets
  irrelevant; een echte implementatie zou risk_manager.py's 1–1,5%-regel
  volgen — hier niet aangeraakt, niet uitgevoerd).

### Kostenmodel (vooraf, gelijk aan projectconventie)
- 12 bps roundtrip taker fee (codebase-breed default, o.a.
  `planner_estimated_roundtrip_fee_bps`, `forward_paper_roundtrip_fee_bps`)
  + 2 bps slippage-buffer (H-4D-1-conventie) = **14 bps totaal**.
  Omgerekend naar R: `fee_r = 0,0014 / stop_pct` (conventie uit
  `scripts/validation_engine.py`).
- Stress-toets (vooraf gepland, Fase 3): 25 bps roundtrip.

### TRAIN / VAL / SEALED TEST (chronologisch, vooraf vastgelegd)
8800 bars/symbool, geen hersplitsing achteraf:
- **TRAIN** = eerste 40% (bar-index 0–3519, ≈ 2026-04-06 → 2026-05-16):
  **uitsluitend** gebruikt om P20/P80-drempels op de z-verdeling te bepalen
  (distributie-eigenschap, geen uitkomstdata/returns na signaal bekeken).
- **VAL** = volgende 30% (bar-index 3520–6159, ≈ 2026-05-16 → 2026-06-11):
  eerste blik op signaal→resultaat — analoog aan DEV in H-4D-1.
- **TEST** = laatste 30% (bar-index 6160–8799, ≈ 2026-06-11 → 2026-07-07):
  verzegeld, één run, analoog aan REP in H-4D-1.
Exacte kalenderdata worden herbevestigd bij uitvoering (bar-index is het
harde anker); geen wijziging van de 40/30/30-verdeling achteraf.

### Poorten (vooraf vastgelegd)

**VAL-poort (go/no-go naar verzegelde TEST — geen bewijspoort op zich)**:
teken conform registratie (netto R > 0 in reversal-richting); N ≥ 30 trades;
dag-geclusterde |t| ≥ 1,5; profit factor > 1,0 ná 14 bps kosten. Alle vier
vereist om naar TEST te gaan. Faalt VAL → stop, verwerp, **geen TEST-run**
(zegel blijft intact voor eventuele toekomstige, materieel andere hypothese).

**TEST-poort (finale bewijspoort voor HISTORICAL_GREEN_CANDIDATE)**: zelfde
teken als VAL én als registratie (geen tekenwissel); N ≥ 30; dag-geclusterde
|t| ≥ 2; profit factor > 1,3 ná 14 bps kosten (economische marge, niet
breakeven); overleeft kostenstress (profit factor > 1,0 bij 25 bps);
maand-tekenconsistentie (3 kalendermaanden in TEST-venster, geen enkele
maand > 70% van totale R). Alle vereist.

### Faalcondities → verwerp permanent, geen redding
Tekenwissel VAL→TEST; N < 30 in TEST (ondermachtig → informatieve
non-conclusie, gedocumenteerd, niet als candidate); profit factor ≤ 1,0 ná
kosten; > 70% van totale R uit één kalendermaand; effect verdwijnt bij
25 bps stress (dan kosten > edge, zelfde les als fase 2-3: −5,28 all-time
PnL op 367 trades).

### Bekende bias-bronnen (vooraf gedocumenteerd)
- Slechts ~3 maanden, één format van marktregime(s) — zelfs bij PASS is dit
  een pilot; volledige acceptatie vereist regimespreiding (meerdere
  maanden/regimes) die deze dataset niet biedt. Expliciet GEEN vrijbrief
  voor live-implementatie zonder aanvullend bewijs (zie GO_LIVE_CHECKLIST
  poort A7).
- Basket-samenstelling is de volledige beschikbare set (geen selectie),
  maar die set zelf is niet vooraf door een symbool-onafhankelijk proces
  gekozen (het is "wat toevallig lokaal beschikbaar is") — resultaat geldt
  conditioneel op deze basket.
- 43 gaps/symbool (exchange-breed, gelijk verdeeld) — niet-random rond
  onderbrekingen; niet verwacht materieel maar niet apart getoetst.
- Kleine sample: TEST bevat ~2640 bars vóór drempelfilter; werkelijke
  trade-N hangt af van hoe vaak P20/P80 geraakt wordt ná de "één positie
  tegelijk"-regel — mogelijk laag. Als N < 30: informatieve non-conclusie,
  géén drempelverlaging om N te redden.

### Macht (vooraf, indicatief)
TRAIN-venster (3520 bars) bepaalt drempels; bij ~20% hit-rate per staart
en gemiddelde R-multiple in de orde van 0,3-0,5R zou N≈30-80 in TEST nodig
zijn voor |t|≥2 bij een matig effect — dit wordt ná drempelbepaling (TRAIN),
vóór VAL-resultaten bevestigd. Als de macht ontoereikend blijkt, wordt dat
gerapporteerd als zodanig, niet verzwegen.

### Script
`research/hab1_relstrength_reversal.py` (te schrijven, reproduceerbaar,
ruwe run-artefacten naar `reports/analysis/` — gitignored, per-run
reproduceerbaar).

**Wat dit NIET is**: geen wijziging aan LSRL, geen wijziging aan productie/
risk_manager.py/position_manager.py, geen live- of forward-paper-config
aangeraakt. Uitkomst is bij PASS hooguit `HISTORICAL_GREEN_CANDIDATE` —
nooit `PROSPECTIVE_GREEN`, `PAPER_GREEN` of `MICRO_LIVE_GREEN`. Live blijft
gebonden aan GO_LIVE_CHECKLIST.md (stand vóór deze toets: A 0/9 · B 0/5 ·
C 1/7 · D 0/5 · E 0/3 → NIET LIVE) en MASTER_PLAN Fase E ("GEBLOKKEERD tot
D-exit").
