"""Launch-time live-capability check that uses the application's OWN settings resolution.

`scripts/start_bot.sh` must refuse to start when the environment it would hand to `python -m app.main` is
live-capable. That decision used to be re-implemented in shell (awk over `.env`) and it diverged from the app in
three constructed cases:

  * duplicate variable names that differ only by case (the guard read the FIRST, the app the LAST),
  * python-dotenv `${VAR}` interpolation inside `.env`,
  * pydantic's boolean tokens (`t` / `T` are true for the app, not for the shell list).

Two parsers whose interpretations can diverge are exactly how a launcher waves a live environment through, so
there is now ONE: this module builds the very same `Settings` the app builds -- same class, same `.env` (in the
current directory), same OS environment, same validators -- and reports what it resolved. It prints a verdict and
reason NAMES only, never a value.

    exit 0   LIVE_CAPABLE=NO
    exit 10  LIVE_CAPABLE=YES reasons=...
    exit 11  LIVE_CAPABLE=UNKNOWN  (the settings cannot be resolved: fail closed)
"""
from __future__ import annotations

import sys

EXIT_NOT_LIVE_CAPABLE, EXIT_LIVE_CAPABLE, EXIT_UNRESOLVABLE = 0, 10, 11


def resolve_live_capability() -> tuple[str, list[str]]:
    from app.config import Settings

    try:
        settings = Settings()
    except Exception as exc:                                    # noqa: BLE001 - never echo the message: it may carry values
        fields: list[str] = []
        try:
            fields = sorted({str(e["loc"][0]) for e in exc.errors()})       # pydantic ValidationError: field NAMES only
        except Exception:                                                     # noqa: BLE001
            pass
        return "UNKNOWN", [f"settings_unresolvable:{type(exc).__name__}" + (f":{','.join(fields)}" if fields else "")]
    reasons: list[str] = []
    if str(settings.execution_mode).strip().upper() == "LIVE":
        reasons.append("EXECUTION_MODE=LIVE")
    if settings.execution_enabled:
        reasons.append("EXECUTION_ENABLED=true")
    if settings.is_live_execution:
        reasons.append("is_live_execution")
    for name, secret in (("BITGET_API_KEY", settings.bitget_api_key), ("BITGET_API_SECRET", settings.bitget_api_secret),
                         ("BITGET_API_PASSPHRASE", settings.bitget_api_passphrase)):
        if secret.get_secret_value().strip():
            reasons.append(f"{name}(non-blank)")
    return ("YES" if reasons else "NO"), reasons


def main() -> int:
    verdict, reasons = resolve_live_capability()
    print(f"LIVE_CAPABLE={verdict}" + (f" reasons={','.join(reasons)}" if reasons else ""))
    return {"NO": EXIT_NOT_LIVE_CAPABLE, "YES": EXIT_LIVE_CAPABLE}.get(verdict, EXIT_UNRESOLVABLE)


if __name__ == "__main__":
    sys.exit(main())
