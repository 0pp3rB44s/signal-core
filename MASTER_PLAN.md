# MASTER PLAN — signal-core → live autonome tradingbot

**VERSIE 2026-07-18 · Dit is de centrale bron van waarheid voor de koers.**
**Huidige operationele modus: MODE 1 — Strict Forward Paper (24/7, actief).**
Modusdefinities en overgangen: [GO_LIVE_RUNBOOK.md](GO_LIVE_RUNBOOK.md) ·
Live-poorten: [GO_LIVE_CHECKLIST.md](GO_LIVE_CHECKLIST.md) · Operatie:
[DAILY_OPERATIONS.md](DAILY_OPERATIONS.md) · Actuele stand:
[PROJECT_STATUS.md](PROJECT_STATUS.md) · Hypothese-grootboek:
[docs/RESEARCH_JOURNAL.md](docs/RESEARCH_JOURNAL.md) · Bediening:
[BEDIENING.md](BEDIENING.md) · Wijzigingen: [CHANGELOG.md](CHANGELOG.md)

## Missie

Een 24/7 autonome live tradingbot op Bitget Futures met **aantoonbare
statistische en economische edge**. Elke beslissing wordt getoetst aan één
vraag: *brengt dit ons dichter bij een betrouwbaar live systeem?* — met één
onwrikbare randvoorwaarde: **statistische discipline wordt nooit
opgeofferd voor snelheid**. Een bot die live gaat zonder bewijs is geen
versnelling maar kapitaalvernietiging met extra stappen (fase 2-3 bewees
dit live: kosten > edge, −5,28 all-time PnL op 367 trades).

HOOFDREGEL: Safety > Truth > Expectancy > Alpha.

## PATH TO LIVE — zes fasen, achterwaarts ontworpen vanaf live

De fasen zijn sequentieel; een fase start pas als de exitcriteria van de
vorige aantoonbaar zijn gehaald. "Bewijs" betekent altijd: reproduceerbaar,
gedocumenteerd in het research journal of in reports/, en onafhankelijk
controleerbaar.

### FASE A — PLATFORM (status: AFGEROND, onderhoudsmodus)
- **Doel**: betrouwbare, geteste basis: feature engine, funnel-telemetrie,
  candidate lifecycle, strict forward-paper-runtime, risk/execution-engine,
  CI, runner-deployinfra.
- **Waarom**: zonder betrouwbaar platform is elk onderzoeksresultaat en
  elke live trade onbetrouwbaar.
- **Bewijs geleverd**: veld-exacte engine-pariteit (PR #7), hash-chained
  funnel (PR #8), crash-veilige lifecycle (PR #9), runner-infra + CI
  (PR #10), archiver (PR #11); 260/260 tests groen.
- **Exitcriteria**: gehaald. **Restwerk (onderhoud)**: bot-venv → 3.12;
  branch-opschoning; legacy-besluit dashboards/agents/optimizer.
- **Risico**: platform-drift — gemitigeerd door CI op elke PR.

### FASE B — DATAVERZAMELING (status: ACTIEF, dag 1 van ~28-56)
- **Doel**: ≥ 4-8 weken lückenlose microstructuurdata (orderbook 10s,
  funding, liquidaties) + doorlopende forward-paper-observaties van de
  volledige pijplijn.
- **Waarom**: alle op publieke OHLCV toetsbare families zijn verworpen; de
  resterende kansrijke families (orderflow, liquidatie-dynamiek,
  hoogfrequente imbalance) vereisen data die niemand ons kan naleveren —
  we moeten haar zelf bouwen. Dit is het kritieke pad naar edge.
- **Werkzaamheden**: archiver 24/7 (runner), bot 24/7 in strict
  forward-paper-only (**actief sinds 2026-07-18**, bewaakt door
  `scripts/forward_paper_keepalive.sh`), dagelijkse healthcheck via
  `scripts/daily_ops_check.sh`, wekelijks geautomatiseerd
  datakwaliteitsrapport, forward-paper-pariteitsmeting (signaaltijd vs
  uitvoerbare tijd, spread/slippage-aannames).
- **Afhankelijkheden**: runner operationeel (morgen); supervisor-run bot.
- **Risico's**: stille datagaten (mitigatie: SOURCE_STALE-monitoring +
  wekelijks rapport); diskdruk (disk-guard + retentie); klokdrift
  (exchange_ts is authoritative; lag wordt gelogd).
- **Acceptatiecriteria**: 7 dagen 100% dagdekking per bron; bot ≥ 5 dagen
  onafgebroken; pariteitsrapport week 1.
- **Exitcriteria**: ≥ 28 dagen data per bron zonder gaten > 3× interval
  én ≥ 2 uitgeschreven pre-registraties (fase C mag dan starten).
- **Duur**: 4-8 weken. **Verwachte uitkomst**: eerste in-house dataset die
  de geblokkeerde families toetsbaar maakt.

### FASE C — RESEARCH OP EIGEN DATA (status: VOORBEREID, start ~2026-08-15)
- **Doel**: pre-geregistreerde hypothesecycli op de microstructuurdataset
  tot één kandidaat alle poorten haalt — of eerlijk vaststellen dat ook
  deze families geen edge dragen.
- **Waarom**: dit is de enige route naar live die we accepteren.
- **Werkzaamheden**: pre-registraties (mechanisme, richting, DEV/REP,
  BH-familie, economische drempels, power) **vóór** het bekijken van
  uitkomstdata; uitvoering exact volgens registratie; falsificatiebatterij
  op elke positieve uitkomst.
- **Prioriteitsvolgorde hypotheses** (op mechanisme-prior × datadekking):
  1. **Liquidatie-cascade-dynamiek** — geforceerde flow is niet-informatief
     gedreven; vervolg-drift/reversie na cascades is het klassiekste
     microstructuurmechanisme dat we nu kunnen meten (Bybit-events als
     marktbreed signaal, Bitget-orderbook als conditie).
  2. **Orderbook-imbalance @10s-resolutie** — géén recycling van H-4D-1:
     15× hogere cadans, diepte-banden en wall-features; mechanisme
     (quote-aanpassingstraagheid) leeft mogelijk op kortere horizon dan
     2,5-min-snapshots konden zien. Vereist nieuw registratie-argument.
  3. **Spread/depth-regimeovergangen** rond funding-settlements (eigen
     settlementreeks + orderbook).
- **Afhankelijkheden**: fase B-exitcriteria.
- **Risico's**: multiple-testing over cycli heen (mitigatie: BH per
  familie + beperkt aantal families per dataset-window + alles in het
  journal); overfitting-druk naarmate verwerpingen zich opstapelen
  (mitigatie: registratie-vóór-data blijft hard).
- **Acceptatie-/exitcriteria**: kandidaat met BH-p<0,05 DEV, replicatie
  zelfde teken |t|≥2, effect > kosten met marge, stabiliteit over
  maanden/symbolen/regimes, falsificatiebatterij overleefd →
  `EDGE_ACCEPTANCE_REPORT` met alle poorten PASS. Anders: gedocumenteerde
  verwerping en volgende familie of verlengde verzameling.
- **Duur**: 2-6 weken per cyclus. **Verwachte uitkomst**: ofwel een
  geaccepteerde edge, ofwel een eerlijk "nog niet".

### FASE D — KANDIDAAT-VALIDATIE IN FORWARD PAPER (status: NIET GESTART)
- **Doel**: de geaccepteerde kandidaat als strategie implementeren
  (minimale parameters, bevroren vóór evaluatie) en ≥ 4 weken forward
  paper draaien met research-pariteit.
- **Waarom**: een event-study-edge is nog geen uitvoerbare strategie;
  fills, latency, spread en missed trades kunnen het effect opeten.
- **Werkzaamheden**: strategie-implementatie via bestaand
  detector/planner-raamwerk; volledige testset (signaal→plan→forward);
  pariteitsrapportage theoretisch vs uitvoerbaar.
- **Bewijs nodig**: forward-resultaat binnen vooraf gedefinieerde band van
  de research-verwachting; geen materiële aannameschendingen.
- **Exitcriteria**: ≥ 4 weken, n vooraf bepaald op power; afwijking
  research↔forward < geregistreerde tolerantie.
- **Risico's**: stille regime-shift (mitigatie: maanddecompositie);
  verleiding tot tussentijds tunen (verboden zonder her-registratie).
- **Duur**: 4-6 weken.

### FASE E — LIMITED LIVE (status: GEBLOKKEERD tot D-exit)
- **Doel**: kleinste zinvolle live-size (probe-risico, 1 symboolcluster,
  strakke kill-switches) om echte fills/kosten te meten.
- **Bewijs nodig vooraf**: volledige live-gate-checklist
  (PROJECT_STATUS.md) afgevinkt + afzonderlijke expliciete
  eigenaar-autorisatie. Geen uitzonderingen.
- **Werkzaamheden**: runner-deploy op geannoteerde tag; risk-config-audit
  op actuele equity; rollback-oefening; live-vs-paper-pariteitsmonitor.
- **Exitcriteria**: ≥ 4 weken live binnen verwachtingsband; fee-drag
  gemeten < geregistreerde marge; alle safety-events correct afgehandeld.
- **Risico's**: executie-realiteit slechter dan paper (mitigatie: probe-
  size maakt de les goedkoop); operationeel falen (mitigatie: supervisor,
  watchdog, dagelijkse check).
- **Duur**: 4-8 weken.

### FASE F — PRODUCTION LIVE (status: TOEKOMST)
- **Doel**: van limited live naar normale productieallocatie op de
  gevalideerde strategie, met automatische monitoring en onafhankelijke
  stopmechanismen.
- **Bewijs nodig vooraf**: volledige MODE 2-validatie (geregistreerde
  duur + minimum trades, resultaat binnen band, 0 kritieke incidenten) +
  nieuwe expliciete autorisatie.
- **Verboden shortcuts**: overslaan van limited live; opschalen zonder
  band-check; risicolimieten versoepelen.
- **Duur**: instap 4-8 weken na E-exit.

### FASE G — CONTROLLED SCALING (status: TOEKOMST)
- **Doel**: gecontroleerde groei van risico/universum zolang de edge
  aantoonbaar standhoudt; capaciteits- en turnover-grenzen respecteren.
- **Stappen**: exchange-minimum → kleine vaste risicofractie → beperkte
  uitbreiding → productieallocatie; per stap minimumduur, minimumtrades,
  band-check én afzonderlijke autorisatie.
- **Exitcriteria per stap**: expectancy blijft binnen band na elke
  verhoging; drawdown-limieten nooit versoepeld zonder bewijs.
- **Risico's**: capaciteitsverval, zelfimpact, regimeverandering —
  doorlopende monitoring + maandelijkse her-toetsing. Nooit opschalen na
  winst-euforie of om verlies terug te winnen.

## Operationele strategie tijdens B (instrument, geen edge)

Alle detectors blijven actief in de scan; **low_vol_reclaim** is de
primaire forward-paper-workhorse. Kwantitatieve onderbouwing (gemeten
2026-07-18, eerste 25 cycli na herstart): 63 van 93 kandidaten (68%;
historisch 73% van 367 live trades), alle 5 detectors leveren
funnel-events, 24 volledige pipeline-passages
(SCORING→RISK→PLANNER→EXECUTABLE) in ~25 min — hoogste doorstroom voor
pipeline-/logging-/lifecycle-validatie. Expliciet: observatie-instrument
(24,5% WR historisch, fee-drag > edge); **GEEN LIVE KANDIDAAT
BESCHIKBAAR** — er bestaat momenteel geen strategie die de
GO_LIVE_CHECKLIST-poorten haalt.

## Actieve processen, PR's en eigenaar-acties (2026-07-18)

- Bot: strict forward paper actief (Work Mac), keepalive beschikbaar.
- Archiver: actief (Work Mac); migreert morgen naar de Intel-runner.
- Open PR: [#12](https://github.com/0pp3rB44s/signal-core/pull/12)
  (docs + ops-tooling) — wacht op eigenaar-review.
- **Eigenaar-acties**: (1) PR #12 reviewen/mergen; (2) morgen op de
  runner: `.env` lokaal vullen via template en de geannoteerde tag
  `runner-v2026.07.19.1` aanmaken/pushen (of expliciet autoriseren);
  (3) optioneel: keepalive inplannen (cron/launchd of tmux).
- **Volgende opdracht**: runner-deploy uitvoeren (ROADMAP §MORGEN),
  daarna fase B-cadans draaien (DAILY_OPERATIONS.md).

## Planning

| Horizon | Doelen & deliverables | Acceptatie |
|---|---|---|
| **Morgen (07-19)** | Intel-runner operationeel: bootstrap 3.12, .env lokaal, eigenaar-tag `runner-v2026.07.19.1`, preflight, deploy, archiver op runner, rollbacktest (stappen: ROADMAP §MORGEN) | preflight PASS; archiver ≥ 1 h 0 errors op runner; rollback aantoonbaar |
| **Deze week (t/m 07-25)** | Bot 24/7 strict forward-paper (gestart 07-18); dagelijkse healthcheck; Werk-Mac-archiver uit zodra runner ≥ 1 h stabiel; branch-opschoning | bot ≥ 5 dagen onafgebroken; 7 dagen 100% dagdekking archief |
| **2 weken (t/m 08-01)** | wekelijks datakwaliteitsscript + rapport; pariteitsmeting forward paper; bot-venv → 3.12 | rapport week 1 in reports/; geen handmatige checks meer nodig |
| **1 maand (t/m 08-18)** | ≥ 28 dagen data; 2 pre-registraties (liquidatie-cascades, 10s-imbalance) met power-analyses, geschreven zonder uitkomstdata te zien | registraties gecommit vóór eerste toetsrun |
| **2 maanden (t/m 09-18)** | fase C-cyclus 1 afgerond met verdict; bij PASS falsificatiebatterij + start D-voorbereiding | verdict in journal, reproduceerbaar |
| **3 maanden (t/m 10-18)** | bij C-PASS: strategie-implementatie + forward-paper-kandidaatrun (D); bij C-FAIL: cyclus 2 of verlengde verzameling | D-instap alleen met EDGE_ACCEPTANCE_REPORT |

## Wat we bewust NIET doen

Geen parameteroptimalisatie zonder pre-registratie; geen curve fitting;
geen "kleine live test" zonder de volledige gate; geen recycling van
verworpen families zonder materieel nieuwe data én nieuw
mechanisme-argument; geen versoepeling van risk-limieten om resultaten
mooier te maken.
