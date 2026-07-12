# CGCAgent TODO

## Active backlog (data-backed; strategie-audit 2026-07-10, besluiten door Bryon)

Portfoliobesluiten 2026-07-10: (1) low_vol_reclaim echte pauze via HARD-PAUSE op
leerrapport-status; (2) momentum_breakout houden; (3) breakdown/continuation op
probe; (4) liquidity_sweep close-pos gate gerepareerd; (5) geen nieuwe strategieën
tot de basis winstgevend is.

- [x] ~~EIGENAAR-BESLUIT: HARD-PAUSE-beleid~~ BESLOTEN 2026-07-12: herkwalificatie-modus (optie b) — PATCH-065. Gepauzeerde strategie met wr>=40% mag op probe-size max 15 post-fix-cohort-trades doen; vanaf n>=10 bepaalt het cohort de status. Reclaim (wr 36%) blijft hard dicht.
- [ ] Meet post-fix cohort (trades sinds 2026-07-11T14:30): eerste 5 = FET +0.052 (TP-hit!), ENA +0.010, BNB -0.002, AAVE -0.110, DOGE -0.125. Na ~15-20 trades opnieuw beoordelen.
- [ ] strategy_performance.csv logt geen EXECUTABLE-plan-rijen voor trend_continuation (AAVE/FET executies zonder PLAN-rij) -> funnel-metingen ondertellen. Vind de logging-plek en dek alle strategieën.
- [ ] Leerketen-cadans: daily_learning_report.json was >26h oud (defensieve kill-switch-input!), learning.json 10 dagen. De keten hangt aan strategy_expectancy-leeftijd (24h) en morning_audit draait met check=False (stille fouten). Eigen versheids-checks per artefact + luide failure.
- [ ] bot.out ongerotecteerd (72MB in één nacht; startscript trunceert bij start -> historie weg). Rotatie of gzip-archief bij start (handmatig gedaan op 2026-07-12: bot.out.pre_audit_restart_20260712.gz).
- [x] ~~Verlaag fee-drag: analyseer maker-entry fill-rate~~ AFGEROND 2026-07-12: 0/7 maker-fills zelfs bij 30s venster. Post-only vult niet op momentum-entries (de prijs loopt per definitie weg). Extended-wait uit (PATCH-064); hybride 4s + market-fallback blijft.
- [ ] TP-geometrie low_vol_reclaim: mediane MFE is 0.39% terwijl TP1 op 1.30R (0.39-1.1%) ligt; TP1-hitrate 9.6%. Herontwerp entries of TP-profiel op de MFE-verdeling voordat de strategie van HARD-PAUSE af mag. Vereist menselijke goedkeuring.
- [x] ~~SHORT-bias~~ HERZIEN NA BOT-ONLY ATTRIBUTIE (2026-07-13): de export-asymmetrie (LONG +3.45 vs SHORT -3.05) bleek van 5 handmatige trades (+6.82). Bot-only: LONG -0.034/trade vs SHORT -0.031/trade — geen richting-probleem. Opgelost als LERENDE laag (PATCH-068): richting-expectancy in strategy_expectancy.json + slapende asymmetrie-gate in risk_manager (probe bij gap >= 0.04/trade, hard-pause na vol requalify-cohort). Zie docs/EXCHANGE_TRUTH_ANALYSIS_20260713.md correctie-sectie.
- [ ] momentum_breakout overige poorten (na 2026-07-11 close_pos-fix): exhaustion-block (expansion_exhaustion_score>=85, 40x/nacht) en volume-block (ratio<1.20 mtf, 35x/nacht) blijven staan. Dit zijn echte kwaliteitsfilters — NIET losdraaien zonder forward-return data die bewijst dat de geblokkeerde setups winstgevend zijn. Meet eerst via trade_stats na een paar dagen live breakout-trades.
- [ ] Monitor liquidity_sweep_reversal na de close-pos reparatie: eerste 10 trades reviewen voordat de strategie meer ruimte krijgt. (Status 2026-07-11: strategie vuurt nog 0 kandidaten — dominante reject `no_sweep_reclaim`; zeldzaam patroon, geen bug. Nog 0 afgesloten trades = geen track record.)
- [ ] Onderzoek overtrading: trades korter dan 1 uur verliezen (-3.67 USDT gecombineerd), trades langer dan 1 uur winnen (+2.42 USDT). Overweeg minimale houd-tijd of minder scan-entries per dag. (Herbevestigd 2026-07-11: <30m avg +$0.02 fee-scratch vs >4h avg +$0.63.)
- [ ] NIEUW (2026-07-11): fee-margin-filter — weiger trades waarvan TP1 de roundtrip-fee niet met ruime marge cleart, om de <30m fee-scratch churn te elimineren. Uitwerken MÉT backtest-verificatie, niet blind live zetten.
- [ ] NIEUW (2026-07-11): journal-drift root-cause — live_trade_journal.json wordt niet gesloten bij een exchange-sync-close (blijft OPEN). Wire de exchange-sync-close aan LiveTradeJournal.log_close + startup-reconcile. (Loopt als aparte sessie/taak; analytics-only, geen trading-impact.)
- [ ] Prevent completed tasks from being suggested again.

## Done

- [x] Context budget enforcement.
- [x] Pending patch cleanup after successful apply.
- [x] Post-apply tests after successful apply.
- [x] Link improvement planner to docs/INDEX.md and docs/TODO.md.
- [x] Add model capability registry for local Ollama models (model_router: fast/strong split, env-overridable).
- [x] Improve rollback to track patched files from pending diff (orchestrator rolls back only files from the applied patch).
- [x] (2026-07-11) momentum_breakout close_pos false-positive gefixt (blokkeerde alle breakout-trades) — PATCH-052.
- [x] (2026-07-11) Break-even-geometrie coherent: fee-adjusted BE-floor, ATR-stop-cap, BE op echte fill — PATCH-057/058/059/061.
- [x] (2026-07-11) Entry chase-limit (skip >15bps weggelopen breakout) — PATCH-062.
- [x] (2026-07-11) live_trade_journal.json gereconcilieerd (28 stale OPEN → CLOSED tegen exchange truth) — PATCH-063.
