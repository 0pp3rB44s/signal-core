# Release A control plane

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
