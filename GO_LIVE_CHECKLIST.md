# GO_LIVE_CHECKLIST — harde poorten vóór echte orders

**Elke poort moet objectief PASS zijn. Eén FAIL of ONBEKEND = niet live.**
Verificatie waar mogelijk via commando; bewijs wordt gearchiveerd in
reports/ of docs/RESEARCH_JOURNAL.md. Activering van MODE 2 vereist ná
alle poorten bovendien expliciete, afzonderlijke eigenaar-autorisatie.

## A. Bewijs (research)

| # | Poort | Verificatie |
|---|---|---|
| A1 | Pre-registratie bestond vóór eerste uitkomstinspectie (commit-bewijs) | `git log docs/RESEARCH_JOURNAL.md` — registratiecommit < resultatencommit |
| A2 | BH-gecorrigeerde DEV-significantie < 0,05 binnen geregistreerde familie | resultaten-JSON + journal |
| A3 | Replicatie: zelfde teken én |t| ≥ 2 (cluster-robuust) in REP | idem |
| A4 | Effect > alle kosten (taker/maker, spread, slippage, funding) met ≥ 50% marge | kostenmodel in registratie |
| A5 | Maand-tekenconsistentie ≥ geregistreerde drempel; niet gedreven door 1 maand | falsificatie-uitvoer |
| A6 | Niet gedreven door ≤ 2 symbolen (leave-two-out) | idem |
| A7 | Overleeft ≥ 3/4 regimes én delayed-entry-test | idem |
| A8 | Onafhankelijke reproductie uit ruwe inputs is exact | herrun-bewijs |
| A9 | `EDGE_ACCEPTANCE_REPORT` compleet, alle poorten PASS | document in docs/ |

## B. Candidate Forward Paper (MODE 1, ≥ 4 weken)

| # | Poort | Verificatie |
|---|---|---|
| B1 | Immutable candidate: config-hash + commit gepind vóór start | registratie in journal |
| B2 | ≥ 4 weken zonder parameterwijziging | git-historie strategie/config |
| B3 | Resultaat binnen vooraf geregistreerde verwachtingsband (frequentie, WR, expectancy, drawdown, kosten) | preregistered evaluation plan |
| B4 | Forward-paper rekent niet gunstiger dan de exchange (fees/fills/latency geverifieerd) | pariteitsrapport |
| B5 | 0 protection-failures, 0 reconciliation-failures, 0 onverklaarde stops | daily-ops-logs |

## C. Operationeel platform

| # | Poort | Verificatie |
|---|---|---|
| C1 | Runner draait productie vanaf geannoteerde `runner-v*`-tag | `scripts/verify_checkout.sh` op runner |
| C2 | Rollback daadwerkelijk getest (deploy → rollback → deploy) | deploy-log |
| C3 | `scripts/daily_ops_check.sh` ≥ 14 dagen op rij PASS | ops-logboek |
| C4 | Supervisor/keepalive fail-closed bewezen (restarttest + rapid-fail-stop) | keepalive-log |
| C5 | Kill-switch, daily loss cap, weekly freeze en exposure-caps getest op de runner | testrun-bewijs |
| C6 | Reboot-recovery getest | herstartbewijs |
| C7 | Archiver-datacontinuïteit tijdens alle bovenstaande tests intact | data_audit |

## D. Risicoconfiguratie (MODE 2-instap)

| # | Poort | Verificatie |
|---|---|---|
| D1 | Sizing afgeleid van exchange-minimums + accountgrootte + stopafstand (geen willekeurige bedragen) | berekening in autorisatievoorstel |
| D2 | Max 1 positie, isolated margin, vaste max leverage, geen averaging/martingale — technisch afgedwongen | configtest |
| D3 | Harde daily-loss-cap en totale drawdown-cap actief en getest | testbewijs |
| D4 | `ACCOUNT_EQUITY_USDT`-fallback actueel; live equity-sync geverifieerd | configcheck |
| D5 | Fee-drag-heranalyse voor déze kandidaat (les fase 2-3: kosten > edge) | rapport |

## E. Autorisatie

| # | Poort | Verificatie |
|---|---|---|
| E1 | Eigenaar heeft het volledige autorisatiepakket ontvangen (strategie, commit, config-hash, symbolen, leverage, sizing, caps, bewijsrapport, forward-resultaat, rollbackprocedure) | overdracht gedocumenteerd |
| E2 | Expliciete, afzonderlijke schriftelijke eigenaar-autorisatie voor MODE 2 | vastgelegd |
| E3 | Autorisatie is per modus en per schaalstap — nooit overdraagbaar | GO_LIVE_RUNBOOK |

**Stand 2026-07-18: A 0/9 · B 0/5 · C 1/7 (C7) · D 0/5 · E 0/3 → NIET LIVE.**
