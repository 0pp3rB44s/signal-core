# Dashboard V3 operations

Dashboard V3 is the password-gated, read-only Flask operations console. It has no
routes, imports, or client calls that can submit/cancel orders, close positions,
change risk, rewrite environment files, or control the LIVE engine.

## Runtime

- Entry point: `python -m dashboard_v3.app`
- Port: `8501` by default (`DASHBOARD_PORT` may override it)
- Refresh: 5 seconds for the cached operational model; slower panel TTLs protect
  exchange and historical sources.
- Runner LAN URL: `http://192.168.178.95:8501`
- Authentication: `DASHBOARD_PASSWORD` is mandatory; startup fails closed without it.
- Network: the supported launcher opts into the configured LAN bind. Traffic is
  plain HTTP, so use it only on the trusted home LAN or put TLS in front of it.

## Commands on Runner

```bash
cd ~/cgc/bitget_ai_agent_phase7
./scripts/start_dashboard.sh
./scripts/status_dashboard.sh
./scripts/stop_dashboard.sh
```

The stop script verifies that the recorded PID belongs to `dashboard_v3.app`
before sending TERM. It never uses broad `pkill` patterns and cannot signal the
LIVE engine, supervisor, or collectors.

For boot persistence, install the independent `com.cgc.dashboard` agent:

```bash
./deploy/launchd/install_dashboard_agent.sh
```

This service invokes only `scripts/start_dashboard.sh`. It has no dependency on
and performs no action against `com.cgc.live`.

## Sources and truth rules

Structured sources are preferred: authenticated Bitget GET endpoints, runtime and
equity JSON, order-intent state, collector status JSON, the closed-trade journal,
bounded immutable segments, and bounded structured log markers. Obsidian is read
only for project context when `DASHBOARD_OBSIDIAN_VAULT` or the standard sibling
vault exists.

Missing, corrupt, stale, or unreachable data renders `UNKNOWN`, `STALE`, or
`OFFLINE`; it is never converted to a healthy zero. Current RiskManager telemetry
does not persist correlated or total exposure after every evaluation, so those
fields remain `UNKNOWN` until production emits them. Entry-quality counters remain
`UNKNOWN` unless a structured `state/dashboard_review_summary.json` exists; the
dashboard does not recompute research from narrative notes.

Trade-history MAE, hold duration, and legacy TP1 fields are visibly quarantined.
Performance money is restricted to displayable authoritative closes.

## Troubleshooting

Use `./scripts/status_dashboard.sh`, then inspect `logs/dashboard.out`. A dashboard
failure never restarts LIVE. If the exchange or Runner is unavailable, pages stay
up and label the affected panels `UNKNOWN`/`OFFLINE`.
