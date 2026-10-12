# RUNNERMAC_ENV_LIVE_AUDIT

Agent E · 2026-10-12 19:35–19:50 UTC · RunnerMac `macbook-air-van-bryon.local` (user `bryonkolkman`) · production checkout `4d2fb71`.
Geen secretwaarden in dit document. Er is **geen order geplaatst**, niets op de exchange gewijzigd, geen proces herstart of gestopt.

## 1. Exact configuratiebestand
| item | waarde |
|---|---|
| Bestand | `/Users/bryonkolkman/cgc/bitget_ai_agent_phase7/.env.live` (git-genegeerd, checkout `git status` = 0 vóór én na) |
| Rechten | `-rw-------` (600), eigenaar `bryonkolkman` — **behouden** |
| Hoe het geladen wordt | `scripts/launch_live.sh:34` en `deploy/launchd/live_agent.sh:93` → `guard_load_env ".env.live"` (`scripts/lib/env_guard.sh:114`: `set -a; . ./.env.live`). Variabelen komen dus als **omgevingsvariabelen** in het proces; `app/config.py:Settings` leest `os.environ` vóór elke `.env` (`env_file=".env"` is alleen een fallback). `.env.live` wordt nooit direct door Python gelezen. |
| Actief entrypoint | `python -m app.main` (`scripts/launch_live.sh` / `live_agent.sh`), met `single_live_executor_lock` + `ExecutionIdentity` in `app/main.py` (één live executor per account/host) |
| Draait er nu een proces met dit bestand? | **Nee.** Geen live-engine actief; `state/live.stop` (sinds 2026-09-09) en `LIVE_PILOT_AUTHORISATION` aanwezig. `state/live_runtime.state`: `commit=4d2fb71`, sessie 2026-09-05. |

## 2. Backup
| item | waarde |
|---|---|
| Pad | `/Users/bryonkolkman/.cgc_env_backups/env.live.20261012T193752Z.bak` |
| Rechten | bestand `600`, map `700`; buiten elke git-repo; originele mtime behouden (`copy2`) |
| Integriteit | sha256 identiek aan het origineel op het moment van backup |
| Overig | bestaande oudere kopie `.env.live.backup_20260908_112147` (600) ongewijzigd. Tijdelijke kandidaat- en dubbele-backupbestanden (credentials bevattend) heb ik zelf aangemaakt en direct weer verwijderd. |

## 3. Wijzigingen (alleen niet-geheime waarden getoond)
### Gewijzigd (7)
| variabele | oud | nieuw | reden |
|---|---|---|---|
| `EXECUTION_ENABLED` | `true` | **`false`** | effectieve live-veiligheidsschakelaar; het bestand zette live order-plaatsing aan (zie §6) |
| `EXECUTION_MODE` | `LIVE` | **`DRY_RUN`** | idem; `is_live_execution` is nu `False` |
| `ACCOUNT_EQUITY_USDT` | `60` | `0.20` | verouderd; echt totaal ≈ 0,21 USDT (futures 0,0945 + spot 0,12). `app/equity.py` valt terug op deze waarde als de live-snapshot "onaannemelijk" is (factor 25) |
| `ACCOUNT_BALANCE_USDT` | `60` | `0.20` | idem |
| `MICROFLOW_SCALPER_ENABLED` | `true` | `false` | gepensioneerde strategie; `true` start bij `StartupRunner.__init__` (`app/runner.py:518`) een MicroFlow-publieke-feed-runtime met `MICROFLOW_DATA_DIR=data_store/microflow_live` (de bron van de eerdere 15 GB-schijfdrain) terwijl `ENABLED_STRATEGIES` de strategie al blokkeert |
| `WATCHLIST` | 28 symbolen (verouderd) | = `PRODUCTION_SYMBOL_ALLOWLIST` (12) | `env_guard.sh:guard_apply_canonical_symbol_allowlist` overschrijft dit toch bij launch; nu ook consistent als Python het direct leest |
| `EXECUTION_CONFIRM_SYMBOLS` | 8 symbolen (incl. verouderde) | = `PRODUCTION_SYMBOL_ALLOWLIST` (12) | idem |
### Verwijderd (2) — bewezen ongebruikt
| variabele | bewijs |
|---|---|
| `OPENAI_API_KEY` | niet in `Settings`-aliases, niet in enige `os.environ`/`getenv`-aanroep (scan over app, execution, risk, clients, strategies, planning, data, telemetry, market_*, forward_paper, microflow, candidate_lifecycle, archiving, scripts, dashboard_v3, agents*); repo-brede grep: alleen `MASTER_STATUS_REPORT.md` (tekst). Verwijderd om de blast-radius van het trading-envbestand te beperken; staat nog wel in de backup. |
| `SHORTS_ALLOWED_STRATEGIES` | niet geconsumeerd (alleen `ENABLE_SHORTS` bestaat); misleidend omdat het een beperking suggereert die niet wordt afgedwongen |
### Toegevoegd: geen
Alle 26 door de shell-guards vereiste variabelen waren al aanwezig; de 49 ontbrekende risk/execution-velden hebben veilige code-defaults (o.a. `MAX_TOTAL_EXPOSURE_PCT=200`, `MAX_CORRELATED_POSITIONS=2`, `SESSION_RISK_MULTIPLIER=0.5`).
### Bewust ongewijzigd (hoort bij aparte beslissing)
`DASHBOARD_HOST=0.0.0.0` (zie risico R5), `MAX_LEVERAGE/DEFAULT_LEVERAGE=10`, alle risicoparameters, `ADAPTIVE_TREND_RETIRED=true`, `ADAPTIVE_TREND_LIVE_ENTRY_ENABLED=true` (door `env_guard.sh` vereist zolang `ENABLED_STRATEGIES=adaptive_trend_tsmom_v1`), credentials.

## 4. Welke variabelen worden daadwerkelijk gebruikt
116 sleutels in het bestand (nu 114): **114 worden door `Settings` geconsumeerd**; 0 alleen via code-`environ`; de 2 overige zijn hierboven verwijderd. De 26 shell-guard-vereisten zijn allemaal aanwezig. `Settings` gebruikt `extra="ignore"`, dus een sleutel die niet in de Settings-aliases staat zou stilzwijgend genegeerd worden — dat was hier alleen bij de twee verwijderde het geval.

## 5. Validatieresultaten
| controle | resultaat |
|---|---|
| Configuratie parseert (Settings, kandidaat én live-bestand) | **PASS** |
| Verplichte variabelen aanwezig (26 shell-guard-vereisten) | **PASS** |
| 13 shell-invarianten re-geïmplementeerd (symbolenaantal = `MAX_SYMBOLS`, `MAX_OPEN_POSITIONS=2`, `EXECUTION_MAX_PER_CYCLE=2`, `STRATEGY_ISOLATION_ENABLED`, `OLD_STRATEGIES_NEW_ENTRIES_ENABLED=false`, grid uit, `MAX_LEVERAGE≤10`, TP-percentages = 100, …) | **PASS (13/13)** |
| **Effectieve veiligheid uit de code** (niet alleen uit het bestand) | `execution_enabled=False`, `execution_mode=DRY_RUN`, **`is_live_execution=False`**, `forward_paper_only=False`, `app_env=production` |
| "Flip to live" valideert zonder fouten (EXECUTION_ENABLED=true + MODE=LIVE in het productie-validatorpad) | **PASS** — go-live is een expliciete 2-regelige wijziging |
| Bitget-authenticatie | **PASS** (`code 00000`) |
| Futures-account lezen (USDT-FUTURES) | **PASS**: totaal ≈ 0,21 USDT; hedge_mode, isolated; BTCUSDT 10x/10x |
| Futures-marktdata (candles, symbol-price, contracten) | **PASS** |
| Posities / open orders / TP-SL-plannen lezen | **PASS**: **0 / 0 / 0** (eerst misleidend "2" gerapporteerd door `len()` op een dict; herteld met echte lijsttelling) |
| Order geplaatst? | **Nee** (0 POST-calls; alleen GET) |
| Bestaand tradingproces verstoord? | **Nee**: geen proces herstart of gestopt; prod-checkout `git status` = 0 |
| Bot start in veilige modus | **PASS**: `StartupRunner` gebouwd in een wegwerp-cwd met exact de omgevingsvariabelen van `guard_load_env`; `execution_service.execute([])` → `[]`; `microflow_runtime` niet gestart; alleen `state/bitget_rate_limit.json` in de tijdelijke map geschreven (verwijderd) |

## 6. Effectieve configuratie voor execution (uit de code)
| gebied | waarde | status |
|---|---|---|
| Product / marge | `USDT-FUTURES`, `EXECUTION_MARGIN_MODE=isolated`, margin-coin USDT | consistent met exchange (isolated) |
| Positiemodus | hedge (de bot gebruikt `holdSide`); exchange = `hedge_mode` | consistent |
| Leverage | `DEFAULT=MAX=10`; BTCUSDT op exchange 10x. **Niet gewijzigd.** De bot zelf zet leverage per entry (`execution_service.py:1020`, begrensd door `MAX_LEVERAGE`); SOLUSDT staat op de exchange op 100x/50x | OK/let op |
| Orders | `EXECUTION_MAX_PER_CYCLE=2`, `EXECUTION_REQUIRE_CONFIRMATION=true`, notional 10–35 USDT, maker-entry aan (`MAKER_ENTRY_FALLBACK_MARKET=false`) | actief zodra live |
| Risico | `MAX_OPEN_POSITIONS=2`, `ACCOUNT_RISK_PER_TRADE_PCT=0.5`, `MAX_DAILY_LOSS_PCT=1.0`, `HARD_DAILY_STOP_PCT=1.5`, `WEEKLY_FREEZE_LOSS_PCT=7.0`, `SYMBOL_COOLDOWN_MINUTES=30`, `PLANNER_LARGEST_LOSS_GUARD_BPS=85` | **actief** (risk_manager) |
| SL/TP | `TP1/2/3_CLOSE_PCT=40/30/30`, `MOVE_STOP_TO_BE_AFTER_TP1=true`, `PLANNER_TP{1,2,3}_R=1.2/1.8/2.6`, `PLANNER_STOP_BUFFER_BPS=8`, `POSITION_MANAGER_ENABLED=true`, check-interval 10 s | **herkend** door Settings |
| Kill-switch | launcher-niveau `state/live.stop` (blijft staan); `RiskManager._kill_switch_gate`; `single_live_executor_lock`. In-proces noodstop bestaat **alleen** in de MVP-branch (niet uitgerold) | gedeeltelijk |
| Journal/logs | `state/live_trade_journal.json`, `state/executed_trades.json`, `state/execution_events.json`, `logs/` (relatief t.o.v. de checkout; geen env-variabelen voor deze paden); `LOG_LEVEL=INFO` | OK, schijf: Runner 8 GiB vrij |
| Mission Control-export | **niet beschikbaar in `4d2fb71`**; `execution_status_v1` zit in de MVP-branch (`scripts/export_execution_status.py`) en is nog niet uitgerold. Er bestaat geen env-variabele om dit te configureren | OPEN |

## 7. Runtime-veiligheid (Phase 5)
| proces / pad | kan Bitget-orders plaatsen? |
|---|---|
| `app.main` PID 62906 (`sunday_shadow_runner`) | **Nee**: omgeving `FORWARD_PAPER_ONLY=true`, `EXECUTION_MODE=DRY_RUN`, `EXECUTION_ENABLED=false`, API-sleutels **leeg** |
| Turtle shadow PID 30894 | Nee: DRY_RUN, `EXECUTION_ENABLED=false`, sleutels leeg |
| `dashboard_v3` PID 47490 (`dashboard_runtime`) | Nee: geen Bitget-sleutels in zijn omgeving of `.env`; bevat wel order-capabele clientcode |
| LSRL PAPER 34422 / capacity monitor 34432 | Nee: geen Bitget-variabelen, paper |
| **Niet-geladen launchd-plists** (`com.cgc.live` KeepAlive, `watchdog`, `dashboard`, `research.*-collector`) | Latent: laden bij eerste GUI-login. `com.cgc.live` → `live_agent.sh` weigert op `state/live.stop`; en **na deze wijziging** faalt `guard_assert_live_mode` (vereist `EXECUTION_ENABLED=true`) → dubbel gesloten. *Vóór de wijziging* hadden `LIVE_PILOT_AUTHORISATION` + `live_runtime.state(commit=4d2fb71)` + `.env.live=LIVE` alleen nog `live.stop` als barrière. |
**Meerdere execution engines op één account:** geen draaiend. Structureel voorkomen door `single_live_executor_lock` (+ `guard_assert_no_duplicate`).
**Gepensioneerde strategie die nog kan ordenen?** **Nee** onder de huidige configuratie: `ADAPTIVE_TREND_RETIRED=true` blokkeert nieuwe entries op twee plaatsen (`execution_service.py:577`, `adaptive_trend_entry.py:67`; 10/10 retirement-tests PASS, inclusief "retired blokkeert ook als LIVE_ENTRY_ENABLED=true"); alle andere strategieën worden door `ENABLED_STRATEGIES` (allow-list, `env_allowed`) geblokkeerd; MicroFlow-runtime is nu uit. **Geen HIGH-blocker**, maar let op: `ENABLED_STRATEGIES` leeg zou *alle* strategieën toestaan (`env_allowed = not enabled_set or …`).

## 8. Execution readiness / blockers (buiten het envbestand)
| # | blocker | ernst |
|---|---|---|
| 1 | **Account ≈ 0,21 USDT** (min. smoke-trade ≥ 1,51 USDT vrije futures-margin; bij `PLANNER_MIN_LIVE_NOTIONAL_USDT=10` ruim meer) | BLOKKEREND voor elke trade |
| 2 | **Geen goedgekeurde actieve strategie**: adaptive_trend is gepensioneerd, MicroFlow uit. Autonoom handelen kan dus (bewust) niets plaatsen; de enige orderroute is het guarded smoke-harnas uit de MVP-branch | BLOKKEREND voor autonoom handelen |
| 3 | MVP-branch (noodstop, `execution_status_v1`, smoke-harnas) is **niet uitgerold** op de Runner | OPEN |
| 4 | API-sleutel heeft trade-rechten **zonder IP-whitelist** | MIDDEL |
| 5 | Losse credential-kopieën op de Runner: `~/Desktop/.env 2` (geverifieerd: Bitget-sleutels gezet, `EXECUTION_MODE=LIVE`, `EXECUTION_ENABLED=false`); daarnaast bestaan er `.env`-bestanden in `~/Desktop` en `~/Downloads` (inhoud niet geïnspecteerd) en `envoverzetten.rtf`; `DASHBOARD_HOST=0.0.0.0` in `.env.live` (en dashboard_v3 op `*:8501`) | MIDDEL |
| 6 | RunnerMac-schijf 8,0 GiB vrij (live bot + eventuele MicroFlow-data) | OPERATIONEEL |
| 7 | `state/live.stop` en niet-geladen `com.cgc.live`-plist: laat `live.stop` staan tot een bewuste activatie; overweeg de plist te verwijderen (aparte goedkeuring) | AANBEVELING |

## 9. Go-live (niet uitgevoerd) — bewust 2 stappen
1. Fund het account en zet `ACCOUNT_EQUITY_USDT/ACCOUNT_BALANCE_USDT` op het echte saldo.
2. Alleen voor een live proces: `EXECUTION_ENABLED=true`, `EXECUTION_MODE=LIVE` in `.env.live` (gevalideerd), `state/live.stop` verwijderen, `scripts/launch_live.sh` met zijn 4 lagen; en een **goedgekeurde, niet-gepensioneerde strategie** (anders blijven entries geblokkeerd).
Terugdraaien: `cp -p ~/.cgc_env_backups/env.live.20261012T193752Z.bak ~/cgc/bitget_ai_agent_phase7/.env.live` (herstelt de oorspronkelijke LIVE-waarden — niet aanbevolen).

## 10. Status
Configuratie: gevalideerd, veilig (`is_live_execution=False`), Bitget-authenticatie en account/posities/orders-lezen werken, SL/TP en risicobesturing herkend en actief, geen order geplaatst, geen proces verstoord. Resterende blockers (funding, strategie, uitrol, sleutelhardening) liggen buiten het envbestand.

**RUNNERMAC_ENV_LIVE_READY**
