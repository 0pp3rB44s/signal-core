# Legacy module disposition

This inventory is based on tracked Python imports, shell entrypoints,
documentation and known manual report workflows. Ambiguous modules are retained.

| Path | Status | Evidence / action |
| --- | --- | --- |
| `app/main.py`, `app/runner.py` | active | `scripts/start_bot.sh` starts `app.main`; runner owns the scan and monitor loops. |
| `dashboard_v2/` | active | `scripts/start_dashboard.sh` starts `dashboard_v2.app`; `stop_all.sh` stops it. |
| `agents_v2/learning/coach_rules.py` | active | Imported and invoked by `app/runner.py`. |
| `agents_v2/learning/learning_service.py` | active | Imported by the v2 dashboard data provider. |
| Remaining `agents_v2/` audit/learning tools | compatibility | Produce the documented reports under `agents_v2/reports`; retained for manual workflows. |
| `app/settings.py` | compatibility | Import-time compatibility shim around `app.config.get_settings`; retain until external imports are inventoried. |
| `agents/` | migrate | No automated runtime or shell import found, but it contains audit reports and callable manual utilities. Migrate documented workflows to `agents_v2`, then deprecate with one release notice before removal. |
| `app/dashboard.py` | migrate | Old dashboard/control implementation; launch scripts explicitly stop it but start v2. It can mutate `.env` and lacks the v2 authentication boundary. Do not run it; first redirect any manual users and then remove in a separate security migration. |
| `telemetry/event_logger.py` | demonstrably unused | Removed: tracked zero-byte placeholder, no imports, entrypoints or implementation since the initial repository commit. |

Ignored or untracked local directories (including a possible `agents_v3/`) are
not repository modules and were neither classified as deployable nor removed.

## Migration sequence

1. Confirm there are no external invocations of `python -m app.dashboard` or
   scripts under `agents/`.
2. Replace any remaining manual v1 audit workflow with its `agents_v2`
   equivalent and document the command.
3. Add a deprecation window and import/entrypoint regression tests.
4. Remove compatibility modules only in a dedicated pull request.
