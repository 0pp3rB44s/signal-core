# Agent A — LSRL_V1 micro-live owner ceremony preparation

Engineering/operations only. No strategy or prospective-evaluation verdict is issued. No approval, capital authorization, credential, production environment, order, position, kill switch or protected prospective file was created or changed.

## Scope and result

Prepared and tested an isolated ceremony component and a locked dedicated entry point, `python -m lsrl.tools.owner_ceremony`. The shipped command prints NO_GO and never prompts, constructs a private exchange client, creates an authorization file, starts a runtime, or changes an allow-list. It cannot be enabled using flags, environment variables, stdin, or a saved authorization.

**PRODUCTION_INTEGRATION=NOT_IMPLEMENTED.** This is a preparation component, not an approved/wired live launcher. The real read-only evidence provider, independently reviewed approval pins and live-runtime handoff remain uninstalled. No claim is made that helper tests prove those production connections. The existing generic `launch_live.sh`, Settings, central RiskManager and LSRL executor are unchanged. The known process-down passive-entry exposure gap is NOT repaired by a ceremony.

Base SHA (verified before edits): `679b6e0b5eeb8c90c87b20d892006a9a8d7dd4f2`.
Engineering branch: `codex/lsrl-owner-ceremony-20260922`.
Engineering worktree on Runner: `/Users/bryonkolkman/cgc/lsrl-owner-ceremony-20260922`.
Accepted PAPER worktree: `/Users/bryonkolkman/cgc/lsrl-v1` (left at exact accepted commit).
Final engineering SHA: recorded after commit in the delivery manifest and final response; the report cannot contain its own commit hash.

## Preconditions and trust boundary

The component requires all eighteen requested gates plus separately approved capital. A missing/false/unknown gate or evidence older than five seconds refuses before the readiness banner. Both the actual identity and independently approved identity must agree on exact 40-character SHA, frozen config hash, effective non-secret runtime-config hash, non-secret account identity, mode, strategy, symbol, exchange/account semantics and capital/exposure ceiling. Fixed strategy/config fields are also checked directly. All numbers must be finite; missing exposure data is never an empty account.

The `Provider` interface specifies the trusted future composition, not a way to submit verdicts through CLI/API. It must independently establish:

1. Git HEAD equals the reviewed final engineering release, clean including untracked files, and frozen core bytes match accepted `679b6e0` (`git_facts` implements the filesystem checks).
2. Strategy/config freeze and effective runtime configuration match the reviewed release policy.
3. Genuine, independently issued PROSPECTIVE_GREEN and MICRO_LIVE engineering approvals, including an explicit process-down PASS, bound to release/config/account/strategy/mode. `pinned_artifact` checks bytes against an externally approved digest and checks bindings/expiry. An artifact cannot supply its own trusted digest. No real artifact or trusted approval digest was created.
4. Fresh authenticated exchange semantics and complete paginated positions, normal orders and every applicable conditional/plan-order type. Failure/malformed/partial responses refuse. A dedicated pilot starts only on a fully flat, clean audited account; the ceremony never cancels foreign exposure.
5. Proposed live-runtime clock, feed, recorder, disk, central RiskManager, kill switches, reconciliation and protection capability. PAPER health is not a substitute for those live observations.
6. Owner-approved pilot capital and maximum total exposure; no defaults choose live capital.

**None of the production verifier connections above is claimed verified by this mission.** Current authorization blockers are known from the owner's instruction, Agent C's report and CURRENT_STATE. The locked CLI conservatively refuses without any credential access or authenticated audit. Unknown live health remains unverified rather than PASS.

## Interactive and one-use behavior

Only after validation does the library display the ready summary: LSRL_V1, exact release/config/runtime hashes, SOLUSDT/Bitget, account mode, NAV, frozen 0.50% maximum trade risk in USDT, approved capital/exposure caps, audited exposure and safety statuses. It requires a foreground controlling terminal and terminal stdin/stdout; no `/dev/tty` escape for piped input. Pretyped input is flushed. Only the exact case-sensitive line `START LIVE PILOT` succeeds; mismatch, EOF or 30-second timeout aborts.

All gates and identity are freshly checked after the phrase. The result is a non-serializable, in-memory, process-bound, five-second permit, consumed at most once and revalidated at consumption. Failed consumption burns it. A new ceremony invalidates an unused old permit. There is no file token, restart restoration, environment override or real runtime call.

The additional `within_capital` helper only vetoes orders. It never sizes upward to meet minimums; below-minimum frozen sizing returns NO TRADE. A future approved adapter must invoke it on every entry using total position plus all outstanding ladder exposure, after the existing central authorization; maintain these limits for the pilot lifetime, and preserve all frozen stops/risk rules. That per-order production wiring is outstanding, not silently inferred from this helper.

## Tests and limits

Final focused suite: **72 passed** (60 new ceremony tests plus 12 existing frozen-rule tests). Static compilation passed. Tests run only in the separate engineering worktree using the existing Python runtime; no environment/credential files were loaded by the new component.

Tests cover each gate refusal before prompt; wrong SHA/config/runtime/account/mode/capital binding; position/order/conditional exposure and unknown/partial audits; stale/future/nonfinite evidence; absent/unpinned/misbound/expired fake artifacts; no upward minimum-size adjustment; wrong/lowercase/extra-space phrase; piped input; EOF; timeout; exact acceptance through a disposable foreground PTY; replay, expiry, process identity and serialization refusal; health/account changes after the phrase; and the shipped CLI's locked NO_GO even with a flag, environment phrase and piped phrase.

The successful path uses an explicitly fake account and synthetic state. It reaches `OWNER_PHRASE_ACCEPTED=YES` only in the test harness, then consumes/rejects replay of a simulated permit. No real exchange mutation method is available there. The first test run exposed a test-harness subprocess cwd issue caused by the repository's isolation fixture; explicit test-worktree cwd fixed it. No safety gate was weakened.

No full unrelated regression suite was run: existing runtime, executor, risk manager, strategy and launcher files are byte-unchanged; focused frozen-rule and new-code tests cover this isolated addition. Independent Agent C review is still required. Principal remaining work: trustworthy production evidence collection and reviewed release-policy format/pins; actual per-order capital/authorization handoff; process-down mitigation and its independent approval; prospective verdict; owner capital approval and eventual manual ceremony.

## PAPER continuity

Read-only checks during this task confirmed PID **67100** with the same process start identity (Runner local `Mon Sep 21 20:54:08 2026`, corresponding to the reported approximately 03:54 UTC launch), epoch `1d0070a9ec724909b18803a29e2ec4a5`, PAPER mode, accepted SHA and frozen digest. Status-file mtime continued advancing. Final metadata is saved separately in `PAPER_FINAL.json`. No start/stop/restart/signal was sent to that process. Point-in-time observations support continuity across this engineering session; they are not a new retrospective coverage or outcome audit.

The frozen prospective launcher is pre-existing untracked operational source, documented in Agent A's start report; this task does not pretend that it is part of commit 679b6e0. Its unchanged hash is checked separately. No prospective outcomes, decisions or trade returns were read.

## Changed files

- `lsrl/tools/owner_ceremony.py`: isolated ceremony and safety-veto helpers; locked CLI.
- `tests/test_lsrl_owner_ceremony.py`: fake-only and terminal adversarial tests.
- `docs/agent-control/reports/AGENT_A_LSRL_V1_MICROLIVE_CEREMONY_PREP.md`: this report.

No control-plane or Obsidian edits, no merge/push/deploy, no running PAPER source/config changes. The engineering commit is a descendant on its own branch; the accepted PAPER branch remains at 679b6e0.

```text
CEREMONY_IMPLEMENTED=PREPARATION_COMPONENT_ONLY
EXACT_PHRASE=START LIVE PILOT
INTERACTIVE_ONLY=YES
SINGLE_USE=YES
SHA_BOUND=YES
CONFIG_BOUND=YES
ACCOUNT_BOUND=YES
EXPIRING=YES
REPLAY_PROTECTED=YES
PRODUCTION_INTEGRATION=NOT_IMPLEMENTED
CURRENT_PROSPECTIVE_GREEN=NO
TRADEABLE_EDGE_GATE=NOT_MET
CURRENT_PROCESS_DOWN_GATE=NOT_MET
CURRENT_ENGINEERING_GATE=NOT_MET
CURRENT_OWNER_AUTHORIZATION_GATE=NOT_MET
MICRO_LIVE_CAPITAL_AUTHORIZED=NO
CURRENT_LIVE_DECISION=NO_GO
REAL_EXCHANGE_MUTATION_TESTED=NO
REAL_ORDER_PLACED=NO
PRODUCTION_ARMED=NO
PAPER_EPOCH_STILL_RUNNING=YES_AT_OBSERVED_CHECKS
NEXT_ACTION=wait for prospective evidence and remaining micro-live safety approval
```
