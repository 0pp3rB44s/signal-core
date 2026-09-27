# DAILY_OPERATIONS — operationele cyclus en incidentafhandeling

## Dagelijks (± 5 min)

```bash
scripts/daily_ops_check.sh    # exit 0 = ALLES PASS
```
Controleert: forward-paper-status en -modus, heartbeat-versheid,
archiver-health per bron, datakwaliteit vandaag (rijen + duplicaten),
diskruimte en tradingstatus. Bij FAIL: zie herstelacties hieronder; bij
herhaalde FAIL: incident aanmaken (classificatie onderaan).

Aanvullend bij MODE 2/3 (live): positie-/order-reconciliatie op de
exchange en kill-switch-status — 2×/dag in de eerste 48 h.

## Wekelijks (± 30 min)

1. Datakwaliteitsrapport: dekking per bron per dag, gaps > 3× interval,
   dedupe-ratio's, veldvulling, exchange-lag-verdeling → reports/.
2. `git fetch origin` + branch-hygiëne (geen onbedoelde drift; PR-status).
3. Log-review: WARN/ERROR-patronen in archiver.log en botlogs.
4. Disk-trend en rotatie/gzip-controle (gisteren ge-gzipt?).
5. Forward-paper-week: signalen, candidates, fills, outcomes vs vorige week.

## Maandelijks

1. Retentie-verificatie (oudste bestanden ≤ ARCHIVE_RETENTION_DAYS).
2. Dependency-controle: `pip check` + gerichte security-updates via PR.
3. Herstart-oefening: nette stop → start → dedupe-herstel verifiëren.
4. Rollback-oefening op de runner (deploy vorige tag → terug).
5. MASTER_PLAN/PROJECT_STATUS actualiseren (fase-voortgang, poorten).

## Incidentclassificatie

| Klasse | Definitie | Voorbeelden | Reactie |
|---|---|---|---|
| **P1** | Kapitaal- of dataverlies dreigt nu | onbeschermde live positie; reconciliation-mismatch; disk vol; corrupte state | Onmiddellijk: emergency shutdown (runbook), exchange handmatig verifiëren, daarna pas diagnose |
| **P2** | Verzameling of validatie stilgevallen | bot/archiver down > 30 min; bron DEGRADED > 3× interval; keepalive fail-closed | Zelfde dag: herstellen via runbook, oorzaak in log vastleggen |
| **P3** | Kwaliteit sluipend geraakt | duplicaten, veldregressie, klokdrift, groeiende lag | Binnen de week: analyseren, fix via PR met test |

Escalatie: elke P1 en elke herhaalde P2 → eigenaar informeren met feiten
(wat, sinds wanneer, bewijs, voorgestelde actie). Nooit stil herstellen
zonder logvermelding.

## Herstelprocedures (kort)

- **Bot down**: `scripts/forward_paper_keepalive.sh` (of handmatig
  `scripts/start_forward_paper.sh 60`); bij fail-closed keepalive eerst
  `logs/forward_paper_keepalive.log` en `state/last_shutdown.json` lezen.
- **Archiver down**: `scripts/start_archiver.sh`; dedupe-herstel is
  automatisch (ARCHIVE_RESUME in log); daarna daily_ops_check.
- **Bron DEGRADED**: archiver.log op reconnect/backoff-patronen; bij
  WS-problemen: verbinding herstelt zelf met backoff — alleen ingrijpen
  als > 1 h zonder herstel.
- **Disk laag**: retentie draait automatisch; bij < 5 GB handmatig oude
  gz-dagen naar externe opslag verplaatsen vóór verwijdering (nooit
  zonder kopie verwijderen).
- **Corrupte hash-chain (funnel)**: niet herstarten; corrupt bestand
  bewaren, incident P1-melding, analyse eerst (zie BEDIENING.md).

## Verantwoordelijkheden

Automatisering doet het meten (daily_ops_check, keepalive, heartbeats,
disk-guard); de mens beslist bij afwijkingen. Elke handmatige ingreep
wordt gelogd (wat, waarom, resultaat) — het logboek is onderdeel van de
C3-poort in GO_LIVE_CHECKLIST.md.
