# RELEASE NOTES — signal-core

Per release-candidate. Nieuwste bovenaan. Uitsluitend feitelijke, bewijsgedragen
statements; verwerping en falen zijn normale uitkomsten en worden hier net zo
expliciet vastgelegd als successen.

---

## RC1 — Forward-Paper Validated Baseline (2026-07-27)

**Baseline commit:** `cda8187` (validatierun) · documentatie- en archiefcommits daarna.
**Status:** Release Candidate — **NIET vrijgegeven voor live trading.**

### Wat deze RC vastlegt

De volledige forward-paper-levenscyclus is end-to-end aantoonbaar: signaal → order →
fill → positie → SL/TP actief → beheer → sluiting → outcome → PnL, inclusief herstel na
herstart. Drie blokkerende defecten zijn opgelost en met regressietests afgedekt:

| # | Defect | Bewijs |
|---|---|---|
| D1 | `ContractSpec` niet JSON-serialiseerbaar brak **elke** paper-write | 0 bytes eventstore in 11 dagen; 147 executable plannen, 0 trades; 297× `FORWARD_PAPER_FAILED_CLOSED` |
| D2 | Scanloop zonder exception-afhandeling; DNS-storing doodde het proces | 20 uur onopgemerkte uitval op 2026-07-24 |
| D3 | Break-even-stop boekte winst op een prijs die de markt nooit verhandelde | stop op 74.004931 terwijl candle-high 73.947 was |
| D4 | Bitget weigert `granularity=1h` (alleen `1H`) | HTTP 400 code 400171, 742× in run 1 |
| D5 | Mislukte scan publiceerde `scan_cycle_complete` als succes | 106 cycli "voltooid" met `snapshot_count: 0`, healthcheck HEALTHY |

Testsuite: **339 passed**, tweemaal reproduceerbaar. Beide defecten uit run 1 zijn
geverifieerd reproduceerbaar met de fix teruggedraaid.

### Validatieresultaat — eerlijk samengevat

De 72-uursvalidatie is **niet gehaald**. Run 2 eindigde na 42,75 uur door een
host-reboot; 22,19 uur daarvan was het proces opgeschort door host-sleep. Effectieve
scantijd: **20,56 uur = 28,5 %** van de eis.

Wat wél bewezen is binnen die tijd:

- **Veiligheid 13/13** — 0 private endpoints, 0 orderaanroepen, 9 892 requests, 100 % HTTP 200.
- **Event-integriteit** — hashketen VALID over 49 events ná een suspensie van 22 uur én een SIGTERM; 0 duplicaten, 0 conflicten.
- **Boekhouding** — beide gesloten trades reconciliëren onafhankelijk tot < 1e-6; geen onmogelijke fills.
- **Netwerkherstel** — een **echte** DNS-storing werd door de retrylaag opgevangen; cyclus voltooid.

Wat niet bewezen is: continue uptime, sleep-preventie, herstart na reboot,
monitoringcontinuïteit. En het **live orderpad is nooit uitgevoerd**.

### Live-readiness

**PASS 7 · PASS WITH LIMITATION 5 · FAIL 3** (Restart Recovery, Supervisor, Monitoring).
Eindoordeel van de audit: **NOT READY FOR NEXT PHASE**.

Dit is geen bevinding tegen de handelslogica. Zolang de host wakker was deed het
systeem exact wat het moest doen. Het blokkerende punt is dat de omgeving het niet
wakker kon houden, niet kon herstarten en niet kon melden dat het gestopt was.

### Bewijsarchief

`validation_72h/archive/RC1_forward_paper_validation.tar.gz`
SHA-256 `3b5bf71527184038eb58dc4772c582aa179e7d8747f7089f703eb4a7d5590739`
912 KB gecomprimeerd, 12 MB uitgepakt, 63 bestanden, integriteit 63/63 geverifieerd
(inclusief round-trip-verificatie na uitpakken).

### Openstaande risico's

2 Critical, 3 High, 5 Medium, 3 Low — zie `docs/RISK_REGISTER.md`.
Beperkingen: `docs/KNOWN_LIMITATIONS.md`.

### Onveranderd

**Geen bewezen edge.** Alle hypothesefamilies uit fases 2-4D blijven verworpen. De 2
gesloten trades in deze RC hebben **geen statistische betekenis** en mogen nooit als
performance worden aangehaald. `EXECUTION_ENABLED=false` blijft het eigenaar-besluit
sinds 2026-07-13.
