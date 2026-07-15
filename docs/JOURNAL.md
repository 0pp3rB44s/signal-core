# TRADINGBOT JOURNAL — CGC BOT

Dit journal documenteert de ontwikkelfasen in verhalende vorm.
Voor de technische handleiding: docs/BLUEPRINT.md.
Voor de patchhistorie: docs/PATCHES.md.
Voor de actuele status: ROADMAP.md.

Hoofdstukken 1-28 zijn het V4-archief (aangeleverd door eigenaar,
2026-06). Vanaf hoofdstuk 29 wordt dit journal per fase bijgewerkt.

═══════════════════════════════════
DEEL 1 — V4 ARCHIEF (samenvatting per hoofdstuk)
═══════════════════════════════════

1.  Breakout Intelligence Engine — pressure/tightening/readiness detectie
    (market_data/breakout_engine.py)
2.  Execution-Aware Ranking — score niet alleen trend maar ook execution-
    kwaliteit (selector, penalties voor spread/wick/candle-positie)
3.  RR_TO_TP1 Guard — expectancy-bescherming in de planner
4.  Reclaim & Pullback Intelligence — pullback_depth, reclaim_proximity,
    reclaim timing, vertical_extension_risk (continuation.py)
5.  Execution Cost Guard — spread/candle-top/extended-reclaim blocks
6.  TP/SL Lifecycle Bug opgelost — oude SL cancelen vóór nieuwe plaatsen
    (kapitaalbescherming; SEIUSDT live-bewijs)
7.  TP Recovery Debug opgeschoond — alleen WARNING bij echte mismatch
8.  Market Context Logging — gestructureerde context naar market_context.csv
9.  Risk & Execution Safety Layer — kapitaal beschermen vóór alpha
10. Continuation Engine — setup-detectie + timing intelligence
11. Selector & Ranking Engine — A+ filtering, TOP1/2/3
12. Bot Runtime & Scan Engine — app/runner.py orchestratie
13. State & Lifecycle Persistence — state/executed_trades.json
14. Logging & Analytics Infrastructure — logs als leerdata-fundament
15. Strategy Quality & Participation Intelligence — participation_score,
    followthrough, acceptance; fake-breakout filtering
16. Strategy Calibration — participation-eisen gekalibreerd (bot was verlamd)
17. Strategy Performance Analytics — strategy_performance.csv infra
18. Protection Integrity & Exchange Reconciliation — protectie bestaat pas
    na exchange-verificatie
19. Continuation Overtrading Intelligence — pressure gating, duplicate blocks
20. Trade Dataset Integrity — gegarandeerde CLOSED rows; dataset-waarheid
21. Bitget REST Split — één monoliet → gespecialiseerde clients
22. Market Data Quality — candle sanity, stale detection, orderbook risk-off
23. Market Engines Intelligence — spread-regimes, wall-traps, volatility-regimes
24. Candidate Selection & Fallback Bridge — adaptive_momentum_continuation
    (later observe-only gezet: bleek expectancy-lek)
25. Execution Lifecycle Crisis & Protection Hardening — UNI-incident;
    side/direction/size compatibility; fail-safe close hardening
26. Single TP Baseline Mode — van 3 TP's naar 1 TP full close (fragiliteit ↓)
27. Dashboard Redesign Split — dashboard losgekoppeld van bot-core
28. Exchange Truth Validation — exchange wint altijd van lokale data;
    Adaptive Continuation Crisis (richting klopte, timing niet);
    Entry Timing Intelligence (origin_distance/freshness/exhaustion);
    Master Roadmap V5 vastgesteld (Safety > Truth > Expectancy > Alpha)

2026-06-01 — P1.1.1 bewezen: ENTRY_PROTECTION_CONFIRMED live (XLMUSDT);
FAILED_CONTINUATION_SL_TIGHTEN_FAILED ontdekt en gepatcht.

═══════════════════════════════════
DEEL 2 — V5 FASE (vanaf 2026-07-05)
═══════════════════════════════════

═══════════════════════════════════
29. De Grote Waarheids-Schoonmaak (2026-07-05)
═══════════════════════════════════

BESTANDEN / MODULES

telemetry/trade_logger.py, telemetry/csv_rotation.py, tests/conftest.py
planning/trade_planner.py, execution/adaptive_tp_engine.py
scripts/run_backtest.py, app/runner.py

WAT ER GEBEURDE

De leerloop bleek 3 WEKEN dood (launchd exit 1 + 600MB CSV's).
De testsuite bleek nepwinsten in de productie-leerdata te schrijven:
53 rijen, ~+162 aan fictieve PnL. Na opschoning bleek de echte
all-time PnL −5,28 i.p.v. +32.

En de reclaim-strategie (89% van al het volume) draaide op wiskundig
gegarandeerd verlies: 1,00R target met 12bps fees = netto 0,7R winst
tegen 1,3R verlies; breakeven pas bij 62% winrate, werkelijk 45%.

FIXES

- CSV-rotatie op alle loggers (25MB cap) → leerloop draait weer
- conftest-isolatie: tests kunnen nooit meer productie-data raken
- reclaim 1,30R end-to-end coherent (engine, planner, gates, soft bridge)
- 30-dagen rolling window i.p.v. all-time expectancy
- leerketen draait voortaan IN de bot (launchd is TCC-geblokkeerd)

BELANGRIJKSTE LES

Een bot die van vervuilde data leert, leert vol vertrouwen
de verkeerde dingen. Dataschoonmaak is geen onderhoud — het is
de fundering van elke andere beslissing.

═══════════════════════════════════
30. Hedgefund-Allocatie & Flow-Herstel (2026-07-06)
═══════════════════════════════════

BESTANDEN / MODULES

risk/risk_manager.py, execution/execution_service.py
agents_v2/learning/pattern_detector.py, .env

WAT ER GEBEURDE

Trade-flow was ingestort: 2 trades in 7 uur. Diagnose:
- reclaim bezette met churn permanent beide positie-slots (1078 skips)
- de coach blokkeerde strategieën hard op n=3 trades (statistische ruis)
- de TP-engine bouwde 0,8-0,9R targets terwijl de planner-gates ≥1,0R
  eisen → 94+93 wiskundig gegarandeerde blocks per dag

FIXES

- Probe-modus: negatieve expectancy → halve size i.p.v. bevriezen
  (een bevroren strategie kan zich nooit herkwalificeren)
- 4 slots, max 2 per strategie (regime-diversificatie afgedwongen)
- TP-engine vloer 1,05R + testcontract (deze bug-klasse kan niet terug)
- Sessie-vensters: 08-12 en 23-01 UTC structureel rood → size ×0,5

Ook: position_manager (2900 regels) opgesplitst in 3 modules + orchestrator,
12 lifecycle-safety-tests, fail-closed kill-switch.

BELANGRIJKSTE LES

Engine en poorten moeten hetzelfde contract spreken. Een systeem dat
setups genereert die zijn eigen gates gegarandeerd afkeuren, produceert
geen voorzichtigheid maar stilte.

═══════════════════════════════════
31. Het Geometrie-Anker (2026-07-07, doorbraak van de week)
═══════════════════════════════════

BESTANDEN / MODULES

planning/trade_planner.py, execution/execution_service.py
clients/bitget_order_client.py

WAT ER GEBEURDE

Legitimiteits-audit van 16 executies: elk plan passeerde zijn gates
correct — maar de planner prijsde alle geometrie vanaf het
detectie-retest-niveau, terwijl de executie tegen marktprijs vult.
Mediaan 30bps drift. Vanaf de ECHTE fill gemeten: stop ~30bps
(de noise-vloer), TP op 2,6-3,8R i.p.v. de ontworpen 1,05-1,30R.

Dit was dé verklaring voor de ingestorte TP1-hit-rate (10%).
En het was onzichtbaar omdat de slippage-meting zelfreferentieel
bleek: expected_entry verwees naar de fill zelf (altijd 0,0000).

Daarbovenop (N8): execution las 4 fill-metrics-sleutels die de
extractor nooit heeft geproduceerd — fill-truth was volledig dood
door naam-drift.

FIXES

- Planner ankert alle geometrie op de actuele marktprijs
- detection_entry_drift_bps zichtbaar in elk plan
- Fill-extractie gerepareerd + retry + contract-test op source-niveau

BELANGRIJKSTE LES

Elke poort afzonderlijk kan kloppen terwijl het geheel niet klopt.
Toets altijd het contract tussen ontwerp (plan) en werkelijkheid (fill).
Een metriek die zichzelf als referentie gebruikt, is geen metriek.

═══════════════════════════════════
32. Excursie-Oogst & Safety-Gaten (2026-07-07)
═══════════════════════════════════

BESTANDEN / MODULES

risk/risk_manager.py, app/equity.py, execution/tp_sl_lifecycle.py
scripts/run_supervised.sh, market_data/liquidity_heatmap.py

WAT ER GEBEURDE

Excursie-analyse op 19 verse closes: ELKE trade ging eerst de goede
kant op (mediaan piek +0,37%, MAE≈0) — de richting was nooit het
probleem, het oogsten wel. Simulatie: profit-lock op 45% van TP1
had het periodeverlies van −1,43 naar −0,47 gebracht.

Gap-audit vond drie gaten die niemand zag:
- WEEKLY_FREEZE_LOSS_PCT stond in .env maar werd door NIETS gelezen
- alle sizing draaide op statische €100 terwijl het echte saldo €62,51 was
- geen auto-herstel na crash (watchdog notificeert alleen)

Plus live-bewijs van een 28-minuten protectie-gap (FILUSDT): mislukte
SL-tighten werd pas een half uur later opnieuw geprobeerd.

FIXES

- Profit-lock 60%→45%; weekly freeze afgedwongen; live equity-sync
  (fail-closed resolver); supervisor-script; persistente tighten-retry;
  dead-trade timeout (90m/240m); liquidity heatmap als read-only laag

BELANGRIJKSTE LES

Een geconfigureerde veiligheidsknop is geen veiligheid — alleen
afgedwongen en getest gedrag telt. En: winst die je niet oogst
wanneer de move sterft, was nooit winst.

═══════════════════════════════════
33. Entry Alpha: De Coil (2026-07-07 avond)
═══════════════════════════════════

BESTANDEN / MODULES

strategies/momentum_breakout.py, risk/risk_manager.py
planning/trade_planner.py

WAT ER GEBEURDE

Eigenaar-vraag: "zitten we dichtgetimmerd met controles?"
Meting: nee — 42 van 43 geblokkeerde plannen faalden op 2-3
onafhankelijke checks tegelijk. Slechts 1 stierf aan een enkele poort.
De bottleneck was setup-TIMING: alle momentum-kandidaten arriveerden
ná de expansie.

Forward-return studie (12 symbolen × 1000 candles, 331 entries, echte
BreakoutEngine):

  COIL na expansie:   +0,198R, 61,5% TP1  ← enige positieve bucket
  COIL vóór expansie: −0,065R, 40,4% TP1
  CHASE vóór expansie:−0,121R, 30,8% TP1
  CHASE na expansie:  −0,065R, 25,5% TP1, 48,9% timeout

De exhaustion-gate had gelijk voor chases maar blokkeerde óók de
beste setup-klasse: de verse coil ná een grote move — precies het
"push meeliften" waar de eigenaar om vroeg.

FIXES

- entry_model=pre_breakout_coil detectie (opgerold ≤0,20% onder trigger,
  druk ≥55), long/short symmetrisch
- Exhaustion-gate: coils → probe-size, chases blijven hard geblokkeerd
- master_entry_quality gedemoteerd naar observe-only (dood gewicht:
  43/43 raak, 1× beslissend)

BELANGRIJKSTE LES

Een A+-gate bereik je niet door controles strakker te zetten maar door
setups eerder te vinden. Meet welke poort de flow écht bepaalt
(sole-blocker analyse) voordat je aan drempels draait.

═══════════════════════════════════
HUIDIGE STATUS — 2026-07-07 EOD
═══════════════════════════════════

✅ leerloop dagelijks in-bot, zelfsturend allocatiemodel
✅ exchange truth + fill truth + geometrie-anker coherent
✅ alle safety-lagen afgedwongen én getest (71 tests)
✅ 16 executies/dag flow met A+ filtering
✅ read-only liquidity intelligence verzamelt data
✅ patchregister + blueprint + journal actief

VOLGENDE FASE

- coil-bucket bewijzen via leerloop (promotie bij ≥15 verse trades)
- N7/N8 valideren op echte fills
- entry-context backfill afronden → L3 setup-quality learning
- confluence-regels liquidity heatmap (na backtest + goedkeuring)
# 2026-07-15 — Minimal backtest execution-realism closure

- Replaced unconditional historical fills and fixed-R profitability with a deterministic execution contract.
- Conservative defaults: next-candle market entry, 4 bps spread, 2 bps adverse entry/exit slippage, 6 bps taker fee, 2 bps maker fee, and `CONSERVATIVE` same-candle handling.
- Limit entries require OHLC touch before their configured expiration. TP/SL evaluation begins on the candle after the fill.
- Backtest PnL now uses executable quantity, partial exits, entry/exit fees, net PnL and sequential equity updates.
- Basic contract constraints cover price tick, quantity step, minimum quantity and minimum notional; invalid/unfilled records retain explicit reasons.
- Known limitations: no order-book queue, stochastic fill, portfolio margin, liquidation or funding model. Contract parameters are global unless a caller supplies symbol-specific settings. Live maker partial-fill handling remains a separate execution bugfix.
- Added 16 deterministic numerical execution-contract tests. Phase 2 is permitted only if the complete validation remains green and controlled reconciliations match manual arithmetic.
