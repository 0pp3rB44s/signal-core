# BEDIENING — wat is automatisch, wat is handmatig (2026-07-13)

Status: bot in OBSERVE-MODE (EXECUTION_ENABLED=false, PATCH-073).
Scant en logt alles, kan geen orders plaatsen.

---

## AUTOMATISCH (zolang het bot-proces draait)

| Proces | Cadans |
|---|---|
| Scannen van 40 symbolen (dynamische watchlist op expectancy→volume→move) | elke ~2-3 min |
| Plannen genereren + funnel-logging (trade_plans.csv, strategy_performance.csv) | per cyclus |
| Forward-paper-data verzamelen (EXECUTABLE plannen met entry/SL/TP) | continu |
| Positie-monitoring incl. wick-aware MFE, BE-locks, protectie-retries | per cyclus |
| Exchange-truth backfill bij closes (alle 3 close-paden) | bij elke close |
| Leerketen-refresh: strategy_expectancy, daily_learning_report, learning.json | elk artefact >24h oud → keten draait; kill-switch fail-closed bij >48h |
| Gates (HARD-PAUSE, requalify, richting-laag) lezen verse rapporten | per plan |
| Dashboard (als gestart) | http://127.0.0.1:8501 |

**LET OP — niet automatisch:** de bot herstart NIET zichzelf. De watchdog
meldt alleen. Na een reboot/crash moet je zelf starten (zie hieronder).

## HANDMATIG — dagelijks/wekelijks ritme (PLAN_VOORUIT.md)

```bash
# dagelijks (10 min) — jouw pool-kaart voor handmatige A+ setups
python3 scripts/pool_kaart.py                  # of: pool_kaart.py SOLUSDT XRPUSDT

# na elke handmatige trade — journal + kwaliteitsvelden (ENTRY_PLAYBOOK.md)
python3 scripts/journal.py add --symbol SOLUSDT --dir LONG \
  --entry 76.10 --stop 75.35 --exit 78.20 \
  --pool-tf 4H --touches 5 --sweep wick --bevestiging ja --rr-plan 2.8 --sessie londen
python3 scripts/journal.py stats               # expectancy + fase-2 oordeel + finetune-lus

# wekelijks (tussenstand, niet beslissen) / maandelijks (formeel oordeel)
python3 scripts/maandmeting.py                 # A: hand | B: bot-forward | C: regime
```

## HANDMATIG — beheer

```bash
# bot starten (na reboot/crash — gebeurt NIET vanzelf!)
bash scripts/start_bot.sh <reden>
bash scripts/start_dashboard.sh                # dashboard op 127.0.0.1:8501

# gezondheidscheck
kill -0 $(cat state/bot.pid) && echo RUNNING; grep -cE ' ERROR ' logs/bot.out

# alles stoppen
bash scripts/stop_all.sh

# nieuwe Bitget-export (position history CSV) inlezen in de leerdata
python3 scripts/import_exchange_export.py <pad-naar-export.csv>

# leerrapport handmatig verversen (gaat ook vanzelf elke 24h)
python3 scripts/run_backtest.py --validation-only
```

## DE GROTE SCHAKELAAR (pas na fase-2 groen bewijs)

```bash
# weer live:   .env -> EXECUTION_ENABLED=true   + bash scripts/start_bot.sh live_hervat
# weer observe:.env -> EXECUTION_ENABLED=false  + herstart
```
Regel (PLAN_VOORUIT fase 2): pas bespreken na ≥30 afgeronde forward-paper-
trades MET positieve expectancy — en dan eerst een kleine probe, nooit vol.

## PROMPTS VOOR CLAUDE (aansturing via chat)

- **Wekelijks:** "draai de maandmeting en interpreteer de cijfers"
- **Maandelijks:** "formele maandmeting: vel het fase-2 oordeel volgens PLAN_VOORUIT"
- "check of de bot gezond draait" (proces, errors, funnel, versheid rapporten)
- "analyseer mijn journal en scherp de entry-checklist aan" (finetune-lus)
- "hier is een verse Bitget-export, importeer en analyseer"
- "de regime-monitor zegt trending — zet de trend-following her-test op (paper)"
- "zet de bot weer live" → Claude checkt eerst de fase-2 regels en weigert
  zonder groen bewijs (zo is het afgesproken en in memory vastgelegd)

## WAAR ALLES STAAT

- Plan & beslisregels: docs/PLAN_VOORUIT.md
- Entry-methode: docs/ENTRY_PLAYBOOK.md
- Patch-historie: docs/PATCHES.md | verhaal: docs/JOURNAL.md
- Handmatig journal: data_store/manual_journal.csv
- Forward-paper-bron: logs/trade_plans.csv (EXECUTABLE sinds 2026-07-13T20:00)
