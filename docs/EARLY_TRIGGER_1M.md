# 1-Minute Early-Trigger Layer

## Problem

`momentum_breakout` evaluates the last **closed 15m candle** (`candles[-1]`). A
breakout that starts at minute 2 of a 15m candle is not detected until that
candle closes — up to **13 minutes late**. By then the move is often already
extended, and the momentum-quality gate correctly rejects it as "too extended /
late." The bot is structurally too slow for fast breakouts, exactly the moves a
human hand also misses.

## Design principle (how commercial systems do it)

Separate four timeframes that are currently collapsed into one:

    context 1H  ->  setup 15m  ->  confirm 5m  ->  trigger 1m

- **Context / trend TF (1H)** — already in the snapshot (`confirmation`).
- **Setup TF (15m)** — already the `primary`.
- **Confirm TF (5m)** — the last closed 5m candle must push in the breakout
  direction and sit on the right side of its EMA20. A lone 1m spike that the 5m
  does not corroborate is rejected.
- **Trigger TF (1m)** — detects the breakout *as it forms*, ~1 minute after it
  starts instead of up to 15.

The 1m and 5m are **signal** timeframes, never hold timeframes. We enter earlier
and at a better price, but we still **hold for the 15m/1H move** with a
**structural 15m stop**. This is the opposite of scalping the 1m — the bot's own
data shows sub-1h scalps lose to fees (−3.25 / −3.49 USDT), while the 1–4h bucket
is the only non-loser. Detecting faster improves the *entry* of the trades that
already work; it does not add a new fee-heavy scalp.

The 1m and 5m candles are **reused from the multi_tf_cache**, which the scan loop
already refreshes every cycle (`market_data_service.refresh_many` fetches
1m/5m/15m/1h/4h). So the layer adds **zero extra API calls**.

## False-breakout defence

Low-timeframe breakouts whipsaw constantly. The trigger only fires when, on the
1m close:

1. **Trend alignment** — the 15m/1H `alignment` agrees with the breakout
   direction (no counter-trend 1m breakouts).
2. **Level break on close** — the 1m candle *closes* beyond the prior 1m range
   high/low (not just a wick).
3. **Volume expansion** — 1m volume ratio ≥ `min_volume_ratio` (default 2.0).
4. **Body strength** — candle body ≥ `min_body_pct` of range (a real push, not a
   doji/wick).
5. **Freshness** — displacement past the level ≤ `max_displacement_pct` (default
   0.5%), so we catch it fresh, not after it has already run.
6. **5m corroboration** — the last closed 5m candle pushes in the same direction
   and is on the right side of its EMA20 (`early_trigger_5m_confirm_enabled`,
   default on; fail-open if 5m data is missing).

Order-flow / tick confirmation (the gold standard) is out of scope: the bot does
not ingest tick data. This is an OHLCV-1m layer, which has a known ceiling — it
is a latency fix, not an HFT edge.

## Integration (why it is safe)

- **Feature-flagged** (`EARLY_TRIGGER_1M_ENABLED`, default ON since 2026-07-11;
  set to `false` in `.env` to disable). When off, `detect()` returns immediately,
  no cache read happens, and the scan is unchanged.
- **Probe size until proven.** Early-trigger candidates carry
  `early_trigger_probe=true`, which forces the risk manager into probe mode
  (`PROBE_RISK_MULTIPLIER`, half size) regardless of the momentum profile's own
  decision. Enabling the layer is therefore low-risk — it earns full size only by
  proving itself on live data.
- **Reuses the `momentum_breakout` / `momentum_breakdown` identity.** The emitted
  candidate flows through the *exact same* scoring profile, risk gates (including
  the close_pos fix, exhaustion gate) and planner TP/SL geometry. A distinguishing
  note `entry_trigger=1m_early` + an `EARLY_TRIGGER_1M_FIRED` log line make it
  traceable.
- **Additive only.** The trigger fills the momentum candidate slot *only when the
  normal 15m detection produced nothing* — it plugs the 0–15m latency gap and
  never overrides a real 15m signal.
- **Structural 15m stop.** Invalidation = min low (LONG) / max high (SHORT) of
  the last `structural_stop_lookback_15m` (default 4) closed 15m candles — a real
  structure level, so the trade holds for the bigger move instead of being wicked
  out on a tight 1m stop. (For momentum the planner does not ATR-clamp the stop,
  so the invalidation we pass is the stop.)
- **Volume scale fix.** The detector confirms volume on the 1m scale
  (`trigger_1m_volume_ratio`); the momentum-quality gate skips its 15m volume
  block for early-trigger candidates (different scale) but keeps every other gate.
- **Fail-open.** A stale/missing 1m or 5m timeframe (or any read error) logs and
  the scan proceeds; the trigger simply does not fire.

## Components

| File | Change |
|---|---|
| `app/config.py` | `early_trigger_1m_*` + `early_trigger_5m_confirm_enabled` (env-overridable, default ON) |
| `strategies/early_breakout_trigger.py` | NEW `EarlyBreakoutTrigger.detect()` + `candles_from_cache_rows()` |
| `app/runner.py` | flag-gated: read 1m/5m from `multi_tf_cache`, fill empty momentum slot |
| `risk/risk_manager.py` | momentum-quality gate skips 15m volume block for early trigger; probe-size for `early_trigger_probe=true` |
| `tests/test_early_breakout_trigger.py` | unit tests (13) |

## Rollout

1. Enabled at probe (half) size (this change).
2. Watch `EARLY_TRIGGER_1M_FIRED` logs and measure `entry_trigger=1m_early` trades
   separately (via `agents_v3 analyze` / logs).
3. Give the layer full size only once the probe-size sample proves positive.
