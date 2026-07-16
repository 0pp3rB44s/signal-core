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

# 2026-07-15 — Phase 2A uniform performance baseline

- Froze execution baseline commit `2009a4a5cc8525436df8fb4e09c93a5b2bd237f2`.
- Ran `scripts/strategy_performance_baseline.py` from a clean source export with `_env_file=None` and operational runtime files forbidden.
- Fixed analysis contract: eight tracked 15m datasets, UTC, 2026-07-03 10:15 through 2026-07-05 12:00, 200 candles per symbol, shared 1h aggregation, conservative execution defaults from the frozen commit.
- Analysed momentum breakout, momentum breakdown, trend continuation, liquidity sweep reversal, low-vol reclaim, and the disabled adaptive fallback.
- Result: 256 HTF warm-up points failed closed, 936 valid snapshots produced zero detector candidates, zero orders and zero trades. Every strategy is `INSUFFICIENT DATA`; cost, outlier, direction, symbol, timeframe, session, regime and calendar performance cannot be estimated.
- Reports: `reports/analysis/phase2a_20260715_execution_2009a4a/`.
- Known limitation: the available dataset covers only 49h45m and cannot support strategy ranking. Next diagnostic step is to obtain a substantially longer, gap-checked historical dataset under the same frozen contract; do not tune detectors to force trades in this sample.

# 2026-07-15 — Phase 2B twelve-month futures baseline

- Phase 2A tooling frozen at `235274958ff2a68052b9b43a8cddb6380478fcc4`; execution remains frozen at `2009a4a5cc8525436df8fb4e09c93a5b2bd237f2`.
- Downloaded public Bitget `USDT-FUTURES` market-price candles from `/api/v2/mix/market/history-candles`, with candle-open UTC millisecond timestamps and closed candles only.
- Dataset `20250715_20260715_acq20260715`: eight symbols, 35,040 15m candles each, common window 2025-07-15 00:00 through 2026-07-14 23:45 UTC. Dataset hash `9053781ed26065ebb6cc693cfd363fd5f784493488916ab319675cbf199a0f76`.
- Quality: zero canonical duplicates, gaps, invalid OHLC rows, negative volumes or timestamp-alignment failures. Raw backward pages contain expected page-order transitions; canonical files are sorted.
- Frozen baseline evaluated 279,680 valid snapshots. Raw candidates: momentum breakout 98, breakdown 98, continuation 401, sweep 238, low-vol reclaim 0. Selector accepted 475 candidates, but frozen risk rejected all on missing historical orderbook context (`orderbook risk-off`). No orders or trades were produced.
- Two isolated runs completed in 199 seconds each; core reports were byte-identical. Result hash `4ac865f94f9df0a80c35ef169b34c87d34164c7289ba18dcbbc3c6209ca3bfe3`.
- Reports: `reports/analysis/phase2b_20250715_20260715_dataset_9053781e/`. Phase 3 remains blocked: candle-only history cannot satisfy the production orderbook risk gate, and no candidate may be fabricated or gate disabled in this phase.

# 2026-07-15 — Phase 2C historical risk-gate parity

- Phase 2B frozen in commit `523fc77`; raw/canonical 58 MB payloads remain reproducible local data while manifests and quality evidence are tracked.
- Exact production blocker: `RiskManager.evaluate` immediately fails closed on `orderbook_available=false` or `orderbook_risk_off=true`. Production and forward-paper behavior remain unchanged.
- Added explicit `PRODUCTION`, `HISTORICAL_STRUCTURAL_ONLY` and `HISTORICAL_CONSERVATIVE_PROXY` research modes. Historical activation is typed and explicit, with no Settings/`.env` path.
- Frozen proxy hash `722bb6962e575931e5d4b2ee58ce175413729c587f9eed5a796b69930a349cbc`: volume ratio >=0.50, range <=5%, volatility rank <=90 and TP1 reward >=2x configured round-trip cost (48 bps under the frozen 24 bps baseline).
- Shadow results: production 0/475 accepted; structural 272 accepted and 212 closed; proxy 144 accepted and 123 closed. Proxy ending equity 986.31, net PnL -13.69; structural ending equity 981.65, net PnL -18.35.
- Two isolated runs per mode produced identical result hashes: production `4ac865f9`, structural `6ef0a421`, proxy `2d898466`. Comparison hash `16605778`.
- Phase 3 remains blocked: trend continuation is a 102-trade negative sample in the official proxy baseline; the positive sweep result has only 10 trades; all other strategies have negative or insufficient samples.

# 2026-07-15 — Phase 2D entry/exit/gate attribution

- Phase 2C frozen at `2ad6f73`; production fail-closed behavior and official proxy result hash `2d898466` reproduced unchanged.
- Added observation-only candidate competition records plus offline MFE/MAE, stop/target, exit-counterfactual, cost, segment, gate and uncertainty analysis. No detector, gate, selector, stop, target or execution setting changed.
- Trend continuation: 102 trades, 81.4% adverse-first, mean MFE 1.35R, mean MAE 1.46R, negative close displacement from candle 1 through 16. Every permitted exit counterfactual stays negative. Primary failure: `ENTRY FAILURE`; decision: `REJECT STRATEGY AS CURRENTLY DEFINED`.
- Sweep: 10 trades, mean MFE 1.60R, mean MAE 0.72R, 9/10 TP1-capable and 7/10 final-target-capable over 16 candles. Bootstrap mean interval crosses zero; primary failure: `INSUFFICIENT SAMPLE`.
- Sweep attrition: 238 detector hits -> 54 selected -> 28 structural -> 11 proxy -> 10 closed. The 184 selector rejects are all mixed-alignment without MTF confirmation, not losses to another strategy; 151 reconstructable outcomes total -12.34.
- Deterministic diagnosis hash `c42283851c0a0c04a57a75e3a385a12297475274dbac3863c80b4449d2936382` across two runs.
- Single next experiment: add one non-overlapping year and rerun only the frozen sweep diagnostic without logic changes.

# 2026-07-15 — Phase 2E independent sweep replication

- Phase 2D was committed separately as `2405e8244c9eced57864561ed80b1cdbd7b6d52e`; diagnosis hash `c42283851c0a0c04a57a75e3a385a12297475274dbac3863c80b4449d2936382` reproduced before the commit.
- Acquired the exact non-overlapping Bitget `USDT-FUTURES` year 2024-07-15/2025-07-15 for all eight symbols. Each has 35,040 candles; canonical gaps, duplicates, invalid OHLC, zero/negative volumes and alignment failures are all zero. Dataset hash `d7d7a7670b6bd5723cc5f0b7b279b099c3b0258659f2cfd384c9b9179b0953fb`.
- Universe A, B and all per-symbol maximum windows coincide with the full requested year; no symbol was excluded. Raw/canonical payloads remain ignored; manifests, quality and checksums are reproducible metadata.
- Frozen independent sweep funnel: 222 detector hits -> 30 selected -> 17 structural -> 7 proxy -> 7 fills/closed, with zero rejected or unresolved orders. Proxy configuration remains `722bb696`.
- Two sweep-filtered replays produced result hash `8a6f13a1199dcf0a54a4a2755b46762265a833985cd8aabafea88e8f4a2ea53a` and byte-identical core hash `0175c9821237c544bc07bd0ba09a157d23f87ee8f1cd6e1efcd129a5cc61f222`.
- Independent sweep: 7 trades, gross price PnL `+0.111832`, execution-adjusted gross `-0.205299`, total costs `0.610825`, net `-0.498993`, PF `0.605700`, expectancy `-0.071285`; both LONG and SHORT are negative.
- Combined sweep: 17 trades, gross price PnL `+1.314301`, costs `1.467193`, net `-0.152892`, PF `0.903867`, expectancy `-0.008994`. The independent and combined bootstrap mean intervals cross zero.
- Phase 2E artifact hash is `27541af8673d20baf646ae9fc671436069a97b4461244ad13c60100eada9a158` in two byte-identical runs. The explicit sweep-only execution filter produced the exact same seven sweep records and PnL as the unfiltered diagnostic run while preventing unrelated strategies from affecting research equity.
- Decision: `FAILED INDEPENDENT VALIDATION — REJECT CURRENT STRATEGY`. Continuation remains `REJECTED_FOR_RESEARCH_AS_CURRENTLY_DEFINED` and was excluded. No current strategy is promoted to Phase 3.

# 2026-07-15 — Phase 3A confirmation-entry hypothesis

- Branched `research/liquidity-sweep-confirmation-entry` from frozen Phase 2E commit `4763c3ce5d9bd5793c3cde974458d905774d823e`; reproduced Phase 2E analysis hash `27541af8` and replay hash `8a6f13a1`.
- Tested exactly one research-only change: after frozen sweep risk acceptance, wait at most two closed 15m candles for a close beyond the signal extreme and enter at the following open. Detector, selector, gates, original stop/absolute targets, costs, sizing and execution remain frozen.
- Funnel: prior 11 accepted -> 6 confirmed -> 5 closed; independent 7 -> 5 -> 5; seven candidates expired. All pre-confirmation counts match control.
- Independent confirmation: net `-0.843561`, PF `0.031998`, expectancy `-0.168712`, gross price PnL `-0.414459`; all materially worse than control.
- Combined confirmation: 10 trades, gross price PnL `-0.131832`, costs `0.855604`, net `-0.987435`, PF `0.051776`, expectancy `-0.098744`. Mean MFE fell to `1.1812R`; adverse-first rose to 80%.
- Paired confirmed-candidate mean difference is `-0.157336`, bootstrap 95% interval `[-0.281097, -0.064616]`. Four expired losers were avoided and three winners missed, but this selection benefit did not offset worse confirmed entries.
- Verdict: `HYPOTHESIS REJECTED`. The experimental variant remains unregistered in production, paper and live paths.
- Two complete runs are byte-identical with Phase 3A artifact hash `ddb74fedb1cdae6b1a0b4f603434aa714b886f60b1634bc7a3d162bd59244336`.

# 2026-07-15 — Phase 3B new-strategy preregistration

- Branched `research/preregister-next-strategy` at frozen Phase 3A commit
  `4c44d3fbeeb694091fdd523293ffe3980edd8517`; Phase 3A hash `ddb74fed`
  remains unchanged.
- Built an OHLCV-only inventory over two non-overlapping years, eight symbols
  and five permitted families. No execution, trade record or performance field
  was loaded. Two runs are byte-identical with evidence hash `662f35b9`.
- Compression breakout, continuation, volatility expansion and extreme
  structural mean reversion were contradicted. Failed breakout reversal alone
  had repeated positive directional medians at 4, 8 and 16 candles in both
  years with broad cross-symbol and cross-direction frequency.
- Froze one hypothesis, `failed_range_escape_reversal_v1`, with document hash
  `e7117eefbf5e387646f2a5bceb444d5125a46c56b438eb6f2c8d2e6f69077da9`.
  It is neither implemented nor registered; Phase 3C criteria are immutable.

# 2026-07-15 — Phase 3C failed-range-escape locked validation

- Verified preregistration hash `e7117eef` and froze the research-only
  implementation in commit `dbfafe6`, implementation hash `6acdcfb1`.
- Development reconciliation: 24,945 raw escapes, 4,349 valid re-entries,
  2,314 candidates, 3 overlaps, 72 exchange rejects, 2,239 closed and zero
  unresolved. Two pre-freeze runs were deterministic.
- Official locked validation: 22,592 raw escapes, 3,723 valid re-entries,
  1,510 candidates, 4 overlaps, 67 exchange rejects and 1,439 closed trades.
- Locked result: gross price PnL `-1.797002`, costs `127.299386`, net
  `-129.096388`, PF `0.566146`, net expectancy `-0.089713`, drawdown `12.92%`.
  Both directions, every traded symbol, all sessions and both HTF contexts lose.
- Nine of thirteen immutable acceptance criteria fail. Verdict:
  `REJECTED — FAILED LOCKED VALIDATION`. No production/paper/live promotion.

# 2026-07-16 — Phase 4A market edge discovery map

- Froze Phase 3C at `cad0693d` and archived failed range escape v1 as
  `PERMANENTLY REJECTED — NEGATIVE LOCKED GROSS EDGE` without deleting history.
- Mapped 15m closed-candle forward return, MFE/MAE and threshold ordering over
  two disjoint frozen years, eight symbols, six horizons and transparent
  development-frozen state bins. No execution, trade or PnL model ran.
- Dependency-aware inference uses UTC-day standard errors/bootstrap blocks.
  Development has 24/3,528 BH-adjusted effects; replication has 0/3,528.
- No factor passed single-factor replication, so the preregistered causal
  pair-selection gate selected zero interactions rather than searching around
  the failure. No state passed the 24 bps economic and stability contract.
- Verdict: `NO REPLICATED ECONOMIC EDGE FAMILY FOUND`. The single next
  investigation is synchronized historical funding and open-interest data.
- Two full runs are byte-identical with artifact hash `b3759b5a`.
