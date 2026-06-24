# TP/SL Protection Audit

Date: 2026-06-24
Branch: `codex/tpsl-audit-report`
Scope: read-only audit of TP/SL creation, verification, silent-failure risk, duplicate-close risk, missing-protection windows, and test/log coverage.

## Executive Summary

The repository has a strong safety intent around TP/SL protection. Live entry is blocked when a plan has no stop loss or take profit, entry fills are followed by exchange-position confirmation, and unverified entry protection triggers a fail-safe close path. Position sync also attempts protection repair and avoids local auto-close while Bitget still reports an open live position.

The largest reliability concern is that `clients/bitget_tpsl_client.py::place_futures_protection_orders()` returns immediately after placing position-level TP/SL for only the first take-profit target. The later code that formats and places multiple TP plan orders is unreachable. This means planner output may contain TP1/TP2/TP3, while live exchange protection may only verify one position-level TP plus SL.

The second major concern is protection repair in `execution/position_manager.py::_ensure_exchange_protection()`: it calls `place_futures_protection_orders()` with `trigger_price=stop_loss`, but the client function expects `stop_loss=...`. That can cause repair attempts to fail even when local state contains a valid stop and take profits.

## Files Inspected

- `AGENTS.md`
- `README.md`
- `app/main.py`
- `app/runner.py`
- `app/config.py`
- `planning/trade_planner.py`
- `execution/execution_service.py`
- `execution/position_manager.py`
- `execution/adaptive_tp_engine.py`
- `clients/bitget_tpsl_client.py`
- `clients/bitget_order_client.py`
- `clients/bitget_base_client.py`
- `clients/schemas.py`
- `risk/risk_manager.py`
- `telemetry/trade_logger.py`
- `dashboard_v2/data_provider.py`
- `dashboard_v3/api/data_service.py`
- `scripts/healthcheck.sh`
- `tests/test_execution_safety.py`

## Where TP and SL Are Created

- `planning/trade_planner.py`
  - Builds `TradePlan.stop_loss` from candidate invalidation and stop-buffer logic.
  - Builds TP targets from `AdaptiveTPEngine` R multiples: TP1, TP2, TP3.
  - Can emit single-TP plans for adaptive/low-vol profiles.
  - Applies RR, net-edge, largest-loss, notional, and quality gates before `EXECUTABLE`.

- `execution/execution_service.py`
  - Before live entry, validates `plan.stop_loss > 0` and at least one valid TP.
  - After live market entry, calls `client.place_futures_protection_orders(...)`.
  - Stores local protection fields into `state/executed_trades.json` only after exchange protection is verified.

- `clients/bitget_tpsl_client.py`
  - `place_position_tpsl()` places position-level stop loss and one take profit using `/api/v2/mix/order/place-pos-tpsl`.
  - `place_futures_protection_orders()` currently delegates to `place_position_tpsl()` using the first TP only.
  - Later multi-TP plan-order code exists in the same function but is unreachable because of an earlier `return`.

- `execution/position_manager.py`
  - Moves stop after TP1 to fee-adjusted break-even.
  - Moves stop after TP2 to TP1.
  - Tightens failed continuation stops.
  - Attempts repair for unverified protection.

## Where TP/SL Are Verified

- Entry protection:
  - `execution/execution_service.py` retries `place_futures_protection_orders()` up to three times.
  - It requires `stop_loss_verified`, expected TP count coverage, and `protection_verified`.
  - Failure logs `ENTRY_PROTECTION_VERIFY_FAILED` and invokes fail-safe close.

- Stop-loss verification:
  - `clients/bitget_tpsl_client.py::verify_active_stop_loss()` fetches pending plan orders and verifies a matching stop trigger within tolerance.
  - `clients/bitget_tpsl_client.py::place_position_tpsl()` verifies position-level TP/SL fields from `get_all_positions()`.

- Ongoing position verification:
  - `execution/position_manager.py` checks live position TP/SL fields using `_exchange_position_has_tpsl()`.
  - If protection is missing, it calls `_ensure_exchange_protection_with_retries()`.
  - If repair fails, it attempts to close the unprotected position.

- Dashboard/log checks:
  - `dashboard_v2/data_provider.py` flags open positions missing SL/TP, unverified protection, and TP1-hit-without-BE.
  - `scripts/healthcheck.sh` scans recent logs for `UNPROTECTED`, `TP_PROTECTION_VERIFY_FAILED`, `VERIFY_STOP_LOSS_FAILED`, `ENTRY_PROTECTION_VERIFY_FAILED`, and `FAIL_SAFE_CLOSE_FAILED`.

## Risks Found

### High: multi-TP protection code is unreachable

`clients/bitget_tpsl_client.py::place_futures_protection_orders()` returns immediately after `place_position_tpsl()`. The code below that return builds separate loss/profit plan orders for all TPs, counts expected TPs, verifies TP count, and cleans up partial failures, but it cannot execute.

Impact:
- Planner may create TP1/TP2/TP3, but live exchange protection may only include the first TP.
- `execution_service` reads `expected_take_profit_count` from the returned payload when present, otherwise falls back to local TP count. Current position-level payload appears to verify position TP/SL, not full ladder coverage.
- TP2/TP3 lifecycle logic in `position_manager` may rely on local targets that were never installed as exchange TP orders.

### High: protection repair uses the wrong keyword for stop loss

`execution/position_manager.py::_ensure_exchange_protection()` calls:

```text
placer(..., trigger_price=stop_loss, take_profits=take_profits, ...)
```

But `place_futures_protection_orders()` expects `stop_loss`, not `trigger_price`.

Impact:
- Repair can fail with "valid stop_loss" errors even when local state has a stop.
- An unverified live position may go from repair attempt directly to emergency close instead of restoring protection.

### High: fail-safe direct close may not be reduce-only

`execution/execution_service.py::_fail_safe_close()` first calls `place_futures_market_order(..., side=close_side, trade_side="close")`. The current `place_futures_market_order()` implementation derives direction from side and always builds `"tradeSide": "open"` internally. The fallback `close_futures_position_full()` is reduce-only, but the direct first attempt is risky.

Impact:
- In a protection failure, the first fail-safe close path may not use the strict reduce-only close helper.
- This needs a test or patch before relying on it as the primary emergency close path.

### Medium: local protection payload can mask exchange verification needs

`PositionManager._ensure_exchange_protection()` returns true if `_has_local_protection_payload(position)` is true. That checks local payload and local expected values, not necessarily fresh exchange state.

Impact:
- A stale local protection payload could mark repair as unnecessary.
- This is partially mitigated because the main sync path first checks live exchange TP/SL fields, but the repair helper itself is not exchange-authoritative.

### Medium: local stop-hit closure can still write dataset rows when exchange sync is unavailable

When Bitget sync fails, `PositionManager.sync()` preserves open state. When sync is available and Bitget still shows open, local stop touches do not auto-close. That is good. However, when a local stop hit happens without exchange-open confirmation, local state can close and write dataset rows.

Impact:
- This is acceptable as a fallback, but dataset confidence must stay explicit.
- More tests are needed around `POSITION_SYNC_UNCERTAIN`, local stop hit, and exchange truth backfill.

### Medium: duplicate-close prevention is distributed

Duplicate-close controls exist in several places:
- local status checks before processing non-open positions
- `_closed_trade_dataset_row_exists()`
- `close_futures_position_full()` live-size check
- reduce-only close validation
- local-stop safe mode when exchange still shows open

Impact:
- The design is conservative, but behavior is spread across position manager, trade logger, and Bitget client.
- There is no focused test proving duplicate close is avoided across TP3 close-all, residual cleanup, protection repair close, local stop close, and exchange-closed sync.

### Medium: healthcheck does not scan every relevant protection marker

`scripts/healthcheck.sh` checks several strong markers, but it does not include all useful close/protection markers, such as:
- `UNPROTECTED_POSITION_CLOSE_FAILED`
- `PROTECTION_ACTION_FAILED`
- `EXCHANGE_SL_REPLACE_ABORTED`
- `EXCHANGE_SL_CANCEL_FAILED`
- `LOCAL_STOP_TOUCHED_EXCHANGE_OPEN_NO_AUTOCLOSE_SAFE_MODE`
- `FAIL_SAFE_POSITION_STILL_OPEN`
- `FAIL_SAFE_POSITION_VERIFY_FAILED`

Impact:
- A dangerous protection lifecycle problem may be present in logs without flipping `protection status` to attention required.

### Low: dashboard protection status depends on local state freshness

`dashboard_v2/data_provider.py` reports missing SL/TP and protection status from local state plus dashboard live-position rows. This is useful, but it is not a substitute for direct exchange verification.

Impact:
- Dashboard can show stale or optimistic protection if local state is not freshly reconciled.

## Where Orders Can Fail Silently

- Position-level TP/SL can verify only the first TP while local plan contains more targets.
- Protection repair can fail due to keyword mismatch and then proceed to emergency close path.
- Stop replacement cancels old stop before moving the new one. If cancellation succeeds and replacement/verification fails, local SL is reverted, but exchange may be temporarily without the intended new SL. Existing code logs `TP_PROTECTION_FAILED` or `TP_PROTECTION_VERIFY_FAILED`.
- Order detail analytics failures are warning-only. That is acceptable for analytics, but they can reduce fill-price and fee confidence.
- Dashboard warnings are only as current as local state and recent log parsing.

## Where Missing TP/SL Could Happen After Entry

- After live market entry, before protection placement completes. This is the critical unprotected window; code mitigates it with immediate verification and fail-safe close.
- If `place_position_tpsl()` verifies position-level TP/SL but does not install TP2/TP3 plan orders.
- If Bitget accepts entry but `get_all_positions()` or TP/SL pending-order queries are delayed or inconsistent.
- If position state is recovered from Bitget but live position has no TP/SL and fallback protection cannot be recovered from execution logs.
- If SL replacement after TP1/TP2 cancels old stop and fails to verify the new stop.

## Missing Tests

Add focused tests before changing live behavior:

1. `place_futures_protection_orders()` with three TPs should verify whether all expected TPs are placed, or explicitly document single-TP position-level mode.
2. `PositionManager._ensure_exchange_protection()` should call the client with `stop_loss=...`, not `trigger_price=...`.
3. Entry protection failure after live order should trigger fail-safe close and should not store an `OPEN` local position.
4. Fail-safe close should use a reduce-only close path.
5. TP1 hit should move SL to fee-adjusted BE only after exchange verification succeeds.
6. TP2 hit should move SL to TP1 only after exchange verification succeeds.
7. Failed SL replacement should leave local state unmodified and emit a critical marker.
8. Duplicate close should be prevented across TP3 close-all, residual cleanup, local stop close, and exchange-closed sync.
9. Recovered Bitget position without live TP/SL should either repair protection or close, with a dataset row only once.
10. Healthcheck marker coverage should include all current protection failure log names.

## Suggested Next Patch

Make the smallest safe non-strategy patch:

1. Add tests around `place_futures_protection_orders()` and `PositionManager._ensure_exchange_protection()` using mocked Bitget client responses.
2. Fix the repair keyword from `trigger_price=stop_loss` to `stop_loss=stop_loss`.
3. Decide and enforce one TP/SL contract:
   - either position-level single-TP mode with local TP2/TP3 disabled for live, or
   - multi-TP plan-order mode with all expected TP orders reachable and verified.
4. Change the first fail-safe close attempt to use a reduce-only close helper, or add a test proving the current path sends a reduce-only close.
5. Expand `scripts/healthcheck.sh` marker coverage for protection-action and fail-safe verification failures.

Do not alter risk limits, leverage, sizing, strategy selection, or TP/SL weakening as part of this patch.

## Commands and Checks Run

- `sed -n '1,220p' AGENTS.md`
- `git status --short --branch`
- `git switch -c codex/tpsl-audit-report`
- `rg -n "take_profits|stop_loss|place_futures_protection_orders|verify_active_stop_loss|protection_verified|ENTRY_PROTECTION|UNPROTECTED|FAIL_SAFE|duplicate|dataset_close|close_futures_position|reduceOnly|TP1|TP2|TP3|break_even|cancel_all_futures_tpsl|_ensure_exchange_protection|_protect_after_tp_fill" app execution clients planning risk telemetry dashboard_v2 dashboard_v3 tests scripts README.md -g '*.py' -g '*.sh' -g '*.md'`
- Targeted `sed` reads of planner, execution service, position manager, Bitget TP/SL client, Bitget order client, healthcheck, dashboard protection status, and execution safety tests.

No Python files were modified. No tests were run because this was a markdown-only audit task.
