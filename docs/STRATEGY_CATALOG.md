# Strategy Catalog

## Scope en bewijsniveau

Deze catalogus beschrijft de strategiecode en de reeds aanwezige resultaten op de auditbranch `audit/strategy-catalog`. Tijdens de inventarisatie bleven de strict forward-paper bot en de uurmonitor draaien. Er zijn geen processen gestart, gestopt of gewijzigd en er zijn geen wijzigingen gedaan aan configuratie, gates, strategieparameters, risk, sizing of execution.

De statussen zijn bewust strenger dan “de code kan een plan maken”:

- **PRODUCTIERIJP**: technisch compleet én voldoende reproduceerbaar netto bewijs.
- **PAPER-KLAAR**: technisch compleet en voldoende getest om nieuw bewijs te verzamelen.
- **RESEARCH-ONLY**: concept, bewijs of implementatie vereist verdere validatie.
- **PAUZEREN**: bestaand bewijs is voldoende negatief of inconsistent om live gebruik gepauzeerd te houden; strict paper mag nog wel observeren.
- **VERWIJDERKANDIDAAT**: geen repositorycaller gevonden; verwijderen vereist nog een aparte controle op externe en handmatige workflows.

“Technisch compleet” hieronder betekent uitsluitend dat detector → selector → scoring → risk → planner → forward-paper aanwezig is. Het betekent niet dat een strategie winstgevend, live-goedgekeurd of productierijp is.

## Gedeelde execution flow

1. `app/runner.py` bouwt per publieke marktsnapshot kandidaten uit de primaire detectoren.
2. `strategies/strategies/selector.py` past strategie- en executionfilters toe en rangschikt de overblijvende kandidaten.
3. `strategies/scoring.py` produceert de gedeelde setupscores.
4. `risk/risk_manager.py` past score-, HTF-, expectancy-, coach-, sessie-, exposure-, cost- en kill-switchgates toe.
5. `planning/trade_planner.py` bepaalt entry, stop, TP1 en sizing. Nieuwe plannen gebruiken momenteel één TP met 100% close.
6. In strict forward-paper-only gaat een executable plan uitsluitend naar `forward_paper/service.py`; private exchangecalls en live execution zijn uitgeschakeld.

Gedeelde defaults die strategie-uitkomsten direct raken zijn onder meer 0,75% account risk vóór reducties, leverage 5, stopbuffer 8 bps, geschatte round-trip fee 12 bps, minimale netto edge 4 bps en een fee-aware break-evenbuffer van 0,12%. Dit rapport verandert geen van deze waarden.

## Betrouwbaarheid van bestaande resultaten

### Backtest

De huidige backtest is geschikt als code-/funnelindicatie, niet als productieclaim. Hij gebruikt dezelfde candles als 15m- en 1h-input, hardcodet `volume_ratio=1.8`, simuleert exacte fills en bevat geen realistisch model voor fees, funding, gaps, orderboekliquiditeit of slippage. Symbolen worden sequentieel in plaats van portefeuille-chronologisch verwerkt. Voor vier van de vijf primaire strategieën is het sample bovendien nul tot vier trades.

### Exchange truth

De intern geattribueerde resultaten zijn bruikbaar als waarschuwing, maar niet als volledige causale strategieanalyse. Initial-risk/R, timeframe, regime en slippage zijn grotendeels onvolledig. Een aparte exchange-export met 206 records is betrouwbaarder voor cash-nettoresultaat, maar bevat geen strategielabels. `trading_baseline_by_strategy.csv` en `exchange_truth_analysis_20260713.json` gebruiken verschillende cohorten; aantallen of PnL mogen daarom niet worden samengevoegd.

### Forward paper

Bij deze inventarisatie waren er nog geen complete forward-paper outcomes. De pipeline legt identiteit, context, kosten, lifecycle-events, MFE/MAE en reconstructie-integriteit vast, maar candledata bevat geen tickvolgorde, funding is nul zonder expliciet event en er is geen orderboek-fillmodel.

## 1. momentum_breakout — RESEARCH-ONLY

1. **Naam en doel:** bullish momentum-breakout; een verse rangebreak of streng pre-armed expansiesignaal benutten.
2. **Bestanden en entrypoints:** `strategies/momentum_breakout.py`; `MomentumBreakoutStrategy.detect`; aangeroepen vanuit `app/runner.py`; selectie en score via de gedeelde selector/scoringmodules.
3. **Richting:** alleen LONG.
4. **Bedoeld marktregime:** bullish trend, pre-expansion en verse rangebreakout.
5. **Timeframes:** primaire timeframe standaard 15m, bevestiging standaard 1H.
6. **Entryvoorwaarden:** break boven de 20-bar range high of expliciete pre-arm; pullback houdt het niveau; bullish continuation candle; minimaal 0,12% displacement; maximaal 0,60% entry-extension.
7. **Confirmation filters:** participation ≥0,75; follow-through ≥0,25; close-position ≥0,55; detectorvolume ≥0,90; pullback maximaal 45 bps; breakoutleeftijd maximaal drie bars.
8. **HTF-filters:** bullish alignment, MTF/pre-arm/reversal override; de risklaag reduceert of blokkeert tegengestelde 4H/1D-context.
9. **Liquidity/orderbook/volume:** volume, participation en follow-through in de detector; gedeelde orderbook risk-off-, spread- en costgates later in de funnel.
10. **Risk en sizing:** gedeelde RiskManager; momentumscore minimaal 72; 0,75% basisrisk vóór probe-, sessie- en expectancyreducties; gedeelde leverage- en notionalcaps.
11. **Initial stop:** detectorinvalidation onder entrycontext/breakout-candle-low; planner voegt stopbuffer toe en valideert maximale verliesafstand.
12. **TP:** adaptive TP-engine maakt momentumgeometrie, maar de planner emit alleen TP1 met 100% close.
13. **Break-even/profit-lock/trailing:** bij 60% van de TP1-afstand kan de stop naar fee-aware BE (+0,12% LONG); daarnaast gedeelde near-TP-protectie en failed-continuationlogica.
14. **Exitredenen:** STOP_LOSS, TAKE_PROFIT, PROFIT_LOCK, FAILED_CONTINUATION, DEAD_POSITION_TIMEOUT en reconciliation/operatorredenen buiten strict paper.
15. **Kill-switches/freezes/requalify:** dag-/weekverlies, drie opeenvolgende verliezen, expectancy/coach, cluster/directional exposure, HTF-oppositie, spread/cost/late candle, score en execution quality.
16. **Configuratievelden:** `PRIMARY_TIMEFRAME`, `CONFIRMATION_TIMEFRAME`, strategie-allow/deny lists, `SCORE_GO_THRESHOLD`, `MOMENTUM_*`, spread/costvelden, risk/leverage, stopbuffer en BE-buffer.
17. **Tests:** coil/pre-armtest plus gedeelde HTF-, risk-, planner- en adaptive-TP-tests; geen volledige directe detector-lifecycletest.
18. **Backtest:** 4 trades, 75% winrate, +0,1775R expectancy, PF 1,71; sample en simulator zijn onvoldoende.
19. **Exchange truth:** 48 intern geattribueerde trades, 47,92% winrate, netto -3,127962, fees 1,049052, PF 0,4708.
20. **Nodige forward-paperdata:** coil/pre-armstatus, breakoutleeftijd/-extension, pressure/expansion, participation/follow-through, spread/costbuffer, fill/initial risk, MFE/MAE, giveback, lifecycletransities, regime/sessie/timeframe.
21. **Bugs/inconsistenties/hiaten:** settings noemen momentum min-volume 1,2 terwijl de detector 0,90 gebruikt; geen end-to-end detectortest; bestaand momentumonderzoek eindigt op `INSUFFICIENT DATA`.
22. **Overlap/conflict:** directionele spiegel van breakdown; overlap met trend continuation en adaptive fallback.
23. **Status:** **RESEARCH-ONLY**. Strict paper mag data blijven verzamelen; geen productieclaim.

## 2. momentum_breakdown — PAUZEREN

1. **Naam en doel:** bearish momentum-breakdown; verse downside rangebreak of pre-armed expansie benutten.
2. **Bestanden en entrypoints:** samen met breakout in `strategies/momentum_breakout.py`; `MomentumBreakdownStrategy.detect`; runner/selector/scoring gedeeld.
3. **Richting:** alleen SHORT.
4. **Bedoeld marktregime:** bearish trend, pre-expansion en verse breakdown.
5. **Timeframes:** standaard 15m + 1H.
6. **Entryvoorwaarden:** break onder 20-bar range low of pre-arm; reclaim invalideert niet; bearish continuation candle; displacement ≥0,12%; extension ≤0,60%.
7. **Confirmation filters:** participation ≥0,75; follow-through ≥0,25; close-position ≤0,38; volume ≥0,90; reclaim ≤35 bps; maximaal twee bars oud; strengere downside pressure.
8. **HTF-filters:** bearish alignment of MTF/pre-arm override; gedeelde 4H/1D-oppositiegate.
9. **Liquidity/orderbook/volume:** detectorvolume/participation/follow-through; gedeelde orderbook-, spread- en costgates.
10. **Risk en sizing:** gedeelde momentumfloor 72, basisrisk 0,75% vóór reducties, leverage/notionalcaps.
11. **Initial stop:** boven recente/breakdown-candle highs, met plannerbuffer en geometrievalidatie.
12. **TP:** adaptive momentum TP1; één full-close target.
13. **Break-even/profit-lock/trailing:** gespiegeld fee-aware SHORT-BE; belangrijke asymmetrie: de naamfilter voor failed continuation bevat `momentum_breakdown` niet.
14. **Exitredenen:** STOP_LOSS, TAKE_PROFIT, PROFIT_LOCK, DEAD_POSITION_TIMEOUT en gedeelde lifecycle-/reconciliationredenen.
15. **Kill-switches/freezes/requalify:** dezelfde gedeelde kapitaal-, expectancy-, HTF-, exposure-, cost- en qualitygates.
16. **Configuratievelden:** breakoutvelden plus `ENABLE_SHORTS`; risk/leverage/cost/stop/BE-config.
17. **Tests:** planner single-TP, adaptive TP en gedeelde HTF/risk; geen directe volledige detectortest.
18. **Backtest:** 3 trades, 33,33% winrate, +0,23R, PF 1,8519; onvoldoende.
19. **Exchange truth:** 30 trades, 33,33% winrate, netto -1,596301, fees 0,857381, PF 0,4492.
20. **Nodige forward-paperdata:** breakdownleeftijd, reclaim, extension, downside pressure/expansion, costs, MFE/MAE, giveback en exittransities.
21. **Bugs/inconsistenties/hiaten:** volumeconfigdrift; geen directe detector-lifecycletest; failed-continuation asymmetrie; negatief exchangecohort.
22. **Overlap/conflict:** spiegel van breakout; overlap met bearish continuation en adaptive fallback.
23. **Status:** **PAUZEREN** voor live; strict paper mag huidig gedrag blijven observeren.

## 3. trend_continuation — PAUZEREN

1. **Naam en doel:** hervatting van een bestaande trend na pullback/reclaim of aligned compression.
2. **Bestanden en entrypoints:** `strategies/strategies/continuation.py`; `detect_continuation`; runnerwrapper.
3. **Richting:** LONG en SHORT.
4. **Bedoeld marktregime:** aligned trend, trend grind, compression/pre-expansion.
5. **Timeframes:** standaard 15m + 1H.
6. **Entryvoorwaarden:** pullback naar EMA20 of shallow continuation; EMA/structuurreclaim; directionele body/momentum; alignment of MTF pressure bridge.
7. **Confirmation filters:** basisvolume ≥0,65, volatility rank ≥6, participation ≥0,75, follow-through ≥0,35; SHORT is strenger; aparte MTF/compressiondrempels.
8. **HTF-filters:** primaire/bevestiging-alignment, MTF bridge, gedeelde 4H/1D-oppositie.
9. **Liquidity/orderbook/volume:** volume, vol-rank, participation en follow-through; gedeelde orderbook/spread/cost.
10. **Risk en sizing:** continuation scorefloor 78 strict/74 MTF; gedeelde riskreducties, leverage en notionalcaps.
11. **Initial stop:** recente drie-bar directionele extreme plus plannerbuffer.
12. **TP:** defensievere adaptive continuationtargets, met extra conservatisme voor SHORT; alleen TP1 wordt gepland.
13. **Break-even/profit-lock/trailing:** gedeelde fee-aware BE/profit-lock, failed continuation, near-TP-protectie en 240 minuten default timeout.
14. **Exitredenen:** STOP_LOSS, TAKE_PROFIT, PROFIT_LOCK, FAILED_CONTINUATION, DEAD_POSITION_TIMEOUT en gedeelde lifecycle-/reconciliationredenen.
15. **Kill-switches/freezes/requalify:** gedeelde verlies-, expectancy-, TP1-, coach-, HTF-, exposure-, cost- en scoregates.
16. **Configuratievelden:** timeframes, shorts, allow/deny, score/risk/leverage, stop/BE, failed-continuation en timeout.
17. **Tests:** adaptive TP en lifecyclefixtures plus gedeelde gates; geen directe detectorunit-/lifecycletest.
18. **Backtest:** geen trades in de baseline; geen expectancybewijs.
19. **Exchange truth:** 14 trades, 21,43% winrate, netto -1,06718, fees 0,326738, PF 0,1696.
20. **Nodige forward-paperdata:** pullback/reclaimgeometrie, regime hint, MTF-reden, SHORT-stricte drempels, costs, MFE/MAE, failed continuation en timeout.
21. **Bugs/inconsistenties/hiaten:** geen detectorcoverage; nul baseline backtesttrades; misleidende ongebruikte `ISOLATION_DISABLED`; grote overlap met low-vol reclaim.
22. **Overlap/conflict:** EMA pullback/reclaim overlapt low-vol; momentum kan hetzelfde vervolg beschrijven; adaptive fallback dupliceert het concept.
23. **Status:** **PAUZEREN** voor live; strict paper mag causaliteit vastleggen.

## 4. liquidity_sweep_reversal — RESEARCH-ONLY

1. **Naam en doel:** wick door recente pivot gevolgd door reclaim en displacement.
2. **Bestanden en entrypoints:** `strategies/liquidity_sweep.py`; `LiquiditySweepStrategy.detect`; runner/selector/scoring gedeeld.
3. **Richting:** LONG en SHORT.
4. **Bedoeld marktregime:** trend-aligned sweep/reclaim; alleen bij zeer sterke reclaim een choppy uitzondering.
5. **Timeframes:** standaard 15m + 1H.
6. **Entryvoorwaarden:** wick door 12-bar pivot, reclaim binnen tolerance, displacement ≥0,12%, directionele close, failed break/down en voldoende reclaim strength.
7. **Confirmation filters:** recente window 6 bars, volume ≥1,15, participation ≥0,70, follow-through ≥0,25, wickfractie ≥0,25; choppy uitzondering is strenger.
8. **HTF-filters:** normaal trend-aligned; MTF override vraagt pressure/expansion en strakkere structuur/range; gedeelde HTF-gate.
9. **Liquidity/orderbook/volume:** expliciete pivot-/wickproxy, volume en flow; gedeelde orderbook/spread/cost.
10. **Risk en sizing:** sweep ouder dan één bar wordt in risk geblokkeerd; overige gedeelde risk/leverage/notionalcaps.
11. **Initial stop:** sweep extreme plus plannerbuffer.
12. **TP:** kandidaat kan tegenoverliggende range/origin hinten; planner gebruikt één adaptive TP1.
13. **Break-even/profit-lock/trailing:** gedeelde fee-aware profit lock en timeout; naamfilter failed continuation omvat sweep niet.
14. **Exitredenen:** STOP_LOSS, TAKE_PROFIT, PROFIT_LOCK, DEAD_POSITION_TIMEOUT en gedeelde redenen.
15. **Kill-switches/freezes/requalify:** freshness plus gedeelde verlies-, expectancy-, coach-, HTF-, exposure-, cost- en qualitygates.
16. **Configuratievelden:** alle `SWEEP_*`-velden, timeframes, shorts, allow/deny, score/risk/stop.
17. **Tests:** twee gedeeltelijke tests in `tests/test_strategy.py`; één accepteert detectie óf veilige reject; gedeelde tests dekken de rest.
18. **Backtest:** 2 trades, 100% winrate, +1,5R; PF niet zinvol door alleen winnaars.
19. **Exchange truth:** 1 trade, verlies; netto -0,171574, fees 0,033, PF 0.
20. **Nodige forward-paperdata:** pivot/age, wickexcursie, reclaimtolerance/-strength, choppy exception, pressure/expansion, costs, MFE/MAE en exits.
21. **Bugs/inconsistenties/hiaten:** tests bewijzen geen volledige lifecycle; samples statistisch onbruikbaar; “reversal” is semantisch misleidend omdat countertrend normaliter wordt geblokkeerd.
22. **Overlap/conflict:** reclaim overlapt low-vol; concurreert in de selector met alle primaire strategieën.
23. **Status:** **RESEARCH-ONLY**; strict paper is juist nodig om een sample te bouwen.

## 5. low_vol_reclaim — PAUZEREN

1. **Naam en doel:** low-vol EMA20 retest/reclaim met gecontroleerde spread en continuationkwaliteit.
2. **Bestanden en entrypoints:** `strategies/strategies/low_vol_reclaim.py`; `detect_low_vol_reclaim`; runnerwrapper; speciale plangeometrie in `planning/trade_planner.py`.
3. **Richting:** LONG en SHORT.
4. **Bedoeld marktregime:** lage volatiliteit en rustige trendhervatting rond EMA20.
5. **Timeframes:** standaard 15m + 1H.
6. **Entryvoorwaarden:** EMA20-retest binnen 0,85%, reclaim/zoneclose, body ≥0,04, EMA-afstand ≤2,5%, aligned of MTF-context.
7. **Confirmation filters:** vol-rank ≤55 strict/≤65 MTF, volume ≥0,20, participation ≥0,75, follow-through ≥0,10, spread ≤5 bps, MTF pressure/expansion.
8. **HTF-filters:** alignment/MTF bridge, gedeelde 4H/1D-oppositie; reclaim zonder volledige consensus wordt probe.
9. **Liquidity/orderbook/volume:** expliciete spreadcap, volume/flow en gedeelde orderbook-/costgates.
10. **Risk en sizing:** gedeelde risk; probe bij onvolledige HTF; low-vol stopcap via ATR, begrensd 30–85 bps; gedeelde leverage/notionalcaps.
11. **Initial stop:** recente drie-bar extreme, daarna low-vol ATR/%-cap en stopbuffer.
12. **TP:** vaste TP1 op 1,30R, 100% close.
13. **Break-even/profit-lock/trailing:** gedeelde 60%-trigger en fee-aware BE; failed continuation geldt door `reclaim` in de naam; reclaimtimeout 90 minuten.
14. **Exitredenen:** STOP_LOSS, TAKE_PROFIT, PROFIT_LOCK, FAILED_CONTINUATION, DEAD_POSITION_TIMEOUT en gedeelde redenen.
15. **Kill-switches/freezes/requalify:** verlies-/expectancy-/TP1-/coach-/HTF-/cost-/exposure-/qualitygates.
16. **Configuratievelden:** timeframes, shorts, allow/deny, spread, score, `MIN_RR_TP1`, risk, stop/BE, reclaimtimeout en failed-continuationvelden.
17. **Tests:** TP1=1,30R, single-TP planner, risk, HTF, timeout/lifecycle; geen directe detector-lifecycletest.
18. **Backtest:** 192 trades, 36,46% winrate, -0,121979R, PF 0,7908.
19. **Exchange truth:** 278 trades, 37,41% winrate, netto -6,738436, fees 7,530732, PF 0,6055; grootste negatieve geattribueerde cohort.
20. **Nodige forward-paperdata:** EMA-afstand/retestzone, volatility bucket, HTF/probe, spread/costs, initial risk, MFE/MAE/giveback, BE/profit-lock/timeout en sessie/regime.
21. **Bugs/inconsistenties/hiaten:** telemetrynote noemt follow-through 0,06 terwijl code 0,10 gebruikt; plannertekst noemt minimum 1,00 rond een 1,30-gate; geen detectortest; expectancyrapport heeft nul verse post-geometry trades.
22. **Overlap/conflict:** grote overlap met trend continuation; sommige reclaimcandles overlappen liquidity sweep.
23. **Status:** **PAUZEREN** voor live; strict paper mag de bestaande hypothese controleren.

## 6. adaptive_momentum_continuation — RESEARCH-ONLY

1. **Naam en doel:** optionele execution-aware fallback wanneer geen primaire kandidaat overblijft.
2. **Bestanden en entrypoints:** `app/runner.py::_build_fallback_candidate`; gedeelde risk/planner.
3. **Richting:** LONG en SHORT.
4. **Bedoeld marktregime:** aligned momentum/continuation.
5. **Timeframes:** standaard 15m + 1H.
6. **Entryvoorwaarden:** geen primaire kandidaat; expliciet allow-listed; execution-aware score ≥75; entry quality ≥75; aligned richting; volume/range/breakout/structuurbewijs.
7. **Confirmation filters:** pressure en expansion aanwezig; LONG close-position <0,75, SHORT >0,25; rangegebaseerde synthetische stop.
8. **HTF-filters:** alignment en gedeelde HTF-riskgate.
9. **Liquidity/orderbook/volume:** featurevolume/range plus gedeelde executionfilters.
10. **Risk en sizing:** speciale strategyhandling in gedeelde risk; normale caps; synthetische detectorstop.
11. **Initial stop:** max(45% recente range, 0,20% entry), gespiegeld per richting.
12. **TP:** gedeelde adaptive single-TP planner.
13. **Break-even/profit-lock/trailing:** gedeelde continuation-/profit-lock-/timeoutlifecycle.
14. **Exitredenen:** gedeelde SL/TP/profit-lock/failed-continuation/timeout.
15. **Kill-switches/freezes/requalify:** expliciete allow-list, pas na afwezigheid primaire kandidaat, daarna alle gedeelde gates.
16. **Configuratievelden:** allow/deny, timeframes, shorts, score/risk/leverage.
17. **Tests:** geen directe fallbacktest; alleen gedeelde componenttests.
18. **Backtest:** niet ondersteund door BacktestEngine; nul trades.
19. **Exchange truth:** geen betrouwbaar geïsoleerd huidig cohort.
20. **Nodige forward-paperdata:** fallbackreden, alle afgewezen primaire kandidaten, execution score/entry quality, synthetische stop, features en lifecycle.
21. **Bugs/inconsistenties/hiaten:** geen backtestpariteit, geen directe tests, afwijkende synthetische stop, conceptuele duplicatie en bypass van primaire selectie.
22. **Overlap/conflict:** dupliceert momentum breakout/breakdown en trend continuation.
23. **Status:** **RESEARCH-ONLY** en disabled houden totdat tests en simulatiepariteit bestaan.

## 7. legacy_continuation_prototype — VERWIJDERKANDIDAAT

1. **Naam en doel:** oudere standalone EMA-continuationprototype.
2. **Bestanden en entrypoints:** `strategies/__init__.py::detect_continuation`; geen repositorycallers gevonden.
3. **Richting:** LONG en SHORT.
4. **Bedoeld marktregime:** legacy trend continuation.
5. **Timeframes:** 15m + 1h argumenten.
6. **Entryvoorwaarden:** lokale legacy EMA/trendvoorwaarden.
7. **Confirmation filters:** alleen legacy lokale checks.
8. **HTF-filters:** legacy 1h-context.
9. **Liquidity/orderbook/volume:** niet aangesloten op actieve gedeelde filters.
10. **Risk en sizing:** niet aangesloten.
11. **Initial stop:** legacy output; niet in actieve planner.
12. **TP:** niet in actieve planner.
13. **Break-even/profit-lock/trailing:** geen actieve lifecycle.
14. **Exitredenen:** geen.
15. **Kill-switches/freezes/requalify:** niet aangesloten.
16. **Configuratievelden:** geen actieve koppeling.
17. **Tests:** geen gevonden.
18. **Backtest:** geen attributie.
19. **Exchange truth:** geen attributie.
20. **Nodige forward-paperdata:** geen, tenzij een externe/manual caller wordt gevonden.
21. **Bugs/inconsistenties/hiaten:** dubbele functienaam naast de actieve continuation; geen imports, scripts, entrypoints of tests; externe handmatige use kan repositorysearch niet uitsluiten.
22. **Overlap/conflict:** dupliceert `strategies/strategies/continuation.py`.
23. **Status:** **VERWIJDERKANDIDAAT**; nog niet verwijderen zonder aparte workflowcontrole.

## Portefeuilleconclusies (A–H)

### A. Complete lijst strategieën

De vijf primaire runtime-detectoren zijn `momentum_breakout`, `momentum_breakdown`, `trend_continuation`, `liquidity_sweep_reversal` en `low_vol_reclaim`. Daarnaast bestaat de expliciet te activeren fallback `adaptive_momentum_continuation` en de ongebruikte `legacy_continuation_prototype`.

### B. Technisch complete strategieën

De vijf primaire strategieën hebben een compleet detector→selector→score→risk→planner→forward-paper pad. Geen enkele is op basis van test- en performancebewijs productierijp. De adaptive fallback is technisch partieel door ontbrekende directe tests en ontbrekende BacktestEngine-pariteit.

### C. Inhoudelijk onbewezen strategieën

`momentum_breakout`, `liquidity_sweep_reversal` en `adaptive_momentum_continuation` hebben te weinig betrouwbaar bewijs. Breakdown en continuation zijn niet slechts onbewezen: hun geattribueerde exchangecohorten zijn duidelijk negatief. Low-vol heeft zowel het grootste sample als de duidelijkste negatieve indicatie.

### D. Duplicatie en overlap

- Breakout en breakdown zijn een bewuste directionele strategiespiegel.
- Trend continuation en low-vol reclaim overlappen sterk rond EMA20-pullback/reclaim.
- Adaptive fallback dupliceert momentum- en continuationfeatures en volgt een afwijkend kandidaatpad.
- Liquidity sweep en low-vol kunnen dezelfde reclaimcandle beschrijven.
- De legacy continuation dupliceert de actieve continuationnaam en -bedoeling.

De selector voorkomt gelijktijdige uitvoering door één beste kandidaat te kiezen, maar dit verwijdert geen classificatiebias uit analyses: overlappende signalen moeten in forward paper inclusief alle afgewezen alternatieven worden gelogd.

### E. Onvoldoende logging

Alle strategieën missen nog complete forward-paper outcomes. Historische exchangeattributie mist doorgaans betrouwbare initial risk/R, timeframe, regime en slippage. Backtests missen fees/funding, realistische fills, MFE/MAE, break-evenstate en causal ordering. Adaptive fallback mist bovendien de reden waarom hij primaire kandidaten verving. Sweep heeft geen statistisch bruikbaar lifecycle-sample.

### F. Strategieën die strict forward-paper mogen blijven genereren

De vijf primaire strategieën mogen in de bestaande strict, execution-disabled runtime blijven observeren: breakout, breakdown, trend continuation, liquidity sweep en low-vol reclaim. Dat is toestemming voor dataverzameling, niet voor live trading. De current gates mogen niet worden versoepeld om meer samples af te dwingen.

### G. Voorlopig pauzeren

Live/productiongebruik pauzeren voor `momentum_breakdown`, `trend_continuation` en `low_vol_reclaim` wegens negatieve geattribueerde evidence. `adaptive_momentum_continuation` disabled houden wegens ontbrekende tests/backtestpariteit. De legacy prototype niet activeren. Breakout en sweep blijven research-only totdat er voldoende onafhankelijke netto paperdata is.

### H. Prioriteiten

1. Verzamel volledige strict forward-paper outcomes zonder gates te wijzigen; leg per signaal ook overlappende/afgewezen strategieën vast.
2. Dicht loggingvelden voor initial R, regime, timeframe, spread/slippage/funding, MFE/MAE en exittransities.
3. Voeg directe detector- en lifecycletests toe voor alle vijf primaire strategieën, met nadruk op low-vol en continuation.
4. Herbouw de backtest met echte gescheiden 15m/1h-data, fee/slippage/funding, chronologische portefeuillevolgorde en out-of-sample splits.
5. Onderzoek de volume-thresholddrift, selector fail-open gedrag en failed-continuation naam-asymmetrie als technische issues; verander tradinggedrag uitsluitend via apart bewijs en review.
6. Maak overlap meetbaar met een multi-label signaalrecord vóór de selector, zonder de huidige keuze- of gate-logica te veranderen.
7. Geef adaptive fallback pas een paperstatus na directe tests en backtestpariteit.
8. Controleer externe/manual imports van de legacy prototype; maak daarna pas een afzonderlijk verwijderingsbesluit.

## Belangrijkste bronbestanden

- Runtime en kandidaatflow: `app/runner.py`, `app/config.py`
- Detectoren: `strategies/momentum_breakout.py`, `strategies/liquidity_sweep.py`, `strategies/strategies/continuation.py`, `strategies/strategies/low_vol_reclaim.py`
- Selectie/scoring: `strategies/strategies/selector.py`, `strategies/scoring.py`
- Risk/planning/lifecycle: `risk/risk_manager.py`, `planning/trade_planner.py`, `execution/adaptive_tp_engine.py`, `execution/position_manager.py`, `execution/tp_sl_lifecycle.py`
- Paperobservability: `forward_paper/service.py`
- Resultaten: `reports/trading_baseline_by_strategy.csv`, `reports/backtests/strategy_expectancy.json`, `reports/backtests/strategy_funnel.json`, `reports/exchange_truth_analysis_20260713.json`, `reports/momentum_breakout_research.json`
