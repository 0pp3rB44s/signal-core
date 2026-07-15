# Structured Funnel Telemetry

## Doel en veiligheidsgrens

De funneltelemetry registreert bestaande beslissingen; zij neemt geen beslissingen. De integratie wijzigt geen detector-, selector-, score-, risk-, planner- of executionparameter. Schrijffouten, validatiefouten en corruptie worden binnen de telemetrylaag opgevangen en mogen de handelsloop niet stoppen.

De dataset is uitsluitend een beslisfunnel. Backtestresultaten, exchangefills en forward-paper outcomes worden niet opgenomen. `FORWARD_PAPER_LINK` betekent alleen dat een bestaand executable plan voor verwerking is aangeboden; het is geen fill, trade of outcome.

## Architectuur

Per volledige `_scan_cycle` wordt één willekeurige `scan_id` gemaakt en hergebruikt. Per detectorresultaat wordt `candidate_id` deterministisch berekend als SHA-256 over genormaliseerde strategie, symbool, richting, `candle_open_timestamp` en timeframe. Daardoor blijven base- en fast-lane-candidates ook op gedeelde candlegrenzen gescheiden. Overlap wordt onafhankelijk daarvan gemeten op signaalcandle, symbool en richting.

De runner emit events nadat de bestaande functie haar resultaat heeft teruggegeven:

```text
detector -> selector -> scorer -> risk -> planner -> executable -> paper-link
    |          |          |         |         |            |            |
    +----------+----------+---------+---------+------------+------------+
                               append-only funnel_events.jsonl
```

De base lane registreert de expliciete selectoruitkomst plus de bestaande post-selector-cooldowns. De fast lane heeft in de bestaande runtime geen afzonderlijke selectorcompetitie; iedere daar gedetecteerde kandidaat krijgt daarom een expliciete `SELECTOR_DECISION=PASS` met `selection_path=fast_lane_implicit`. Dit beschrijft het bestaande pad en voegt geen gate toe.

`FunnelEventStore` gebruikt een proces- en threadlock, schrijft één volledige JSON-regel, flusht en `fsync`t. Iedere regel bevat een oplopende sequence, `previous_hash` en een SHA-256 `event_hash` over de canonieke record. Bij hervatting wordt de bestaande keten gevalideerd; daarna worden alleen nieuw bijgeschreven bytes geïndexeerd. Een bestaand `event_id` wordt niet opnieuw toegevoegd.

`reports/funnel_data_quality.json` bevat ketenstatus, eventaantal, duplicaten, ontbrekende verplichte velden en de laatste hash. Het rapport bevat geen eventpayloads of credentials.

## Events

- `DETECTOR_ATTEMPT`: detector is aangeroepen.
- `DETECTOR_DECISION`: detector gaf wel of geen kandidaat terug.
- `SELECTOR_DECISION`: geobserveerde kandidaat werd geselecteerd of niet geselecteerd.
- `SCORING_DECISION`: bestaande score/verdict is vastgelegd.
- `RISK_DECISION`: bestaande riskverdict is vastgelegd.
- `PLANNER_DECISION`: bestaande plannerverdict is vastgelegd.
- `EXECUTABLE_DECISION`: plan is executable of blocked.
- `FORWARD_PAPER_LINK`: alleen de koppeling van een executable plan naar de paperverwerking.

## Schema

Alle events bevatten:

| Veld | Betekenis |
|---|---|
| `schema_version` | Integer schemaversie, momenteel `1`. |
| `event_id` | Deterministische UUID per scan, kandidaat en eventtype. |
| `scan_id` | Eén stabiele UUID voor de volledige scancyclus. |
| `candidate_id` | Deterministische SHA-256 kandidaatidentiteit. |
| `event_type` | Een van de acht vaste eventtypen. |
| `event_timestamp_utc` | Werkelijk emitmoment in UTC. |
| `strategy`, `symbol`, `direction` | Kandidaatidentiteit. |
| `timeframe` | Primaire timeframe. |
| `candle_open_timestamp` | Geobserveerde open-timestamp van de signaalcandle. |
| `signal_timestamp` | Werkelijk vastgelegd signaalmoment; ontbrekende historie wordt niet aangevuld. |
| `session`, `regime` | Geobserveerde context of expliciet `UNKNOWN`. |
| `pass_fail` | Alleen `PASS` of `FAIL`. |
| `primary_reason_code` | Een vaste machineleesbare reason code. |
| `secondary_reason_codes` | Lijst met vaste aanvullende codes. |
| `config_hash` | Hash van een expliciete allowlist niet-geheime funnelconfiguratie. |
| `git_commit` | Commit waarop het proces draait, of `UNKNOWN` als Git niet leesbaar is. |
| `sequence`, `previous_hash`, `event_hash` | Append-only integriteitsvelden. |

Vrije tekst is niet leidend. Optionele `details` mogen alleen niet-geheime, aanvullende observaties bevatten. Reason-classificatie gebruikt uitsluitend de vaste codes in `telemetry/funnel.py`.

## Analyzerprioriteit

Wanneer geldige structurele funnel-events aanwezig zijn, gebruikt de Strategy Funnel Analyzer deze als primaire funnelbron. Sequence, hash-chain, event-ID-uniciteit, schema, vaste reason codes en stagevolgorde worden vóór gebruik gevalideerd. Legacy backtestfunneldata wordt dan niet samengevoegd of gebruikt om ontbrekende structurele stages aan te vullen. Forward-paper outcomes en interne exchange-attributie blijven aparte datasetviews.

Zonder structurele events valt de analyzer terug op de bestaande legacybronnen. Onbekende velden blijven `null`; ontbrekende historische waarden worden niet verzonnen.

## Operationeel

Bestanden:

- `data_store/funnel_events.jsonl` — append-only eventlog, runtimegegenereerd.
- `reports/funnel_data_quality.json` — integriteits- en volledigheidsrapport.
- `docs/FUNNEL_TELEMETRY.md` — schema en gebruiksgrenzen.

De bot hoeft voor ontwikkeling of tests niet gestart of herstart te worden. Tests gebruiken uitsluitend tijdelijke directories.
