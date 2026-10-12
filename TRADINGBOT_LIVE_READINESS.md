# TRADINGBOT_LIVE_READINESS

**Status: code gereed, account NIET gereed.** Dit document is een procedure voor de **owner**. E-INFRA/Agent E voert geen echte order uit.

## 1. Exacte smoke-test parameters (`smoke_spec.example.json`)
| parameter | waarde | bron |
|---|---|---|
| Symbool | **BTCUSDT** (USDT-FUTURES) | meest liquide, `minTradeNum 0,0001`, `minTradeUSDT 5` |
| Richting | LONG | spec |
| Account-modus | **hedge_mode**, **isolated**, leverage **10x** long/short (reeds ingesteld; tool wijzigt niets) | read-only gelezen 2026-10-12 |
| Nominaal | 10 USDT → genormaliseerd **0,0001 BTC ≈ 8,24 USDT** @ 82.382 | `normalize_size` tegen echt contract |
| Maximum notional | spec ≤ 15 USDT (harde cap in code), werkelijk 8,24 | `HARD_NOTIONAL_CAP_USDT` |
| Margin @10x | ≈ 0,82 USDT; tool eist ≥ 1,5× margin + fees ≈ **1,51 USDT beschikbaar** (preflight-berekening, op nominaal 10) | `precheck` |
| Verwachte fees | taker 0,06 % × 2 (market in, market out) ≈ **0,0099 USDT** (maker 0,02 % alleen bij limit) | contract |
| Max verlies bij SL | stop 0,5 % ≈ **0,041 USDT** + fees ≈ **0,05 USDT** | berekend |
| Take-profit | +0,8 % (≈ 0,066 USDT) | spec |
| Stop-loss mechanisme | exchange-side plan-order via `place_futures_protection_orders` (position TP/SL), **verificatie** via exchange-readback; niet bevestigd ⇒ fail-safe full close | bestaande productiecode |
| Minimum ordergrootte | 0,0001 BTC (`sizeMultiplier` 0,0001); nooit omhoog afgerond | exchange |
| Exit | reduce-only full close via `close_futures_position_full`, daarna flat-readback + plan-orders leeg | bestaande productiecode |

## 2. Voorwaarden (alle moeten PASS zijn)
| # | voorwaarde | nu |
|---|---|---|
| P1 | Futures-wallet ≥ **3 USDT** beschikbaar (tool eist 1,51; marge voor fees/slippage) | **FAIL** — 0,0945 USDT (spot 0,12) → owner-actie: overboeken/storten |
| P2 | `ACCOUNT_EQUITY_USDT` in env-file = werkelijk saldo (nu 60) | **FAIL** (R2) |
| P3 | Key-IP-whitelist ingesteld (nu 0 entries) | aanbevolen, owner |
| P4 | Account flat: 0 posities / 0 orders / 0 plan-orders | PASS (2026-10-12) |
| P5 | Noodstop niet actief (`scripts/emergency_stop.py status`) | PASS (geen vlag) |
| P6 | Geen ander proces dat orders kan plaatsen (`ps`), `state/live.stop` in prod-checkout blijft aanwezig | PASS |
| P7 | Branch `feat/tradingbot-mvp-20261012` door owner gereviewd en op de Runner uitgerold (nu alleen op WorkMac, ongepusht) | **OPEN** |
| P8 | Owner-token + hash + getypte zin (zie §3) | **OPEN** (owner) |
| P9 | Waarneming: Mission Control toont `execution_status_v1` of de owner kijkt live in de Bitget-app | optioneel |

## 3. Procedure (door de owner uit te voeren)
1. **Voorbereiding** (read-only): `python scripts/live_smoke_test.py --spec smoke_spec.json --env-file .env.live --mode preflight` → controleer `outcome = DRY_RUN_COMPLETE` en noteer `SPEC_SHA256`.
2. **Autorisatie**: maak `state/LIVE_SMOKE_AUTHORISATION` met de regels `spec_sha256=<hash>` en `authorised_by=<naam>`; `export LIVE_SMOKE_AUTH_SHA256=<hash>`. (Elke wijziging in de spec verandert de hash en maakt het token ongeldig.)
3. **Observatie**: open Bitget-app (positie/orders) en `python scripts/export_execution_status.py --probe-exchange`.
4. **Uitvoeren**: `python scripts/live_smoke_test.py --spec smoke_spec.json --env-file .env.live --mode execute --i-understand-this-places-a-real-order` en typ de getoonde zin.
5. **Verwacht journal**: `PRECHECK, SIZING, ENTRY_PERSISTED, ENTRY_SUBMITTED, FILL_CONFIRMED, PROTECTION_ATTEMPT, PROTECTION_CONFIRMED, EXIT_SUBMITTED, FLAT_CONFIRMED, DONE` in `state/live_smoke_journal.jsonl` (keten valide).
6. **Na afloop**: bevestig in de Bitget-app: 0 posities, 0 open orders, 0 plan-orders; vergelijk fees/PnL met het journal; bewaar journal + screenshots.

## 4. Noodstop & annulering
- **Stop nieuwe entries direct**: `python scripts/emergency_stop.py engage --reason "<tekst>" --by <naam>` (bestaande posities blijven beheerd).
- **Alles sluiten**: `python scripts/emergency_stop.py flatten --by <naam> --i-understand-this-sends-real-close-orders` (engageert eerst, sluit dan via `emergency_flatten_all`). Controleer daarna in de app dat posities en plan-orders 0 zijn.
- **Handmatig fallback** (altijd mogelijk): Bitget-app/web → Futures → positie sluiten + "alle orders annuleren".
- **Hervatten na crash**: `--mode resume` (reconcileert met de exchange; plaatst nooit een tweede entry).
- **Vrijgeven van de stop**: pas nadat flat + geen plan-orders bevestigd zijn: `python scripts/emergency_stop.py clear --by <naam> --note "<reden>"`.

## 5. Rollback
Er is niets te deployen in productie: de smoke-test loopt uit de MVP-werkboom. Terugdraaien = `emergency_stop engage`, positie flat bevestigen, `LIVE_SMOKE_AUTHORISATION` verwijderen en `LIVE_SMOKE_AUTH_SHA256` unsetten. De productiecheckout op de Runner blijft op `4d2fb71` met `state/live.stop`.

## 6. Wat dit NIET bewijst
Eén trade bewijst de uitvoeringsketen, geen edge. Er is **geen strategie** goedgekeurd voor live; afgewezen historische strategieën mogen niet als productie-alpha dienen.
