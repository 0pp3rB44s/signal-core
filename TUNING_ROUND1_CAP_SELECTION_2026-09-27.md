# Tuning Round 1 — cap selection, frozen BEFORE any comparative evaluation

**This document is written, and its value hashed, before any A/B comparison code exists or has run on this
branch.** The commit history (`git log`) is the evidence of that ordering. Nothing below was chosen by
trying values against PnL.

## The one change

`app/config.py::Settings.max_cluster_exposure_pct`: **120.0 → 40.0** (percent of NAV). Nothing else in
`Settings`, and no detector/scorer/selector/planner/feature-engine code, is touched. This is
[tuning proposal #1](AGENT_A_FUNNEL_TUNING_PROPOSALS_PHASE2_2026-09-27.md) from Phase 2 and only that one.

## Why exactly 40, derived from arithmetic already in the codebase, not from any PnL comparison

Three constants already fix the arithmetic:

- `execution_max_live_notional_per_trade_usdt = 35.0` — a **flat dollar** cap per trade, independent of NAV.
- `max_open_positions = 2` — a **global** cap (not per-cluster) on how many positions may be open at once.
- `CAPITAL_MIN_USDT = 100.0` (`lsrl/capital.py`) — the floor of the owner-approved capital band this bot is
  meant to eventually run on.

At the floor NAV of 100 USDT, the current cap (120% of NAV = **120 USDT**) is **structurally non-binding**:
the largest same-cluster exposure two concurrent 35 USDT positions could ever produce is 2 × 35 = **70
USDT**, which is already below 120 USDT. The existing cap does zero work anywhere in the approved capital
band (it would need NAV below ~58.3 USDT before 120% of NAV even reached 70 USDT) — it has never actually
constrained anything for an account sized the way this bot is meant to be capitalised.

For the cap to do the one thing "tighten cluster/symbol exposure" is supposed to do — stop **two**
concurrent full-size positions from landing in the **same** cluster — it must sit strictly between one
position's share and two positions' share of the floor NAV:

```
one position:  35 / 100 = 35%
two positions: 70 / 100 = 70%
```

**40%** is chosen: comfortably above 35% (so a legitimate single first position in a cluster is never
refused by rounding/edge cases) and, more specifically, chosen so that the room LEFT after one full-size
position (40 − 35 = **5 USDT**) sits below `planner_min_live_notional_usdt = 10.0` — the existing "a trade
that cannot meet minimum size is skipped, never upsized" floor. A second same-cluster position at the floor
NAV is therefore not blocked by a new hard rule; it is resized down by the existing, unmodified
`CentralRiskGate._ceiling()` arithmetic until it falls below the existing minimum, and the existing,
unmodified "below minimum → skip" behavior takes it from there. **No new gate logic. No new threshold
concept. One existing config number, changed once.**

**Disclosed limitation, stated rather than hidden:** this reasoning is anchored to the *floor* of the
100–150 USDT capital band. At the top of the band (150 USDT), 40% of NAV = 60 USDT, leaving 60 − 35 = 25
USDT of headroom after one position — above the 10 USDT minimum, so a second same-cluster position could
still be sized (down to ≤25 USDT) rather than skipped at that NAV. A single flat percentage cannot bind
identically across a variable-NAV band; fixing that would require a second, NAV-aware mechanism, which is
explicitly out of scope for "one minimal change only." This is disclosed, not silently smoothed over.

## Effect on the actual 8-symbol universe, mechanically (not a PnL claim)

`BETA_CLUSTERS` (`risk/risk_manager.py`): `MAJORS = {BTCUSDT, ETHUSDT}`, `L1_BETA = {SOLUSDT, AVAXUSDT,
SUIUSDT, INJUSDT, SEIUSDT}`. Of the 8 owner-approved symbols, BTCUSDT is the only one in MAJORS (ETHUSDT is
not traded), so the MAJORS cap becomes, in effect, a single-symbol concentration limit on BTCUSDT — without
naming BTCUSDT anywhere in the change, and applying identically to any future symbol added to that cluster.
SOLUSDT, AVAXUSDT, SUIUSDT and SEIUSDT share L1_BETA, so the same cap also limits how much of the book those
four correlated symbols can occupy simultaneously. XLMUSDT, DOGEUSDT and TRXUSDT are in no defined cluster
and are **unaffected** by this change (a disclosed, structural gap in `BETA_CLUSTERS` itself, not something
this round's single change touches).

## Freeze

```json
{
  "tuning_change": "app.config.Settings.max_cluster_exposure_pct",
  "baseline_value": 120.0,
  "tuned_value": 40.0,
  "derivation": "35pct (one trade's floor-NAV share) < X < 70pct (two trades'), chosen so post-one-position room (X - 35) falls below planner_min_live_notional_usdt (10.0) at CAPITAL_MIN_USDT (100.0)",
  "frozen_at_utc": "2026-09-27T00:00:00Z",
  "frozen_before_any_comparison": true
}
```

`TUNING_HASH` (sha256 of the JSON block above, byte-for-byte as committed): see
`TUNING_ROUND1_FROZEN_SPEC_2026-09-27.json` and its printed hash in the final report. This file and that one
are committed together, before any A/B code is written.
