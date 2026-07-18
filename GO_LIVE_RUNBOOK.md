# GO_LIVE_RUNBOOK — modusovergangen en noodprocedures

Drie strikt gescheiden modi. Wissel is configuratiegedreven; er is geen
herbouw nodig. **Elke overgang omhoog vereist de bijbehorende poorten uit
[GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md) plus eigenaar-autorisatie.**
Overgangen omlaag (fallback) zijn altijd en onmiddellijk toegestaan.

## Modusdefinities

| | MODE 1 — Strict Forward Paper | MODE 2 — Limited Live | MODE 3 — Production Live |
|---|---|---|---|
| Marktdata | echt | echt | echt |
| Orders | onmogelijk (credentials leeg + HTTP-blokkade) | echt, minimaal | echt |
| Posities | gesimuleerd | max 1, isolated, vaste max leverage | volgens risicobudget |
| Vereist | niets (default) | checklist A-E volledig + autorisatie | MODE 2-validatie + autorisatie per schaalstap |
| Runtime | `FORWARD_PAPER_ONLY=true` (forceert alle veilige settings) | expliciete live-config + caps | idem + schaalconfig |

## MODE 1 — starten / herstellen (huidige productie)

```bash
scripts/start_forward_paper.sh 60      # weigert: vuile tree, niet-main, dubbele processen
bash scripts/check_forward_paper.sh    # verwacht: status=HEALTHY, mode=FORWARD_PAPER_ONLY
scripts/daily_ops_check.sh             # verwacht: ALLES PASS
```
Continuïteit: `scripts/forward_paper_keepalive.sh --loop 120` (tmux) of
periodiek; stopt fail-closed na 3 snelle herstarts/30 min.

## MODE 1 → MODE 2 (alleen na volledige checklist + autorisatie)

1. Bevestig checklist A-E volledig PASS; archiveer het autorisatiepakket.
2. Deploy de goedgekeurde commit op de runner via geannoteerde tag:
   `scripts/deploy_runner.sh runner-vYYYY.MM.DD.N` (preflight eerst).
3. Eigenaar plaatst credentials uitsluitend lokaal in `.env` op de runner
   (nooit via git/chat; alleen presence-check met
   `scripts/compare_env_presence.sh`).
4. Zet de live-configuratie exact zoals in het geautoriseerde voorstel
   (symbolen, leverage, sizing, caps). Config-hash vastleggen.
5. Preflight live: exchange-permissiecheck, balans, margin-mode, leverage,
   min-ordergrootte, kill-switch-status — alles read-only verifiëren.
6. Start; eerste trade handmatig meekijken: fill → onmiddellijke
   SL/TP-plaatsing → reconciliation. Bij afwijking: direct fallback.
7. Eerste 48 h: daily_ops_check 2×/dag + positie-reconciliatie per trade.

## MODE 2 → MODE 1 (fallback — altijd toegestaan, geen toestemming nodig)

```bash
./scripts/stop_all.sh strict_forward_paper_stop   # sluit nette shutdown af
# posities handmatig sluiten of laten aflopen volgens SL/TP; verifieer op
# de exchange dat geen open orders/posities resteren
scripts/start_forward_paper.sh 60
```
Trigger-criteria (elk voldoende): protection-failure, reconciliation-
mismatch, daily-loss-cap geraakt, onverklaard gedrag, exchange-storing,
afwijking > geregistreerde band, eigenaar-verzoek.

## MODE 2 → MODE 3

Alleen na de volledige MODE 2-validatieperiode (vooraf geregistreerde
duur + minimum trades + resultaat binnen band + 0 kritieke incidenten) en
nieuwe expliciete autorisatie. Schaalstappen: exchange-minimum → kleine
vaste risicofractie → beperkte uitbreiding → productieallocatie; per stap
minimumduur, minimumtrades, band-check en autorisatie. Nooit opschalen na
winst-euforie of om verlies terug te winnen.

## Emergency shutdown (elke modus)

```bash
./scripts/stop_all.sh emergency
pgrep -af "app.main|dashboard_v2.app"   # verwacht: leeg
```
In MODE 2/3 daarna onmiddellijk op de exchange verifiëren: open posities,
open orders, SL/TP-orders. Handmatig sluiten wat de bot niet meer beheert.
Incident vastleggen (DAILY_OPERATIONS.md → incidentclassificatie).

## Deployment-rollback (runner)

```bash
scripts/deploy_runner.sh --rollback refs/runner-backups/<ref>
scripts/verify_checkout.sh
```
Elke deploy maakt automatisch een backup-ref; rollback is dus altijd
beschikbaar. Na rollback: daily_ops_check + één volledige scancyclus
verifiëren.

## Post-deploy-validatie (elke deploy, elke modus)

1. `scripts/verify_checkout.sh` — juiste commit/Python/venv.
2. Proces-identiteit: pid, cwd, interpreter (`lsof -p <pid> -d cwd`).
3. `scripts/daily_ops_check.sh` — ALLES PASS.
4. Modus verifiëren (`check_forward_paper.sh` of live-configcheck).
5. Logs 15 min volgen op ERROR/WARN; daarna normale cadans.

## Credential handling

Credentials bestaan uitsluitend in lokale `.env` op de machine die ze
nodig heeft. MODE 1 heeft ze niet nodig (launcher leegt ze). Nooit in
git, chat, logs of rapporten; alleen presence-checks (naam + wel/niet
gezet). Rotatie: eigenaar wijzigt op de exchange + in lokale `.env`,
daarna herstart + preflight.
