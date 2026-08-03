# Release A control plane

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
