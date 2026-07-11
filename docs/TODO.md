# CGCAgent TODO

## Active backlog (data-backed; strategie-audit 2026-07-10, besluiten door Bryon)

Portfoliobesluiten 2026-07-10: (1) low_vol_reclaim echte pauze via HARD-PAUSE op
leerrapport-status; (2) momentum_breakout houden; (3) breakdown/continuation op
probe; (4) liquidity_sweep close-pos gate gerepareerd; (5) geen nieuwe strategieën
tot de basis winstgevend is.

- [ ] Verlaag fee-drag: analyseer maker-entry fill-rate in logs (MAKER_ENTRY_* regels) en verhoog maker_entry_wait_seconds of offset zodat meer entries maker-fee krijgen in plaats van taker.
- [ ] TP-geometrie low_vol_reclaim: mediane MFE is 0.39% terwijl TP1 op 1.30R (0.39-1.1%) ligt; TP1-hitrate 9.6%. Herontwerp entries of TP-profiel op de MFE-verdeling voordat de strategie van HARD-PAUSE af mag. Vereist menselijke goedkeuring.
- [ ] Onderzoek SHORT-bias: 92 shorts (-1.81 USDT) vs 53 longs (+0.56 USDT) sinds 1 juli. Analyseer of shorts een strengere confluence-eis nodig hebben (voorbeeld: continuation.py heeft al SHORT_MIN_* drempels).
- [ ] momentum_breakout overige poorten (na 2026-07-11 close_pos-fix): exhaustion-block (expansion_exhaustion_score>=85, 40x/nacht) en volume-block (ratio<1.20 mtf, 35x/nacht) blijven staan. Dit zijn echte kwaliteitsfilters — NIET losdraaien zonder forward-return data die bewijst dat de geblokkeerde setups winstgevend zijn. Meet eerst via trade_stats na een paar dagen live breakout-trades.
- [ ] Monitor liquidity_sweep_reversal na de close-pos reparatie: eerste 10 trades reviewen voordat de strategie meer ruimte krijgt.
- [ ] Onderzoek overtrading: trades korter dan 1 uur verliezen (-3.67 USDT gecombineerd), trades langer dan 1 uur winnen (+2.42 USDT). Overweeg minimale houd-tijd of minder scan-entries per dag.
- [ ] Prevent completed tasks from being suggested again.

## Done

- [x] Context budget enforcement.
- [x] Pending patch cleanup after successful apply.
- [x] Post-apply tests after successful apply.
- [x] Link improvement planner to docs/INDEX.md and docs/TODO.md.
- [x] Add model capability registry for local Ollama models (model_router: fast/strong split, env-overridable).
- [x] Improve rollback to track patched files from pending diff (orchestrator rolls back only files from the applied patch).
