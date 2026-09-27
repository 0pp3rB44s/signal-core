# CHANGELOG — signal-core

Alleen gemergede wijzigingen op `main`. Details per onderwerp: gekoppelde
docs; onderzoeksverdicts: docs/RESEARCH_JOURNAL.md.

## 2026-07-27 — RC1: forward-paper-validatie afgesloten en gearchiveerd
- **Release Candidate 1** vastgelegd. Volledige forward-paper-levenscyclus
  aantoonbaar; 72-uursvalidatie **niet gehaald** (20,56 u effectief van 72 u).
  Eindoordeel audit: NOT READY FOR NEXT PHASE. Zie RELEASE_NOTES.md.
- Vijf defecten opgelost, elk met regressietest (suite: 339 passed, 2×):
  ContractSpec-serialisatie (brak elke paper-write), scanloop zonder
  exception-afhandeling (20 u uitval), break-even-stop die winst boekte op een
  niet-verhandelde prijs, `granularity=1h` door Bitget geweigerd (400171, 742×),
  en een mislukte scan die zichzelf als `scan_cycle_complete` publiceerde.
- Timeframe-normalisatie heeft nu één canonieke grens:
  `clients/bitget_market_client.py:api_granularity()`. Minuten kleine letter,
  uren/dagen/weken/maanden hoofdletter; `1M` (maand) hoofdlettergevoelig
  gescheiden van `1m` (minuut). Alle 18 aliassen live geverifieerd.
- Healthcheck kent nu `SCAN_PRODUCED_NO_MARKET_DATA`, `SCAN_LOOP_FAILING` en
  `DEGRADED`; een levend proces telt niet langer als een werkend proces.
- Bewijsarchief: `validation_72h/archive/RC1_forward_paper_validation.tar.gz`
  (912 KB, 63 bestanden, integriteit 63/63 geverifieerd).
- Nieuwe documentatie: docs/FORWARD_PAPER_VALIDATION.md, docs/RISK_REGISTER.md,
  docs/RECOVERY_PROCEDURES.md, docs/MONITORING_GUIDE.md,
  docs/KNOWN_LIMITATIONS.md, docs/REPOSITORY_STRUCTURE.md, RELEASE_NOTES.md.
- **Onveranderd:** geen bewezen edge; 2 gesloten papertrades hebben geen
  statistische betekenis; `EXECUTION_ENABLED=false` blijft van kracht.

## 2026-07-18 — PR #11: microstructuur-archivering + loggerfix
- `archiving/`-pakket: orderbook (Bitget REST, 10 s), funding (poll +
  settlements), liquidations (Bybit v5 WS; Bitget heeft geen publiek
  kanaal). Dedupe met crash-herstel, dagrotatie+gzip, retentie, disk-guard,
  heartbeat, AST-geborgde scheiding van executiecode. Runbook:
  docs/ARCHIVING.md.
- Fix market-context-logger: gestructureerde orderbook-kolommen
  (spread/imbalance/bias/walls) worden weer gevuld (root cause:
  notes-formaatdrift na unified-engine-refactor).
- Onderzoeksbewijsketen H-4D-1 (verworpen) meegemerged.

## 2026-07-17/18 — research/h4d2-time-of-day (archiefbranch, niet gemerged)
- H-4D-1 reproductie-audit: exact gereproduceerd, verwerping bevestigd.
- H-4D-2 (time-of-day/sessie): 0/30 tests BH-significant → VERWORPEN.
- H-4D-3 (VWAP-deviation reversie): BH-p ≥ 0,26, richting tegengesproken →
  VERWORPEN. Programma-stand: geen edge in huidige publieke data.

## 2026-07-17 — PR #10: GitHub- en dual-architecture-runnersynchronisatie
- CI-workflow (compile, shell-syntax, hygiene, pytest, arch-preflight).
- `scripts/bootstrap_mac.sh`, `deploy_runner.sh` (annotated `runner-v*`-tags,
  preflight, rollback-refs), verify-scripts; `.python-version` = 3.12.
- Contract: docs/RUNNER_MIGRATION.md (GitHub transporteert nooit secrets,
  state of logs).

## 2026-07-16 — PR #9: unified candidate lifecycle
- Eén levenscyclus voor kandidaten met crash-veilige paper-transities;
  deterministische candidate-/plan-id's.

## 2026-07-16 — PR #8: structured strategy funnel telemetry
- Hash-chained funnel-events (DETECTOR→…→OUTCOME), corruptiedetectie,
  concurrentie-veilige append; analyzer met reproduceerbare hashes.

## 2026-07-16 — PR #7: unified market feature engine
- Productie, backtest, replay en validatie delen exact dezelfde
  feature-berekening (veld-exacte pariteit, contract-getest).

## Ouder (samengevat)
- Fase 2-3: 367 live trades, exchange-truth-reconciliatie, kill-switches,
  weekly freeze, geometrie-anker-fix — uitkomst: geen strategieniveau-edge;
  sinds 2026-07-13 observe-only (eigenaar-besluit).
- Fase 4A-4C: OHLCV-directioneel, funding/OI, basis/mark-index — verworpen.
