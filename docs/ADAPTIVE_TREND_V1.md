# AdaptiveTrend v1 — frozen live-entry specification and current truth

Status as of 2026-08-27T06:28Z: **LIVE-ENTRY ENABLED, RISK-BLOCKED status
superseded — the weekly freeze cleared naturally on 2026-08-27, ahead of
schedule but within the predicted window (~2026-08-27T02:47:34Z).
`ACCOUNT_RISK_MODE=GREEN`, `weekly_loss_pct≈4.2%`, well under the 7.0%
threshold.** `adaptive_trend_tsmom_v1` is the sole enabled entry strategy
and, as of this update, the account is genuinely entry-eligible: a
qualifying signal on the next 6H boundary can place a real order. See
"CURRENT TRUTH — 2026-08-27" at the end of this file for the latest
snapshot; treat the body of this document as accurate except where that
section says otherwise. MicroFlow (`microflow_scalper_v1`) remains retired
from LIVE eligibility (see below) and `ENABLED_STRATEGIES` no longer
includes it.

## Why AdaptiveTrend, and why MicroFlow stopped

Three internally-designed candidate strategies were tested against real
Bitget tick data with strict chronological train/validation/holdout splits
and pre-registered parameters (2026-08 "STRATEGY REBUILD" phases) and were
all rejected on holdout: liquidity sweep+reclaim (holdout net PF 0.829),
breakout-retest (holdout net PF 0.128), extreme-displacement mean reversion
(disqualified at the pre-registration screen). MicroFlow itself was
separately, conclusively retired after three independent measurements
converged on no actionable edge (PF 0.2544, expectancy -0.1448 USDT/trade,
-19.40 USDT net over 134 economically-confirmed trades). Its retirement is
enforced unconditionally in `risk/risk_manager.py`
(`_microflow_retirement_gate`), independent of `ENABLED_STRATEGIES` —
disabling it there does not re-enable MicroFlow; nothing does, short of a
future deliberate code change with its own evidence bar.

AdaptiveTrend was deliberately chosen to escape this project's own
historical-data bubble: a 6H time-series-momentum + ATR-trailing-stop
framework externally sourced from a published paper
(arXiv:2602.11708), integrated into the existing, already-proven execution
infrastructure rather than backtested/optimized against this project's own
history. **No historical backtest of this exact frozen configuration
exists, and none was run — that omission is deliberate**, to avoid
overfitting a strategy chosen specifically to sidestep this project's prior
overfitting failures.

## Frozen v1 parameters

Source: `strategies/adaptive_trend_tsmom.py`. Not to be retuned against
observed outcomes — see `docs/RISK_REGISTER.md`-style discipline: a change
here requires the same PR/test/evidence bar as any other production code
change, not a parameter sweep.

| Parameter | Value | Provenance |
|---|---|---|
| Strategy id | `adaptive_trend_tsmom_v1` | — |
| Timeframe | 6H | source-faithful |
| Symbol universe | BTCUSDT, ETHUSDT, SOLUSDT | our own choice; not paper-specified |
| `MOM_LOOKBACK` | 24 (candles, = 6 days) | **our own assumption** — paper re-optimizes L monthly via grid search; no fixed paper default exists |
| `MOM_THRESHOLD` | 0.03 | **our own assumption**, same reason |
| `ATR_PERIOD` | 14 | our own choice (standard ATR convention) |
| `ATR_MULT` | 2.5 | **source-faithful** — matches the paper's own stated optimum exactly |
| `RISK_PCT_PER_TRADE` | 0.50% | venue-specific adaptation to this account |
| `MAX_EFFECTIVE_RISK_PCT` | 1.00% | hard ceiling; sizing rejects (`ACCOUNT_TOO_SMALL_FOR_SAFE_ORDER`) rather than exceed it |
| `MAX_LEVERAGE` | 2x (strategy design intent, see note) | see note below — `MAX_LEVERAGE=10x` is the account-wide setting; AdaptiveTrend's own sizing has never come close to needing it |
| `MAX_TOTAL_EXPOSURE_PCT` | 100% (account-wide setting) | shared with the account, not AdaptiveTrend-specific |
| `MAX_OPEN_POSITIONS` (AdaptiveTrend-specific) | **1**, total across all three symbols | `ExecutionService.PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE`, stricter than the account-wide default of 2 |
| Ranking / tie-break | highest `MOM_STRENGTH` wins; ties broken BTC > ETH > SOL | `rank_candidates()`, frozen, never selects on historical profitability |
| Exit model | trailing-stop-only, `take_profits=[]` | deliberate — no fixed TP; `is_trailing_stop_only_strategy()` makes this a first-class, guarded configuration rather than a missing field |

**Note on leverage:** the account-wide `MAX_LEVERAGE=10x` in `.env.live` is
shared with the (retired) MicroFlow config and was never revisited for
AdaptiveTrend specifically. In practice this is currently moot: at the
account's live equity (~$27), the sizing formula is dominated by the
exchange minimum notional (~$5), not by the risk/leverage targets — see
§ Sizing reality below. This is not a defect; it is documented here so a
future agent does not mistake "10x is available" for "10x is used."

## Execution architecture (as deployed)

```
6H candle close (fetch_6h_candles, native Bitget granularity)
  → closed_only / unprocessed_since dedup (adaptive_trend_candles.py)
  → compute_momentum / compute_atr (adaptive_trend_tsmom.py)
  → classify_signal → SignalCandidate
  → rank_candidates (BTC>ETH>SOL tie-break)
  → size_position (equity, stop distance, exchange minimum)
  → shadow log ALWAYS (adaptive_trend_shadow.py — this never changes,
    regardless of live-entry eligibility)
  → [if ADAPTIVE_TREND_LIVE_ENTRY_ENABLED and not weekly_freeze_active]
    build_trade_plan → ExecutionService.execute()
  → HYBRID SAFE MODE gate (execution_service.py) — recognizes
    adaptive_trend_tsmom_v1 ONLY when the flag above is true
  → EntryOrderSubmitter (generic, strategy-agnostic identity/idempotency)
  → initial protection (place_futures_protection_orders, SET semantics)
  → PositionManager._sync_adaptive_trend_position → ATR ratchet every cycle,
    reading the exchange's own reported stop as ground truth (never local
    cache) — restart-safe by construction
```

Every stage above is covered by dedicated tests: entry/protection race
scenarios (`tests/test_adaptive_trend_entry_race_scenarios.py`), hybrid-gate
eligibility (`tests/test_hybrid_gate_adaptive_trend_eligibility.py`),
production launch guard (`tests/test_launch_guard_adaptive_trend_eligibility.py`),
restart reconciliation and trailing idempotency
(`tests/test_adaptive_trend_position_sync.py`). One-position/no-hedge is a
single mechanism (`PER_STRATEGY_MAX_OPEN_POSITIONS_OVERRIDE`), not two
separate gates.

## Sizing reality at current account equity (~$27)

`size_position(equity=27.44, entry=79046.4, stop=74834.9,
exchange_min_notional=5.0)` returns `rounded_notional=5.0` (clamped up from
a raw target of ~$2.58), `effective_risk_pct≈0.97%` (not the intended
0.50% — the exchange minimum dominates, not the risk target),
`required_margin=27.44` (effectively the whole account), and
`leverage≈0.18x` (nowhere near the 10x ceiling). **At this account size, the
exchange minimum notional is the operative constraint, not
`RISK_PCT_PER_TRADE` or `MAX_LEVERAGE`.** This is `MAX_EFFECTIVE_RISK_PCT`
doing its job as designed — 0.97% is still comfortably under the 1.00%
hard cap — not a bug. It does mean the sizing knobs currently have very
little room to move independently of one another; that will change once
equity grows past the point where the raw risk-target notional clears the
exchange minimum on its own.

## Evidence level (2026-08-26)

- **Historical/backtest evidence: NONE**, deliberately (see above).
- **Shadow evidence:** 10 recorded shadow decisions since first evaluation
  (2026-08-24), all `BTCUSDT LONG`, all `ACCOUNT_FREEZE_BLOCKED`. Zero
  actionable signals have ever reached the execution path — the weekly
  freeze has been active continuously since before AdaptiveTrend's own
  first evaluation.
- **Live evidence: ZERO real trades.** `ADAPTIVE_TREND_LIVE_ENTRY_ENABLED`
  and `ENABLED_STRATEGIES` were only made structurally sufficient on
  2026-08-24/25; every signal since has been blocked by the weekly freeze,
  not by the entry-eligibility wiring.
- **Counterfactual reconstruction (n=1, descriptive only):** replaying the
  first blocked signal (2026-08-24T16:00 UTC candle close, entry 79046.4,
  initial stop 74834.9) forward through the real production ATR ratchet
  against actual subsequent Bitget 6H candles: the stop ratcheted twice
  (74834.9 → 75382.92 → 76783.05) and then held flat for six consecutive
  candles through 2026-08-26T16:00 UTC, where the position would still be
  open, essentially flat (~-0.07 USDT unrealized on a $5 notional position,
  after touching ~+0.09 USDT unrealized favorable at the 2026-08-25T04:00
  candle). **This is one unclosed hypothetical trade. It proves the
  mechanism works end-to-end; it proves nothing about edge.**

## Current risk state (2026-08-26T18:23Z)

- `weekly_loss_pct` ≈ 33.6% against a 7.0% freeze threshold (mode=RED),
  down from a peak of 47.99% (both figures verified against the
  authoritative `RiskManager._weekly_realized_pnl()` path and cross-checked
  against a full rolling-7-day dataset reconstruction — real, MicroFlow-era
  losses, not a calculation defect).
- The loss predates AdaptiveTrend and is entirely attributable to
  MicroFlow's historical live trades (now retired). It will clear naturally
  as those trades age out of the rolling 7-day window; no reset, bypass, or
  threshold change has been made or requested to be made.
- Freeze applies independently of `ADAPTIVE_TREND_LIVE_ENTRY_ENABLED` —
  both gates must pass, exactly as designed, and this has been
  demonstrated live (the BTCUSDT shadow signals were correctly blocked).

## Explicitly NOT proven yet

- No real AdaptiveTrend entry has ever occurred.
- No initial-protection placement has been exercised outside test mocks.
- No ATR-trailing update has been applied to a real exchange position.
- No fee/slippage figure is measured from real fills — only the
  configured assumption (`PLANNER_ESTIMATED_ROUNDTRIP_FEE_BPS=12.0`) is
  known.
- Long/short asymmetry, symbol-specific behavior, and threshold/lookback
  alternatives are all open hypotheses with zero supporting evidence
  either way — not rejected, not confirmed.

## CURRENT TRUTH — 2026-08-26

- Deployed SHA: `63a0d5713bc3ebc860ec6a7eb266c484721de4d3`.
- `adaptive_trend_tsmom_v1` is the only entry-enabled strategy;
  `microflow_scalper_v1` is retired and unconditionally blocked at the
  RiskManager layer regardless of `ENABLED_STRATEGIES`.
- Infrastructure (execution/protection/trailing/restart/reconciliation/
  one-position/no-hedge/launch guards) is proven by tests and one live
  ~10-hour unattended run with zero faults. **Economic edge is not proven
  in any direction — not positive, not negative.** Treat any claim to the
  contrary in older docs (`docs/RISK_REGISTER.md`, `ROADMAP.md`) as
  superseded by this file for anything strategy-related dated after
  2026-07-28.
- Do not resurrect MicroFlow. Do not backtest-optimize AdaptiveTrend's
  frozen parameters against this project's own historical data — that is
  the exact failure mode it was chosen to avoid.

## CURRENT TRUTH — 2026-08-27 (reconciliation/finetuning audit)

Full reconciliation + finetuning audit performed against deployed code,
runtime, exchange truth, and this document. **No proven defect found.
Zero code/config changes applied this pass** — everything provable was
already fixed in the PRs referenced above; the audit itself is the
evidence.

- **Weekly freeze cleared naturally 2026-08-27**, on schedule.
  `ACCOUNT_RISK_MODE=GREEN`, `weekly_loss_pct≈4.2%`. The account is
  genuinely entry-eligible now — a qualifying signal can place a real
  order. None has yet (last evaluated: 2026-08-27T04:00 UTC candle,
  `winner=None`, no symbol met threshold).
- **Signal-selectivity observation (Bucket C — do not act on this
  alone):** across every evaluable candle in the ~2-week history the
  exchange makes available (19 candles × 3 symbols = 57), `MOM_THRESHOLD
  = 0.03` was met by **100%** of them — zero near-misses, zero clear
  rejections. The threshold has provided no observed selectivity in this
  window. This may reflect a genuinely atypical, synchronized bull regime
  across BTC/ETH/SOL rather than a threshold defect — the sample is too
  short and too regime-specific to conclude either way. Pre-registered
  for a future test once a wider/more varied window exists; not acted on.
- **Symbol-correlation observation (Bucket C):** BTC, ETH and SOL moved in
  near-identical momentum patterns throughout the observed window
  (correlated declining-momentum trajectories over the same candles). The
  3-symbol universe may currently function as one correlated exposure
  routed through a tie-break, not three independent opportunities. Not
  actionable without a longer, more varied sample — noted as a hypothesis
  only.
- **Sizing/exchange-minimum interaction (already documented above, this
  pass's fresh number):** at $27.44 equity, a minimum-size AdaptiveTrend
  trade carries `effective_risk_usdt≈$0.266` (0.97% of equity, still
  under the 1.00% hard cap) and `required_margin≈$27.44` (effectively the
  whole account) at `leverage≈0.18x` — nowhere near the 10x ceiling.
- **Fee/breakeven economics (new this pass):** at the configured
  `PLANNER_ESTIMATED_ROUNDTRIP_FEE_BPS=12.0` assumption (not yet
  empirically confirmed from a real fill), round-trip cost on a $5
  minimum-notional trade is ~$0.006 — about 2.3% of the 1R risk budget
  and ~44x smaller than the ~5.3% stop distance observed on the reference
  BTCUSDT counterfactual. **Fee drag is immaterial to this strategy's
  economics at its current stop-distance/holding-period profile.**
  Breakeven win-rate/avg-R hurdles (fees included, negligible effect):
  40% win rate needs ~1.5R average winner, 45% needs ~1.22R, 50% needs
  1.0R, 55% needs ~0.82R.
- **Out-of-band finding, not part of this audit:** PRs #89 and #90
  (`dashboard_v4`, merged 2026-08-26, author `0pp3rB44s`) added
  AdaptiveTrend-aware dashboard pages and a single-verdict eligibility
  panel. Dashboard-only — no execution/risk/strategy code touched, no
  control endpoints. Not reviewed or deployed by this audit; flagged here
  so a future reader knows it exists and was authored elsewhere.
- Live engine (PID 913 at time of this audit) ran continuously
  throughout this entire audit, untouched — this document and its
  companion reconciliation report were produced read-only.
