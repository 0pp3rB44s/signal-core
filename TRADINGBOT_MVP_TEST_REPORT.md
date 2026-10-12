# TRADINGBOT_MVP_TEST_REPORT

2026-10-12 · branch `feat/tradingbot-mvp-20261012` · python 3.13.7 (WorkMac). **Geen enkele order is naar de exchange verzonden.** Mock-tests gebruiken geen netwerk en geen credentials.

## 1. Testruns
| run | resultaat |
|---|---|
| Bestaande execution/risk-veiligheidsset (baseline vóór wijziging, `4d2fb71`): `test_execution_safety`, `test_entry_order_idempotency`, `test_risk_manager`, `test_close_reconciliation`, `test_position_lifecycle_safety`, `test_risk_layer_gate`, `test_bitget_minimum_size` | **206 passed** |
| Nieuw `tests/test_tradingbot_mvp.py` | **29 passed** |
| Gecombineerd (nieuw + baseline-set + `entry_intent_recovery`, `position_manager_close_economics`, `protection_stop_divergence`, `hostile_p13…no_direct_close`) | 306 passed + 1 failed op de eerste run: `test_bitget_minimum_size::test_idempotency_module_untouched_by_this_patch` — een bestaande tripwire die faalt zolang `execution/entry_submitter.py` **ongecommit** gewijzigd is; **na commit 66/66 passed** (95 passed samen met de nieuwe tests) |
| Niet herhaald | de volledige suite (≈ 10 min, 3.100+ tests); alleen de execution/risk-relevante sets zijn gedraaid |

## 2. De 10 vereiste demonstraties
| # | stap | bewijs | omgeving |
|---|---|---|---|
| 1 | API-authenticatie | read-only preflight tegen **echte** Bitget met de productie-sleutel (`code 00000`, `authorities` gelezen) | **echt (GET)** |
| 2 | Marktdata | `last_price` BTCUSDT 82.382 USDT, contractspecs (`minTradeNum 0,0001`), `server_time` offset +49 ms | **echt (GET)** |
| 3 | Position sizing | `BitgetPort.normalize_size` tegen echt contract: 10 USDT → 0,0001 BTC (8,24 USDT), omlaag afgerond, nooit opgeblazen; mock: `test_happy_path…` | echt (GET) + mock |
| 4 | Order creation | `test_happy_path_all_ten_steps` (exact 1 `place_market_entry`); idempotentie-set (206 tests) | mock |
| 5 | Fill handling | `test_ack_is_not_a_fill_…` (geen positie ⇒ geen bescherming/exit, BLOCKED_UNKNOWN); `test_ambiguous_submit_is_reconciled_never_resent…` | mock |
| 6 | SL/TP-plaatsing | `…happy_path` (PROTECTION_CONFIRMED); `test_stop_loss_unconfirmed_triggers_failsafe_close` (2 pogingen → full close) | mock |
| 7 | Exit-afhandeling | `…happy_path` (EXIT_SUBMITTED → FLAT_CONFIRMED → DONE); `test_close_not_confirmed_escalates_…`; `test_orphan_plan_orders_after_flat_escalate` | mock |
| 8 | Journal-persistentie | hash-keten gevalideerd; `test_journal_chain_detects_tampering` | mock |
| 9 | Restart-reconciliatie | `test_restart_reconciliation_after_crash_between_submit_and_protection_never_resubmits`; `test_resume_with_no_persisted_entry_is_a_noop`; bestaand `STARTUP_RECOVERY_*` in de 206-set | mock |
| 10 | Noodstop | `test_emergency_stop_*`, `test_entry_submitter_blocks_every_entry_…_never_posts`, `test_execution_service_entry_guard_returns_emergency_reason_in_every_mode`, `test_emergency_stop_blocks_smoke_run` | mock |

Extra: `authorise()`-tupel (5 aparte weigeringen), 6 precheck-weigeringen (exchange onbekend, posMode, marginMode, leverage, margin, contractstatus), account-niet-flat, execute-zonder-autorisatie, statuscontract (UNKNOWN≠0, onbeschermde positie, SL onbevestigd).

## 3. Echte-exchange preflight van de smoke-harnas (read-only) [V]
Via stdin op RunnerMac, tegen productie-checkout `4d2fb71` (daarna `git status` = 0, tijdelijke bestanden verwijderd):
`outcome = ABORTED — insufficient available margin: have 0.09448638 USDT, need >= 1.5120`.
Dat bewijst dat de volledige leesroute (account, posities, open orders, plan-orders, prijs, contract, sizing) tegen de echte API werkt én dat de gate een order voorkomt wanneer het account niet gefinancierd is.

## 4. Niet getest (eerlijk)
- **Echte order, echte fill, echte SL/TP, echte close**: niet uitgevoerd (geen autorisatie; account niet gefinancierd). De `BitgetPort`-schrijfroutes (`place_futures_market_order`, `place_futures_protection_orders`, `close_futures_position_full`) zijn de bestaande productiemethodes; signatures zijn mechanisch gecontroleerd en de bestaande unit-tests dekken ze, maar ze zijn niet door dit harnas tegen de echte exchange gedraaid.
- **Bitget demo/paper-exchange**: niet beschikbaar (geen `BITGET_DEMO_*`-sleutels; productielijn heeft geen `paptrading`-header).
- Netwerkfouten (429/5xx/timeouts) zijn alleen via de bestaande idempotentietests gedekt.
