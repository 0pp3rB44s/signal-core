# Unified feature-engine report

## Oordeel

`FEATURE ENGINE UNIFIED`

De branch blijft ongecommit. Production, backtest, replay en validation gebruiken
dezelfde builder, aggregator en gesloten-candle-indexsemantiek.

## A. Architectuur vóór

| Feature | Production | Backtest/replay/audit | Afwijking |
|---|---|---|---|
| EMA20/50, trend | `data/market_fetcher.py` | backtest-SMA en validatiescript-EMA | verschillende formules/inputvensters |
| Volume ratio | impliciet `candles[-2]` | backtest constant `1.8` | synthetische waarde en indexverschil |
| Volatility/pressure | `VolatilityEngine` en `BreakoutEngine` | synthetische notes of ontbrekend | niet productie-equivalent |
| Spread | live orderboek | hardcoded/ontbrekend | historische waarde werd verzonnen of ontbrak |
| HTF | live 1D/4H fetch | eigen resampling/ontbrekend | andere aggregatie |
| 15m→1h | exchange of lokale resample | meerdere resamplers | incomplete buckets niet uniform behandeld |
| Closed candle | impliciet actief/`[-2]` | laatste array-element | geen expliciete as-of-grens |

## B. Nieuwe gedeelde architectuur

`market_features/engine.py` bevat nu:

- expliciete as-of closed-candleselectie;
- OHLCV-, volgorde-, duplicate- en gapvalidatie;
- EMA20/EMA50, trend, range en volume ratio;
- complete timeframeaggregatie;
- productie-`VolatilityEngine` en `BreakoutEngine`;
- volatility rank, pressure- en structuurnotes;
- getypeerde `LiveMarketContext` voor orderbook, liquidity, spread, risk-off,
  entry quality, pressure, structure, contractmetadata, HTF en notes;
- de enige nieuwe `MarketSnapshot`-constructor.

Ontbrekende spread/HTF wordt expliciet als unavailable gemarkeerd en nooit
gesynthetiseerd.

## C. Gewijzigde bestanden

- `market_features/engine.py`, `market_features/__init__.py`: gedeelde engine.
- `clients/schemas.py`: as-of- en closed-candlemarkers.
- `data/market_fetcher.py`: één productionpad dat alleen publieke data/context
  verzamelt en exact eenmaal de gedeelde builder aanroept.
- `app/runner.py`: expliciete production-`as_of` op beide scanpaden.
- `backtesting/backtest_engine.py`: synthetische snapshot vervangen.
- `analysis/detector_replay.py`: replay via dezelfde builder/aggregator.
- `scripts/validation_engine.py`: eigen resample/EMA verwijderd; validation gebruikt gedeelde snapshots en aggregatie.
- `market_data/htf_regime.py`: EMA gedelegeerd naar de gedeelde engine.
- primaire detectorbestanden: gesloten-candlehelpers en gecorrigeerde momentumoffsets.
- `tests/test_unified_feature_engine.py`: pariteit en fail-closed regressies.

## D. Pariteitstesten

Voor dezelfde candle-array, live context en expliciete `as_of` zijn het echte
productionpad, backtest, replay en validation als volledige dataclasses exact
gelijk. Dit omvat EMA20, EMA50, trend, volume ratio, volatility rank,
pressure/structure, orderbook/liquidity/entry quality, spreadstatus, HTF,
timestamps en closed marker. Dezelfde detectoren geven op alle vier paden exact
dezelfde uitkomst.

- Gerichte feature/production/pariteitstests: 29 passed.
- Volledige suite: 174 passed in 3.34s.
- Compileall: passed.
- `git diff --check`: passed.

## E. Replayresultaten

Twee runs waren byte-identiek.

- Evaluated snapshots: 8.628
- Signalen: 0
- Detectorpogingen: 8.628 per strategie
- Bestands-SHA256: `c162766759ec74b37e006d8860abceda9c8e5d90fd4d5366a4439385cedba831`
- Payload replay hash: `cc1fd6bdf1cb1e7ff740402e68626f18dede0b779ae0dd9f4599e0d3fbc504ae`

## F. Outputverschillen

De oude backtest produceerde features die niet uit marktdata kwamen: volume
ratio `1.8`, pressure `70`, expansion probability `75`, spread `1.0bps` en een
SMA die als EMA20 werd aangeboden. Die waarden zijn verwijderd.

De replay levert nul signalen. Stage-diagnostiek toont dat dit geen resterende
indexfout is. Momentum breakout wordt voornamelijk door bestaande trendalignment
(8.430) geblokkeerd; breakdown idem (8.627). Continuation strandt voornamelijk
op trendalignment (8.613). Liquidity sweep vindt meestal geen sweep/reclaim
(7.687). Alle 8.628 low-volpogingen worden nu afzonderlijk als
`DATA_QUALITY_MISSING_SPREAD` geclassificeerd; dit is geen bewezen
strategieafwijzing. Geen threshold is versoepeld om signalen te produceren.

## G. Performance-impact

Lokale microbenchmark: beste run circa 375,3 µs per snapshotbuild.
Netwerk-I/O blijft dominant in productie.
De engine voert per snapshot één volatility- en één breakoutanalyse uit.

## H. Resterende verschillen

- Spread en HTF zijn externe context, geen candle-afleidingen. Historische paden
  geven deze waarden alleen door wanneer een betrouwbare snapshot beschikbaar is;
  anders blijven ze expliciet unavailable.
- `MarketFetcher._ema`, `_score_hint` en `_volatility_rank` blijven bestaan als
  backwards-compatible adapters, maar delegeren onvoorwaardelijk naar de
  gedeelde engine of falen gesloten bij incomplete input.
- `market_data.breakout_engine`, `market_data.volatility_engine`,
  `market_data.orderbook_analyzer`, `market_data.entry_quality` en
  `market_data.liquidity_heatmap` zijn gedeelde featurecomponenten die uitsluitend
  door `market_features.engine` worden samengesteld; dit zijn geen alternatieve
  snapshotbuilders. `market_data.htf_regime` is externe HTF-context en gebruikt
  de gedeelde EMA.
- De nul-signal historische dataset kan geen outcomevergelijking leveren. De
  stage-resultaten verklaren dit volledig als bestaande fail-closed gates.

## I. Gedeeld candle-indexcontract

Alle vier paden vereisen expliciet `as_of_timestamp_ms`; geen helper leidt dit
stil af van het laatste array-element. De engine ontvangt ruwe candles plus `as_of`, selecteert en valideert gesloten
candles en zet een marker. `closed_window`, `latest_closed_candle` en
`previous_closed_candle` en `closed_candle_at_offset` verifiëren die marker. Alle primaire detectoren gebruiken
deze helpers. Momentum zoekt dezelfde bedoelde drie gesloten bars nu op offsets
1–3 in plaats van de oude active-candle offsets 2–4; thresholds en gates zijn
ongewijzigd.
