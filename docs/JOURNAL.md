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

## CGCAgent Patch Lifecycle — 2026-07-10T22:08:00+02:00

- Status: SUCCESS
- Files: strategies/strategies/low_vol_reclaim.py
- Tests passed: True
- Runtime restarted: True
- Details: Bot restart requested.
Start success: True
Running: True
10656 /Library/Frameworks/Python.framework/Versions/3.13/Resources/Python.app/Contents/MacOS/Python -u -m app.main

═══════════════════════════════════
34. De Fill-Waarheid & Maker-Fees (2026-07-08)
═══════════════════════════════════

BESTANDEN / MODULES

planning/trade_planner.py, execution/execution_service.py
execution/maker_entry.py, clients/bitget_order_client.py

WAT ER GEBEURDE

Ochtend-audit (25 nacht-trades, break-even +0,27): reclaim (mean-reversion)
verdient alleen edge MÉT HTF-consensus. De 90d-sweep bevestigde het:
consensus +0,071R, geen consensus −0,15R — en juist zonder consensus draaide
de bot 's nachts (56% van het volume). Reclaim zonder volledige 1D+4H-consensus
ging naar probe-size.

Daarna een fundamentele fix: SL/TP werden op vaste plan-niveaus geplaatst
(geankerd op latest_close) terwijl de market-order op de live prijs vult —
structureel 0,1-0,4% verderop, altijd richting de stop. De stopafstand
verschrompelde 30-90% (rr tot 22:1) → uitgestopt op ruis vóór TP. Dé
verklaring voor de lage winrate ondanks correcte richting.

En de fees bleken 197% van de bruto-edge. Maker-entry-infrastructuur gebouwd
(post-only limit), eerst default UIT, daarna live bewaakt. Maar de maker-
fill-rate was 0/6 (post-only vult zelden in 4s) → hybride model: eerst maker
proberen, niet gevuld → alsnog market. Nooit een trade missen, fee besparen
waar het kan.

FIXES

- reclaim vereist HTF-consensus (anders probe)
- TradePlan.geometry_entry: stop/TP herankerd op de echte fill
- maker-entry met market-fallback + cancel-race safety net

BELANGRIJKSTE LES

De richting klopte al maanden; de prijs waartegen we hem vertaalden niet.
Fee-drag en fill-drift vraten de edge die de strategie wél had.

═══════════════════════════════════
35. De Autonome Patcher & de Portfolio-Snoei (2026-07-10)
═══════════════════════════════════

BESTANDEN / MODULES

agents_v3/ (CGCAgent v3), strategies/strategies/low_vol_reclaim.py
leerrapport-keten, .env

WAT ER GEBEURDE

CGCAgent v3 kwam online: een autonome patch-agent met tool-loop, trade-analyse
en guardrails (verplichte pad-verificatie, coulante actie-parsing). Zijn eerste
echte werk: low_vol_reclaim MIN_BODY_PCT 0,04 → 0,08.

Daarna een strategie-audit met portfoliobesluiten van de eigenaar:
low_vol_reclaim — de grootste volumebron — bleek een structurele verliezer
(24,5% WR) en ging in echte HARD-PAUSE via leerrapport-status. momentum_breakout
blijft, breakdown/continuation op probe, liquidity_sweep's close-pos gate
gerepareerd. Regel: geen nieuwe strategieën tot de basis winstgevend is.

BELANGRIJKSTE LES

Een strategie pauzeren die 73% van je volume levert voelt als stilvallen,
maar het is het tegenovergestelde: je stopt met betalen om te verliezen.

═══════════════════════════════════
36. Break-Even-Geometrie & de Tweede Strategie (2026-07-11)
═══════════════════════════════════

BESTANDEN / MODULES

strategies/momentum_breakout.py, strategies/liquidity_sweep.py
strategies/early_breakout_trigger.py, execution/ (BE/stop-lifecycle)
state/live_trade_journal.json

WAT ER GEBEURDE

Eerst een blokkade-bug: een close_pos false-positive keurde ÁLLE
breakout-trades af. Gefixt. Daarna een early-trigger-laag (1m/5m) om snelle
breakouts ~1 min te vangen i.p.v. tot 15 min te laat, met 5m-bevestiging en
probe-size.

De rode draad van de dag was break-even-geometrie. Drie samenhangende fixes:
de BE-stop dekt nu fees + marge (0,16% i.p.v. 0,10%), de momentum-stop wordt
op ATR gecapt zodat TP1 überhaupt bereikbaar is, en er ligt een BE-floor —
elke break-even-actieve stop staat gegarandeerd op ≥ fee-adjusted BE. Ook werd
de BE voortaan op de ECHTE fill berekend i.p.v. de geplande entry (de SL stond
eronder). En een entry chase-limit: een breakout die >15bps voorbij het plan
is weggelopen wordt geskipt i.p.v. achterna gejaagd.

liquidity_sweep werd gerepareerd en als tweede strategie geactiveerd
(reversal-aspect).

'S AVONDS — DE JOURNAL-SCHOONMAAK & EEN EERLIJKE VERWACHTING

Eigenaar-vraag: "2 strategieën — hoeveel trades/dag en hoeveel winst?"
Meting op de echte data legde bloot dat de .env níet 2 maar 5 strategieën
enabled had, dat liquidity_sweep 0 kandidaten vuurt (no_sweep_reclaim, een
zeldzaam patroon — geen bug), dat low_vol_reclaim 100% HARD-PAUSE'd wordt en
momentum_breakout op negative-expectancy PROBE staat. Frequentie recent ~1
trade/dag; de daling kwam doordat de verliezers gepauzeerd zijn, niet doordat
er iets kapot dichtzat.

Vervolgvraag: "zitten we dichtgeblokkeerd?" De blokkade-analyse toonde dat de
positie-gate exchange truth leest (execution_service.py:125), niet de journal.
Maar de journal zelf bleek vervuild: 28 rijen stonden nog op OPEN terwijl
executed_trades.json (365 CLOSED_SYNCED, 0 open) bewees dat álles dicht was.
Gereconcilieerd tegen exchange truth: 4 recente met echte pnl, 24 oude
(geroteerd uit alle state-files) eerlijk als pnl-onbekend i.p.v. verzonnen.
Root-cause — de journal wordt niet gesloten bij een exchange-sync-close —
staat als aparte taak open zodat het niet terugkomt.

BELANGRIJKSTE LES

Een break-even-stop die de fees niet dekt, is een verliesstop met een mooie
naam. En een journal die closed trades als open blijft tonen, liegt niet tegen
de bot (die leest de exchange) maar tegen jou. Meet vóór je een knop omzet:
de meeste "blokkades" waren het vangnet dat werkte.

═══════════════════════════════════
37. De Audit: Waarheid over de Waarheid (2026-07-12)
═══════════════════════════════════

BESTANDEN / MODULES

execution/position_manager.py, execution/closed_trade_writer.py
app/config.py, agents_v2/learning/knowledge_builder.py

WAT ER GEBEURDE

Eigenaar-vraag: "1 trade vannacht — zitten we dichtgeblokkeerd? Check de
hele bot." De audit vond de echte keten: om 22:15 had de leerloop ALLE
vier strategieën op status=PAUSE gezet (drempel: expectancy <= 0 -> PAUSE),
en momentum_breakout (n=32) viel daarmee onder de HARD-PAUSE die op 07-10
voor low_vol_reclaim was gebouwd. De bevroren-strategie-val van 06-07,
één dag na herintroductie dichtgeklapt.

Dieper graven vond iets ergers: elke close in executed_trades.json stond
op net_pnl ~ -0.012 — een CONSTANTE. Oorzaak: net_pnl wordt bij OPEN gezet
op -entry-fee en werd op close nooit bijgewerkt; de exchange-truth backfill
repareerde realized_pnl en exchange_truth_pnl maar vergat net_pnl. De echte
nacht: FET +0.052 (eerste TP-hit!), ENA +0.010, BNB -0.002, AAVE -0.110 (SL),
DOGE -0.125 (SL) — geboekt als vijf identieke scratches. Gelukkig bleek
trade_dataset_v2.csv (de leerloop-bron) WEL correct; de vervuiling zat in
de state-records en alles wat die leest.

Verder: de coach draaide op een learning.json van 2 JULI (10 dagen oud,
mét een testdata-rij), de PROFIT_LOCK_BE spamde 197 gedoemde API-calls op
één nacht (BE-stop boven de mark-prijs = Bitget 40917, elke cyclus opnieuw),
en het maker-30s-experiment gaf zijn antwoord: 0/7 fills, terwijl het
30s-wachten twee chase-limit skips veroorzaakte.

En één vals alarm: een "geest-positie" bleek een tijdzone-misinterpretatie
van de auditor zelf (bot.out logt lokaal/CEST, state in UTC). De
positie-sync werkt correct en instant.

FIXES (PATCH-064)

- exchange-truth backfill schrijft ook net_pnl (3 plekken)
- PROFIT_LOCK_BE placeable-check (skip stil zolang prijs onder BE)
- maker extended-wait default uit (experiment afgerond, terug naar 4s)
- coach-testdatafilter + learning.json geregenereerd op verse data

ECHTE CIJFERS (30d, exchange-truth): reclaim -0.027/trade (36.5% WR),
momentum -0.075 (48.6%), breakdown -0.027 (44.4%), continuation -0.064
(22.2%). Alles negatief — maar het venster meet overwegend de OUDE
geometrie (TP1 pas bereikbaar sinds de ATR-cap van 07-11 14:24).

BELANGRIJKSTE LES

"Exchange truth" is geen label maar een discipline: één vergeten veld
(net_pnl) en vijf dagen aan trades logen tegen elke lezer — terwijl het
juiste getal er in hetzelfde record naast stond. En een vangnet dat op
zulke data beslist, pauzeert met evenveel overtuiging de verkeerde dingen.

═══════════════════════════════════
HUIDIGE STATUS — 2026-07-11 EOD
═══════════════════════════════════

✅ low_vol_reclaim HARD-PAUSE'd (bewezen verliezer, 24,5% WR)
✅ momentum_breakout actief op negative-expectancy PROBE (getthrottled)
✅ liquidity_sweep als 2e strategie geactiveerd (wacht op eerste setups)
✅ BE-geometrie coherent: fee-adjusted floor, ATR-cap, echte-fill-anker
✅ live_trade_journal.json gereconcilieerd (0 stale OPEN-rijen)
✅ PATCHES/JOURNAL bijgewerkt t/m PATCH-063

AANDACHTSPUNTEN

- Edge blijft dun: fee-drag > edge; recent ~1 trade/dag, kleine verliezen
- liquidity_sweep heeft 0 afgesloten trades → geen live track record
- Journal-drift root-cause loopt als aparte taak (exchange-sync-close → journal-close)
- Volgende verdedigbare stap: fee-margin-filter op <30m churn (mét backtest,
  niet blind live)
