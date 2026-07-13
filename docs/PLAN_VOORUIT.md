# PLAN VOORUIT — van kennis naar positie (2026-07-13)

Geschreven na de eerlijkste week van dit project: vier strategie-tests, een
volledige data-audit, en het eigenaar-besluit tot observe-mode (PATCH-073).
Dit plan bouwt op wat bewezen is — niet op wat we hopen.

## Het uitgangspunt (wat de data ons vertelde)

1. Infrastructuur ≠ edge. De bot is een uitstekende meet- en executiemachine;
   het signaal eronder had geen voorspellende kracht.
2. De enige winst in de hele dataset kwam van de eigenaar zelf: 4 handmatige
   trades (+6,83 bruto) — weinig, geduldig, hoge conviction, grote bewegingen.
3. Fees bepalen de lat: 0,12% roundtrip maakt korte timeframes wiskundig
   bijna onwinbaar voor retail. Hoe langer de hold, hoe kleiner de fee-tol.
4. Asymmetrie verslaat gelijk-hebben: 38% WR was winstgevend in de backtest
   zodra winnaars 3x groter waren dan verliezers.
5. Test vóór inzet werkt: vier keer voorkwam het een verliezende wijziging.

## Het principe: positioneren, niet voorspellen

"Goed zitten" =
- **verlies klein en vast** (het enige dat je 100% zelf bepaalt),
- **winst variabel en open** (laten lopen als het loopt),
- **alleen spelen als de kansen scheef staan** (A+ setup, anders niets doen),
- **weinig trades** (elke trade betaalt tol; zeldzaamheid is een filter).

## FASE 0 — Fundament (staat al) ✅

- Bot in observe-mode: scant, logt, leert — kan geen geld verliezen.
- Datapijplijn eerlijk: exchange-truth overal, funnel compleet.
- Pool-detector (`market_data/liquidity_pools.py`): objectieve kaart van
  buy-side/sell-side liquiditeit, sterkte, swept/unswept.
- Kapitaal (~€50) beschermd. Dit is het onderzoeksbudget, geen groeimotor.

## FASE 1 — Bewijs verzamelen, nul risico (4-8 weken)

Twee sporen, parallel:

**Spoor A — de eigenaar traadt handmatig (de bewezen kant).**
- Alleen A+ setups volgens de eigen sweep/liquiditeit-methode, met de
  pool-detector als kaart. Richtlijn: max 2-3 trades per WEEK. Verveling is
  onderdeel van de strategie.
- Vast risico per trade (bv. 1% = ~€0,50): klein genoeg om 20x fout te
  mogen zitten.
- ELKE trade in het journal: setup, pool, reden, screenshot, uitkomst, les.
  Zelfde meetlat als de bot: na 20-30 trades kennen we WR, R-ratio en
  expectancy van de HAND — met dezelfde eerlijkheid.

**Spoor B — de bot verzamelt forward-bewijs (de metende kant).**
- Observe-mode logt wat de strategieën ZOUDEN doen: gratis out-of-sample
  data die niet gecurve-fit kan zijn (de toekomst bestond nog niet).
- Maandelijks meetmoment: forward-expectancy per strategie uit de funnel.
- Regime-monitor: BTC 4H trend-sterkte (|EMA20-EMA50|/ATR). Metriek bestaat;
  markt is ~38% van de tijd 'trending'. Geen actie — alleen weten in welk
  weer we zitten.

## FASE 2 — Meten en beslissen (na fase 1)

Beslisregels, vooraf vastgelegd zodat emotie ze niet kan buigen:

- Handmatige trades ≥20 én expectancy positief → DAT is de edge. Vervolg:
  de bot wordt copiloot (alerts op pool-sweeps, sizing-rekenaar, journal-
  automatisering) — nooit autopiloot. Mens beslist, machine ondersteunt.
- Handmatige trades negatief → ook die waarheid accepteren we; dan is
  sparen/stacken de enige eerlijke groeistrategie en is dit project een
  afgeronde leerschool.
- Bot-forward-data toont ergens ≥30 paper-trades met positieve expectancy
  → kleine live-probe overwegen (probe-size, maandbudget, vooraf bepaald).
- Geen van beide → niets live. Geld dat je niet verliest is rendement.

## FASE 3 — Groei (maanden tot jaren, alleen na bewezen fase 2)

- Kapitaalgroei komt uit drie bronnen, in volgorde van betrouwbaarheid:
  (1) bijstorten uit inkomen, (2) compounding van een bewezen edge,
  (3) nooit: leverage op een onbewezen edge.
- Realistisch anker: een bewezen edge die 40-60%/jaar doet is uitzonderlijk
  goed. €60 wordt zo geen €1000 dit jaar — maar een bewezen proces + €50/mnd
  bijstorten + compounding is over 3-5 jaar levensveranderend groter dan
  elke gok die volgende maand op nul eindigt.
- Als crypto een echte trending fase ingaat (regime-monitor), mag de
  trend-following-aanpak (backtest +80R over 1,4jr; OOS zwak) een
  her-test krijgen — eerst paper, dan pas probe.

## Wat dit plan NIET belooft

- Geen voorspellingen, geen "eind dit jaar X". 
- Geen nieuwe strategie-tuning op oude data (dat is vier keer gesneuveld).
- Geen garantie dat er een edge IS — wel de garantie dat we hem eerlijk
  meten als hij er is, en dat we niets meer verliezen aan ruis.

## Gereedschap (gebouwd 2026-07-13, alle drie getest)

```
python3 scripts/pool_kaart.py [SYMBOLEN]   # dagelijkse pool-kaart (1H + 4H)
python3 scripts/journal.py add --symbol .. --dir .. --entry .. --stop .. --exit .. --setup ".."
python3 scripts/journal.py stats           # hand-expectancy + fase-2 oordeel
python3 scripts/maandmeting.py             # A: hand / B: bot-forward / C: regime
```

- pool_kaart: onaangeroerde pools boven/onder, x-aantal = sterkte, STERK bij x3+.
- journal: elke handmatige trade erin; vanaf n=20 velt hij zelf het fase-2 oordeel.
- maandmeting: simuleert de EXECUTABLE plannen van de bot (sinds observe) tegen de
  echte candles erna = out-of-sample forward-bewijs; plus BTC-regime-check die
  meldt wanneer de markt weer trending is (her-test-moment trend-following).

## Ritme

- **Dagelijks (eigenaar, 10 min):** pool-kaart bekijken; alleen handelen
  bij een A+ setup. Geen setup = geen trade = een goede dag.
- **Wekelijks (samen, 15 min):** journal bijwerken, trades bespreken.
- **Maandelijks (samen, 1 sessie):** forward-cijfers bot + hand-expectancy
  + regime-check. Beslissen volgens de regels van fase 2, niet volgens gevoel.
