from app.config import get_settings
from app.logger import setup_logging
from app.runner import StartupRunner


def main() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    runner = StartupRunner(settings=settings)
    runner.run()


if __name__ == "__main__":
    main()
