# Strategy Funnel Findings

Dit bestand bevat uitsluitend geobserveerde feiten uit de huidige analysetooling. Het bevat geen optimalisatie- of wijzigingsvoorstellen.

## Bronafbakening

- De backtestfunnel is gegenereerd op `2026-07-13T20:54:05+00:00` en bevat vijf primaire strategieën.
- De forward-paperbron bevatte bij generatie nul events en nul outcomes.
- De interne exchange-attributie bevatte 373 unieke gesloten records.
- Backtest-, forward-paper- en exchange-aantallen komen uit verschillende cohorten.
- Een volledig detector→outcome-conversiepercentage is daardoor niet berekenbaar.

## Funnel per strategie

| Strategie | Observed candidates | Score pass | Planner pass | Executable | Forward open | Forward closed | Exchange trades | Wins | Losses | BE |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| momentum_breakout | 134 | 79 | 0 | 0 | 0 | 0 | 48 | 23 | 25 | 0 |
| momentum_breakdown | 81 | 81 | 36 | 36 | 0 | 0 | 30 | 10 | 20 | 0 |
| trend_continuation | 43 | 41 | 0 | 0 | 0 | 0 | 14 | 3 | 11 | 0 |
| liquidity_sweep_reversal | 11 | 7 | 0 | 0 | 0 | 0 | 1 | 0 | 1 | 0 |
| low_vol_reclaim | 286 | 286 | 0 | 0 | 0 | 0 | 278 | 91 | 187 | 0 |
| adaptive_momentum_continuation | onbekend | onbekend | onbekend | onbekend | 0 | 0 | 0 | 0 | 0 | 0 |

- Momentum breakout: 134 van 134 observed candidates werden in de backtestfunnel niet executable.
- Momentum breakdown: 45 van 81 observed candidates werden niet executable; 36 werden executable.
- Trend continuation: 43 van 43 observed candidates werden niet executable.
- Liquidity sweep reversal: 11 van 11 observed candidates werden niet executable.
- Low-vol reclaim: 286 van 286 observed candidates werden niet executable.
- Adaptive momentum continuation ontbreekt in de backtestfunnel.
- Selectorpass/fail is voor alle strategieën onbekend.
- Riskpass/fail is voor alle strategieën onbekend.

## Rejectanalyse

De rejectbron bevat 50 unieke `PLAN_REJECT`-records. Na opsplitsing en normalisatie ontstonden 73 strategie-/reason-fragmenten. Een fragment kan context binnen een rejected plan zijn en hoeft niet zelfstandig de blokkade te hebben veroorzaakt.

### momentum_breakout

- `Negative expectancy detected for strategy momentum_breakout.`: 21 vermeldingen.
- `blocked: long without bullish primary trend`: 20 vermeldingen.
- `HTF regime blocks LONG: <N>D=bearish, <N>H=bearish`: 17 vermeldingen.
- `expectancy-watch: strategy weak but not hard-paused`: 15 vermeldingen.
- `score below Safe Mode minimum: <N> < <N>`: 13 vermeldingen.
- `HTF alignment opposes long setup`: 9 vermeldingen.

### momentum_breakdown

- `expectancy-watch: strategy weak but not hard-paused`: 3 vermeldingen.
- `strategy weighting PROBE: negative expectancy, trading at reduced size`: 3 vermeldingen.
- `momentum-quality blocked: late breakdown entry without strong follow-through`: 2 vermeldingen.
- `LARGEST_LOSS_GUARD`: 1 vermelding.
- `blocked: orderbook risk-off`: 1 vermelding.

### trend_continuation

- `shorts disabled`: 4 vermeldingen.
- `strategy weighting PROBE: negative expectancy, trading at reduced size`: 4 vermeldingen.
- `LARGEST_LOSS_GUARD`: 2 vermeldingen.
- `expectancy-watch: strategy weak but not hard-paused`: 2 vermeldingen.

### liquidity_sweep_reversal

- `blocked: orderbook risk-off`: 1 vermelding.
- `shorts disabled`: 1 vermelding.
- `sweep_directional_pressure_weak`: 1 vermelding.
- `sweep_entry_quality_weak`: 1 vermelding.
- `verdict=NO_GO`: 1 vermelding.

### low_vol_reclaim

- `expectancy-watch: strategy weak but not hard-paused`: 42 vermeldingen.
- `shorts disabled`: 42 vermeldingen.
- `strategy weighting HARD-PAUSE`: 42 vermeldingen.
- `TP1_NET_EDGE below minimum after spread/fees buffer`: 17 vermeldingen.
- `DAY_DEFENSIVE_LOW_VOL_RECLAIM_BLOCK entry_quality`: 15 vermeldingen.
- `DAY_DEFENSIVE_LOW_VOL_RECLAIM_BLOCK pressure_expansion_weak`: 8 vermeldingen.

### adaptive_momentum_continuation

- Geen PLAN_REJECT-fragment met deze strategienaam in de bron.

## Symbool, sessie en timeframe

- Rejectsymbolen zijn beschikbaar en staan per reason in het JSON-rapport.
- De hoogste breakoutreason kwam voor bij ATOM, BTC, ETC, ETH, GALA, LINK, NEAR, SUI, TRX, XLM en XRP.
- De vijf geregistreerde liquidity-sweep adverse fragments betroffen ARBUSDT.
- De rejectbron bevat geen sessieveld.
- De rejectbron bevat geen timeframeveld.
- Sessie en timeframe staan daarom als `UNKNOWN` in plaats van een afgeleide waarde.

## Overlap

- Geen beschikbare structurele dataset bevat alle pre-selector kandidaten per candle.
- De huidige overlapstatus is `INSUFFICIENT_DATA`.
- Alle 15 strategieparen hebben `same_candle_count=null`.
- `null` betekent onbekend; het bewijst geen nul overlap.
- De decision snapshot bevat geen bruikbare multi-strategie-candlepopulatie voor overlapmeting.

## Datakwaliteit

- Forward event-chainstatus: geldig volgens het quality report.
- Duplicate forward event IDs: 0.
- Duplicate forward outcomes: 0.
- Open zonder close: 0.
- Close zonder open: 0.
- Complete forward outcomes: 0.
- Exchange-attributierecords: 373.
- Dubbele exchange-identiteiten volgens de analyzer: 0.
- Exchange-records zonder actieve strategienaam: 2.
- Decision snapshot-rijen totaal: 1.573.
- Semantisch geldige decision snapshot-rijen: 172.
- Semantisch verschoven decision snapshot-rijen: 1.401.
- De verschoven rijen zijn niet gebruikt voor funnelcounts.
- Duurzame detectorattempt-, selectorpass/fail- en riskpass/fail-events ontbreken.

## Reproduceerbaarheid

- De analyzer leest iedere bron tweemaal en sluit een bron uit wanneer de bytes tijdens de read veranderen.
- Iedere bron krijgt een SHA-256 in de source manifest.
- Het JSON-rapport bevat een `analysis_hash` waarbij de generatie-timestamp niet wordt meegerekend.
- De CSV bevat 18 rijen: zes strategieën maal drie gescheiden datasetviews.
