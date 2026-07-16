# Work Mac branch migration audit — 2026-07-16

## Canonical identity

- Work repository: `/Users/bryonprivee/Desktop/bitget_ai_agent/bitget_ai_agent_phase7`
- Remote: `git@github.com:0pp3rB44s/signal-core.git`
- Default branch: `main`
- Remote main at audit: `6f1b93edc8d3df443bef9a4bc7e633bf43a3ec17`
- Local main at audit: `7da2c9e5d81f48b9a8470ab82412598c0c5d95ff` (one local test-configuration commit ahead)
- Phase 4C: `research/basis-mark-index-edge-map-v2` at `3413bf8d19c2744dec70587dee6bf64ed8ec1dcc`

The primary worktree was dirty only through untracked duplicate Phase 4C source
files; they were not staged, committed, overwritten, or used for this PR. The
canonical Phase 4C worktree itself was clean. Infrastructure work was therefore
performed in a separate worktree and branch.

## Production foundation versus research

Production-relevant correctness work through candidate lifecycle is already an
ancestor of current `main`. Local `main` adds only the pytest project-root
configuration. The later Phase 2–4 chain is research-only and must remain on
archival research branches.

```text
origin/main 6f1b93e
  └─ local main 7da2c9e (pytest root; carried by infrastructure PR)

41aecdd (candidate lifecycle; ancestor of main)
  └─ 2009a4a (execution contract)
      └─ 2352749 (performance baseline)
          └─ 523fc77 (historical baseline)
              └─ 2ad6f73 (historical risk contract)
                  └─ 2405e82 (strategy diagnosis)
                      └─ 4763c3c (independent sweep year)
                          └─ 4c44d3f (confirmation test)
                              └─ 924e2a4 (preregistration)
                                  └─ dbfafe6 (locked implementation)
                                      └─ cad0693 (locked result)
                                          └─ 878356a (OHLCV edge map)
                                              └─ aaeb856 (funding/OI audit)
                                                  └─ 3413bf8 (basis map)
```

The entire lower chain is preserved for reproducibility, not proposed for
deployment. Production-relevant extraction from it, if ever required, needs a
new clean integration branch and an independent PR.

## Branch disposition

| Group | Representative branches | Main ancestry | Remote | Recommendation |
|---|---|---|---|---|
| Canonical | `main` | yes | yes | protect; PR-only |
| Phase 2–4 research | `fix/minimal-backtest-execution-realism`, `research/liquidity-sweep-confirmation-entry`, `research/preregister-next-strategy`, `research/failed-range-escape-v1-validation`, `research/market-edge-discovery-map`, `research/funding-open-interest-edge-map`, `research/basis-mark-index-edge-map-v2` | no after 41aecdd | local-only at audit | push/archive; never deploy |
| Merged aliases | detector audit/fix branches, strategy catalog, trading baseline | yes | mixed | retain temporarily, then delete only after human confirmation |
| Published feature branches | unified feature/candidate lifecycle, structured funnel, forward-paper observability | mixed; final work is on main | yes | archive; no runner deployment |
| Published historical fixes | strict runtime, process-exit diagnosis, TP/SL branches | not all merged | yes | review independently; do not merge blindly |
| Unreviewed local experiments | `claude/nervous-*`, `claude/quizzical-*`, `review-prearmed-risk-changes` | no | no | keep local pending explicit review; do not publish automatically |
| Obsolete/colliding branch | `research/basis-mark-index-edge-map` | no | no | archive locally; canonical Phase 4C is `-v2` |

All SHAs named by the migration request resolve to commit objects. No SHA was
assumed from its abbreviation. Worktree clean/dirty status is recorded in the
final task report because several legacy worktrees contain user-owned changes
and must not be removed automatically.

## Tag and deployment status

No deployment tag existed at audit time. Consequently no Runner checkout or
process change is authorized by this branch. The first tag must be created only
after this infrastructure PR is reviewed and its approved commit is reachable
from `origin/main`.
