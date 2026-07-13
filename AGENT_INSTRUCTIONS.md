# CGC TradingBot – Instructies voor de AI-agent

## Verplichte opstartprocedure

Lees bij iedere nieuwe sessie eerst, indien aanwezig:

1. INDEX.md
2. BLUEPRINT.md
3. TODO.md
4. PATCHES.md
5. JOURNAL.md
6. EARLY_TRIGGER_1M.md
7. CGC_MASTER_JOURNAL_V5.docx

Gebruik deze levende documenten als bron van waarheid.

Controleer daarna relevante code en logs voordat je conclusies trekt.
Neem nooit aan dat documentatie en implementatie automatisch gelijklopen.

## Communicatieregels

Werk zichtbaar en interactief.

Bij iedere opdracht:

1. Bevestig direct dat de opdracht is begrepen.
2. Benoem welke bestanden, logs of codeonderdelen je gaat onderzoeken.
3. Geef tijdens langere taken tussentijdse statusupdates.
4. Meld duidelijk wanneer een onderdeel is afgerond.
5. Sluit af met een samenvatting van wat daadwerkelijk is uitgevoerd.
6. Benoem fouten, ontbrekende gegevens en onzekerheden expliciet.

Gebruik waar passend:

- ✅ Opdracht begrepen
- 📖 Documentatie lezen
- 🔍 Analyse bezig
- 🧪 Validatie bezig
- 🛠️ Wijziging uitgevoerd
- ⚠️ Probleem of onzekerheid
- ✅ Taak voltooid

Reageer niet stilzwijgend tijdens langere werkzaamheden.

## Veiligheidsregels

- Wijzig geen code tenzij de gebruiker daar expliciet opdracht voor geeft.
- Wijzig nooit zelfstandig `.env`, API-sleutels of live trading-instellingen.
- Verlaag geen risico-, kwaliteits- of strategiepoorten zonder aantoonbaar bewijs.
- Start geen live orders buiten de bestaande tradingbotlogica.
- Verwijder geen logs, datasets, journals of rapporten.
- Maak vóór risicovolle wijzigingen duidelijk wat je gaat veranderen.
- Voer na codewijzigingen relevante tests en validaties uit.
- Meld exact welke bestanden zijn aangepast.

## Analysemodus

Wanneer wordt gevraagd om alleen te analyseren:

- maak geen codewijzigingen;
- pas geen configuratie aan;
- verzamel feiten, aantallen en voorbeelden;
- onderscheid bewezen problemen van hypotheses;
- vermeld sample sizes;
- geef maximaal drie concrete aanbevelingen;
- beschrijf per aanbeveling hoe deze gevalideerd moet worden.

## TradingBot-principes

- Kapitaalbescherming staat boven tradefrequentie.
- Proces gaat boven incidentele winst.
- Geen optimalisatie op basis van enkele trades.
- Geen versoepeling van filters zonder forward data.
- Exchange truth gaat boven lokale state.
- Iedere wijziging moet controleerbaar en terug te draaien zijn.
