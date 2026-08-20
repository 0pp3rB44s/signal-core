# Dashboard V3 recovery report

## Summary

Extended the authoritative Flask Dashboard V3 from production SHA
`c984fd82b5b3b970117d7e905cacfa9afa447ce6`. The dashboard now projects current
deployment identity, authenticated read-only account/order state, persisted
RiskManager/equity-breaker state, MicroFlow configuration and funnel state,
authoritative trade windows, collector/sensor health, redacted operational logs,
and canonical Obsidian project context. Dashboard V2 remains a retired historical
surface.

## Files changed

- Dashboard adapters, routes, templates, cache/source utilities and safety tests.
- Dedicated dashboard start/status/stop scripts.
- Independent `com.cgc.dashboard` launchd template/install/uninstall scripts.
- Dashboard operations and legacy-disposition documentation.

No strategy, entry, exit, RiskManager, order-routing, position-management, or
environment file was changed.

## Risk impact

Capital/execution risk: none. Dashboard V3 contains no execution/RiskManager
imports or order mutation method path. Missing/unreachable state remains UNKNOWN,
and exchange reads use only the existing GET allow-list. The stop script verifies
the recorded command before signalling the dashboard PID and cannot signal LIVE.

Operational risk is reduced by exposing SHA drift, unresolved intents,
recovery/ownership markers, equity breaker state, collector stream failures, and
source staleness. LAN access remains password-gated but is plain HTTP; use only on
the trusted home LAN or behind TLS.

## Tests/checks run

- Dashboard-focused: `118 passed`.
- Shell syntax, Python compile, plist lint and `git diff --check`: passed.
- All authenticated routes: HTTP 200 in local read-only smoke.
- Local warm request latency: 0.7–1.2 ms per cached route; first command build
  0.16 s; CPU 0.0%; RSS 57 MB.
- Full suite: `1744 passed, 63 failed, 1 skipped` when incorrectly invoked with
  process-wide development environment overrides. Every failed module was rerun
  without those overrides: `139 passed, 1 skipped`, proving the failures were
  invocation contamination rather than code regressions.

## Remaining concerns

- Runner/account/collector truth is UNKNOWN until the owner is on the home LAN;
  this task does not deploy or start anything there.
- Current production telemetry does not persist correlated exposure or total
  portfolio exposure on every risk evaluation; Dashboard V3 deliberately displays
  UNKNOWN for those values.
- Reviewed entry-quality counters require a structured
  `state/dashboard_review_summary.json`; the dashboard will not infer research
  metrics from narrative notes.
- The local detached cold start took 36.6 s on this macOS host before the listener
  became ready; the readiness loop handled it. Cached request latency and steady
  resource use met the lightweight runtime target.
