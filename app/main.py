import logging

from app.config import get_settings
from app.logger import setup_logging
from app.runner import StartupRunner
from app.runtime_diagnostics import get_runtime_diagnostics


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    diagnostics = get_runtime_diagnostics()
    diagnostics.install()
    try:
        runner = StartupRunner(settings=settings)
        runner.run()
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
