# CGC BOT V5 — MASTER ROADMAP HERSTRUCTUREERD

VERSIE: 2026-07-06
STATUS: ACTIEF
VORIGE VERSIE: 2026-06-16

═══════════════════════════════════
NORTH STAR (ongewijzigd)
═══════════════════════════════════

MISSIE: Institutionele crypto execution engine die zelfstandig trades
selecteert, zichzelf analyseert, leert van gesloten trades, kapitaal
beschermt en schaalbaar is.

HOOFDREGEL: Safety > Truth > Expectancy > Alpha
WERKWIJZE: logs → bewijs → analyse → patch → validatie

UPDATE 2026-07-06 — ALLOCATIEMODEL:
Harde strategie-blokkades zijn vervangen door hedgefund-stijl dynamische
allocatie (eigenaar-goedgekeurd): strategie-niveau bepaalt de SIZE
(probe-modus op 50% risico bij negatieve expectancy), setup-niveau bepaalt
de GO/NO-GO (geometrie, entry-kwaliteit, regime). Kill-switches en
symbool-blokkades blijven hard.

═══════════════════════════════════
HUIDIGE STATUS — 2026-07-06
═══════════════════════════════════

BOT STATUS:

[x] Bot start stabiel, 1 runner actief, Public/Private API OK
[x] MTF refresh gezond (28 symbolen)
[x] Max open positions: 4 (eigenaar-goedgekeurd), max 2 per strategie
[x] Dagelijkse leerloop draait IN de bot (launchd is TCC-geblokkeerd op ~/Desktop)
[x] CSV-rotatie actief op alle telemetrie-loggers (25MB cap, 10 backups)
[x] AI-audit-agent (morning_audit) draait mee in dagelijkse keten
[x] AI-coach in-process actief, elke scan-cyclus

OPGELOSTE PROBLEMEN SINDS 2026-06-16:

[x] Leerloop was 3 weken dood (launchd + 600MB CSV's) → in-bot refresh + rotatie
[x] Testsuite schreef nepwinsten in productie-leerdata (53 rijen, ~+162) → conftest-isolatie + data opgeschoond
[x] Echte all-time PnL bleek −5,28 i.p.v. +32 (nepwinst-vervuiling)
[x] low_vol_reclaim R-math: 1,00R + fee-korting = wiskundig verlies → coherent 1,30R end-to-end
[x] Slot-verstopping: reclaim bezette beide slots (1078 skips) → 4 slots, cap 2/strategie
[x] Coach "reduce" was hard block op n=3 → probe-modus + MIN_TRADES=5
[x] Expectancy over volledige historie → 30-dagen rolling window (herkwalificatie mogelijk)
[x] tp1_hit_rate werd nooit berekend → nu per strategie in rapport (voedt adaptive TP)
[x] Adaptive TP-engine kreeg hardcoded 0-hitrates → echte rates uit expectancy-rapport
[x] Sessie-analyse: 08-11h + 23-00h UTC structureel rood → size ×0,5 in die vensters
[x] close_futures_position(direction=...) bug → al gefixt (direction→hold_side mapping + **_ vangnet)
[x] Spread parser mismatch: notes zeggen "spread_bps=X", parsers lazen "spread " → beide formaten (planner + reclaim); planner rekende met spread=0!
[x] Duplicate close rows → dedupe-blocker in TradeDatasetV2Logger (ook over herstarts)
[x] Daily kill-switch schaalt met equity i.p.v. flat $10

ACTIEVE AANDACHTSPUNTEN:

[ ] TP1-hit-rate is 10,5% op oude geometrie — verse data met 1,30R moet uitwijzen of reclaim levensvatbaar is
[ ] Sessie-vensters zijn hypothese op n=167 — leerloop moet ze blijven toetsen
[ ] Bewijs verzamelen dat de 4 regimes nu allemaal daadwerkelijk executeren
[ ] Entry-context backfill (volume ratio, candles_held, MAE/MFE in close rows) — loopt als aparte sessie

═══════════════════════════════════
PHASE 0 — SAFETY / EXPECTANCY LEAK STOPPER
═══════════════════════════════════

P0.1 — RISK / EXPECTANCY GUARDS — AFGEROND, met wijzigingen:

[x] Alle eerdere [x]-punten blijven staan
[x] Low-vol-reclaim minimum rr_to_tp1: 1,00 → **1,30** (fee-wiskunde: bij 12bps
    roundtrip is 1,00R netto 0,7R win / 1,3R loss = verlies onder 62% WR)
[x] TP-engine en planner aligned op 1,30R (was: gates eisten 1,30 maar TP werd
    op 1,00 gebouwd — trades konden alleen via soft bridge door)
[x] Soft bridge eist nu ook 1,30
[x] Reclaim TP-cap afgeleid van werkelijke stop (×1,45) i.p.v. ATR-formule
    die elke 1,30R-target blokkeerde
[x] Reclaim net-edge floor: TP1-move ≥ 2,5× (spread+fees); 0,70-ondermijning verwijderd
[x] Duplicate close spam: dedupe-blocker actief (L13)

NOG TE BEWIJZEN (logs):

[ ] RR_TO_TP1_PASS zichtbaar op nieuwe reclaim-trades
[ ] Geen 4+ correlated shorts tegelijk — bewijs nodig
[ ] Bot draait stabiel door de nacht op nieuwe allocatie — bewijs verzamelen

P0.5 — WINSTREALISATIE — GEDEELTELIJK AFGEROND:

[x] Kernvraag beantwoord met data: trades die TP1 halen zijn +0,083/trade (61% WR),
    trades die TP1 niet halen −0,045/trade. TP1-bereikbaarheid IS de groen/rood-scheiding.
[x] Fees/slippage impact: fees waren groter dan de bruto edge (reclaim: gross −3,89, fees 4,57)
[x] Realized PnL per dag uit exchange truth
[ ] Time-to-profit meting → zie entry-context backfill sessie
[ ] Dead trade timeout engine
[ ] Max trade duration per strategy
[ ] Reclaim target op liquidity/origin i.p.v. vaste RR — NA bewijs uit verse 1,30R-data

P0.7 — EXECUTION EXPECTANCY AUDIT — GROTENDEELS AFGEROND:

[x] adaptive_momentum_continuation: observe-only in planner + geblokkeerd door
    ENABLED_STRATEGIES allow-list in risk én execution (uniform)
[x] Strategy weighting: SOFT_PENALTY → **PROBE-modus** (supersedes HARD_BLOCK-plan;
    eigenaar-goedgekeurd: 50% size i.p.v. bevriezen, zodat herkwalificatie mogelijk is)
[x] tp1_hit_rate < 0,25 bij trades ≥ 5 → PROBE (was gepland als hard block)
[x] Spread parser gestandaardiseerd (spread_bps= én spread )
[x] Execution unsupported-strategy gate met expliciete allow-list uit .env
[x] Max open positions uniform tussen risk_manager en execution_service
    (beide lezen settings.max_open_positions; execution heeft extra per-strategie cap)
[x] hard_cap_notional gelogd bij iedere executable trade (EXECUTABLE_TRADE_CAPS)
[ ] fallback_candidate_bridge uit reclaim-detectie zodra adaptive fallback definitief dood is
[ ] decision_snapshot markeren als PRE_EXECUTION_SKIPPED wanneer plan execution-gates niet haalt
[ ] realized execution report met skip_reason categorieën

P0.8 — RED DAY DEFENSIVE MODE — AFGEROND (kern):

[x] Daily loss kill-switch (equity-geschaald, HARD_DAILY_STOP_PCT)
[x] 3 losses op rij → hard block via kill-switch gate
[x] Strategy negatieve expectancy → probe-modus (supersedes tijdelijk HARD_BLOCK)
[x] Dagelijkse RED/GREEN mode gelogd per scan-cyclus (DAY_MODE | mode=...)
[x] Sessie-vensters: rode uren automatisch halve size (nieuw, data-gedreven)
[ ] RED/GREEN mode opnemen in daily_learning_report

═══════════════════════════════════
PHASE 1 — STABILITEIT & BESCHERMING
═══════════════════════════════════

P1.1 — PROTECTION LIFECYCLE: ongewijzigd open; bewijs verzamelen nu de bot
op nieuwe allocatie draait.

P1.1A — NEAR-TP PROTECTION ENGINE — DATA IS ER NU:
De TP1-analyse (61% WR na TP1 vs 43,6% zonder) ondersteunt eerdere protectie.
Volgende stap: histogram 80/85/90-trigger bouwen op verse 1,30R-trades en
simuleren hoeveel SL's voorkomen waren. Daarna pas activeren.

P1.4 — RUNTIME RELIABILITY — DEELS:
[x] Watchdog (notificatie-only) geladen; meldt bot-down via macOS-notificatie
[x] Learning refresh + audit resilient (crasht nooit de keten)
[ ] heartbeat monitor, memory/cpu/disk monitor, auto-restart: open

P1.5 — PROTECTION INTERFACE INTEGRITY:
[x] close_futures_position(direction=...) — gefixt (was actieve bug)
[ ] protection action integration tests
[ ] lifecycle action validation

NIEUW — P1.6 — LAUNCHD/TCC BEPERKING (gedocumenteerd):
macOS TCC blokkeert launchd-children die in ~/Desktop schrijven
("operation not permitted"). Daarom draait de dagelijkse keten in-bot.
Opties als launchd ooit terug moet: Full Disk Access voor zsh/python,
of project verhuizen buiten ~/Desktop.

═══════════════════════════════════
PHASE 2 — EXCHANGE TRUTH
═══════════════════════════════════

P2.1/P2.2 — status ongewijzigd t.o.v. 2026-06-16, plus:
[x] Oude fake-PnL rows opgeschoond (53 test-rijen verwijderd met backup)
[x] Duplicate closes uitgesloten (dedupe-blocker — TRUSTED-voorwaarde dichterbij)
[x] Expectancy-rapport: 30-dagen window + tp1_hit_rate + recovery gescheiden van strategy
[ ] exchange_truth_missing_pnl_count = 0 — blijft open
[ ] data_confidence_verdict = TRUSTED — blijft open
[ ] confidence dashboard

P2.3 — EXECUTION TRUTH LAYER: open (slippage/latency/fill-kwaliteit).

═══════════════════════════════════
PHASE 3 — DATA FOUNDATION — open zoals gepland, met prioriteitsnotitie:
═══════════════════════════════════

Eerst het entry-context backfill (aparte sessie loopt): entry_volume_ratio,
candles_held, MAE/MFE in close rows. Dat is de kleinste stap die de
leerloop het meest voedt. Parquet-laag (P3.2) en event_logger (P3.1)
daarna.

═══════════════════════════════════
PHASE 4 — LEARNING ENGINE — grote stappen gezet:
═══════════════════════════════════

[x] P4.5 kern: per-strategie expectancy vóór entry gelezen (weighting gate)
[x] Sample-size guard: min 5 trades (coach én weighting gate)
[x] PROBE i.p.v. HARD_BLOCK bij negatieve expectancy (eigenaar-goedgekeurd)
[x] 30-dagen rolling window → degradatie snel zichtbaar, herkwalificatie mogelijk
[x] TP1-hit-rate per strategie → adaptive TP engine
[ ] P4.3 live↔backtest parity: open
[ ] P4.4 verdict aggregation: open

L8 — LEARNING GATES herzien:
De poort "geen TP/SL-tuning zonder 30 MFE/MAE-trades" is deels gepasseerd:
de 1,30R-kalibratie is gedaan op fee-wiskunde + 167 echte closes (geen gevoel).
Verdere kalibratie (TP-afstand per regime, near-TP protectie) wacht op verse data.

═══════════════════════════════════
PHASE 5 — ENTRY ALPHA — ongewijzigd open
═══════════════════════════════════

Late Entry Killer (P5.3) deels actief: exhaustion >80 block werkt live
(gezien: ATOMUSDT score 116 terecht geweigerd op exhaustion 97).

═══════════════════════════════════
NIEUWE PUNTEN — 2026-07-06
═══════════════════════════════════

N1 — REGIME EXECUTION PROOF (prioriteit 1)
[x] Funnel-rapport per dag: reports/backtests/strategy_funnel.json
    (candidates → GO → plans → EXECUTABLE → EXECUTED/SKIPPED per strategie,
    plus regime_coverage_verdict). Timestamps toegevoegd aan candidates/plans/
    executions CSV's (schema-safe rotatie bij header-wijziging).
[ ] Bewijzen dat alle 4 strategieën daadwerkelijk executies halen
    (verdict moet ALL_REGIMES_EXECUTING worden — data verzamelen)
[ ] Na 2 weken: eerste expectancy-lezing per strategie op verse data

N2 — SESSIE-VENSTER VALIDATIE
[ ] Sessie-vensters (08-12, 23-01 UTC) periodiek hertoetsen in daily report
[ ] Per-uur expectancy opnemen in daily_learning_report
[ ] Vensters automatisch bijstellen zodra n per uur ≥ 15

N3 — PROBE→FULL PROMOTIE-REGELS
[x] Promotie/demotie gebeurt inherent per scan-cyclus: window-expectancy ≥ 0
    → FULL, < 0 (bij ≥ 5 trades) → PROBE; kill-switch → BLOCKED
[x] Transities gelogd: ALLOCATION_CHANGED | strategy | oud -> nieuw
[ ] Evaluatie na 2 weken of een expliciete ≥10-trades promotiedrempel nodig is
    (nu: zelfde drempel als demotie; hysterese toevoegen indien flip-flop zichtbaar)

N4 — TP1-HIT-RATE HERSTELMETING (reclaim 1,30R)
[x] fresh_since_geometry_fix per strategie in strategy_expectancy.json
    (cutoff 2026-07-05T13:00Z = deploy 1,30R): trades, tp1_hit_rate,
    expectancy, winrate op uitsluitend verse trades
[ ] Beslisregel uitvoeren: als fresh tp1_hit_rate < 20% na 15 verse trades →
    reclaim TP-model herzien richting liquidity/origin targets (P0.5-punt)

N5 — AI-AUDIT MODELKEUZE
[ ] Lokale modellen (qwen2.5-coder:14b, gemma4) leveren geen valide audit-JSON
    op volledige prompt; pipeline valt terug op rule-based (werkt).
[ ] Optie: kleinere gerichte prompts per sectie, of gehost model voor de
    dagelijkse audit.

N6 — INSTRUMENTATIE-BACKFILL (loopt — aparte sessie)
[ ] entry_volume_ratio, candles_held, MAE/MFE in close rows
[ ] Daarna: L3 setup-quality learning kan pas echt beginnen

TODO — Dashboard V5 Refactor later oppakken (ongewijzigd)
- Components verder uitsplitsen: learning_engine, todays_priorities,
  live_status, wallet_overview, executive_kpi_row
- Daarna Dashboard V5 freeze: alleen bugfixes.
