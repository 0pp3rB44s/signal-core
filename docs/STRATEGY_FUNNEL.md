# Strategy Funnel Analyzer

## Scope

De Strategy Funnel Analyzer is een nieuwe, losstaande read-only analysetool. Hij importeert geen bot-, client-, strategy-, risk-, planner-, execution- of forward-papermodule. De tool leest uitsluitend reeds gematerialiseerde JSON-, JSONL- en CSV-snapshots en schrijft twee rapportbestanden.

De analyzer optimaliseert niets, verandert geen beslissingen en voert geen strategiecode uit. Onbekende waarden worden als `null` gerapporteerd. Tellingen uit verschillende datasets worden niet gebruikt om conversiepercentages over de volledige funnel te berekenen.

Uitvoeren:

```bash
python3 -m analysis.run_strategy_funnel
```

Optionele overlapinput:

```bash
python3 -m analysis.run_strategy_funnel \
  --candidate-csv /pad/naar/offline_pre_selector_candidates.csv
```

De optionele candidate-CSV moet minimaal `timestamp` of `candle_timestamp`, `symbol`, `direction` en `strategy` bevatten. De analyzer gebruikt uitsluitend exacte matches op timestamp, symbool en richting.

## Pipelinearchitectuur

```text
publieke marktsnapshot
        │
        ▼
strategie-detector
        │ StrategyCandidate / None
        ▼
select_best_candidate
        │ één kandidaat / None
        ▼
StrategyScorer.score
        │ StrategyScore
        ▼
RiskManager.evaluate
        │ RiskVerdict
        ▼
TradePlanner.build
        │ TradePlan: BLOCKED / EXECUTABLE
        ▼
ForwardPaperService.process
        │ append-only lifecycle-events
        ▼
ForwardPaperReconstructor.reconstruct
        │ complete outcome + quality report
        ▼
afzonderlijke exchange-attributie
```

### Detector

De detectorstage is strategiespecifiek. Er bestaat geen uniforme duurzame eventrecord voor iedere detectoraanroep of detectorreject. Daardoor is `detected` in de huidige output de historische `candidates`-telling uit `reports/backtests/strategy_funnel.json`, niet het aantal gescande candles.

### Selector

- Module: `strategies/strategies/selector.py`
- Functies: `select_best_candidate`, `_hard_filters`, `_selector_score`
- Basisscore: 72
- Momentum pre-armed floor: 70
- Continuation MTF-floor: 74
- Gates: allow/deny, richting/alignment, wick/body, late entry, strategiespecifieke evidence, entry quality, execution penalty, retest en selector score.

Selectorrejects worden tekstueel gelogd, maar niet als structureel event met candle-id, sessie en timeframe opgeslagen. `selector_pass` en `selector_fail` zijn daarom `null`.

### Scoring

- Module: `strategies/scoring.py`
- Functie: `StrategyScorer.score`
- Output: total, breakdown, verdict en reasons
- Configuratie: `STRATEGY_SCORE_GO_THRESHOLD`, `STRATEGY_SCORE_WATCH_THRESHOLD`

De historische funnel gebruikt `candidates_go` als `score_pass`; `score_fail` is `candidates - candidates_go`.

### Risk

- Module: `risk/risk_manager.py`
- Functie: `RiskManager.evaluate`
- Gates: scoreverdict, dag-/week-/consecutive-loss kill-switches, strategy/symbol expectancy, coach/requalification, clusterexposure, costs, Safe Mode score, 1D/4H-oppositie, alignment en sessiereductie.
- Momentum scorefloor: 72
- Continuation: 78 strict of 74 MTF
- Probe-riskmultiplier: 0,5

Riskstatus is in plannen en decision snapshots ingebed en heeft geen zelfstandig structureel event. `risk_pass` en `risk_fail` zijn daarom `null`.

### Planner en executable

- Module: `planning/trade_planner.py`
- Functie: `TradePlanner.build`
- Gates: master entry quality, risk allowed, RR/RR-to-TP1, largest-loss guard, stop/targetgeometrie, TP1-net-edge en minimum notional.
- Geschatte round-trip fee: standaard 12 bps
- Minimale netto edge: standaard 4 bps
- Low-vol minimum RR-to-TP1: 1,30
- Maximaal stop/TP1-ratio: 1,20

`planner_pass` is gelijk aan het aantal historische `plans_executable`. `planner_fail` is `plans - plans_executable`. `executable` gebruikt dezelfde bronwaarde; dit is geen onafhankelijke volgende stage.

### Forward paper en outcome

- Module: `forward_paper/service.py`
- Functies: `ForwardPaperService.process`, `open_trade`, `update_market`
- Reconstructor: `forward_paper/store.py::ForwardPaperReconstructor.reconstruct`
- Gates: EXECUTABLE plan, snapshotmatch, volledige kritieke velden, unieke actieve identiteit en geldige append-only chain.

Forward opens komen uit `TRADE_OPENED` events. Forward closes, wins, losses en BE komen uitsluitend uit complete `dataset=forward_paper` outcomes. Records met een andere dataset worden uitgesloten en als datakwaliteitsfout gemarkeerd.

## Strategieën

### momentum_breakout

- Detector: `strategies/momentum_breakout.py::MomentumBreakoutStrategy.detect`
- Richting: LONG
- Kernvoorwaarden: verse break boven 20-bar high of pre-arm; ≥0,12% displacement; detectorvolume ≥0,90; participation ≥0,75; follow-through ≥0,25; close-position ≥0,55; pullback ≤45 bps; leeftijd ≤3 bars; extension ≤0,60%.
- Detectorrejects: context niet aligned zonder override; geen verse break/bevestiging; level gebroken; late/extended entry; onvoldoende volume/flow/close.
- Configuratie: primaire/bevestigingstimeframe, allow/deny lists, momentumvolume, score en spread.

```text
134 observed candidates
↓
? selector pass
↓
79 score pass
↓
? risk pass
↓
0 planner pass
↓
0 executable
↓
0 forward open
↓
0 forward closed

Afzonderlijke exchange-attributie: 48 trades, 23 wins, 25 losses, 0 BE.
```

### momentum_breakdown

- Detector: `strategies/momentum_breakout.py::MomentumBreakdownStrategy.detect`
- Richting: SHORT
- Kernvoorwaarden: verse break onder 20-bar low of pre-arm; ≥0,12% displacement; detectorvolume ≥0,90; participation ≥0,75; follow-through ≥0,25; close-position ≤0,38; reclaim ≤35 bps; leeftijd ≤2 bars; extension ≤0,60%.
- Detectorrejects: shorts/context niet toegestaan; geen breakdown/failed reclaim; late/extended entry; onvoldoende volume/flow/close.
- Configuratie: timeframes, `ENABLE_SHORTS`, allow/deny, momentumvolume, score en spread.

```text
81 observed candidates
↓
? selector pass
↓
81 score pass
↓
? risk pass
↓
36 planner pass
↓
36 executable
↓
0 forward open
↓
0 forward closed

Afzonderlijke exchange-attributie: 30 trades, 10 wins, 20 losses, 0 BE.
```

### trend_continuation

- Detector: `strategies/strategies/continuation.py::detect_continuation`
- Richting: LONG en SHORT
- Kernvoorwaarden: aligned trend/MTF bridge; EMA20 of shallow pullback; reclaim; volume ≥0,65; vol-rank ≥6; participation ≥0,75; follow-through ≥0,35; SHORT gebruikt strengere voorwaarden.
- Detectorrejects: trend/confirmation, reclaim, pressure of volume/flow onvoldoende.
- Configuratie: timeframes, shorts, allow/deny en score.

```text
43 observed candidates
↓
? selector pass
↓
41 score pass
↓
? risk pass
↓
0 planner pass
↓
0 executable
↓
0 forward open
↓
0 forward closed

Afzonderlijke exchange-attributie: 14 trades, 3 wins, 11 losses, 0 BE.
```

### liquidity_sweep_reversal

- Detector: `strategies/liquidity_sweep.py::LiquiditySweepStrategy.detect`
- Richting: LONG en SHORT
- Kernvoorwaarden: wick door 12-bar pivot; reclaim binnen tolerance; maximaal 6 bars oud; displacement ≥0,12%; volume ≥1,15; participation ≥0,70; follow-through ≥0,25; wickfractie ≥0,25.
- Detectorrejects: pivot/reclaim ontbreekt; sweep te oud; displacement/volume/wick/flow onvoldoende; countertrend zonder sterke uitzondering.
- Configuratie: `SWEEP_*`, timeframes en shorts.

```text
11 observed candidates
↓
? selector pass
↓
7 score pass
↓
? risk pass
↓
0 planner pass
↓
0 executable
↓
0 forward open
↓
0 forward closed

Afzonderlijke exchange-attributie: 1 trade, 0 wins, 1 loss, 0 BE.
```

### low_vol_reclaim

- Detector: `strategies/strategies/low_vol_reclaim.py::detect_low_vol_reclaim`
- Richting: LONG en SHORT
- Kernvoorwaarden: EMA20 retest/reclaim; vol-rank ≤55 strict/≤65 MTF; volume ≥0,20; participation ≥0,75; follow-through ≥0,10; spread ≤5 bps; EMA-afstand ≤2,5%; retestafstand ≤0,85%.
- Detectorrejects: modebevestiging, follow-through, spread, EMA-retest/reclaim of HTF-richting onvoldoende.
- Configuratie: timeframes, shorts, allow/deny, hardcoded detectorspread, score en `PLANNER_MIN_RR_TO_TP1`.

```text
286 observed candidates
↓
? selector pass
↓
286 score pass
↓
? risk pass
↓
0 planner pass
↓
0 executable
↓
0 forward open
↓
0 forward closed

Afzonderlijke exchange-attributie: 278 trades, 91 wins, 187 losses, 0 BE.
```

### adaptive_momentum_continuation

- Entrypoint: `app/runner.py::_build_fallback_candidate`
- Status: disabled fallback; alleen actief bij expliciete allow-list.
- Richting: LONG en SHORT
- Kernvoorwaarden: geen primaire kandidaat; execution-aware score ≥75; entry quality ≥75; alignment plus pressure/expansion/feature-evidence.
- Rejects: niet allow-listed; primaire kandidaat aanwezig; score/entry quality/alignment/features onvoldoende.
- Configuratie: allow/deny, timeframes, shorts en score.

```text
? observed candidates
↓
? selector pass
↓
? score pass
↓
? risk pass
↓
? planner pass
↓
? executable
↓
0 forward open
↓
0 forward closed

Afzonderlijke exchange-attributie: 0 trades.
```

## Datasetgrenzen

| Dataset | Gebruikte velden | Niet toegestaan als bewijs voor |
|---|---|---|
| Backtest funnel 2026-07-13 | candidates, candidate GO, plans, executable | forward/exchange-outcomes |
| Current forward paper | opens, closes, paper wins/losses/BE | historische detectorconversie |
| Internal exchange attribution | exchange trades en resultaat | detector→planner conversie |
| Decision snapshots | uitsluitend datakwaliteit | funnelcounts wegens schema shift |
| Trade funnel report | genormaliseerde PLAN_REJECT-fragmenten | exacte selector/riskstage |

De samengestelde strategieobjecten in JSON bevatten daarom `not_a_single_cohort=true` en een provenance-map per metriek.

## Rejectanalyse

Een `PLAN_REJECT`-record kan zowel blokkerende als contextuele reason-fragmenten bevatten. De analyzer classificeert elk fragment als:

- `BLOCKING_OR_ADVERSE`: tekst bevat een expliciete block, below, disabled, freeze/pause, weak, risk-off of vergelijkbare adverse marker;
- `CONTEXT`: overige reason-fragmenten zoals `risk gate passed` of een bronvermelding.

Frequenties, strategie en symbolen worden behouden. Sessies en timeframes zijn `UNKNOWN`, omdat het bronrapport die velden niet bevat. Numerieke waarden worden naar `<N>` genormaliseerd zodat dezelfde reden kan worden geteld zonder thresholdwaarden te wijzigen.

## Overlap

De huidige structurele datasets bewaren geen complete pre-selector kandidatenverzameling per candle. Daardoor staat iedere overlapcel op `null` en heeft de analyse status `INSUFFICIENT_DATA`. Een waarde `null` betekent onbekend en niet nul overlap.

Wanneer een expliciete offline candidate-CSV wordt meegegeven, telt de analyzer een overlap alleen wanneer timestamp, symbool en richting exact gelijk zijn en twee verschillende strategieën aanwezig zijn.

## Outputschema

`reports/strategy_funnel_report.json` bevat:

- pipeline- en strategiespecificatie;
- per-strategie gecombineerde metriek met provenance;
- datasetgescheiden views;
- rejectanalyse;
- volledige overlapmatrix;
- datakwaliteitsissues;
- tekstuele funnels;
- een deterministische `analysis_hash` zonder generatie-timestamp.

`reports/strategy_funnel.csv` bevat één rij per strategie en datasetview. Daardoor worden backtest, forward paper en exchange niet stil vermengd.

## Bekende meetgrenzen

- Detectorattempts vóór selectie worden niet structureel opgeslagen.
- Selector- en riskpass/fail hebben geen eigen duurzame eventrecords.
- De actuele forward-paperset bevat nog geen trade-event of complete outcome.
- 1.401 van 1.573 decision snapshot-rijen zijn semantisch één kolom verschoven en worden uitgesloten.
- Twee exchange-attributierecords gebruiken geen actieve strategienaam.
- Rejectbrondata bevat geen sessie of timeframe.
- Exchange-, backtest- en forwarddata hebben verschillende perioden en populaties.

## Resterende vragen

1. Welk bestaand duurzaam record vertegenwoordigt formeel een detectorattempt vóór selectorcompetitie?
2. Bestaat buiten de repository een export met structurele selector- en riskevents?
3. Welke exchange-exportrecords kunnen zonder inferentie aan een strategie worden gekoppeld?
4. Welke timestamp moet voor overlap als candle-open versus signal-timestamp gelden?
