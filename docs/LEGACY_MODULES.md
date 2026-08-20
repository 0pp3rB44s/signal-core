# Legacy module disposition

This inventory is based on tracked Python imports, shell entrypoints,
documentation and known manual report workflows. Ambiguous modules are retained.

| Path | Status | Evidence / action |
| --- | --- | --- |
| `app/main.py`, `app/runner.py` | active | `scripts/start_bot.sh` starts `app.main`; runner owns the scan and monitor loops. |
| `dashboard_v2/` | retired duplicate | Kept only as a historical rollback target for v3. No supported entrypoint starts or controls it. |
| `dashboard_v3/` | **authoritative** | `scripts/start_dashboard.sh` starts `dashboard_v3.app`. Read-only and password-gated; the supported Runner launcher explicitly opts into the configured home-LAN bind. |
| `agents_v2/learning/coach_rules.py` | active | Imported and invoked by `app/runner.py`. |
| `agents_v2/learning/learning_service.py` | active | Imported by the v2 dashboard data provider. |
| Remaining `agents_v2/` audit/learning tools | compatibility | Produce the documented reports under `agents_v2/reports`; retained for manual workflows. |
| `app/settings.py` | compatibility | Import-time compatibility shim around `app.config.get_settings`; retain until external imports are inventoried. |
| `agents/` | migrate | No automated runtime or shell import found, but it contains audit reports and callable manual utilities. Migrate documented workflows to `agents_v2`, then deprecate with one release notice before removal. |
| `app/dashboard.py` | **removed** | Unsafe legacy control surface, deleted 2026-08-10. See "Dashboard control plane" below. |
| `telemetry/event_logger.py` | demonstrably unused | Removed: tracked zero-byte placeholder, no imports, entrypoints or implementation since the initial repository commit. |

Ignored or untracked local directories (including a possible `agents_v3/`) are
not repository modules and were neither classified as deployable nor removed.

## Migration sequence

1. ~~Confirm there are no external invocations of `python -m app.dashboard`~~
   — done 2026-08-10: no import, test, deploy or script dependency existed, and
   the module was removed. Still to confirm for scripts under `agents/`.
2. Replace any remaining manual v1 audit workflow with its `agents_v2`
   equivalent and document the command.
3. Add a deprecation window and import/entrypoint regression tests.
4. Remove compatibility modules only in a dedicated pull request.

## Dashboard control plane

**The dashboard is read-only. It does not start, stop or configure the engine.**

`app/dashboard.py` was removed on 2026-08-10. It exposed
`POST /api/control/{start_bot,stop_all,restart_bot,execution_off,execution_on_dryrun}`
and rewrote `.env` in place, including `EXECUTION_ENABLED` and `EXECUTION_MODE`.
`start_bot` shelled out to `scripts/start_bot.sh`, which bypasses all four
authorisation layers in `scripts/launch_live.sh`: open critical risks, the
owner-signed `LIVE_PILOT_AUTHORISATION` token, the `.env.live` invariants, and
the typed confirmation. One HTTP POST could therefore begin real-money trading,
and `stop_all` could kill an engine holding an open position. It had no
authentication boundary and no tests.

`dashboard_v2/bot_control.py` had already retired the same capability for the
same reason; that rationale is preserved there in full and is the canonical
statement of it. `dashboard_v3` never had it.

Starting and stopping the engine is an operator action performed from a
terminal through the launcher that enforces those layers. It is not a web
endpoint. Observation (`is_bot_running`, liveness probes, `pgrep`) is fine,
because it only reads.

`tests/test_dashboard_security.py` pins this: the removed file must stay
removed, `/login` must be the only route accepting a mutating method, no
`/control` namespace may exist, and `dashboard_v3` may not reference the
launcher scripts or write files.
