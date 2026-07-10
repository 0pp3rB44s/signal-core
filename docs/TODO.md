# CGCAgent TODO

## Active backlog (data-backed, from trade analysis 2026-07-10)

Bron: logs/trade_dataset_v2.csv, 145 closed trades sinds 1 juli. Netto -1.25 USDT,
bruto vóór fees +2.48 USDT, fees 3.74 USDT. Fees zijn de grootste verliespost.

- [ ] Verlaag fee-drag: analyseer maker-entry fill-rate in logs (MAKER_ENTRY_* regels) en verhoog maker_entry_wait_seconds of offset zodat meer entries maker-fee krijgen in plaats van taker.
- [ ] Onderzoek low_vol_reclaim: 106 van 145 trades sinds 1 juli, winrate 24.5%, pnl -2.90 USDT. Voeg een strengere entry-filter toe of verlaag de trade-frequentie van deze strategie. Vereist menselijke goedkeuring voor livegang.
- [ ] Onderzoek SHORT-bias: 92 shorts (-1.81 USDT) vs 53 longs (+0.56 USDT) sinds 1 juli. Analyseer of shorts een strengere confluence-eis nodig hebben.
- [ ] Onderzoek overtrading: trades korter dan 1 uur verliezen (-3.67 USDT gecombineerd), trades langer dan 1 uur winnen (+2.42 USDT). Overweeg minimale houd-tijd of minder scan-entries per dag.
- [ ] Prevent completed tasks from being suggested again.

## Done

- [x] Context budget enforcement.
- [x] Pending patch cleanup after successful apply.
- [x] Post-apply tests after successful apply.
- [x] Link improvement planner to docs/INDEX.md and docs/TODO.md.
- [x] Add model capability registry for local Ollama models (model_router: fast/strong split, env-overridable).
- [x] Improve rollback to track patched files from pending diff (orchestrator rolls back only files from the applied patch).
