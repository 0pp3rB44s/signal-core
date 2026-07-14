# CGC BOT V5 — BLUEPRINT & HANDLEIDING

Laatst bijgewerkt: 2026-07-07 (PATCH-040)
Dit document beschrijft hoe de bot werkt, wat elk onderdeel doet, waar je
alles terugvindt en hoe je hem bedient. Zie docs/PATCHES.md voor de
patchhistorie en ROADMAP.md voor wat er nog komt.

═══════════════════════════════════
1. WAT DE BOT IS — IN ÉÉN ALINEA
═══════════════════════════════════

Een autonome crypto futures execution engine op Bitget (USDT-perpetuals,
15m/1H timeframes, 28 symbolen) die elke 60 seconden de markt scant, setups
detecteert via 5 strategieroutes, elke kandidaat door een keten van kwaliteits-
en risicopoorten haalt, trades uitvoert met exchange-geverifieerde SL/TP,
posities elke 10 seconden bewaakt, elke close terugleest van de exchange
(exchange truth), en dagelijks van zijn eigen resultaten leert om de
allocatie per strategie bij te sturen.

HOOFDREGEL: Safety > Truth > Expectancy > Alpha
WERKWIJZE:  logs → bewijs → analyse → patch → validatie

═══════════════════════════════════
2. DE PIJPLIJN — VAN SCAN TOT LEREN
═══════════════════════════════════

Elke trade doorloopt deze keten. Elke schakel heeft een eigen bestand:

  SCAN (elke 60s)                    app/runner.py
    ↓ candles 15m + 1H, orderbook   data/market_fetcher.py
    ↓ marktcontext-engines           market_data/breakout_engine.py
                                     market_data/volatility_engine.py
                                     market_data/entry_quality.py
                                     market_data/orderbook_analyzer.py
                                     market_data/liquidity_heatmap.py (read-only)
  DETECTIE (5 strategieroutes)       strategies/ (zie §3)
    ↓ StrategyCandidate
  SCORING                            strategies/scoring.py (0-100, GO≥70)
    ↓ StrategyScore
  RISK GATES                         risk/risk_manager.py (zie §4)
    ↓ RiskVerdict (allowed + risk% + probe)
  PLANNING                           planning/trade_planner.py
    ↓ TradePlan: entry (marktprijs-anker!), SL (structuur),
    ↓ TP's (adaptive TP engine), sizing, EXECUTABLE/BLOCKED
  EXECUTIE                           execution/execution_service.py
    ↓ market order → fill-truth → SL/TP op exchange → verificatie
  POSITIE-BEHEER (onafhankelijke loop) execution/position_manager.py
    ↓ + tp_sl_lifecycle.py (TP-hits, BE, profit-lock, tighten)
    ↓ + position_reconciler.py (exchange↔lokaal sync, ghost-preventie)
    ↓ + closed_trade_writer.py (gegarandeerde CLOSED rows)
  CLOSE → EXCHANGE TRUTH             Bitget position history = waarheid
    ↓ echte PnL/fees/exit
  DATASET                            telemetry/trade_logger.py
    ↓ logs/trade_dataset_v2.csv
  LEERLOOP (dagelijks, in-bot)       telemetry/dataset_builder.py
    ↓                                scripts/run_backtest.py --validation-only
    ↓                                morning_audit.py (AI-audit)
  ALLOCATIE                          reports → risk_manager leest terug:
                                     negatieve expectancy → probe (halve size)
                                     positief → volle size (automatisch)

═══════════════════════════════════
3. DE STRATEGIEËN (elk dekt een marktregime)
═══════════════════════════════════

| Strategie | Bestand | Regime | Geometrie |
|---|---|---|---|
| low_vol_reclaim | strategies/strategies/low_vol_reclaim.py | rustige markt, EMA-reclaim scalps | 1 TP op 1,30R, stop 30-85bps |
| momentum_breakout | strategies/momentum_breakout.py | opwaartse expansie | TP1≥1,05R + ladder |
| momentum_breakdown | zelfde bestand (subclass) | neerwaartse expansie | spiegel van breakout |
| trend_continuation | strategies/strategies/continuation.py | lopende trend, pullback-reclaim | TP1≥1,05R + ladder |
| liquidity_sweep_reversal | strategies/liquidity_sweep.py | stop-hunt reversals (diepe liquiditeit) | TP1≥1,05R |

Speciaal: **pre_breakout_coil** (PATCH-040) — een momentum-kandidaat die
opgerold binnen 0,20% van het triggerniveau zit met druk ≥55 vóórdat de
uitbraak er is. Herkenbaar aan note `entry_model=pre_breakout_coil`.
Bewijs: enige netto-positieve bucket in de forward-return studie
(+0,198R, 61,5% TP1). Draait op probe-size tot de leerloop promotie geeft.

De TP-geometrie komt uit execution/adaptive_tp_engine.py en heeft een
harde vloer van 1,05R (test-contract: de engine mag nooit TP's bouwen die
de planner-gates wiskundig afkeuren).

═══════════════════════════════════
4. DE POORTEN — WIE MAG BLOKKEREN EN WAAROM
═══════════════════════════════════

Volgorde: kandidaat → score → risk → planner → execution. "PROBE" =
doorlaten op halve size i.p.v. blokkeren.

RISK MANAGER (risk/risk_manager.py):
- Kill-switch dagverlies: ≥ HARD_DAILY_STOP_PCT van echte equity → HARD
- 3 verliezen op rij → HARD
- **Weekly freeze**: 7-daags verlies ≥ WEEKLY_FREEZE_LOSS_PCT → HARD
- Symbol-pause (negatieve expectancy per symbool) → HARD
- Coach (AI): symbol avoid → HARD; strategy reduce → PROBE
- Strategy weighting: negatieve 30d-expectancy of TP1-rate <25% → PROBE
- Momentum-quality: volume binnen 75% van eis → PROBE; chase na expansie
  → HARD; **coil na expansie → PROBE** (bewijs-gedreven, PATCH-040)
- Sessie-vensters (08-12, 23-01 UTC): size ×0,5 (data: structureel rood)
- Cluster-risk (gecorreleerde exposure), HTF-alignment, score-minima → HARD
- ENABLED_STRATEGIES allow-list (.env) → HARD, identiek in execution

PLANNER (planning/trade_planner.py):
- Geometrie geankerd op MARKTPRIJS (niet het detectie-retest-niveau!)
- rr_to_tp1 ≥ 1,00 (reclaim: 1,30) → HARD
- Stop ≤ 1,2× TP1-afstand (risk shape) → HARD
- Largest-loss guard: stop ≤ 85bps → HARD
- TP1 net-edge boven spread+fees (reclaim: ≥2,5× kosten) → HARD
- master_entry_quality: observe-only (gedemoteerd PATCH-040, was dood gewicht)

EXECUTION (execution/execution_service.py):
- Standaard max 2 open posities, max 2 per strategie, max 1 nieuwe per cyclus
- Notional-caps op echte equity; balance guard vóór order-send
- Confirmatie-allowlist per symbool

POSITIE-BEHEER (bescherming ná entry):
- SL/TP wordt op de exchange geverifieerd (protectie bestaat pas na verificatie)
- Profit-lock: standaard bij 60% van TP1-afstand → SL naar fee-adjusted break-even
  (minimaal 12bps kostendekking)
- Failed-continuation tighten: momentum dood → SL aanscherpen (persistent
  retry per position-sync tot geverifieerd; alleen strakker, nooit ruimer)
- Dead-trade timeout: vlak + geen TP1 na 90 min (reclaim) / 240 min → close
- Dedupe-blocker: geen dubbele close-rows, ook niet na herstart

═══════════════════════════════════
5. HET ALLOCATIEMODEL (hedgefund-stijl)
═══════════════════════════════════

Strategie-niveau bepaalt SIZE, setup-niveau bepaalt GO/NO-GO:

  FULL   → maximaal 0,75% risico per trade (ACCOUNT_RISK_PER_TRADE_PCT)
  PROBE  → maximaal 0,375% (×0,5) — bij negatieve expectancy, zwakke TP1-rate,
           coach-reduce, volume-tekort of coil-experiment
  Sessie-venster (rode uren) → nog eens ×0,5
  BLOCKED→ alleen kill-switches en symbool-blokkades

Promotie/demotie gebeurt automatisch per scan-cyclus op het 30-dagen
rolling window uit strategy_expectancy.json. Transities worden gelogd als
ALLOCATION_CHANGED. Sizing gebruikt de ECHTE Bitget-equity (elke cyclus
gesnapshot naar state/account_equity.json; fallback .env bij staleness).

═══════════════════════════════════
6. DE LEERLOOP (dagelijks, in-bot)
═══════════════════════════════════

Draait binnen de bot (launchd is TCC-geblokkeerd op ~/Desktop) zodra het
expectancy-rapport >24h oud is. Keten:

1. telemetry/dataset_builder.py — leest logs → data_store/
2. scripts/run_backtest.py --validation-only — bouwt:
   - reports/backtests/strategy_expectancy.json (30d window, per strategie:
     trades/winrate/expectancy/tp1_hit_rate + fresh_since_geometry_fix)
   - reports/backtests/strategy_funnel.json (candidates→GO→plans→
     EXECUTABLE→EXECUTED per strategie + regime_coverage_verdict + slippage)
   - reports/backtests/daily_validation.json
3. morning_audit.py — AI-audit (Ollama lokaal; valt terug op rule-based)
   → agents_v2/reports/audit.md + coach_decisions.json

De risk manager leest deze rapporten LIVE terug — dit is het zelflerende
mechanisme: slechte strategieën krimpen automatisch, goede groeien.

Logmarkers: LEARNING_REFRESH_STARTED / LEARNING_REFRESH_OK.

═══════════════════════════════════
7. WAAR VIND IK ALLES — BESTANDSGIDS
═══════════════════════════════════

Alles staat in de hoofdmap (zichtbaar in VS Code). De belangrijkste plekken:

LOGS (logs/):
- bot.out            → live proces-output; hier zie je DAY_MODE, PLAN_REJECT,
                       EXECUTABLE_TRADE_CAPS, ALLOCATION_CHANGED, LEARNING_*
- agent.log(.1-.7)   → gestructureerd applicatielog (roteert op 5MB)
- market_scan.csv    → elke scan per symbool (met liq_* heatmap-notes)
- strategy_candidates.csv → elke gedetecteerde setup + verdict
- trade_plans.csv    → elk plan + EXECUTABLE/BLOCKED + alle notes (rr, edge,
                       geometry_anchor, coil, blokkeer-redenen)
- executions.csv     → elke order: fill, slippage, SL/TP, notional
- trade_dataset_v2.csv → de leerdataset: elke close met exchange-truth PnL,
                       fees, MFE/MAE, TP-hits, close_reason
  (.1 = geroteerd archief; roteert op 25MB én bij schemawijziging)

STATE (state/):
- executed_trades.json   → levende posities + volledige lifecycle-status
- account_equity.json    → live equity-snapshot (elke cyclus ververst)
- liquidity_heatmap.json → read-only orderbook-heatmap per symbool
- bot.pid                → draaiend proces

RAPPORTEN (reports/backtests/):
- strategy_expectancy.json → wie verdient/verliest (30d window) — DE bron
                             voor allocatie
- strategy_funnel.json     → dagelijkse funnel per strategie + verdict
- daily_validation.json / latest_backtest.json / timestamped kopieën

AI (agents_v2/reports/):
- audit.md / audit.json    → dagelijkse audit
- coach_decisions.json     → live coach-besluiten die de risk gate leest

DOCS (docs/):
- BLUEPRINT.md (dit document), PATCHES.md, JOURNAL.md,
  position_manager_audit.md (dependency map van de refactor)
ROADMAP.md (hoofdmap) → actuele status + open punten

DASHBOARD: http://localhost:8501 (login vereist) — posities, protectie,
funnel, expectancy, equity curve, liquidity heatmap, AI-coach, bot start/stop.

═══════════════════════════════════
8. BEDIENING — COMMANDO'S
═══════════════════════════════════

Start bot:            bash scripts/start_bot.sh
Start met auto-herstel: tmux new -s cgcbot 'bash scripts/run_supervised.sh'
Stop alles:           bash scripts/stop_all.sh
Dashboard:            bash scripts/start_dashboard.sh  (of al draaiend)
Alle tests:           .venv/bin/python -m pytest tests/ -q
Backtest + rapporten: .venv/bin/python scripts/run_backtest.py
Alleen validatie:     .venv/bin/python scripts/run_backtest.py --validation-only
Verse candles:        .venv/bin/python scripts/download_backtest_data.py
Handmatige AI-audit:  .venv/bin/python morning_audit.py

Snel de dag checken (voorbeelden):
  grep "DAY_MODE" logs/bot.out | tail -3
  grep "EXECUTED" logs/executions.csv | tail -10
  python3 -m json.tool reports/backtests/strategy_funnel.json

═══════════════════════════════════
9. LOGMARKERS — WOORDENBOEK
═══════════════════════════════════

DAY_MODE                  → GREEN/RED + dag/week-PnL + echte equity per cyclus
PLAN_ACCEPTED/PLAN_REJECT → planner-uitkomst + redenen
NEAR_EXECUTABLE           → bijna-trade: wat ontbrak er precies
EXECUTABLE_TRADE_CAPS     → sizing-bewijs per uitgevoerde trade
ENTRY_PROTECTION_CONFIRMED→ SL/TP staat geverifieerd op de exchange
PROFIT_LOCK_BE            → winst-slot geactiveerd (standaard 60% van TP1)
FAILED_CONTINUATION_*     → momentum dood → SL-tighten (+ retry-intent)
DEAD_TRADE_TIMEOUT        → vlakke trade na max duur gesloten
POSITION_CLOSED_CLEAN     → nette close + exchange truth + dataset-row
ALLOCATION_CHANGED        → strategie FULL↔PROBE↔BLOCKED transitie
LEARNING_REFRESH_OK       → dagelijkse leerketen afgerond
AI_AGENT_DECISIONS_REFRESHED → coach-besluiten herladen
EQUITY_SNAPSHOT_FAILED    → equity-sync mislukt (fallback actief)
MASTER_ENTRY_QUALITY_OBSERVE → zou geblokkeerd hebben (observe-only)

═══════════════════════════════════
10. HUISREGELS VOOR WIJZIGINGEN
═══════════════════════════════════

1. Eerst meten (logs/funnel/dataset), dan hypothese, dan bewijs
   (simulatie of studie), dan pas patchen, dan effect valideren.
2. Safety-poorten falen dicht (fail-closed). Kwaliteitspoorten mogen
   naar probe degraderen, nooit stilletjes verdwijnen.
3. Elke patch krijgt een nummer in docs/PATCHES.md en een commit met
   het patchnummer in de titel.
4. Geen TP/SL- of strategie-tuning zonder data-onderbouwing.
5. Exchange truth wint altijd van lokale staat.
6. Tests groen vóór deploy; bot herstart via scripts/start_bot.sh.
