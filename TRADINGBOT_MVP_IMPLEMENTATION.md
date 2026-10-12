# TRADINGBOT_MVP_IMPLEMENTATION

Branch `feat/tradingbot-mvp-20261012` @ `e099bb3` (+ docs), basis `4d2fb71`. **Lokaal gecommit, niet gepusht.** Productiecheckout en RunnerMac zijn niet gewijzigd.

## Principe
Niets herschreven. Drie kleine, geteste toevoegingen + twee minimale haken in bestaande code. Alles is fail-closed en standaard uit.

## 1. Noodstop — `execution/emergency_stop.py` (nieuw, 89 regels)
- Flagbestand `state/EMERGENCY_STOP.json` (override: env `EMERGENCY_STOP_PATH`). Bestaat het → **alle nieuwe entries geblokkeerd**; bestaande posities blijven beheerd (SL/TP-onderhoud, protection-repair, closes). Het verwijdert nooit bescherming.
- **Fail-closed**: onleesbaar bestand = actief. `engage` is idempotent (bewaart oorspronkelijke reden), `clear` vereist een naam en schrijft `.history.jsonl`.
- Haken (8 + 15 regels):
  - `execution/entry_submitter.py` `submit_entry` (`:139`, universeel chokepoint van elke live entry): retourneert `REJECTED/EMERGENCY_STOP`, **vóór** er een intent wordt gepersisteerd of een POST kan gebeuren.
  - `execution/execution_service.py` `_entry_guard_reason` (`:2194`): evalueert de stop **eerst**, in alle modi.
- CLI `scripts/emergency_stop.py`: `status | engage | clear | flatten`. `flatten` engageert eerst, en sluit pas posities met `--i-understand-this-sends-real-close-orders` via de bestaande `emergency_flatten_all()`.

## 2. Read-only statuscontract — `execution/status_contract.py` (nieuw)
- `execution_status_v1` (schema: `contracts/execution_status_v1.schema.json`): `connection`, `mode`, `account`, `positions`, `orders`, `fills`, `risk`, `emergency_stop`, `heartbeat`, `producer{generated_at_*, ttl_s, read_only}`, `content_sha256`.
- Regels: onbekend is `null`/`"UNKNOWN"`, **nooit 0**; geen exchange-snapshot ⇒ `source = UNAVAILABLE` en `risk.entries_blocked = true`; positie zonder bevestigde SL ⇒ `stop_loss_confirmed: false|null`, `risk.operator_attention_required = true`; `fills` zijn expliciet gelabeld `LOCAL_JOURNAL_NOT_EXCHANGE_TRUTH`.
- `ExchangeReadOnlyProbe`: uitsluitend GET's (accounts, account-config, posities, open orders, TP/SL-plannen, server-time).
- Exporter: `scripts/export_execution_status.py [--probe-exchange] [--out-dir state/mission_control_export]` — atomair, geen secrets. **Mission Control-frontend niet aangeraakt**; Agent B consumeert het bestand.

## 3. Gecontroleerde smoke-lifecycle — `execution/smoke_lifecycle.py` + `scripts/live_smoke_test.py` (nieuw)
Eén trade: `PRECHECK → SIZING → ENTRY_PERSISTED → ENTRY_SUBMITTED → FILL_CONFIRMED → PROTECTION_CONFIRMED → EXIT_SUBMITTED → FLAT_CONFIRMED → DONE`, elke fase in een **hash-gekoppeld journal** (`state/live_smoke_journal.jsonl`) en een staatsbestand (`state/live_smoke_state.json`).
Invarianten (allemaal getest):
- ACK ≠ fill; positie bestaat pas na positie-endpoint; "gesloten" pas na flat-read.
- clientOid wordt gepersisteerd **vóór** submit; een dubbelzinnige submit wordt **nooit** opnieuw verzonden (lookup + positions; ABSENT ⇒ abort, UNKNOWN ⇒ BLOCKED + noodstop).
- SL niet bevestigd (2 pogingen) ⇒ fail-safe full close; close niet bevestigd of orphan plan-orders ⇒ noodstop + `OPERATOR_ATTENTION`.
- Exchange onbekend ⇒ geen order; account niet flat ⇒ geen order; leverage/margin/posMode moeten **al** overeenkomen — de tool wijzigt ze nooit.
- Echte order vereist: `--mode execute` + lange vlag + env `LIVE_SMOKE_AUTH_SHA256` = hash van de exacte spec + token `state/LIVE_SMOKE_AUTHORISATION` (`spec_sha256=…`, `authorised_by=…`) + getypte zin + `EXECUTION_ENABLED/LIVE` in env-file + notional ≤ 15 USDT (harde cap).
- Restart: `--mode resume` reconcileert tegen exchange en voegt **nooit** een tweede entry toe.
- Standaardmodus `preflight` is volledig read-only.

## 4. Niet gedaan (bewust)
Geen nieuwe strategie, geen herontwerp van de risk engine (bestaand en getest), geen demo-exchange-integratie (geen demo-sleutels, productielijn heeft geen `paptrading`-header), geen wijziging aan `.env*`, leverage of account, geen push, geen live order.
