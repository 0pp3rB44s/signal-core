# CGC BOT V5 — MASTER ROADMAP HERSTRUCTUREERD

VERSIE: 2026-07-07 (EOD)
STATUS: ACTIEF
VORIGE VERSIE: 2026-07-06

═══════════════════════════════════
NORTH STAR (ongewijzigd)
═══════════════════════════════════

MISSIE: Institutionele crypto execution engine die zelfstandig trades
selecteert, zichzelf analyseert, leert van gesloten trades, kapitaal
beschermt en schaalbaar is.

HOOFDREGEL: Safety > Truth > Expectancy > Alpha
WERKWIJZE: logs → bewijs → analyse → patch → validatie

ALLOCATIEMODEL (2026-07-06, eigenaar-goedgekeurd): strategie-niveau bepaalt
de SIZE (probe = 50% risico bij negatieve expectancy of zwakke TP1-hit-rate),
setup-niveau bepaalt GO/NO-GO. Kill-switches en symbool-blokkades blijven hard.

═══════════════════════════════════
HUIDIGE STATUS — 2026-07-07 EOD
═══════════════════════════════════

BOT STATUS:

[x] Bot draait stabiel (0 errors), 4 slots, cap 2/strategie
[x] 16 executies op één dag — trade-flow hersteld (was 2/7uur)
[x] Funnel-verdict: PARTIAL_COVERAGE (reclaim, breakdown, continuation
    executeren; sweep + breakout nog 0 — sweep vandaag pas ontgrendeld)
[x] Dagelijkse leerloop in-bot; AI-audit draait mee (rule-based fallback)
[x] Live equity-sync actief: echte saldo €62,51 (configured stond op €100!)
[x] Supervisor-script beschikbaar (scripts/run_supervised.sh via tmux)

DOORBRAAK VAN DE DAG — GEOMETRIE-ANKER (Truth-bug, gefixt + live):
De planner rekende alle geometrie vanaf het detectie-retest-niveau, maar
executie vult tegen marktprijs — mediaan 30 bps verderop. Vanaf de échte
fill: stop ~30 bps (noise-vloer), TP op 2,6-3,8R i.p.v. ontworpen
1,05-1,30R. Sterkste verklaring voor de ingestorte TP1-hit-rate, en
onzichtbaar doordat de slippage-meting zelfreferentieel was (altijd 0,0000).
Fix: build() en _build_stop() ankeren op market.primary.latest_close;
plans loggen geometry_anchor + detection_entry_drift_bps.

OPGELOST 2026-07-07 (alles getest, 59 tests groen):

[x] Schema-drift guard op alle DictWriter-loggers (68-kolom rows in
    59-kolom file schoof alle waarden een kolom op) + 13 scheve rijen
    gerepareerd met backup
[x] Excursie-analyse (19 closes): elke trade ging éérst goed (mediaan piek
    +0,37%, MAE≈0) — winst werd niet geoogst. Simulatie: lock@45% → verlies
    −1,43 → −0,47. PROFIT_LOCK_TP1_FRACTION 0,60 → 0,45
[x] Momentum volume-gate: binnen 75% van de eis → probe-size i.p.v. hard
    block (74 push-kandidaten stierven per dag op deze poort)
[x] Sweep-detector ontgrendeld: displacement 0,12→0,09, volume 1,15→1,05
    (78+52 near-miss rejects; diepe-liquiditeit regime had nul dekking)
[x] WEEKLY_FREEZE_LOSS_PCT was dode knop → afgedwongen in kill-switch
    (7d rolling PnL) + in DAY_MODE zichtbaar
[x] Statische equity → live Bitget-snapshot elke cyclus; risk/planner/
    execution via fail-closed resolver (stale → min(snapshot, configured))
[x] 28-min tighten-gap (FILUSDT live-bewijs): mislukte SL-tighten zet nu
    persistente retry-intent → elke 10s-cyclus opnieuw, tighter-only
[x] Dead-trade timeout: vlak + geen TP1 na 90 min (reclaim) / 240 min
    (overig) → nette close, alleen met verified exchange-state
[x] Slippage-meetregel in strategy_funnel.json
[x] Geometrie-anker (zie boven)
[x] Legitimiteits-audit 16 trades: poorten + sizing volledig traceerbaar
    (probe 0,25% ✓, sessie ×0,5 ✓, equity-caps ✓, 2/cyclus ✓)

EERDER OPGELOST (2026-07-05/06 — verkort):
leerloop 3 weken dood → in-bot; testdata-vervuiling opgeschoond (echte
all-time PnL −5,28); reclaim 1,30R end-to-end; slot-verstopping → 4 slots;
coach probe-modus + MIN_TRADES=5; 30d rolling window; tp1_hit_rate in
rapport → adaptive TP; sessie-vensters 08-12/23-01 UTC ×0,5; spread-parser;
dedupe-blocker; equity-geschaalde daily kill-switch; TP-engine floor 1,05R
(94+93 geometrie-blocks per dag opgelost); position_manager 2900→1138
regels + 3 modules; 14 lifecycle-safety-tests; fail-closed daily report.

ACTIEVE AANDACHTSPUNTEN:

[ ] TP1-hit-rate meting is GERESET: meet vanaf 2026-07-07T15:04Z
    (geometrie-anker deploy) — eerdere "verse" trades hadden opgeblazen
    TP-afstanden en tellen niet als bewijs tegen de strategieën
[ ] ACCOUNT_EQUITY_USDT=100 in .env bijwerken naar ~60 (fallback-waarde;
    resolver gebruikt live saldo, maar de fallback moet realistisch zijn)
[ ] Supervisor adopteren: bot starten via
    tmux new -s cgcbot 'bash scripts/run_supervised.sh'
[ ] Entry-context backfill (aparte sessie) — status checken

═══════════════════════════════════
PHASE 0 — SAFETY / EXPECTANCY LEAK STOPPER — AFGEROND
═══════════════════════════════════

P0.1 risk/expectancy guards: afgerond (1,30R coherent, dedupe, guards).
P0.5 winstrealisatie: kernvragen beantwoord met data; dead-trade timeout
  en max-duration per strategie zijn gebouwd (2026-07-07).
  [ ] Reclaim target op liquidity/origin i.p.v. vaste RR — beslissen op
      verse market-anchor data (zie N4)
P0.7 execution audit: afgerond op 3 restpunten na:
  [ ] fallback_candidate_bridge uit reclaim-detectie (adaptive is dood)
  [ ] decision_snapshot → PRE_EXECUTION_SKIPPED markering
  [ ] realized execution report met skip_reason categorieën
P0.8 red day defensive: afgerond.
  [ ] RED/GREEN mode opnemen in daily_learning_report

NOG TE BEWIJZEN (logs):
[ ] Geen 4+ correlated shorts tegelijk (cluster-gate bestaat — bewijs)
[ ] Nacht-stabiliteit onder supervisor

═══════════════════════════════════
PHASE 1 — STABILITEIT & BESCHERMING
═══════════════════════════════════

P1.1A — NEAR-TP / PROFIT-LOCK — ACTIEF OP BEWIJS:
[x] PROFIT_LOCK_BE bij 45% van TP1-afstand (was 60; simulatie op 19 echte
    trades: −1,43 → −0,47). Tighter-only, idempotent, getest.
[ ] Na 2 weken: giveback-reductie meten; 40/45/50-trigger vergelijken
[ ] Vervolgstap: partial-profit i.p.v. alleen BE-lock overwegen (op data)

P1.4 — RUNTIME RELIABILITY:
[x] Supervisor met backoff + max-crashes fail-closed stop (2026-07-07)
[x] Watchdog-notificatie (launchd)
[ ] heartbeat/memory/cpu/disk monitor: open

P1.5 — PROTECTION INTERFACE INTEGRITY:
[x] close_futures_position(direction=...) gefixt
[x] Tighten-retry persistent (2026-07-07)
[ ] protection action integration tests
[ ] lifecycle action validation

P1.6 — LAUNCHD/TCC: gedocumenteerd (in-bot keten; Full Disk Access of
project buiten ~/Desktop als launchd ooit terug moet).

═══════════════════════════════════
PHASE 2 — EXCHANGE TRUTH
═══════════════════════════════════

[x] EXCHANGE_TRUTH op alle 13 verse closes (na schema-repair zichtbaar)
[x] Duplicate closes uitgesloten; fake rows opgeschoond
[ ] exchange_truth_missing_pnl_count = 0 — bijna (enkele closes zonder pnl)
[ ] data_confidence_verdict = TRUSTED
[ ] confidence dashboard

P2.3 — EXECUTION TRUTH LAYER — GESTART:
[x] Slippage-meetregel in funnel-rapport
[ ] N8: échte fill-extractie (FILL_ANALYTICS_FALLBACK gezien; expected==
    actual==plan-gemiddelde → meting pas betrouwbaar met echte fills)
[ ] order fill latency, protection placement latency

═══════════════════════════════════
PHASE 3 — DATA FOUNDATION — open
═══════════════════════════════════

Eerst entry-context backfill (aparte sessie), dan event_logger (P3.1),
dan parquet-laag (P3.2).

═══════════════════════════════════
PHASE 4 — LEARNING ENGINE
═══════════════════════════════════

[x] Expectancy vóór entry (weighting gate), 30d window, probe-allocatie
[x] TP1-hit-rate → adaptive TP engine
[ ] P4.3 live↔backtest parity
[ ] P4.4 verdict aggregation
[ ] L5 TP/SL-kalibratie per regime — NU met schone geometrie meetbaar

═══════════════════════════════════
PHASE 5 — ENTRY ALPHA — GESTART (2026-07-07 avond)
═══════════════════════════════════

FUNNEL-BALANS AUDIT (eigenaar-vraag "zitten we dichtgetimmerd?"):
Nee — van 43 geblokkeerde plannen stierf er 1 aan een enkele poort; de rest
faalde op 2-3 onafhankelijke kwaliteitschecks tegelijk (gem. 2,4/plan).
Poorten versoepelen laat alleen slechtere trades door. De bottleneck was
setup-TIMING: alle momentum-kandidaten waren al "prearmed" maar arriveerden
ná de expansie → 38/40 exhaustion-blocks.

FORWARD-RETURN STUDIE (12 symbolen × 1000 candles, 331 gesimuleerde
entries, echte BreakoutEngine):
  COIL (pre-breakout) na expansie:  +0.198R/trade, 61.5% TP1 (n=26) ← beste
  COIL zonder expansie:             -0.065R, 40.4% TP1 (n=151)
  CHASE (post-breakout) zonder exp: -0.121R, 30.8% TP1 (n=107)
  CHASE na expansie:                -0.065R, 25.5% TP1, 48.9% timeout (n=47)
Conclusie: pre-breakout coils > post-breakout chases over de hele linie;
de exhaustion-gate had gelijk voor chases maar blokkeerde óók de beste
bucket (coil-na-expansie = "push meeliften").

GEÏMPLEMENTEERD:
[x] Coil-detectie: prearmed kandidaat die opgerold binnen 0,20% van het
    triggerniveau zit met pressure >= 55 krijgt entry_model=pre_breakout_coil
    (+coil_distance_pct), long/short symmetrisch, per-detect reset
[x] Exhaustion-gate: coil-na-expansie → PROBE-size i.p.v. hard block
    (n=26 is klein; leerloop moet promotie verdienen); chases blijven hard
[x] master_entry_quality gedemoteerd naar observability
    (raakte 43/43, was 1x sole blocker; note master_entry_quality_would_
    have_blocked=true bewaart het bewijs voor eventuele her-promotie)
[x] 3 gate-tests (coil-probe, chase-block, coil-vol-size)

OPEN:
[ ] Coil-performance volgen in funnel/expectancy (aparte bucket zichtbaar
    via entry_model note) — promotie naar volle size op >= 15 verse trades
[ ] Score-drempel overlap (kandidaat GO>=70 vs risk-minima 74-82):
    gedocumenteerd, bewust NIET samengevoegd (gedragswijziging zonder nood)
[ ] Late Entry Killer versie 2: origin_distance/freshness op coil-niveau

═══════════════════════════════════
OPEN PUNTEN — 2026-07-07
═══════════════════════════════════

N1 — REGIME EXECUTION PROOF
[x] Funnel-rapport per dag draait
[~] 3 van 4 regimes executeren (reclaim/breakdown/continuation);
    sweep vandaag ontgrendeld, breakout wacht op setup
[ ] ALL_REGIMES_EXECUTING verdict halen

N2 — SESSIE-VENSTER VALIDATIE
[ ] Per-uur expectancy in daily report; vensters hertoetsen bij n≥15/uur

N3 — PROBE→FULL PROMOTIE
[x] Werkt inherent per cyclus + ALLOCATION_CHANGED logging
[ ] Hysterese-evaluatie na 2 weken (flip-flop check)

N4 — TP1-HIT-RATE HERSTELMETING — CUTOFF VERSCHOVEN
[ ] Meet vanaf 2026-07-07T15:04Z (geometrie-anker deploy); beslisregel:
    fresh tp1_hit_rate < 20% na 15 trades → reclaim TP naar liquidity/
    origin targets. Verwachting met schone ankering: TP-afstanden 40-70bps
    i.p.v. 90-150bps.

N5 — AI-AUDIT MODELKEUZE
[ ] Lokale modellen leveren geen valide audit-JSON; rule-based fallback
    werkt. Optie: sectie-prompts of gehost model.

N6 — INSTRUMENTATIE-BACKFILL (aparte sessie)
[ ] entry_volume_ratio, candles_held in close rows → daarna L3 learning

N7 — GEOMETRIE-ANKER VALIDATIE (nieuw)
[ ] Eerste market-anchor trades controleren: detection_entry_drift_bps
    in plan-notes, TP-afstand vs fill, TP1-bereik
[ ] Drift-guard overwegen: plan verwerpen als markt >X bps van
    detectie-niveau weggelopen is (nu: herprijzen; guard = strenger)

N8 — FILL TRUTH (nieuw)
[ ] Echte fill-prijs extraheren uit Bitget order-payload (nu vaak
    FILL_ANALYTICS_FALLBACK → expected==actual==plan-gemiddelde)
[ ] Daarna: slippage-cijfers betrouwbaar → limit-entry beslissing (P5)

TODO — Dashboard V5 Refactor (ongewijzigd, na freeze alleen bugfixes)
# Phase 2A baseline — 2026-07-15

- [x] Freeze deterministic execution baseline at `2009a4a5cc8525436df8fb4e09c93a5b2bd237f2`.
- [x] Create isolated, versioned performance-analysis command and reports.
- [x] Inventory all active/analysable strategies and aliases.
- [x] Run the uniform contract on all eight available tracked datasets.
- [ ] Acquire sufficient historical coverage: current 200 candles/symbol (~49h45m) yielded zero candidates and is insufficient for performance inference.
- [ ] Repeat Phase 2A unchanged on the longer dataset before any Phase 3 diagnostics or optimisation.

# Phase 2B evidence — 2026-07-15

- [x] Acquire 12 months of Bitget USDT-futures 15m candles for all eight required symbols.
- [x] Validate raw/canonical layers, hashes, common window, gaps, OHLC and alignment.
- [x] Re-run the frozen baseline twice with byte-identical core outputs.
- [x] Prove detectors are not permanently silent: 835 raw candidates across four active strategies.
- [ ] Resolve the evidence gap for historical orderbook risk context before profitability analysis; all 475 selector-accepted candidates currently fail closed at risk.
- [ ] Do not enter Phase 3 or alter gates until that missing historical context has an evidence-backed treatment.
