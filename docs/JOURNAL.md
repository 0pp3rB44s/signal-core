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

# 2026-07-16 — GitHub and Runner synchronization foundation

> **Governance superseded 2026-08-11:** the owner designated
> `origin/production/live-baseline-cd8671` as the authoritative Runner release
> ref. The `main`-authority statements below record the historical contract and
> are no longer operational guidance.

- Audited the Work Mac repository, remote, worktrees, ignored operational data and local research history.
- Defined reviewed `main` plus immutable annotated runner tags as deployment authority; research branches are never deployable.
- Added Work Mac bootstrap, checkout verification, tracked-file hygiene and explicit Runner deploy/rollback contracts.
- Deployment requires a clean checkout and explicit main-reachable tag/SHA, preserves ignored `.env`/state, records the deployed SHA and never starts trading.
- Added no-secret/no-live CI and documented the required on-device Runner audit and separate maintenance approval.
- Follow-up Runner audit identified Intel `x86_64`, macOS 14.8.7 and a Python 3.11 virtualenv. The architecture contract now supports both Intel and Apple Silicon.
- Selected Python 3.12: all locked wheels resolve on both architectures and 237 tests pass natively on the M4; Python 3.13 is blocked by the pyarrow wheel chain.
- Added non-mutating deployment preflight, preserved-venv recreation, names-only environment templating and presence-only configuration comparison. No Runner deployment occurred.
- **2026-07-25/27 — Forward-paper validation campaign (RC1).** Run 1 was invalidated
  after 1 h 51 m: `granularity=1h` is rejected by Bitget (only `1H`), so all 106 scans
  failed while the health check reported HEALTHY — 742 × HTTP 400171 against an empty
  event store. Two defects, not one: a wrong value at an un-normalised API boundary,
  and an observability layer that reported a cycle as complete having built zero
  snapshots. Run 2 started on `cda8187` and ended at T+42.75 h when the host rebooted;
  22.19 h of that had already been lost to host sleep despite an active power
  assertion, leaving 20.56 h effective — 28.5 % of the 72 h criterion. Within that
  time the system was clean: 9 892 API requests at 100 % HTTP 200, zero private
  endpoints, hash chain VALID across 49 events after a 22-hour suspension and a
  SIGTERM, both closed trades reconciling to < 1e-6, and a **real** DNS outage absorbed
  by the retry layer. The audit verdict is NOT READY FOR NEXT PHASE, with the three
  failures — restart recovery, supervisor, monitoring — all in the operational envelope
  around the bot rather than in its trading logic. The lasting lesson is that the
  monitor itself died silently 22.7 h before the end and nothing noticed: absence of
  alerts is not evidence of health. Evidence frozen in
  `validation_72h/archive/RC1_forward_paper_validation.tar.gz`.

# 2026-08-10 — OBSERVABILITY_FIRST: ranked-plan telemetry

Research stopped being limited by ideas and started being limited by
measurement. Twelve hypotheses were tested in the preceding week; four were
decided on sufficient data, and the rest ended in "directional, n too small" or
"the field is empty". Two of those dead ends had the same shape:

- **The ranker could not be measured, only reconstructed.** Production logs the
  winner and nothing else, so every ranking question required rebuilding the
  order offline. That reconstruction was wrong twice — once from an incomplete
  execution-score map, once because Runner logs are local time and the CSVs are
  UTC. Both were caught, but only because someone looked.
- **`participation_score` was empty in all 9301 rows** of the 2026-08-10
  snapshot, while 3422 of those rows carried the value in `raw_notes`. It is
  the central field of the strongest surviving setup hypothesis.

This release fixes the measurement, not the trading.

`logs/ranked_plans.jsonl` now carries one row per selection cycle with every
ranked plan in the selector's own order, the four ranking keys **taken from the
`RankedPlan` objects rather than recomputed**, and a clearly separated
`diagnostic` block. A second implementation of the same arithmetic is how
telemetry starts disagreeing with runtime, so there isn't one.

The `participation_score` fix is one line and no new semantics:
`_extract_first_float` reads the legacy space-separated note form and returned
`""` for the `key=value` form the producers actually emit. The repo already had
the two-branch pattern for `spread_bps` and `orderbook_imbalance`; this field
never got it. It is the only column in the schema that was empty while its
value was demonstrably present.

Cost: 0.13–0.34 ms per cycle, ~0.01 s/day, 62–205 KiB/day. No extra exchange
calls, no fsync in the scan loop.

**What this does not change:** no strategy, threshold, gate, ranker behaviour,
TP/SL, execution, risk or sizing. A behavioural-invariance test pins that the
selection fingerprint — winner, order, all four keys, rejections — is identical
with and without telemetry attached, and a mutation proves the suite catches
telemetry that reorders on its own.

**Next evidence gate:** 30 POST_DIRECTION_FIX closes with ≥ 10 LONG and ≥ 3
strategies represented, before the participation and momentum-capacity
hypotheses are retested.
# 2026-08-10 — Removed the legacy dashboard control surface

A dashboard audit found three generations coexisting. `scripts/start_dashboard.sh`
starts `dashboard_v3` and kills the other two, so only one is authoritative —
but `app/dashboard.py` was still present, importable and one command away from
being run.

It exposed `POST /api/control/{start_bot,stop_all,restart_bot,execution_off,
execution_on_dryrun}` and rewrote `.env` in place, `EXECUTION_ENABLED` and
`EXECUTION_MODE` included. `start_bot` shelled out to `scripts/start_bot.sh`,
which bypasses all four authorisation layers in `launch_live.sh`. One HTTP POST
could have begun real-money trading; `stop_all` could have killed an engine
holding an open position. It had no authentication boundary and, alone among
the three generations, no tests.

Removed. The only code reference was one `pkill` line, which went with it.
`dashboard_v2` keeps the canonical rationale for why bot control does not belong
in a dashboard — it retired the same capability earlier for the same reason —
and `dashboard_v3` never had it.

Four regression tests now pin the boundary: the file stays removed, `/login` is
the only route accepting a mutating method, no `/control` namespace may exist,
and `dashboard_v3` may not reference the launcher scripts or write files.

Worth recording about the audit itself: `dashboard_v3` produced **zero** truth
mismatches. It refuses to show a local snapshot as if it were live, refuses to
sum cohorts that measure different things, and stamps every source with
provenance and a freshness budget. Of everything in this project, it is the
component most careful about not overclaiming — the opposite of `PROJECT_STATUS.md`,
which until today still described the bot as observe-only and not running.

No trading behaviour changed.
# 2026-08-10 — END_TO_END_LINEAGE: a position now names its plan

The dashboard audit could not measure `executable → opened` for any strategy,
and the honest response was to show the stages side by side under
`INCOMPLETE_LINEAGE` rather than divide one by the other. This closes that gap
at the source.

The identity was never missing. `ExecutionReport` already carries `plan_id`,
`candidate_id`, `strategy` and `position_lifecycle_id`, and the stored trade
record in `executed_trades.json` carries all four as well — 49/49 populated in
the 2026-08-10 snapshot. They simply never reached `trade_dataset_v2.csv`,
whose schema had `position_lifecycle_id` and the exchange ids but no plan or
candidate. One CSV schema and two row builders were the entire loss point.

So no new identifier was invented, and nothing is reconstructed from timestamp,
symbol, entry price or nearest plan. Those are research heuristics; crediting a
position to the plan that happened to be closest is exactly how the wrong plan
gets the outcome. OPEN takes the fields from the report of the plan that
actually executed; CLOSE inherits them from the stored position record rather
than re-matching, because a second attribution attempt could disagree with the
first.

Backward compatible in both directions: the columns are appended, never
inserted — `_rotate_on_schema_change` already rotates the file on a header
change, and the 2026-07-07 incident where a changed schema shifted `trade_grade`
into `close_reason` is why that matters. Positions opened before this change
close with blank lineage and unchanged economics; recovered positions are not
rejected; provisional rows still carry their lineage; and deduplication is
untouched, with a test proving that two closes differing only in metadata do not
become two rows.

Metadata only: no order type, price, size, protection, SL/TP, risk decision or
exchange call changes. 15 tests, four mutations killed, full suite 1364 passed.

This unlocks nothing today — the columns fill from the next deployment onward.
# 2026-08-10 — Dashboard: per-strategy funnel and live economics

`/strategy` answers, in one screen: which strategy produces volume, where it
dies, which ones open trades, which earns or loses net, how much of that is
fees, and how large the sample is.

The design constraint was lineage, not layout. Three stages join and three do
not: `strategy_candidates.csv` and `trade_plans.csv` share `candidate_id`
(615/615 in the 2026-08-10 snapshot), and OPEN/CLOSE join on
`position_lifecycle_id` — but `trade_dataset_v2.csv` carries **no `plan_id` or
`candidate_id`**, and "selected" is never written down at all. So
`executable → selected → opened` is not a cohort, and the page shows those
stages side by side under an explicit `INCOMPLETE_LINEAGE` marker rather than
dividing one by the other. Conversion rates appear only where numerator and
denominator are genuinely the same set.

Economics use `is_displayable_close` — imported from
`telemetry/close_record_sources.py`, not reimplemented — so `/strategy` and
`/performance` cannot drift apart; a test pins their LIVE totals equal.
Provisional closes, recovery rows and low-confidence rows are excluded and
counted separately, and a lifecycle can only be counted once.

Gross, fees, funding and net are shown as four separate columns. On the live
snapshot the point lands immediately: `low_vol_reclaim` has a gross of
−0.06 USDT and fees of 1.13, so the cost structure is **18.3× the entire gross
result**. Above 100% the share is rendered as a multiple rather than a
percentage, because "1833.8%" reads like a bug when it is in fact the finding.

Sample size carries a fixed evidence band — NO_DATA / TINY_SAMPLE /
DESCRIPTIVE / REASONABLE_SAMPLE — and no label ever says "profitable".

A ranking section exists as a boundary only. Until the ranked-plan feed is
merged it reports `RANKED_PLAN_TELEMETRY_NOT_AVAILABLE` and reconstructs
nothing: rebuilding the ranking offline is exactly what has already produced
two wrong answers.

Read-only throughout; no trading behaviour touched.
# 2026-08-12 — Candle-first entries paused; MicroFlow collection begins

The owner stopped further LIVE iteration on `low_vol_reclaim_v2.1/v2.2`.
The account was flat, exchange attestation showed no orders, plans,
protections, recovery intents or ownership ambiguity, and the sole executor was
stopped cleanly. No strategy code was deleted.

The replacement research architecture predicts a bounded first-passage event
from public futures tape and L2 state, not the color of a future candle. PR A is
deliberately collector-only: official Bitget `trade` and `books5`, 12 symbols,
rolling flow/book/microprice state, persistence-aware episode deduplication and
immutable SHA-256-manifested gzip segments. There is no order client or
credential dependency. MicroFlow promotion remains blocked pending at least
100 independent events and positive chronological net evidence.
