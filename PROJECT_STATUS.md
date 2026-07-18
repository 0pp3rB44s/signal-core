# PROJECT STATUS — signal-core

**Peildatum: 2026-07-18** · Dit is het autoritatieve statusdocument.
Koers en Path-to-Live: [MASTER_PLAN.md](MASTER_PLAN.md). Historie:
[docs/JOURNAL.md](docs/JOURNAL.md) (verhalend), [CHANGELOG.md](CHANGELOG.md)
(per release/PR), [docs/RESEARCH_JOURNAL.md](docs/RESEARCH_JOURNAL.md)
(hypothese-grootboek). Checklists: [ROADMAP.md](ROADMAP.md).

## Kernstand in één alinea

Er bestaat op dit moment **geen statistisch bewezen edge** (fases 2-4D:
alle hypothesefamilies verworpen na pre-geregistreerde toetsing). De bot
staat sinds 2026-07-13 op **observe-only** (eigenaar-besluit) en draait op
dit moment niet; de laatste run eindigde 2026-07-15 07:51 UTC met een nette
SIGTERM (bewuste stop, geen crash — `state/last_shutdown.json`). De **microstructuur-archiver draait
24/7** vanaf gemergede main (orderbook/funding/liquidations) en bouwt de
dataset die de geblokkeerde onderzoeksfamilies over 4-8 weken toetsbaar
maakt. De focus tot die tijd: datakwaliteit, forward-paper-validatie en
runner-deployment — géén live trading.

## Fase-overzicht

| Fase | Inhoud | Status |
|---|---|---|
| 1-3 | Engine, risk, execution, telemetrie, 367 live trades, strategieniveau-tests | **AFGEROND** — uitkomst: geen edge op strategieniveau |
| 4A-4C | OHLCV-directioneel, funding/OI, basis/mark-index | **AFGEROND — VERWORPEN** |
| 4D | Microstructuur (H-4D-1), time-of-day (H-4D-2), VWAP (H-4D-3) | **AFGEROND — VERWORPEN** (schone pre-registraties, reproducties exact) |
| 4E | Live-archivering microstructuurdata | **ACTIEF** — archiver stabiel in productie sinds 2026-07-18 |
| 5 | Forward-paper-validatie 24/7 (strict FORWARD_PAPER_ONLY) | **GESTART, GEPAUZEERD** — bot bewust gestopt 07-15 (nette SIGTERM); herstart onder supervisor kan zodra gewenst |
| 6 | Runner-deployment (Intel MacBook) | **VOORBEREID** — infra gemerged (PR #10), eerste deploy-tag ontbreekt nog |
| 7 | Nieuwe hypothesecycli op gearchiveerde data | **NIET BEGONNEN** — wacht op ≥4 weken data + nieuwe pre-registratie |
| — | Live trading | **GEBLOKKEERD** — zie live-gate-checklist onderaan |

## Componentmaturiteit

| Component | Oordeel | Toelichting |
|---|---|---|
| Unified feature engine / candidate lifecycle / funnel-telemetrie | productie-klaar | PR #7-#9, contract-getest |
| Forward-paper runtime (strict mode) | productie-klaar, onbewaakt | afdwinging van veilige settings getest; 24/7-run vereist supervisor + stop-oorzaakanalyse 07-15 |
| Archiver (orderbook/funding/liquidations) | productie-klaar | draait; dedupe/rotatie/retentie/disk-guard/health getest incl. hersteltest |
| Runner-deployinfra (bootstrap/deploy/rollback/CI) | productie-klaar, ongebruikt | PR #10; eerste echte deploy moet nog |
| Strategieën/detectors | experimenteel, geen edge | blijven als observatie-instrument in forward paper |
| Dashboards (dashboard_v2/v3), agents_v2/v3, backtesting/optimizer | legacy/experimenteel | niet in kritieke pad; opruimkandidaten (zie LEGACY_MODULES.md) |
| Journal/analytics (live_trade_journal) | analytics-only | blokkeert nooit trades; positie-gate leest exchange-truth |

## Researchstatus (kort)

- **Edge: NEE.** Alle families verworpen op vooraf vastgelegde poorten;
  H-4D-1 onafhankelijk exact gereproduceerd; H-4D-2: 0/30 BH-significant;
  H-4D-3: verkeerde richting + geen significantie. Power was steeds
  toereikend voor de economische drempels → informatieve verwerpingen.
- Volledige onderbouwing en reproductiecommando's: docs/RESEARCH_JOURNAL.md.

## Live-gate-checklist (alles verplicht vóór live trading)

1. [ ] `EDGE_ACCEPTANCE_REPORT` met alle poorten PASS (BH-significantie DEV,
       replicatie zelfde teken |t|≥2, economisch > kosten met marge,
       maand/symbool/regime-stabiliteit, falsificatiebatterij overleefd).
2. [ ] Effect blijft staan in ≥ 4 weken forward-paper zonder materiële
       afwijking van de research-aannames (fills, spread, slippage, timing).
3. [ ] Bot 24/7 stabiel in strict forward-paper-only gedurende ≥ 2 weken
       zonder onverklaarde stops (stop 07-15 verklaard: nette SIGTERM).
4. [ ] Runner gedeployed op geannoteerde `runner-v*`-tag, rollback getest.
5. [ ] Kill-switches, weekly freeze, exposure-limieten en equity-sync
       aantoonbaar getest op de runner.
6. [ ] Risicoconfig herzien op actuele equity; fee-drag-analyse herhaald
       (historisch: kosten > edge bij 24,5% WR churn).
7. [ ] Expliciete, afzonderlijke eigenaar-autorisatie ná oplevering van al
       het bovenstaande. Zonder dit: observe/paper.

## Openstaande risico's / technische schuld

1. ~~Stop-oorzaak bot 2026-07-15~~ — opgelost bij audit 2026-07-18: nette
   SIGTERM (bewuste stop, geen crash). Herstart-blocker vervallen; alleen
   supervisor-adoptie blijft als voorwaarde voor 24/7.
2. Work-Mac bot-`.venv` is Python 3.11 terwijl het contract 3.12 zegt
   (`.python-version`); archiver draait al op 3.12 — bot-venv migreren via
   `scripts/bootstrap_mac.sh --recreate-venv` op een stil moment.
3. 25 remote branches, waarvan het merendeel afgerond/stale — opschonen.
4. Ongemergede fix `CLOSED_SYNCED backfill` (branch
   `claude/mystifying-meninsky-6c9e67`) — beoordelen: mergen of sluiten.
5. Legacy-modules (dashboards, agents_v2/v3, optimizer) ongebruikt in het
   kritieke pad — bevriezen of verwijderen na expliciete beslissing.
6. Liquidatiebron is Bybit (Bitget heeft geen publiek kanaal; Binance-WS
   bereikt dit netwerk niet) — cross-venue-aanname documenteren in elke
   toekomstige liquidatie-hypothese.
