from app.config import get_settings

settings = get_settings()

if settings.is_production:
    print("[CGC] running in PRODUCTION mode")
else:
    print("[CGC] running in DEVELOPMENT mode")

if settings.is_live_execution:
    print("[CGC] LIVE EXECUTION ENABLED")
else:
    print("[CGC] DRY RUN / SAFE EXECUTION MODE")
