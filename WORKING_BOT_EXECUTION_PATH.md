# WORKING_BOT_EXECUTION_PATH

Concise map of the forward-paper trading lifecycle and the code that implements it.
Written 2026-07-25. Scope: what actually runs, and where it broke.

---

## 1. Headline

The lifecycle code was substantially complete. It never produced a single trade
because **one serialization bug aborted every paper write**.

`forward_paper_events.jsonl` was 0 bytes from 2026-07-14 to 2026-07-25 while
`funnel_events.jsonl` grew to 75 MB. The funnel proves the pipeline reached the
final stage 147 times:

```
EXECUTABLE_DECISION  PASS = 147     (2026-07-18 .. 2026-07-24)
FORWARD_PAPER_LINK   PASS =   0
```

All 147 died in the same place, 297 logged occurrences:

```
FORWARD_PAPER_FAILED_CLOSED | error=Object of type ContractSpec is not JSON serializable
  app/runner.py            _scan_cycle          -> forward_paper.process
  forward_paper/service.py open_trade           -> self._append
  forward_paper/store.py   content_hash         -> canonical_json -> json.dumps  # raises
```

`market_features/engine.py:263` puts live dataclasses (`ContractSpec`,
`LiveMarketContext`) into `snapshot.context`. `open_trade` copied that dict
straight into the event payload. `json.dumps` cannot encode a dataclass, so
`content_hash` raised before anything was written. The runner caught it as
"fail closed", logged, and continued — so the bot looked healthy while producing
no trades.

---

## 2. Execution path

Entry point: `scripts/start_bot.sh` → `python -m app.main` → [app/main.py:9](app/main.py:9)
→ `StartupRunner.run` [app/runner.py:391](app/runner.py:391) → `_scan_cycle`
[app/runner.py:548](app/runner.py:548), every `SCAN_INTERVAL_SEC`.

| # | Stage | Implementation | Works | Evidence |
|---|-------|----------------|-------|----------|
| 1 | Market data | `MarketFetcher.fetch_snapshot` [data/market_fetcher.py:234](data/market_fetcher.py:234) | yes | live SOLUSDT close 73.931 fetched in demo |
| 2 | Snapshot | `MarketFetcher.build_market_snapshot` [data/market_fetcher.py:358](data/market_fetcher.py:358) → `build_snapshot_from_inputs` [:26](data/market_fetcher.py:26) | yes | production factory, used by new tests |
| 3 | Features | `build_market_snapshot` [market_features/engine.py:190](market_features/engine.py:190) | yes | **source of the defect** — line 263 embeds dataclasses in `context` |
| 4 | Detector | `detect_low_vol_reclaim` [app/runner.py:727](app/runner.py:727), `MomentumBreakoutStrategy`, `detect_continuation`, `LiquiditySweepStrategy` | yes | 935 DETECTOR_DECISION PASS |
| 5 | Selector | `select_best_candidate` [app/runner.py:793](app/runner.py:793) | yes | 566 SELECTOR PASS |
| 6 | Scoring | `StrategyScorer.score` [app/runner.py:1071](app/runner.py:1071) | yes | 586 SCORING PASS |
| 7 | Risk | `RiskManager.evaluate` [app/runner.py:1088](app/runner.py:1088) | yes | 312 PASS / 295 BLOCKED |
| 8 | Plan | `TradePlanner.build` [app/runner.py:1097](app/runner.py:1097) | yes | 147 EXECUTABLE |
| 9 | Paper order + fill | `ForwardPaperService.open_trade` [forward_paper/service.py:98](forward_paper/service.py:98) | **was broken → fixed** | 0 → trades now open |
| 10 | Persist | `ForwardPaperEventStore.append` [forward_paper/store.py:127](forward_paper/store.py:127) | yes | hash-chained JSONL |
| 11 | Manage / SL / TP | `_update_trade` [forward_paper/service.py:214](forward_paper/service.py:214) | yes, **1 accounting bug fixed** | see §4 |
| 12 | Close | `_close` [forward_paper/service.py:310](forward_paper/service.py:310) | yes | TRADE_CLOSED written |
| 13 | Restore | `_reconstruct_trade_states` [forward_paper/service.py:323](forward_paper/service.py:323) | yes | crash-restart restored 1 trade, no duplicate |
| 14 | Outcome + report | `ForwardPaperReconstructor.reconstruct` [forward_paper/store.py:241](forward_paper/store.py:241) | yes | outcomes CSV + data-quality JSON |

Execution is bypassed entirely in forward-paper mode: `execution_service` and
`position_manager` are `None` when `forward_paper_only` is set
([app/runner.py:305](app/runner.py:305), [:307](app/runner.py:307)).

---

## 3. Repairs — defect 1: ContractSpec serialization (P0, was blocking everything)

**Broken:** `canonical_json` / `content_hash` raised `TypeError` on any payload
containing a dataclass. Every `TRADE_OPENED` aborted.

**Changed:**
- `jsonable()` added in [forward_paper/store.py](forward_paper/store.py) — recursively converts
  dataclasses, pydantic models, sets, tuples and non-finite floats into pure JSON.
  The stored form is real JSON, so re-reading and re-hashing reproduces it exactly
  and the hash chain stays valid.
- `canonical_json` hardened with `default=str, allow_nan=False`.
- Applied in `ForwardPaperService._append` (before deriving `event_id` /
  `semantic_key`, so identity and payload agree) and in `store.append`.
- `_market_context()` drops the redundant `live` blob: every field of it is already
  a sibling key, and it nested the full raw orderbook ladder. Opened-trade events
  are ~8.6 KB instead of carrying hundreds of price levels.

**Proof:** `tests/test_forward_paper_production_lifecycle.py`. Reverting the fix
reproduces the exact production error:
`TypeError: Object of type ContractSpec is not JSON serializable`.

---

## 4. Repairs — defect 2: break-even stop fabricated PnL

Found by the live demonstration, not by tests.

**Broken:** `_fee_break_even` moved the stop to `fill * (1 + buffer)` without
checking the market. When the take-profit was narrower than
`BREAK_EVEN_FEE_BUFFER_PCT` (0.12%), the new stop landed *above* the current
price. `stop_touched` fired on the same candle and the trade closed at the stop
price — a price the market never traded.

Observed in `data_store/smoke/run1_prefix_bug/`:

```
stop moved to 74.004931   while candle high was 73.947
TRADE_CLOSED exit_price=74.004931  exit_reason=STOP_LOSS  gross_pnl=+0.025
```

A stop-loss booking a **profit**, filled 0.08% above the traded high. Both run-1
trades did it.

**Changed:** `_protective_stop()` in [forward_paper/service.py](forward_paper/service.py) returns the
break-even stop only when it is still on the protective side of the mark
(below for LONG, above for SHORT); otherwise the existing stop is left alone.
Applied to both the TP1 and profit-lock break-even moves.

**Proof:** `test_break_even_stop_never_fills_beyond_the_traded_range` asserts no
`STOP_UPDATED` or exit price exceeds the traded high, and no stop-loss books a
profit. It fails without the guard.

> Production targets are normally far wider than 0.12%, so this rarely triggered
> in live config — but it silently corrupts any outcome where it does.

---

## 5. Repairs — defect 3: tests read live mutable state

**Broken:** `risk/risk_manager.py:14-16` defines `BASE_PATH`, `REPORTS_PATH` and
`AGENT_DECISIONS_PATH` as **absolute** module constants. `monkeypatch.chdir(tmp_path)`
in `tests/test_public_candidate_pipeline.py` therefore had no effect: the risk gate
read the real repo's `reports/backtests/strategy_expectancy.json`. With live
expectancy data (`momentum_breakout trades=45 exp=-0.016`) the gate blocked every
plan and both end-to-end pipeline tests failed — the repo's only runner-level
lifecycle tests, red for reasons unrelated to the code under test.

**Changed:** test-only. `_runner` now patches the three module constants at
`tmp_path`. No production path resolution was altered. Both tests pass and no
longer depend on whatever the live bot last recorded.

Also fixed: `tests/test_legacy_inventory.py` walked `.claude/worktrees/`, so a
stale git worktree failed the "no runtime reference" assertion. Dot-directories
are now skipped.

---

## 6. Smoke strategy (non-production)

[strategies/forward_paper_smoke.py](strategies/forward_paper_smoke.py) — deterministic engineering harness to
exercise the lifecycle. **Not an edge claim.**

Safety, enforced by `Settings.enforce_forward_paper_only` [app/config.py](app/config.py):

- default **off** (`FORWARD_PAPER_SMOKE_STRATEGY_ENABLED=false`);
- force-disabled unless `forward_paper_only` **and** `forward_paper_enabled` **and
  not** `execution_enabled` — verified for 4 unsafe combinations in
  `tests/test_forward_paper_smoke_safety.py`;
- emits only a `TradePlan`; in forward-paper mode `execution_service is None`, so
  no order-placing path exists;
- every record tagged `strategy="SMOKE_TEST_NON_PRODUCTION"` plus a
  `NON_PRODUCTION_SMOKE_STRATEGY` note;
- entry uses the real live close, so fills are realistic;
- identity derives from the closed-candle timestamp, so replays dedupe rather than
  double-open;
- writes to separate paths (`FORWARD_PAPER_*_PATH`, new settings) so research data
  stays clean.

---

## 7. Runtime demonstration (deliverable 4)

Live SOLUSDT forward-paper run, smoke strategy, 1m primary / 5m confirmation.
Independently re-verified: the hash chain was re-read through
`ForwardPaperEventStore.read_events()` (which validates sequence, `previous_hash`
and per-event checksums) and the accounting was recomputed from raw events.

`data_store/smoke/forward_paper_events.jsonl` — 80 events, chain valid,
0 duplicate event ids, 0 duplicate semantic transitions, 0 terminal conflicts.

Completed trade `paper_c554b2bc10781566d1fb`:

| Field | Value |
|-------|-------|
| symbol / strategy | SOLUSDT / `SMOKE_TEST_NON_PRODUCTION` |
| entry → exit | 2026-07-25T09:56:00Z → 2026-07-25T10:19:00Z (1380 s) |
| simulated fill | 73.972 |
| size / notional | 0.337965717 SOL / exactly 25.000000 USDT |
| exit (TP1) | 73.9867944 |
| gross PnL | +0.005000000 = (73.9867944 − 73.972) × 0.337965717 ✓ |
| entry fee | 0.015000000 = 25.000000 × 12 bps ÷ 2 ✓ |
| exit fee | 0.015003000 ✓ |
| **net PnL** | **−0.025003000** = 0.005 − 0.030003 ✓ |
| exit ≤ traded high | yes — no fabricated fill |

Fee drag (0.030) exceeds the deliberately tiny 0.02 % smoke target (0.005), so the
net is negative by construction. This is an engineering result, **not** an edge
measurement. Lifecycle stages observed: `TRADE_OPENED → MARK_DECISION ×60 →
MFE/MAE_UPDATE → TP_TOUCH → PARTIAL_EXIT → EXIT_REASON_TRANSITION → TRADE_CLOSED`
plus an outcome row and a data-quality JSON.

## 8. Restart recovery (deliverable 5)

`data_store/smoke/run4/` — the bot was started, a position opened, the process was
killed (SIGTERM, pid 82653) and a fresh process started (pid 84740) against the
same event log.

| Check | Result |
|-------|--------|
| events before restart | 3 |
| events after restart | 14 |
| `TRADE_OPENED` before → after | 1 → **1** (restored exactly once) |
| trade_id identical across restart | yes (`paper_8dba688ffd99caa2c031`) |
| management continued | 7 `MARK_DECISION`, 2 MFE, 4 MAE after restart |
| duplicate orders / fills / positions | **0 / 0 / 0** |
| chain valid after restart | yes |
| `FORWARD_PAPER_FAILED_CLOSED` | 0 |
| private/exchange-write calls in logs | 0 |

The smoke strategy re-emitted a plan for the same candle after the restart; the
store deduped it on the deterministic `candidate_id` instead of double-opening.

**Limitation, stated plainly:** this position did **not** close during the run. Its
0.03 % target (73.96718) was never touched — the high water mark reached 73.962 —
so the trade is correctly still open. A restart followed by a *normal close* is
proven deterministically by
`test_full_lifecycle_survives_restart_and_records_accounted_outcome`, and a live
close is proven by the §7 trade, but not both in one continuous live run.

---

## 9. What is still open (backlog, not lifecycle-blocking)

1. `regime` records as `UNKNOWN` for smoke trades — `_regime` reads
   `snapshot.alignment`, which was `mixed`. Cosmetic for analytics.
2. `spread_bps` is parsed out of free-text notes with a regex
   ([forward_paper/service.py:44](forward_paper/service.py:44)) rather than read from `context["spread_bps"]`,
   which is present and typed.
3. `market_context` still duplicates `breakout` under three keys
   (`breakout`/`pressure`/`structure`) — `market_features/engine.py:263`.
4. The risk manager's absolute-path constants remain absolute in production. Tests
   now patch them, but a settings-driven path would be cleaner.
5. Slippage is modelled as `0.0` on exits ([forward_paper/service.py](forward_paper/service.py) `_partial`/`_close`);
   only entry slippage is real. Exit fills are exact at stop/target price.
6. Phase 7 reliability exists but is **not currently running**.
   [scripts/run_supervised.sh](scripts/run_supervised.sh) already provides auto-restart with exponential
   backoff (15s→300s), fail-closed after 5 rapid crashes, a `state/supervisor.stop`
   flag and duplicate prevention via `start_bot.sh` (`pkill app.main` + `state/bot.pid`).
   Health checks exist in [scripts/check_forward_paper.sh](scripts/check_forward_paper.sh) (now exits non-zero on
   `FORWARD_PAPER_FAILED_CLOSED` and on executable-plans-but-no-paper-output),
   [scripts/healthcheck.sh](scripts/healthcheck.sh) and [scripts/daily_ops_check.sh](scripts/daily_ops_check.sh). What is missing is
   only that nothing launches the supervisor: the `com.cgc.tradingbot` launchd job
   is loaded but not running, and the script is designed for `tmux`. Starting it is
   an owner decision, since the bot is in owner-chosen observe mode
   (`EXECUTION_ENABLED=false` since 2026-07-13).
