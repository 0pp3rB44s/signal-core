# TRADINGBOT_MVP_INVENTORY

Agent E · 2026-10-12 · base commit `4d2fb71` (production lineage, "release-a-close-economics-146-g4d2fb71") · worktree `tradingbot-mvp-20261012`, branch `feat/tradingbot-mvp-20261012`.
Evidence: **[V]** gemeten/gelezen in deze sessie · **[A]** afgeleid. Geen secrets in dit document.

## 1. Conclusie in één alinea
De bot **bestaat al en is volledig**: ±10.800 regels Bitget-client/execution/risk-code (`clients/`, `execution/`, `risk/`) die van 2026-08-09 t/m 08-11 echt geld verhandelde (signal-core funnel; stop door owner 2026-08-12 "no proven edge"). Het kortste pad naar een MVP is **hergebruik**, geen herbouw. Wat ontbrak was (a) een in-proces **noodstop**, (b) een **read-only execution-status-contract** voor Mission Control, (c) een **gecontroleerde one-trade smoke-lifecycle**. Die zijn toegevoegd (zie IMPLEMENTATION). Wat de eerste live trade nu blokkeert is **niet de code maar het account**: totaal saldo ≈ 0,21 USDT.

## 2. Waar de bot staat
| plek | wat | commit | status |
|---|---|---|---|
| RunnerMac `~/cgc/bitget_ai_agent_phase7` | **productiecheckout** (detached) | `4d2fb71`, clean [V] | geen live-proces; `state/live.stop` aanwezig sinds 2026-09-09 + `LIVE_PILOT_AUTHORISATION`; `.env.live` met `EXECUTION_ENABLED=true`, `EXECUTION_MODE=LIVE`, leverage 10, max notional 35 USDT [V, niet-geheime sleutels] |
| WorkMac `bitget_ai_agent_phase7` | ontwikkel-repo, 65 worktrees | `1fc9c11` (oudere lijn, 220 commits achter) | niet gebruiken als basis |
| WorkMac `restore-signal-core-funnel-20260927` | LSRL-engine + herstelde funnel (PAPER/DEMO, `MICRO_LIVE` per constructie geweigerd) | `1705b41` | andere codebasis; heeft `paptrading`-demo-ondersteuning |
| **WorkMac `tradingbot-mvp-20261012`** | **MVP-werkboom** (deze taak) | `4d2fb71` → `e099bb3` | lokaal gecommit, niet gepusht |

## 3. Draaiende processen (Phase 2) [V, 2026-10-12 19:04Z]
| machine | proces | kan Bitget-orders plaatsen? |
|---|---|---|
| RunnerMac | `app.main` PID 62906 (`sunday_shadow_runner`, forward-paper) | **Nee**: `forward_paper_only`-staat, geen `.env*` in de werkmap; `PrivateExchangeCallBlocked` in `clients/bitget_base_client.py:28` |
| RunnerMac | LSRL PAPER seg 6 (34422), capacity monitor (34432), Turtle shadow (30894) | Nee (paper/shadow) |
| RunnerMac | `dashboard_v3` (47490, `*:8501`) | Nee (observability) |
| WorkMac | geen trading-proces; `com.cgc.live/.tradingbot/.watchdog` disabled | Nee |
**Geen enkel actief proces kan op dit moment een echte order plaatsen.** De enige live-toegang is `scripts/launch_live.sh` (4 autorisatielagen) en een handmatige start.

## 4. Account (read-only, via de eigen client van de bot, productie-`.env.live`) [V]
| item | waarde |
|---|---|
| Authenticatie | **OK** (`code 00000`); credentials aanwezig: ja; base-url `api.bitget.com` |
| Key-rechten (`authorities`) | `pllw, chow, smow, coow, taxw, p2pw, cpow, ttow, stow` — bevat contract-order- en positie-schrijfrechten (`coow`, `cpow`) en spot-order (`stow`); **geen** withdraw-code. Exacte betekenis per Bitget-docs te bevestigen [A] |
| IP-whitelist | **0 entries** → trade-geschikte key zonder IP-beperking (aanbeveling: whitelisten) |
| Totaal saldo | futures **0,0945 USDT** + spot 0,120 USDT ≈ **0,21 USDT**; funding-wallet 0 |
| Open posities / open orders / TP-SL-plannen | **0 / 0 / 0** |
| Positiemodus / margin | **hedge_mode**, **isolated** (BTCUSDT: long 10x / short 10x; SOLUSDT: long 100x / short 50x) |
| Klok t.o.v. exchange | +49 ms (RTT 344 ms vanaf Runner) |
| Contract BTCUSDT | `minTradeNum 0,0001` (≈ 8,2 USDT @ 82.382), `minTradeUSDT 5`, step 0,0001, maker 0,02 % / taker 0,06 %, status normal |
| `.env.live` equity-config | `ACCOUNT_EQUITY_USDT=60` (verouderd; echt ≈ 0,21) — zie §6 R2 |

## 5. Capaciteitenkaart (hergebruik)
| capability | implementatie | status |
|---|---|---|
| Authenticated REST (HMAC), retries, rate-limit, order-ambiguity-classificatie | `clients/bitget_auth.py`, `clients/bitget_base_client.py` (`BitgetOrderSubmissionAmbiguous/NotSent/Rejected`) | bestaand, getest, **live bevestigd (GET)** |
| Balans / posities / open orders / TP-SL-plannen | `clients/bitget_account_client.py`, `clients/bitget_tpsl_client.py` | **live gelezen** |
| Marktdata | `clients/bitget_market_client.py`, `data/market_fetcher.py` | bestaand |
| Market-order | `clients/bitget_order_client.py:303` `place_futures_market_order` (reduce-only/flag-validatie, min-notional) | bestaand, getest, **niet live gerund in deze taak** |
| Limit-order (maker/IOC/post-only) | `…bitget_order_client.py` `place_futures_limit_order`, `place_futures_ioc_order`, `execution/maker_entry.py` | bestaand |
| Order-status / fill-bevestiging | `…bitget_order_client.py:156` `find_order_by_client_oid` (FOUND/ABSENT/UNKNOWN), `extract_fill_metrics` | bestaand |
| Idempotente entries (clientOid vóór submit, geen blind re-send) | `execution/entry_submitter.py:139`, `execution/order_intent_store.py`, `execution/order_identity.py` | bestaand, 206 tests |
| Position-reconciliatie na herstart | `execution/execution_service.py` (`STARTUP_RECOVERY_*`), `execution/position_reconciler.py:28`, `entry_submitter.py:728` | bestaand |
| SL/TP plaatsen + verifiëren | `clients/bitget_tpsl_client.py:758` `place_futures_protection_orders`, `:219` `verify_active_stop_loss`; SL-verplaatsing `execution/tp_sl_lifecycle.py` | bestaand |
| Onbeschermde positie → fail-safe close | `execution/execution_service.py:1711`, `execution/position_manager.py:1166`, `execution/tp_sl_lifecycle.py:1105` | bestaand |
| Cancel order | `…bitget_order_client.py` `cancel_futures_order`; `bitget_tpsl_client.py` `cancel_all_futures_tpsl_orders` | bestaand |
| Emergency flatten | `clients/bitget_rest.py:54`, `execution/position_manager.py:104` | bestaand; **nu met CLI + stop-vlag** |
| Risk: daily-loss/hard-stop/weekly freeze/max posities/notional caps/kill-switch-gate | `risk/risk_manager.py:178,228,241`, `app/config.py` (`MAX_OPEN_POSITIONS`, `MAX_DAILY_LOSS_PCT`, `HARD_DAILY_STOP_PCT`, `EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT`) | bestaand |
| Exchange-state onbekend → entries blokkeren | `execution_service.py` `LIVE_ENTRY_BLOCKED_POSITION_SYNC_FAILED`, `…PENDING_ORDER_SYNC_FAILED`, `STARTUP_RECOVERY_FAILED \| new_entries_blocked=True` | bestaand |
| `LIVE_TRADING_ENABLED=false` default | `app/config.py:277-278` `EXECUTION_ENABLED=False`, `EXECUTION_MODE="DRY_RUN"`; `is_live_execution` `:480` | bestaand |
| Geen martingale/averaging | geen code gevonden voor scale-in/averaging (grep leeg); `MAX_ENTRY_SUBMISSIONS` begrenst herpogingen; planner-"ladder" is planning, uitvoering = één logische entry [A] | bestaand |
| Strategie-interface | `strategies/*`, `planning/trade_planner.py` → `TradePlan` (`clients/schemas.py`) | bestaand; paper-smoke-strategie (`FORWARD_PAPER_SMOKE_STRATEGY_ENABLED`) voor simulatie |
| Paper/shadow-runner | `forward_paper/`, `app/runner.py` (`FORWARD_PAPER_ONLY`) | bestaand |
| Mission Control-integratie | **ontbrak** (alleen onbeperkt dashboard_v3) | **nieuw**: `execution_status_v1` |
| In-proces noodstop | **ontbrak** (alleen `state/live.stop` voor de launcher/supervisor) | **nieuw**: `execution/emergency_stop.py` |
| Gecontroleerde live smoke-test | **ontbrak** | **nieuw**: `execution/smoke_lifecycle.py`, `scripts/live_smoke_test.py` |

## 6. Bevindingen / risico's (geen wijzigingen aangebracht aan productie)
| id | bevinding | ernst |
|---|---|---|
| R1 | **Account ≈ 0,21 USDT**; minimale smoke-trade vraagt ≥ 1,5 USDT beschikbare futures-margin (preflight berekend) | **BLOKKEREND** |
| R2 | `.env.live` `ACCOUNT_EQUITY_USDT=60` terwijl echt ≈ 0,21; `app/equity.py` valt terug op de configuratie wanneer de live-snapshot "onaannemelijk" is (`PLAUSIBLE_RATIO = 25`, dus < 2,4 USDT) → sizing zou op 60 USDT rekenen (default in code: 1000) | HOOG |
| R3 | Key heeft trade-rechten zonder IP-whitelist | MIDDEL |
| R4 | De bot zelf roept `set_futures_leverage` aan per entry (`execution_service.py:1020`, begrensd door `MAX_LEVERAGE`=10); SOLUSDT staat op de exchange nog op 100x/50x | MIDDEL |
| R5 | Productie-lijn heeft geen Bitget-demo (`paptrading`) ondersteuning; geen `BITGET_DEMO_*` sleutels aanwezig → geen demo-exchange-test mogelijk | INFO |
| R6 | RunnerMac-schijf 8,0 GiB vrij (capacity-monitor alarmgrens 8 GB) | OPERATIONEEL |
