# Release A control plane

## Owner-authorized funding-crowding live pilot — blocked release

- Authorization timestamp (UTC): `2026-08-31T18:09:45Z`
- Owner decision: `SMALL_CAPPED_REAL_MONEY_LIVE_PILOT_AUTHORIZED`
- Live pilot authorized: `YES_CONDITIONAL_ON_RELEASE_GATES`
- Real-order authority: `YES_ONLY_FOR_FROZEN_24H_FUNDING_CROWDING_PILOT`
- Authorized strategy: `24h funding-crowding continuation`
- Frozen spec SHA-256: `cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13`
  (`VERIFIED` against `research/validation/FROZEN_SPECS.json`)
- Leverage: `1x`
- Pilot capital: `UNKNOWN_OWNER_MUST_SPECIFY_EXACT_USDT_ALLOCATION`
- Target/max position notional: `approximately 10 USDT`; an exact enforceable
  ceiling is required before launch
- Maximum concurrent positions: `2`
- Maximum gross exposure: `20% of pilot equity`
- Portfolio kill switch: `5% drawdown`
- Averaging down: `FORBIDDEN`
- Martingale: `FORBIDDEN`
- Exchange-native stop: `REQUIRED`
- Capital scaling: `FORBIDDEN_WITHOUT_NEW_EXPLICIT_OWNER_DECISION`
- AdaptiveTrend modification: `FORBIDDEN`
- Alpha-logic modification during pilot: `FORBIDDEN`
- Evidence purpose: approximately 30 days of real execution and PnL evidence;
  authorization is not validation or production-readiness evidence

Repository-level production isolation is `VERIFIED`: the frozen research spec
is committed only in the physically separate
`/Users/bryonprivee/Desktop/bitget_ai_agent/bitget_ai_agent_research_capital_growth_v3`
worktree on `research/capital-growth-v3`. Runtime/deployment isolation is
`NOT_YET_VERIFIED` because no pilot implementation or launch artifact exists.
AdaptiveTrend and the production checkout were not modified.

Release remains `BLOCKED`. No order may be sent until all of the following are
proven and recorded:

1. Owner specifies exact `PILOT_CAPITAL` and exact hard maximum position
   notional; `approximately 10 USDT` is not an enforceable ceiling.
2. An isolated pilot implementation is wired to the exact frozen-spec hash and
   cannot route any other strategy.
3. Exchange-native stop placement, acknowledgement, restart recovery, orphan
   reconciliation, reduce-only exit, and duplicate-order prevention pass tests.
4. The 5% portfolio drawdown kill switch blocks new entries and safely manages
   existing protected exposure using exchange-truth equity/PnL.
5. Required real evidence fields (signal/order/fill timestamps, prices, fees,
   spread, slippage, funding, stop execution, PnL, equity, drawdown) are wired
   and reconciled to exchange truth.
6. An independent release verifier who did not build the pilot returns an
   explicit safe-to-launch decision.
7. Owner separately approves the verified deployment artifact and launch after
   reviewing the implementation and independent report.

Current final status: `LIVE_PILOT_RELEASE_BLOCKED`. The strategy-specific
authorization is recorded; it does not itself deploy software or send orders.

### Implementation-phase update — 2026-08-31

- Pilot starting equity: `27.44 USDT`
- Compounding: `YES`, from current isolated pilot NAV
- Maximum single position: `0.10 × CURRENT_PILOT_NAV` (initially `2.744 USDT`)
- Maximum gross pilot exposure: `0.20 × CURRENT_PILOT_NAV`
- Minimum-notional behavior: `SKIP`; exposure percentage may not be increased
- Builder artifact: `9cbcbecab7cbb41654942883b45ef5f75c9923b0`
- Independent verification report commit:
  `090fb206c1a9b8197586a4f4defec53162d72cc7`
- Independent verdict: `READY_FOR_FINAL_OWNER_LAUNCH_DECISION=NO`
- Real orders sent: `0`

The artifact provides a frozen-hash guard, isolated SQLite ledger, dynamic NAV
sizing, shared-margin reservation, unknown-state refusal, native-stop lifecycle
contract, duplicate/orphan detection, restart reconciliation, complete telemetry
schema, and latched 5% kill-switch behavior. These paths passed deterministic
tests only. They are not production-wired.

The canonical `ExecutionService` requires take-profit protection for every live
entry, while the frozen pilot specifies a 24-hour time exit and catastrophic
stop without a take-profit. No production module imports `funding_pilot`, and
there is no adapter through `RiskManager`, `ExecutionService`, and
`PositionManager`. Direct Bitget routing would bypass mandatory safeguards and
is prohibited. Release therefore remains `IMPLEMENTATION_BLOCKED`; simulated
stop/recovery/kill/telemetry evidence is not represented as live verification.

## AgentC capital-growth research release

- Authorization timestamp (UTC): `2026-08-31T17:19:11Z`
- Owner decision: `RESEARCH_RELEASED`
- AgentC research gate: `OPEN`
- Phase B research authorized: `YES`
- Isolated worktree required: `YES`
- Verified base SHA: `39e922162c496c2b64eaadfab940545a69e94717`
- Research branch: `research/capital-growth-v3`
- Research worktree: `/Users/bryonprivee/Desktop/bitget_ai_agent/bitget_ai_agent_research_capital_growth_v3`
- Real-order authority: `NO`
- Production-modification authority: `NO`
- Shadow-build authority: `YES_AFTER_VALIDATION`
- Collector authority: `OBSERVATION_ONLY`; no order authority. No matching
  collector process was observed during the release check, so current collector
  ownership and runtime state remain `UNKNOWN`. No collector was stopped,
  restarted, or modified.
- Production protection: AdaptiveTrend, production configuration, live risk,
  live capital, real orders, and production deployment remain outside this
  research authorization. The dirty owner checkout remains protected and was
  not edited, cleaned, reset, stashed, or used as a research sandbox.

This owner-authorized transition releases the earlier prohibition on AgentC
Phase B work only for the isolated capital-growth research program above. It
does not supersede or weaken any production, deployment, capital-protection, or
real-order gate recorded below.

- Owner: Master Orchestrator
- Base: `817bc72f5b7b85f12b23fd6d7b03b09542addaed`
- Branch: `codex/release-a-close-economics-recovery`
- Worktree: isolated Release A worktree
- LIVE checkout: read-only and untouched
- Scope: exchange flatness, exchange close economics, lifecycle identity,
  deduplication, provisional recovery, startup/periodic wiring, tests and
  read-only-by-default migration audit
- Build state: complete; focused and full verification passed
- Deployment state: prohibited; owner review required
- Independent verifier: passed implementation tip `c03c517`; final report commit
  `3187a59`; `SAFE_TO_REVIEW=yes`, `SAFE_TO_DEPLOY_TECHNICALLY=yes`
- Owner gates: merge, migration `--apply`, emergency flatten invocation and
  deployment remain unauthorized
