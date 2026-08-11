import logging

from app.config import get_settings
from app.logger import setup_logging
from app.runner import StartupRunner
from app.runtime_diagnostics import get_runtime_diagnostics
from execution.executor_identity import (
    ExecutionIdentity,
    ExecutionOwnershipError,
    single_live_executor_lock,
)


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    diagnostics = get_runtime_diagnostics()
    diagnostics.install()
    try:
        if settings.is_live_execution:
            identity = ExecutionIdentity.from_settings(settings)
            logging.getLogger("app.main").critical(
                "LIVE_EXECUTOR_IDENTITY | executor_id=%s | host_id=%s | pid=%s | "
                "production_sha=%s | credential_fingerprint=%s | client_namespace=%s",
                identity.executor_id, identity.host_id, identity.pid,
                identity.production_sha, identity.credential_fingerprint,
                identity.client_id_namespace,
            )
            with single_live_executor_lock(identity):
                StartupRunner(settings=settings).run()
        else:
            StartupRunner(settings=settings).run()
    except ExecutionOwnershipError as exc:
        logging.getLogger("app.main").critical(
            "LIVE_EXECUTOR_OWNERSHIP_BLOCKED | error=%s", exc
        )
        diagnostics.record_shutdown("execution_ownership_blocked", exit_code=73)
        raise SystemExit(73) from exc
    except SystemExit as exc:
        code = int(exc.code) if isinstance(exc.code, int) else 1
        diagnostics.record_shutdown("system_exit", exit_code=code)
        raise
    except BaseException as exc:
        logging.getLogger("app.main").exception(
            "RUNTIME_TOP_LEVEL_EXCEPTION | type=%s", type(exc).__name__
        )
        diagnostics.record_shutdown(
            f"uncaught_exception:{type(exc).__name__}", exit_code=1
        )
        raise
    else:
        diagnostics.record_shutdown("main_loop_returned", exit_code=0)


if __name__ == "__main__":
    main()
