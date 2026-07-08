# CGC BOT V5 — PATCHREGISTER

Elke wijziging aan de bot krijgt een oplopend patchnummer. De commit-titel
begint vanaf PATCH-041 met het nummer (bv. "PATCH-041: ..."). Nummers
PATCH-001 t/m PATCH-040 zijn met terugwerkende kracht toegekend aan de
git-historie. Format per regel: nummer | datum | wat + waarom (kort).

## Basis

- PATCH-001 | 2026-06-15 | Initial stable trading bot deployment build
- PATCH-002 | 2026-06-23 | Phase7 working baseline (o.a. reclaim-thresholds aangescherpt na autopsy)
- PATCH-003 | 2026-06-23 | AGENTS-regels toegevoegd

## 2026-07-04 — Coach & runtime

- PATCH-004 | 2026-07-04 | AI-coach (agents_v2) in de risk gate + audit context builder geconsolideerd
- PATCH-005 | 2026-07-04 | launchd-invocatie fix (poging 1)
- PATCH-006 | 2026-07-04 | launchd EX_CONFIG fix: venv python direct excen
- PATCH-007 | 2026-07-04 | plist/healthcheck fix nagestuurd
- PATCH-008 | 2026-07-04 | launchd-supervisie vervangen door periodieke watchdog
- PATCH-009 | 2026-07-04 | Watchdog gedowngraded naar notificatie-only (macOS process-coalition kills)
- PATCH-010 | 2026-07-04 | Daily kill-switch schaalt met equity i.p.v. flat $10

## 2026-07-05 — Leerloop-herstel & geometrie-coherentie

- PATCH-011 | 2026-07-05 | Ontbrekende TP1-hit-rate niet meer als 0% behandelen (onterechte hard-blocks)
- PATCH-012 | 2026-07-05 | Dashboard v2 herbouwd: login gate, all-live data, botcontrole, perf-fix
- PATCH-013 | 2026-07-05 | GROTE DIAGNOSE: reclaim 1,00R→1,30R end-to-end (fee-wiskunde), leerloop
  gerepareerd (3 weken dood door 600MB CSV's → rotatie), testdata-vervuiling opgeschoond
  (53 neprijen; echte all-time PnL bleek −5,28 i.p.v. +32), 30d rolling window,
  conftest-isolatie zodat tests nooit meer productie-data schrijven
- PATCH-014 | 2026-07-05 | AI-audit: dataset-context gecapt, Ollama num_ctx verhoogd
- PATCH-015 | 2026-07-05 | Morning audit crasht nooit meer op AI-backend falen (600s timeout)
- PATCH-016 | 2026-07-05 | Leerrapporten verversen in-bot wanneer stale (launchd TCC-geblokkeerd)
- PATCH-017 | 2026-07-05 | AI-audit-agent draait mee in de dagelijkse in-bot keten

## 2026-07-06 — Allocatiemodel, flow-herstel & production safety

- PATCH-018 | 2026-07-06 | Hedgefund-allocatie: probe-modus (50% size) i.p.v. hard blocks;
  4 slots, cap 2/strategie (slot-verstopping: 1078 skips); coach MIN_TRADES=5
- PATCH-019 | 2026-07-06 | Sessie-vensters (08-12/23-01 UTC ×0,5) + echte tp1_hit_rate in rapport
- PATCH-020 | 2026-07-06 | Roadmap P0.7/P0.8/L13: spread-parser fix (planner rekende met spread=0!),
  ENABLED_STRATEGIES allow-list uniform, EXECUTABLE_TRADE_CAPS log, dedupe-blocker, DAY_MODE
- PATCH-021 | 2026-07-06 | N1/N3/N4: dagelijkse strategy funnel, ALLOCATION_CHANGED logging,
  fresh_since_geometry_fix metriek
- PATCH-022 | 2026-07-06 | Fase 1: position_manager audit + dependency map (docs/)
- PATCH-023 | 2026-07-06 | Fase 2: 12 safety-tests TP/SL lifecycle, reconciliatie, fail-safe
- PATCH-024 | 2026-07-06 | Fase 3a: closed_trade_writer.py geëxtraheerd (gedragsneutraal)
- PATCH-025 | 2026-07-06 | Fase 3b: position_reconciler.py geëxtraheerd
- PATCH-026 | 2026-07-06 | Fase 3c: tp_sl_lifecycle.py geëxtraheerd (2900→1138 regels orchestrator)
- PATCH-027 | 2026-07-06 | Fase 4: kill-switch fail-closed bij corrupt learning report
- PATCH-028 | 2026-07-06 | TP-engine/planner mismatch: engine bouwde 0,8-0,9R terwijl gates ≥1,0R
  eisen → vloer 1,05R (94+93 gegarandeerde blocks per dag opgelost); testcontract

## 2026-07-07 — Truth-doorbraken & entry alpha

- PATCH-029 | 2026-07-07 | Schema-drift guard alle CSV-loggers + 13 scheve rijen gerepareerd;
  profit-lock BE op 60% van TP1 (P1.1A) live
- PATCH-030 | 2026-07-07 | Single-TP mode plaatste TP2 i.p.v. TP1 op de exchange (L12-bug) → gefixt
- PATCH-031 | 2026-07-07 | Excursie-oogst: profit-lock 60%→45% (simulatie: −1,43→−0,47 op 19 echte
  trades); momentum volume-band → probe; sweep-detector ontgrendeld (regime had nul dekking)
- PATCH-032 | 2026-07-07 | WEEKLY_FREEZE was dode knop → afgedwongen; live equity-sync
  (echte saldo bleek €62,51 vs geconfigureerde €100!) met fail-closed resolver
- PATCH-033 | 2026-07-07 | Failed-continuation tighten: persistente retry-intent (28-min gap gedicht)
- PATCH-034 | 2026-07-07 | Dead-trade timeout (90m reclaim/240m rest) + slippage-metriek in funnel
- PATCH-035 | 2026-07-07 | GEOMETRIE-ANKER: planner prijsde vanaf detectie-retest-niveau terwijl
  executie tegen marktprijs vult (mediaan 30bps drift → echte TP's op 2,6-3,8R i.p.v. 1,05-1,30R,
  dé TP1-killer). Nu geankerd op marktprijs; drift zichtbaar per plan
- PATCH-036 | 2026-07-07 | ROADMAP EOD-update (N7/N8 toegevoegd)
- PATCH-037 | 2026-07-07 | N8 FILL TRUTH: execution las 4 sleutels die de extractor nooit
  produceerde → elke fill viel terug op plan-gemiddelde, slippage eeuwig 0,0000. Gefixt + retry
  + contract-tests
- PATCH-038 | 2026-07-07 | Liquidity heatmap: read-only analyselaag (walls, magneet, risk-zone)
  + dashboardpaneel; nul gedragsinvloed (eigenaar-spec)
- PATCH-039 | 2026-07-07 | P5 entry alpha: pre-breakout coil arming; forward-return studie
  (12×1000 candles, 331 entries): coil-na-expansie +0,198R/61,5% TP1 = enige positieve bucket →
  exhaustion-gate wordt probe voor coils, blijft hard voor chases; master_entry_quality
  gedemoteerd naar observe-only (43/43 raak, 1× beslissend = dood gewicht)
- PATCH-040 | 2026-07-07 | Docs: BLUEPRINT.md (handleiding), PATCHES.md (dit register),
  JOURNAL.md (voortgezet), patchnummering ingevoerd

## Vanaf hier

- PATCH-041 | 2026-07-07 | HTF regime-laag (1D/4H, 30-min cache): beide HTF's tegen richting →
  hard block, één → probe; testsuite voor het eerst 100% groen (80/80); .env equity-fallback → 60
- PATCH-042 | 2026-07-07 | Validatie-motor v1 + 90d candle-archief (12 symbolen, 105k candles,
  13.855 gesimuleerde entries): MET HTF-consensus +0,071R/trade, TEGEN −0,33R, zonder consensus
  −0,15R — eerste statistische edge-kaart; bevestigt de HTF-gate en wijst naar consensus-only
  trading als volgende kandidaat (besluit na live vergelijking)

- PATCH-043 | 2026-07-07 | FAST LANE: 5m-entry detectie op de top-8 symbolen van de basisscan
  (5m primair / 15m confirmatie), zelfde detectoren+scorer+gates+planner; HTF 1D/4H-regime geldt
  onverkort. Frequentie uit meer detectiekansen, niet uit lossere poorten; fee-vloer beslist per
  setup. Volledig omkapseld (kan basisscan nooit breken); FAST_LANE_* env-instellingen.

- PATCH-044 | 2026-07-08 | Ochtend-audit (25 nacht-trades, break-even +0,27): reclaim (mean-reversion)
  verdient alleen edge MET HTF-consensus. 90d-sweep bevestigde: consensus +0,071R (1,30R optimaal),
  geen consensus -0,15R (56% van volume, waar de bot 's nachts draaide), tegen -0,35R. Patch:
  reclaim zonder volledige 1D+4H-consensus in de richting -> probe-size (halveert de chop-drag,
  houdt de in-regime edge op volle size). TP-afstand ongewijzigd (bewezen optimaal). Test-isolatie
  van equity-snapshot gefixt (deterministisch). 82/82 groen.

- PATCH-045 | 2026-07-08 | FUNDAMENTELE FIX (grondige audit): SL/TP werden op vaste plan-niveaus
  geplaatst (geankerd op latest_close) terwijl de market-order op de live prijs vult - structureel
  0,1-0,4% verderop, altijd richting de stop. Stopafstand verschrompelde 30-90% (rr tot 22:1) ->
  uitgestopt op ruis vóór TP. Dé verklaring voor lage winrate ondanks correcte richting. Fix:
  TradePlan.geometry_entry + execution herankert stop/TP op de echte fill (zelfde prijs-ratio's).
  85/85 groen.

Nieuwe wijzigingen: verhoog het nummer, zet "PATCH-0XX:" vooraan de
commit-titel en voeg hier één regel toe (datum | wat + waarom).
