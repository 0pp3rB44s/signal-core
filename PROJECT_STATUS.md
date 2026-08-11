# PROJECT STATUS — signal-core

**Peildatum: 2026-08-10** · Dit is het autoritatieve statusdocument.
Koers en Path-to-Live: [MASTER_PLAN.md](MASTER_PLAN.md). Historie:
[docs/JOURNAL.md](docs/JOURNAL.md) (verhalend), [CHANGELOG.md](CHANGELOG.md)
(per release/PR), [docs/RESEARCH_JOURNAL.md](docs/RESEARCH_JOURNAL.md)
(hypothese-grootboek). Checklists: [ROADMAP.md](ROADMAP.md).

## Release governance

GitHub is the code source of truth. The authoritative Runner production
reference is `origin/production/live-baseline-cd8671`; `origin/main` is not
Runner release authority. Runner deployments remain exact-SHA deployments to a
detached checkout, and the target must be reachable from that production ref.

## Kernstand in één alinea

De bot **handelt LIVE met echt geld** op
`5a28980d4bc8f1364cb50fbad3a40a41414e947d`, sinds 2026-08-09T18:46:09Z:
8 symbolen, maximaal 2 gelijktijdige posities, 35 USDT notional-cap,
leverage 3. Er is nog steeds **geen statistisch bewezen edge** — de eerste
12 gesloten trades na de direction-score-fix leveren netto −0,4923 USDT,
0 TP1-hits, en een mediane MFE van 14,1 bps tegen een round-trip fee van
12,0 bps. Het handelsvolume komt vrijwel volledig van één strategie
(`low_vol_reclaim`: 68% van de plannen, en in 134 van 153 cycles het énige
executable plan). De architectuurbeslissing van 2026-08-10 is daarom
**OBSERVABILITY_FIRST**: eerst meetbaar maken, dan pas tunen.

## Huidige strategie-stand

| strategy | rol | bewijs |
|---|---|---|
| `low_vol_reclaim` | **control** — niet verder optimaliseren omdat hij toevallig veel handelt | 43 closes, netto negatief |
| `momentum_breakout` | **active research** | 522 kandidaten, 18 executable, 4 opens; beste historische WR (47,9% over 48 trades, PRE_DIRECTION_FIX) |
| `trend_continuation` | nog niet bewezen | 38 kandidaten, 86,8% executable, 2 opens |
| `momentum_breakdown` | nog niet bewezen | 69 kandidaten, 3 executable, **0 opens** |
| `liquidity_sweep_reversal` | nog niet bewezen | 30 kandidaten, **0 executable ooit** |
| `adaptive_momentum_continuation` | bewust uit (env) | geen |

## Lopende release — observability

`feat/observability-ranked-plans` voegt twee meetvoorzieningen toe en
**verandert geen handelsgedrag**:

- **ranked-plan telemetrie** (`logs/ranked_plans.jsonl`): per selectiecyclus
  alle gerankte plannen in de volgorde van de selector, met de vier
  ranking-sleutels overgenomen uit de selector zelf. Tot nu toe werd alleen
  de winnaar gelogd, waardoor elke ranker-vraag offline gereconstrueerd moest
  worden — twee keer met een fout die achteraf gevonden werd.
- **`participation_score` propagatie**: de kolom was leeg in 9301/9301 rijen
  terwijl 3422 rijen de waarde in `raw_notes` droegen; de parser las alleen
  de verouderde spatie-vorm. Bestaande producerwaarde, geen nieuwe semantiek.

Niet gewijzigd: kandidaatgeneratie, strategy gates, ranking-volgorde,
executie, TP/SL, risk, leverage, position sizing.

## Volgende evidence gate

Eerst **verse post-deploy telemetrie verzamelen**. Geen nieuwe strategy-tuning
en geen threshold-wijzigingen voordat er ≥ 30 POST_DIRECTION_FIX closes zijn
met ≥ 10 LONG en ≥ 3 vertegenwoordigde strategieën. Zie [ROADMAP.md](ROADMAP.md).

---

### Historie — kernstand 2026-07-18 (SUPERSEDED)

> Onderstaande alinea beschreef de toestand vóór de live-promotie en is
> bewaard als context. Zij is **niet meer geldig**: de bot staat niet langer
> op observe-only.
>
> Er bestaat op dit moment **geen statistisch bewezen edge** (fases 2-4D:
> alle hypothesefamilies verworpen na pre-geregistreerde toetsing). De bot
> staat sinds 2026-07-13 op **observe-only** (eigenaar-besluit) en draait op
> dit moment niet; de laatste run eindigde 2026-07-15 07:51 UTC met een nette
> SIGTERM (bewuste stop, geen crash — `state/last_shutdown.json`). De
> microstructuur-archiver draait 24/7 vanaf gemergede main
> (orderbook/funding/liquidations) en bouwt de dataset die de geblokkeerde
> onderzoeksfamilies over 4-8 weken toetsbaar maakt. De focus tot die tijd:
> datakwaliteit, forward-paper-validatie en runner-deployment — géén live
> trading.

## Fase-overzicht

| Fase | Inhoud | Status |
|---|---|---|
| 1-3 | Engine, risk, execution, telemetrie, 367 live trades, strategieniveau-tests | **AFGEROND** — uitkomst: geen edge op strategieniveau |
| 4A-4C | OHLCV-directioneel, funding/OI, basis/mark-index | **AFGEROND — VERWORPEN** |
| 4D | Microstructuur (H-4D-1), time-of-day (H-4D-2), VWAP (H-4D-3) | **AFGEROND — VERWORPEN** (schone pre-registraties, reproducties exact) |
| 4E | Live-archivering microstructuurdata | **ACTIEF** — archiver stabiel in productie sinds 2026-07-18 |
| 5 | Forward-paper-validatie 24/7 (strict FORWARD_PAPER_ONLY) | **ACTIEF sinds 2026-07-18** — strict-mode HEALTHY, keepalive + daily-ops-check beschikbaar (`scripts/daily_ops_check.sh`) |
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

---

## Statusupdate 2026-07-27 — RC1 (aanvulling, vervangt niets hierboven)

**Peildatum: 2026-07-27.** De bot draait op dit moment **niet**: run 2 van de
forward-paper-validatie kreeg een SIGTERM (exit 143) op 2026-07-27T10:53:51Z, 16
seconden vóór een host-reboot (`kern.boottime` 10:54:07Z). Daarna heeft niets de run
hersteld — 7,82 uur dood tot aan de audit. Ook de archiver is bij die reboot gestopt.

**Validatiestand.** 72-uurscriterium niet gehaald: 20,56 uur effectief van 72
(28,5 %). Auditoordeel **NOT READY FOR NEXT PHASE**; live-readiness PASS 7 · PASS WITH
LIMITATION 5 · FAIL 3 (Restart Recovery, Supervisor, Monitoring).

**Wat er wél staat.** De handelslevenscyclus zelf werkt: 1 177 scancycli, mediaan
cadans 63 s, API-latency mediaan 317 ms over 9 892 requests met 100 % HTTP 200, 0
tracebacks, 0 scanfouten, hashketen VALID over 49 events, beide gesloten trades
reconciliëren tot < 1e-6. Drie trades geopend, twee gesloten, één onopgelost bij freeze.

**Wat het blokkeert.** De omgeving, niet de code: host-sleep schortte het proces 22,19
uur op ondanks een actieve power-assertion, er is geen boot-persistente supervisie, en
de monitor stopte stil op 2026-07-26T12:09:03Z zonder dat iets dat opmerkte.

**Onveranderd:** geen bewezen edge; `EXECUTION_ENABLED=false` sinds 2026-07-13.

Details: [RELEASE_NOTES.md](RELEASE_NOTES.md),
[docs/FORWARD_PAPER_VALIDATION.md](docs/FORWARD_PAPER_VALIDATION.md),
[docs/RISK_REGISTER.md](docs/RISK_REGISTER.md).
