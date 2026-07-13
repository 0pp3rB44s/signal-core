# ENTRY PLAYBOOK — de anatomie van de A+ sweep-entry (2026-07-13)

Dit is de handmatige methode (sweep-and-reverse op liquiditeit), ontleed in
vijf toetsbare lagen. Belangrijk: dit is een HYPOTHESE die het journal moet
bewijzen — geen garantie. Maar het is dezelfde structuur als je winnende
handmatige trades, en het spiegelbeeld van waarom de bot-entries faalden
(die kochten bevestiging-van-voortzetting = te laat; dit koopt
afwijzing-van-een-extreem = vroeg, met een hard invalidatiepunt).

De trade-these: prijs wordt naar rustende liquiditeit getrokken (stops boven
equal highs / onder equal lows). Wordt zo'n pool geveegd en WIJST de prijs
het niveau af, dan zitten de doorbraak-kopers/verkopers gevangen en trekt de
prijs naar de tegenoverliggende pool.

---

## Laag 1 — WELKE pool (selectie; hier zat het verschil mens vs bot)

De bot vuurde op elke 1H-pivot en verzoop in ruis (22% WR). Jij selecteert.
Een pool is pas handelbaar als hij BETEKENIS heeft:

- [ ] **HTF-relevantie**: de pool is zichtbaar op 4H (of hoger). 1H-pools
      alleen als ze samenvallen met een 4H-niveau. (pool_kaart toont beide.)
- [ ] **Sterkte**: ≥2 touches (equal highs/lows); x3+ = STERK. Meer touches
      = meer stops = meer brandstof.
- [ ] **Leeftijd**: hoe langer onaangeroerd, hoe meer stops zich erboven/
      eronder hebben opgestapeld. Een pool van gisteren < een pool van een week.
- [ ] **Locatie in de range**: sweep van een LOW onderin de range (long) of
      een HIGH bovenin (short). Nooit mid-range — daar is de magneet zwak.
- [ ] **Confluentie** (bonus): rond getal, sessie-high/low (Azië/Londen/NY),
      4H-pool + 1H-pool op zelfde niveau.

**Niet handelbaar**: verse pool, x1, mid-range, alleen op 1H zichtbaar.

## Laag 2 — HOE wordt hij geveegd (het karakter van de sweep)

- [ ] **Snelle wick, snel terug**: een prik door het niveau die binnen 1-2
      candles terugkeert = stop-run-signatuur (institutioneel ophalen). GOED.
- [ ] **Ondiepe prik**: net voorbij het niveau (< ~0,3-0,5 ATR). GOED.
- **Langzame grind erdoorheen** met closes voorbij het niveau = échte
  doorbraak, GEEN sweep. Niet tegen invaden. SLECHT.
- **Diepe doorschieter** die er ver voorbij blijft hangen = momentum, geen
  stop-run. SLECHT.

## Laag 3 — BEVESTIGING (waar de "perfecte entry" echt woont)

Nooit blind op de sweep instappen (dat deed de mechanische test: 22% WR).
Wacht op bewijs dat de afwijzing echt is:

- [ ] **Close terug binnen het niveau** op je trading-TF (1H of 15m).
- [ ] **Structuurbreuk op de lagere TF**: na een sweep van een LOW breekt
      de 5m/15m zijn laatste lower-high (shift/CHoCH). Dát is de trigger.
- [ ] **Entry op de retrace**: niet de trigger-candle chasen, maar de
      terugtest van het gebroken niveau / de imbalance limit-laddern.
      (Chasen was meetbaar -5x duurder — de 15bps-les van de bot.)

Stop: NET voorbij de sweep-wick. Dat is je harde invalidatie: neemt de
prijs de wick opnieuw, dan was het geen stop-run en is de these dood.
Klein verlies, klaar, geen discussie.

## Laag 4 — DE REKENSOM VOORAF (of: waarom je meestal niét handelt)

- [ ] **Doel = de tegenoverliggende onaangeroerde pool** (pool_kaart).
- [ ] **RR ≥ 2,0 minimaal** van entry tot doelpool, met de stop uit laag 3.
      Ligt de doelpool dichterbij dan 2R -> SKIP, hoe mooi de sweep ook is.
      (Dit ene filter dwingt af wat elke meting zei: alleen trades met ruimte.)
- [ ] **Fee-realiteit**: bij R = 0,5% is de fee ~kwart van je risico. Stop
      < 0,3% -> extra streng zijn op RR.

## Laag 5 — WANNEER NIET (context-veto's)

- Regime CHOP/GRIND (maandmeting deel C < ~0,5): sweeps gebeuren wel maar de
  follow-through ontbreekt — wees dubbel selectief of sla de dag over.
- Rond high-impact nieuws: de "sweep" kan de eerste poot van iets groters zijn.
- Buiten sessie-momenta: Londen-open en NY-open dragen het patroon het best;
  het holst van de nacht het slechtst.
- Na 2 verliezen op een dag: stoppen. (Tilt is de duurste setup die bestaat.)

---

## De finetune-lus (hoe "perfect" echt ontstaat)

Perfectie is geen formule maar een feedback-cyclus op JOUW data:

1. Elke trade in het journal MET de kwaliteitsvelden (pool-TF, touches,
   sweep-karakter, bevestiging gebruikt, geplande RR, sessie).
2. Vanaf ~20-30 trades splitst `journal.py stats` de expectancy per
   ingrediënt: winnen je 4H-pool-trades wél en je 1H-trades niet? Werkt de
   structuurbreuk-trigger beter dan de candle-close? Dan wordt de checklist
   strenger op precies dat punt.
3. Herhaal. Zo convergeert de entry naar "perfect voor JOU" — forward
   bewezen, nooit gecurve-fit.

Dit is exact wat de bot niet kon (pool-selectie, sweep-karakter, timing) en
wat jij als mens wél kunt — nu met een meetlat eronder.
