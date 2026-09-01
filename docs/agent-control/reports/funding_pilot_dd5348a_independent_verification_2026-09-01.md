# Independent verification — funding pilot `dd5348a`

Date: 2026-09-01  
Verifier: AgentC independent verifier  
Builder commit: `dd5348a69f011df397ef6c06c2b279cc1ef396c8`  
Scope: deterministic local core only; no authenticated exchange access and no order transmission.

## Verdict

`dd5348a` fixes the previously identified 100–168-bar universe exclusion, restores the frozen current-depth gate, and correctly retains a live-position uncertain recovery through a pre-close economics checkpoint until a newer exact-position realized-PnL event arrives. The selected regression suite passes.

The deterministic core is still not ready for deployed-runtime verification. An independently constructed order-only uncertain lifecycle exposes a permanent recovery deadlock: after authoritative cancellation and position/order/stop zero proof, `reconcile_uncertain_lifecycles()` creates `RECOVERY_EXIT_PENDING` with an empty `exchange_position_id`. The next and all later cycles require a newer `realized_pnl:*` event for that empty position ID before `SAFE_CLOSED`; such an event cannot exist for an unfilled canceled order. Ownership remains active and the pilot remains halted indefinitely. This violates complete global post-execution recovery and `NO_ZOMBIE_LIFECYCLES`.

## Evidence classifications

- **PROVEN** — Production now accepts exactly 100 completed hourly bars and computes sample volatility from the available log-return tail capped at 168, matching the frozen/research rule.
- **PROVEN** — Production independently requires non-empty bid/ask depth and total current depth of at least 1,000 USDT before classification.
- **PROVEN** — Recorded-snapshot universe, ticker `usdtVolume`, simultaneous percentile ranking, selected symbol/side/timestamp, durable duplicate handling, and the strict `>300000 ms` stale boundary match the independent pandas replay.
- **PROVEN** — A live exact-position `HALTED_UNCERTAIN` recovery checkpoints economics before reduce-only close, keeps ownership, and finalizes only after a newer attributable exact-position realized-PnL event.
- **DEFECT / OPERATIONAL RISK** — Order-only or stop-only uncertain recovery enters an unsatisfiable economics wait with an empty position identity after exchange zero proof.
- **PROVEN** — Prior partial-close economics cannot satisfy a later exit checkpoint; partial A/B source identities remain distinct and exactly-once.
- **PROVEN** — Frozen file hash is `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`.
- **PROVEN** — Production construction remains hard-disarmed (`armed_live=False`); this audit sent no real orders.
- **UNKNOWN / EXTERNAL RUNTIME** — Authenticated Bitget read verification was not available and was not simulated as success.

## Required fields

FROZEN_POLLER_PARITY=100_PERCENT  
SIGNAL_POLLER_WIRED=YES  

ORDER_LIFECYCLE_IDENTITY_VERIFIED=YES  
NO_ZOMBIE_LIFECYCLES=NO  
NO_PREMATURE_OWNERSHIP_REMOVAL=YES  
POST_EXECUTION_EXCEPTION_COVERAGE=INCOMPLETE  

PARTIAL_CLOSE_IDENTITY_VERIFIED=YES  
DELAYED_CLOSE_ECONOMICS_VERIFIED=YES_FOR_EXACT_POSITION_CLOSE  
FINALIZED_EXIT_IDEMPOTENT=YES  

ECONOMICS_TRANSACTION_ATOMIC=YES  
FEE_DOUBLE_COUNT_PREVENTED=YES  
FUNDING_DOUBLE_COUNT_PREVENTED=YES  
ECONOMICS_REPLAY_IDEMPOTENT=YES  

CURRENT_PILOT_NAV_AUTHORITATIVE=NO  
DYNAMIC_COMPOUNDING_CORE_VERIFIED=YES_ARITHMETIC_ONLY  

OWNERSHIP_FALLBACK_REMOVED=YES  
SHARED_POSITION_CONFLICT_GUARD_VERIFIED=YES  
PILOT_STOP_ID_OWNERSHIP_VERIFIED=YES  

ADAPTIVETREND_REGRESSION_PASS=YES  
ADAPTIVETREND_UNTOUCHED=YES  
FROZEN_SPEC_UNCHANGED=YES  

BITGET_AUTH_READ_VERIFIED=NO_EXPECTED_EXTERNAL_RUNTIME  

READY_FOR_DEPLOYED_RUNTIME_VERIFICATION=NO  

BLOCKERS=ORDER_OR_STOP_ONLY_HALTED_UNCERTAIN_RECOVERY_CREATES_UNSATISFIABLE_EMPTY_POSITION_ECONOMICS_PENDING; POST_EXECUTION_EXCEPTION_COVERAGE_NOT_COMPLETE; AUTHORITATIVE_NAV_GATE_DEPENDS_ON_SECTIONS_2_TO_6_PASSING  

REAL_ORDER_ARMED=FALSE  
REAL_ORDERS_SENT=0  

FINAL_STATUS=IMPLEMENTATION_BLOCKED

## Checks run

- Inspected production `funding_pilot/signals.py`, `funding_pilot/canonical.py`, `funding_pilot/bitget_exchange.py`, production runner construction, original research classifier, and frozen spec.
- Ran 65 selected funding-pilot, canonical execution, restart, lifecycle, economics, AdaptiveTrend-isolation, and runner regression tests: all passed.
- Ran an independent order-only uncertain-lifecycle adversarial probe. First reconciliation canceled the exact owned order and appended `EXIT_PENDING(exchange_position_id="")`; second reconciliation remained pending with ownership active and no possible realized-PnL identity.
- Ran `python3 -m compileall -q funding_pilot`: passed.
- Recomputed frozen spec SHA-256: matched the authorized hash.
- Verified working-tree audit performed no exchange operations and no credential access.

## Required remediation

After exact authoritative zero proof, distinguish lifecycles that had a filled exact position from those that had only orders/stops. Require delayed realized economics only when a non-empty exact position identity existed and a close was issued. For order/stop-only recovery, cancel exact owned artifacts, prove position/order/stop zero, then safely terminalize without creating an impossible empty-position economics checkpoint. Add restart/idempotence tests for both order-only and stop-only uncertain states.
