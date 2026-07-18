# SIGNAL-CORE — OPERATIONELE ROADMAP (kortetermijn-checklists)

VERSIE: 2026-07-18 · STATUS: ACTIEF · VORIGE VERSIE: 2026-07-07 (git-historie)

**De centrale koers staat in [MASTER_PLAN.md](MASTER_PLAN.md)** (missie,
Path-to-Live A-F, edge-roadmap, planninghorizonten). Dit document bevat de
uitvoerbare kortetermijn-checklists die daarbij horen. Actuele stand:
[PROJECT_STATUS.md](PROJECT_STATUS.md); historie: [docs/JOURNAL.md](docs/JOURNAL.md).

═══════════════════════════════════
NORTH STAR (ongewijzigd)
═══════════════════════════════════

MISSIE: institutionele crypto-executie-engine die uitsluitend live gaat op
aantoonbaar, gerepliceerd statistisch bewijs.
HOOFDREGEL: Safety > Truth > Expectancy > Alpha.
WERKWIJZE: pre-registratie → data → toetsing → falsificatie → pas dan bouwen.

Kernfeit dat de hele planning stuurt: **er is geen bewezen edge** (fases
2-4D verworpen). Alles hieronder dient dataverzameling, validatie en
operationele betrouwbaarheid — niet het forceren van een strategie.

═══════════════════════════════════
VANDAAG (2026-07-18)
═══════════════════════════════════

DOEL: documentatie gesynchroniseerd; archiver stabiel; runnerplan gereed.
DUUR: 1 dag. AFHANKELIJKHEDEN: geen.
ACCEPTATIE: PR met docsync gemerged; archiver ≥ 12 h onafgebroken OK;
runner-stappenplan goedgekeurd door eigenaar.
RISICO: geen — alleen documentatie en observatie.

- [x] Archiver in productie vanaf gemergede main (PR #11), hersteltest OK.
- [x] Documentatie gelijkgetrokken (README/PROJECT_STATUS/ROADMAP/CHANGELOG).
- [ ] Archiver-health einde dag controleren (status.json, groei, 0 errors).

═══════════════════════════════════
MORGEN (2026-07-19) — INTEL RUNNER OPERATIONEEL
═══════════════════════════════════

DOEL: Runner draait de archiver (en optioneel forward-paper) vanaf een
geannoteerde `runner-v*`-tag. DUUR: 0,5-1 dag.
AFHANKELIJKHEDEN: eigenaar aanwezig voor .env-aanmaak en tag-autorisatie.
RISICO'S: Intel-Homebrew/Python-afwijkingen (afgedekt door preflight);
netwerk-/certificaatverschillen (archiver gebruikt certifi).
ACCEPTATIE: `deploy_runner.sh --preflight` PASS; archiver op runner levert
> 1 uur data met 0 errors; rollback-pad aantoonbaar getest.

Stappenplan (details: docs/RUNNER_MIGRATION.md):
1. Runner: `git fetch origin --prune --tags`; schone checkout bevestigen.
2. `scripts/bootstrap_mac.sh --recreate-venv` (3.11-venv → backup, 3.12 op
   /usr/local-Homebrew; weigert Apple-system-Python).
3. `scripts/verify_checkout.sh` + `scripts/verify_repository_hygiene.sh`.
4. `scripts/create_runner_env_template.sh` → eigenaar vult .env lokaal
   (nooit via git; alleen presence-check).
5. Eigenaar maakt geannoteerde tag `runner-v2026.07.19.1` op de gewenste
   main-commit en pusht die (of autoriseert expliciet).
6. `scripts/deploy_runner.sh --preflight runner-v2026.07.19.1` → PASS.
7. `scripts/deploy_runner.sh runner-v2026.07.19.1` (maakt backup-ref).
8. `scripts/start_archiver.sh` op de runner; status.json + groei valideren.
9. Rollbacktest: `deploy_runner.sh --rollback refs/runner-backups/<ref>`
   en weer vooruit.
10. Werk-Mac-archiver stoppen zodra de runner-archiver ≥ 1 h stabiel is
    (één producent per ARCHIVE_DIR; geen dubbele verzameling).

═══════════════════════════════════
DEZE WEEK (t/m 2026-07-25)
═══════════════════════════════════

DOEL: 24/7 dataverzameling + forward-paper terug in de lucht.
AFHANKELIJKHEDEN: runner operationeel. DUUR: 2-3 dagen werk.
RISICO'S: onverklaarde bot-stop herhaalt zich (mitigatie: supervisor +
stop-oorzaakanalyse eerst). ACCEPTATIE: bot ≥ 5 dagen onafgebroken in
strict forward-paper-only; archiver 7 dagen 100% dagdekking; dagelijkse
health-check gedocumenteerd.

- [x] Root cause bot-stop 2026-07-15 vastgesteld (audit 2026-07-18): nette
      SIGTERM na 13 scancycli — bewuste stop, geen crash.
- [ ] Bot herstarten in strict FORWARD_PAPER_ONLY onder supervisor
      (`scripts/run_supervised.sh`), notify-only watchdog blijft.
- [ ] Dagelijkse operationele check (5 min): archiver-status, botproces,
      forward-paper-heartbeats, disk.
- [ ] Branch-opschoning: gemergde/stale branches archiveren of verwijderen
      (25 remote branches → doel < 10).
- [ ] Besluit eigenaar over ongemergede CLOSED_SYNCED-backfill-branch.

═══════════════════════════════════
KOMENDE 2 WEKEN (t/m 2026-08-01)
═══════════════════════════════════

DOEL: bewezen stabiele observatie-omgeving + datakwaliteitsrapport week 1.
ACCEPTATIE: ≥ 14 dagen archiefdata zonder gaten > 3× interval; forward-
paper-stroom compleet (funnel → plan → forward-outcome); geautomatiseerde
datakwaliteitscontrole (script, geen handwerk).

- [ ] Wekelijks datakwaliteitsscript (dekking, gaps, dedupe-ratio's,
      veldvulling) + rapport in reports/.
- [ ] Forward-paper-pariteitsmeting: signaaltijd vs uitvoerbare tijd,
      spread/slippage-aannames vs realiteit (alleen meten, niets tunen).
- [ ] Bot-venv op Work Mac migreren naar 3.12 via bootstrap (stil moment).

═══════════════════════════════════
KOMENDE MAAND (t/m 2026-08-18)
═══════════════════════════════════

DOEL: eerste toetsbare microstructuur-dataset (≥ 4 weken) + voorbereide
pre-registraties. ACCEPTATIE: ≥ 28 dagen data per bron; ≥ 2 volledig
uitgeschreven pre-registraties (mechanisme, poorten, power) zonder ook
maar één blik op de uitkomstdata.

- [ ] Pre-registraties opstellen voor de eerst ontgrendelde families
      (kandidaten: liquidatie-cascade-dynamiek; orderbook-imbalance op
      10s-resolutie — nieuw regime van data, nieuw mechanisme-argument,
      geen recycling van H-4D-1-parameters).
- [ ] Power-analyses vooraf op verzamelde datavolumes.
- [ ] Legacy-besluit: dashboards/agents/optimizer bevriezen of verwijderen.

═══════════════════════════════════
KOMENDE 2 MAANDEN (t/m 2026-09-18)
═══════════════════════════════════

DOEL: eerste hypothesecyclus op eigen microstructuurdata afgerond met
verdict; live-gate-checklist itereren op bewijs. ACCEPTATIE: verdict
(PASS → falsificatiebatterij → eventueel EDGE_ACCEPTANCE_REPORT; FAIL →
gedocumenteerde verwerping); geen enkele live-activering zonder de
volledige checklist in PROJECT_STATUS.md.

- [ ] H-5-x uitvoeren exact volgens registratie (DEV/REP chronologisch).
- [ ] Bij PASS: falsificatie + forward-paper-verificatie vóór enige
      implementatie-discussie.
- [ ] Bij FAIL: volgende geregistreerde familie of verlengde verzameling.

═══════════════════════════════════
GEPARKEERD TOT LIVE-GATE (niet vergeten, nu niet doen)
═══════════════════════════════════

- Protection-action-integratietests + lifecycle-action-validatie.
- ACCOUNT_EQUITY_USDT-fallback in .env actualiseren.
- heartbeat/memory/cpu/disk-monitor uitbreiden.
- Fee-drag-heranalyse bij elke kandidaat-strategie (historisch: kosten >
  edge; 24,5% WR churn was de doodsoorzaak van fase 2-3).
