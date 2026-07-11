# 1-Minute Early-Trigger Layer

## Problem

`momentum_breakout` evaluates the last **closed 15m candle** (`candles[-1]`). A
breakout that starts at minute 2 of a 15m candle is not detected until that
candle closes — up to **13 minutes late**. By then the move is often already
extended, and the momentum-quality gate correctly rejects it as "too extended /
late." The bot is structurally too slow for fast breakouts, exactly the moves a
human hand also misses.

## Design principle (how commercial systems do it)

Separate three timeframes that are currently collapsed into one:

- **Context / trend TF (1H)** — already in the snapshot (`confirmation`).
- **Setup TF (15m)** — already the `primary`.
- **Trigger TF (1m)** — NEW. Detects the breakout *as it forms*, ~1 minute after
  it starts instead of up to 15.

The 1m timeframe is a **signal**, never a hold timeframe. We enter earlier and at
a better price, but we still **hold for the 15m/1H move** with a **structural
15m stop**. This is the opposite of scalping the 1m — the bot's own data shows
sub-1h scalps lose to fees (−3.25 / −3.49 USDT), while the 1–4h bucket is the
only non-loser. Detecting faster improves the *entry* of the trades that already
work; it does not add a new fee-heavy scalp.

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

Order-flow / tick confirmation (the gold standard) is out of scope: the bot does
not ingest tick data. This is an OHLCV-1m layer, which has a known ceiling — it
is a latency fix, not an HFT edge.

## Integration (why it is safe)

- **Feature-flagged, default OFF** (`EARLY_TRIGGER_1M_ENABLED=false`). With the
  flag off, no 1m data is fetched and nothing in the scan changes — provably zero
  behaviour change on the live bot until the user opts in.
- **Reuses the `momentum_breakout` / `momentum_breakdown` identity.** The emitted
  candidate flows through the *exact same* scoring profile, risk gates (including
  the close_pos fix, volume gate, exhaustion gate) and planner TP/SL geometry.
  No new scoring/risk surface, no divergence. A distinguishing note
  `entry_trigger=1m_early` + an `EARLY_TRIGGER_1M_FIRED` log line make it
  traceable.
- **Additive only.** The trigger fills the momentum candidate slot *only when the
  normal 15m detection produced nothing* — it plugs the 0–15m latency gap and
  never overrides a real 15m signal.
- **Structural 15m stop.** Invalidation = min low (LONG) / max high (SHORT) of
  the last `structural_stop_lookback_15m` (default 4) closed 15m candles — a real
  structure level, so the trade holds for the bigger move instead of being wicked
  out on a tight 1m stop. (For momentum the planner does not ATR-clamp the stop,
  so the invalidation we pass is the stop.)
- **Fail-open.** A 1m fetch failure logs a warning and the scan proceeds exactly
  as before.

## Components

| File | Change |
|---|---|
| `app/config.py` | `early_trigger_1m_*` settings (all env-overridable, default OFF) |
| `data/market_fetcher.py` | flag-gated 1m fetch → `snapshot.context["candles_1m"]` |
| `strategies/early_breakout_trigger.py` | NEW `EarlyBreakoutTrigger.detect()` |
| `app/runner.py` | flag-gated: fill empty momentum slot from the trigger |
| `tests/test_early_breakout_trigger.py` | unit tests |

## Rollout

1. Ship with flag OFF (this change).
2. Enable on probe size only after the breakout close_pos fix has a few days of
   green data.
3. Measure `entry_trigger=1m_early` trades separately via `trade_stats` / logs
   before giving the layer more room.
