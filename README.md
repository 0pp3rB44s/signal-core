# signal-core — Bitget research- & executieplatform

Institutioneel opgezet crypto-futures-platform (Bitget USDT-perps) met één
harde regel: **live gaan mag pas bij aantoonbaar statistisch bewijs**. Dat
bewijs bestaat op dit moment niet — alle geteste hypothesefamilies zijn na
pre-geregistreerde toetsing verworpen (zie
[docs/RESEARCH_JOURNAL.md](docs/RESEARCH_JOURNAL.md)). Het platform draait
daarom **observe-only** en verzamelt microstructuurdata voor de volgende
onderzoekscycli.

**Koers en Path-to-Live: [MASTER_PLAN.md](MASTER_PLAN.md)** · Actuele
stand: [PROJECT_STATUS.md](PROJECT_STATUS.md) · Checklists:
[ROADMAP.md](ROADMAP.md) · Bediening: [BEDIENING.md](BEDIENING.md) ·
Wijzigingen: [CHANGELOG.md](CHANGELOG.md)

## Wat dit platform nu doet

- **Microstructuur-archivering (24/7, productie)** — gestructureerde
  orderbook-snapshots (Bitget), fundingreeksen (Bitget) en
  liquidatie-events (Bybit WS), met dedupe, rotatie, retentie, disk-guard
  en health-monitoring. Zie [docs/ARCHIVING.md](docs/ARCHIVING.md).
- **Forward-paper-observatie (strict `FORWARD_PAPER_ONLY`)** — de volledige
  detectie→scoring→risk→planning→forward-paper-pijplijn draait zonder
  orders en zonder private API-toegang; detectors fungeren als
  observatie-instrument, niet als bewezen strategie.
- **Onderzoeksprogramma** — pre-registratie vóór toetsing, chronologische
  DEV/REP-splitsing, BH-correctie, economische drempels,
  falsificatiebatterijen; verwerpen is een verwacht en gedocumenteerd
  resultaat.

## Geïmplementeerde detectors (geen bewezen edge)

Liquidity-sweep reversal (long/short), momentum breakout/breakdown,
trend-continuation en low-vol reclaim. Deze draaien uitsluitend in
observe/forward-paper; live activering is geblokkeerd door de
live-gate-checklist in [PROJECT_STATUS.md](PROJECT_STATUS.md). De
executie-engine (SL/TP-automatisering, protection-repair, positie-guards)
en het risicoraamwerk (kill-switches, weekly freeze, exposure-caps,
equity-sync) blijven onderhouden en getest, maar staan uit.

## Snelstart (Work Mac / Runner, macOS arm64 of x86_64)

```bash
scripts/bootstrap_mac.sh        # architectuurbewuste Python 3.12-venv + tests
scripts/verify_checkout.sh      # checkout- en omgevingscontrole
.venv/bin/python -m pytest -q   # volledige testsuite

scripts/start_archiver.sh       # microstructuur-archiver (observe-only)
cat data/archive/status.json    # archiver-health

scripts/start_bot.sh            # bot in strict forward-paper-only (geen orders)
```

Runner-deployment gebeurt uitsluitend via geannoteerde `runner-v*`-tags of
een exacte commit die bereikbaar is vanaf de autoritatieve productiereferentie
`origin/production/live-baseline-cd8671`, met `scripts/deploy_runner.sh` (preflight en
rollback ingebouwd): [docs/RUNNER_MIGRATION.md](docs/RUNNER_MIGRATION.md).

## Repository-indeling (hoofdlijnen)

| Map | Inhoud |
|---|---|
| `app/`, `clients/`, `data/`, `market_data/`, `market_features/` | runtime, Bitget-clients, unified feature engine |
| `strategies/`, `planning/`, `risk/`, `execution/` | detectors, trade-planning, risk, executie (uit in observe) |
| `candidate_lifecycle/`, `forward_paper/`, `telemetry/` | funnel-events, forward-paper, gestructureerde logging |
| `archiving/` | microstructuur-archiver (los van executiecode, AST-geborgd) |
| `research/` | hypothese-scripts (reproduceerbaar; zie research journal) |
| `tests/` | pytest-suite (CI: compile, shell-syntax, hygiene, suite) |
| `scripts/` | bootstrap, deploy, verify, start/stop |
| `docs/` | contracten, runbooks, journals |

## Dashboard

Het lokale dashboard faalt gesloten zonder `DASHBOARD_PASSWORD` en bindt
standaard op `127.0.0.1`; een andere interface vereist expliciet
`DASHBOARD_HOST` plus passende netwerkcontroles.

## Beveiliging & hygiëne

Secrets bestaan alleen lokaal (`.env`, nooit in git); de archiver gebruikt
uitsluitend publieke endpoints. CI draait compile-, shell-, hygiene- en
testchecks op elke PR; `main` is beschermd en wordt alleen via PR's
bijgewerkt. Zie [docs/RUNNER_MIGRATION.md](docs/RUNNER_MIGRATION.md) voor
wat GitHub wel en nooit transporteert.

---

## Release Candidate 1 — forward-paper-validatie (2026-07-27)

De forward-paper-levenscyclus is end-to-end aantoonbaar (signaal → order → fill →
positie → SL/TP → beheer → sluiting → outcome → PnL, inclusief herstel na herstart).
De 72-uursbetrouwbaarheidsvalidatie is **niet gehaald**: run 2 eindigde na 42,75 uur
door een host-reboot, waarvan 22,19 uur host-sleep — effectief **20,56 uur = 28,5 %**
van de eis. Auditoordeel: **NOT READY FOR NEXT PHASE**.

Wel bewezen binnen die tijd: veiligheid 13/13 (0 private endpoints, 0 orderaanroepen,
9 892 requests 100 % HTTP 200), event-integriteit (hashketen VALID ná 22 u suspensie én
SIGTERM), boekhouding (beide gesloten trades reconciliëren tot < 1e-6) en herstel van
een **echte** DNS-storing. Niet bewezen: continue uptime, sleep-preventie, herstart na
reboot, monitoringcontinuïteit — en het **live orderpad is nooit uitgevoerd**.

- Volledig verhaal en bewijs: [docs/FORWARD_PAPER_VALIDATION.md](docs/FORWARD_PAPER_VALIDATION.md)
- Release-definitie: [RELEASE_NOTES.md](RELEASE_NOTES.md)
- Risico's: [docs/RISK_REGISTER.md](docs/RISK_REGISTER.md) · Beperkingen: [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md)
- Operationeel: [docs/RECOVERY_PROCEDURES.md](docs/RECOVERY_PROCEDURES.md) · [docs/MONITORING_GUIDE.md](docs/MONITORING_GUIDE.md)
- Repo-indeling: [docs/REPOSITORY_STRUCTURE.md](docs/REPOSITORY_STRUCTURE.md)
- Bewijsarchief: `validation_72h/archive/` (912 KB, 63 bestanden, integriteit geverifieerd)

**Ongewijzigd:** er is nog steeds **geen bewezen edge**. De 2 gesloten papertrades uit
deze validatie hebben geen statistische betekenis.
