# Bediening

## Strict forward-paper-only

`FORWARD_PAPER_ONLY=true` is de veilige runtime voor uitsluitend publieke
Bitget-marktdata en forward-paperobservatie. Deze modus is strenger dan de
gewone observe/DRY_RUN-modus:

| Modus | Publieke marktdata | Private account/positie | Orders | Forward paper |
|---|---:|---:|---:|---:|
| Observe / DRY_RUN | ja | mogelijk | geen entries | configureerbaar |
| Strict forward-paper-only | ja | geblokkeerd | geblokkeerd | verplicht aan |

In strict-modus worden conflicterende instellingen effectief geforceerd naar:

```text
EXECUTION_ENABLED=false
EXECUTION_MODE=DRY_RUN
FORWARD_PAPER_ENABLED=true
POSITION_MANAGER_ENABLED=false
POSITION_LOOP_ENABLED=false
POSITION_SYNC_ON_START=false
```

Daarnaast gebruikt de runner een client die alleen publieke marktmethoden
aanbiedt. De centrale HTTP-laag blokkeert als tweede veiligheidslaag iedere
private request voordat authenticatie of transport plaatsvindt.

## Veilig starten

Start alleen vanaf een schone `main`, zonder actieve bot of dashboard:

```bash
./scripts/start_forward_paper.sh
```

Optioneel kan het scaninterval in seconden worden meegegeven:

```bash
./scripts/start_forward_paper.sh 60
```

Het script stopt nooit bestaande processen, wijzigt `.env` niet en weigert te
starten wanneer bot of dashboard al draait. De veiligheidsinstellingen gelden
alleen voor het gestarte proces.

## Gezondheidscheck

```bash
./scripts/check_forward_paper.sh
```

De check valideert proces en modus, zoekt private-callmarkers, reconstrueert de
outcomes, valideert de event-hash-chain en rapporteert events, open/closed
trades, outcomes en incomplete trades.

De runtime schrijft daarnaast twee genegeerde diagnosebestanden:

- `state/runtime_heartbeat.json`: PID/PPID/process group, laatste scanfase,
  laatste candle-request en aantallen gestarte/voltooide cycli;
- `state/last_shutdown.json`: expliciete shutdownreden, exitcode en eventueel
  ontvangen signal.

De backgroundlauncher plaatst de bot in een nieuwe OS-session. Daardoor blijft
het proces leven wanneer de startende shell sluit; `nohup` alleen is hiervoor
niet voldoende in process-group-isolerende omgevingen.

## Stoppen

```bash
./scripts/stop_all.sh strict_forward_paper_stop
```

Controleer daarna dat geen proces resteert:

```bash
pgrep -af "app.main|dashboard_v2.app"
```

Start bij een ongeldige hash-chain niet opnieuw. Bewaar de corrupte runtimefile
voor onderzoek en herstel vanuit het laatste aantoonbaar geldige eventlog.
