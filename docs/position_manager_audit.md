# Position Manager Audit — Fase 1 (2026-07-06)

Bestand: `execution/position_manager.py` — 2900 regels, 1 klasse, waarvan
`sync()` alleen al ~1075 regels (r37-1111). Publieke interface die gebruikt
wordt door runner/execution_service: **alleen `PositionManager(settings)` en
`.sync(snapshots) -> list[PositionUpdate]`** (plus `.store`/`.event_store`
attributen die execution_service leest).

## Dependency map

### 1. TP-hit beheer (in `sync()`, r775-937)
| Functie/blok | Rol |
|---|---|
| `sync()` r775-818 | TP1: `_target_hit_range` óf size-inferred (exchange remaining ≤ 100−tp1_close_pct+5%) → `tp1_hit=True`, remaining −= tp1_close_pct, BE-move (multi-TP only) |
| `sync()` r820-855 | TP2: idem → SL naar TP1-lock |
| `sync()` r857-937 | TP3: → `close_futures_position_full` (close-all) → status CLOSED → journal+dataset+cooldown |
| `_target_hit` / `_target_hit_range` (r2611-2617) | pure predicaten (candle high/low range) |
| `_next_unhit_target` (r1818) | near-TP tracking doel |
| `_protect_after_tp_fill` (r2438) | gedeelde SL-verplaats-flow na TP-fill (cancel oude SL → plaats nieuwe → verify) |

### 2. SL / stop-hit beheer
| Functie/blok | Rol |
|---|---|
| `sync()` r1011-1074 | stop-hit besluit. KRITIEK: lokale stop-touch sluit ALLEEN lokaal als exchange de positie ook kwijt is; staat de positie op Bitget nog open → SAFE MODE, geen autoclose (r1019-1049) |
| `_stop_hit` / `_stop_hit_range` (r2619-2626) | pure predicaten |
| `_move_exchange_stop_loss(_with_retries)` (r2417, 2560) | SL verplaatsen op exchange |
| `_cancel_existing_exchange_stop_losses` (r2334) | oude SL-orders opruimen vóór nieuwe |
| `_extract_stop_loss_order_ids` / `_store_new_stop_loss_order_id` (r2303, 2384) | order-id boekhouding |
| `_close_unprotected_position` (r2528) | noodclose als protectie niet te plaatsen is |

### 3. Break-even beheer
| Functie/blok | Rol |
|---|---|
| `_fee_adjusted_break_even` (r2595) | BE = entry ± fee-buffer |
| TP1-blok r797-811 | enige plek die BE-move initieert (`move_stop_to_be_after_tp1`, niet in single-TP modus) |
| TP3-blok & failed-continuation | zetten `break_even_active=True` als vlag (grandfathered) |
| `_should_tighten_failed_continuation` (r1878) + r938-1010 | SL-tighten bij falende continuation (aparte reden, zelfde move-machinerie) |

### 4. Bitget sync / exchange truth
| Functie/blok | Rol |
|---|---|
| `sync()` r61-71 | `get_all_positions`; bij exception → `bitget_sync_ok=False` → **freeze pad** (r139-183: OPEN behouden, geen acties) ✓ fail-safe |
| r78-125 | exchange open maar lokaal onbekend → `_recover_missing_local_positions` → STATE_RECOVERED |
| r184-331 | lokaal OPEN maar niet op exchange → CLOSED_SYNCED: close-truth ophalen → TPSL cleanup → dataset/journal/cooldown |
| `_exchange_close_truth_from_position_history` (r2632) | primaire PnL-waarheid |
| `_exchange_close_truth_from_order_history` (r2736) | fallback-waarheid (overlappende parsing — kandidaat voor samenvoegen) |
| `_find_live_position`, `_live_position_size/_entry_price/_mark_price/_direction` | live payload parsing |
| `_ensure_exchange_protection(_with_retries)` (r2243) | TP/SL aanwezig op exchange afdwingen |
| `_heal_missing_protection_from_fallback` (r1741) | protectie herstellen uit execution-log |

### 5. Closed trades / dataset writes
| Functie/blok | Rol |
|---|---|
| `_append_closed_trade_dataset_row` (r2092) | primaire close-row writer (v2 dataset) |
| `_ensure_closed_trade_dataset_row` (r1385) | backfill-writer voor CLOSED rows zonder dataset-row (guard: `dataset_close_written` vlag) |
| `_closed_trade_dataset_row_exists` (r1575) | bestaande-row check (dedupe, naast de nieuwe logger-dedupe) |
| `_sync_journal_close` (r2055) | journal + StrategyPerformanceLogger close |
| `_register_symbol_cooldown` (r2015) | cooldown na close |
| import r13: `telemetry.trade_logger.append_closed_trade_row` | module-level writer (zelf óók dubbel gedefinieerd in trade_logger — aparte cleanup-sessie loopt) |

## Duplicate / overlappende logic (bevindingen)

1. **Drie close-row schrijfroutes**: `_append_closed_trade_dataset_row`,
   `_ensure_closed_trade_dataset_row` en `telemetry.append_closed_trade_row`.
   Mitigatie aanwezig: `dataset_close_written`-vlag + `_closed_trade_dataset_row_exists`
   + (sinds 2026-07-06) dedupe in TradeDatasetV2Logger. Overlap blijft
   refactor-kandidaat → `closed_trade_writer.py`.
2. **TP1/TP2/TP3-blokken zijn drie bijna-identieke kopieën** (hit-detectie +
   size-inference + protect + logging). Kandidaat: één geparametriseerde
   `handle_tp_fill(level)` in `tp_sl_lifecycle.py`.
3. **Event-dict constructie 6× herhaald** (zelfde 12 keys) → `position_event_logger.py`.
4. **Close-truth parsers** (position_history vs order_history) delen veel
   veld-plukwerk → samenvoegbaar met bron-parameter.
5. `_safe_float` bestaat hier én in execution_service én trade_logger.

## Bestaande safety-eigenschappen (behouden!)

- Exchange sync faalt → freeze, geen enkele risky action (r139).
- Lokale stop-touch + exchange nog open → geen autoclose (SAFE MODE r1019).
- TP/SL cleanup alleen op het CLOSED_SYNCED-pad ná bevestigde exchange-afwezigheid.
- Close-flow schrijft dataset row exact één keer (vlag + dedupe).
- `tp*_hit`-vlaggen maken TP-verwerking idempotent over cycles.

## Refactorplan (Fase 3, gedragsneutraal)

Stap 1: `execution/closed_trade_writer.py` — verplaats `_append_closed_trade_dataset_row`,
`_ensure_closed_trade_dataset_row`, `_closed_trade_dataset_row_exists`,
`_ensure_close_dataset_context`, `_hydrate_close_position_size` (delegatie vanuit PM).
Stap 2: `execution/position_event_logger.py` — event-dict bouw + PositionUpdate bouw.
Stap 3: `execution/position_reconciler.py` — `_recover_missing_local_positions`,
close-truth functies, `_hydrate_position_from_open_dataset_row`.
Stap 4 (optioneel, hoogste risico): `tp_sl_lifecycle.py` — TP-blokken → per-level functie.
Elke stap: tests groen → commit. Public interface (`PositionManager.sync`) ongewijzigd.
