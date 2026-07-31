"""Secret-safe configuration attestation for a later authorised deployment."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.symbol_allowlist import parse_symbol_allowlist


SAFE_KEYS = frozenset({
    "DEFAULT_LEVERAGE",
    "MAX_LEVERAGE",
    "ACCOUNT_RISK_PER_TRADE_PCT",
    "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT",
    "MAX_OPEN_POSITIONS",
    "EXECUTION_MAX_PER_CYCLE",
    "PRODUCTION_SYMBOL_ALLOWLIST",
    "MAX_SYMBOLS",
    "ALLOW_AUTO_WATCHLIST_REFRESH",
    "EXECUTION_REQUIRE_CONFIRMATION",
})
SECRET_MARKERS = (
    "KEY", "SECRET", "PASSWORD", "PASSPHRASE", "TOKEN", "WEBHOOK", "CREDENTIAL",
)


@dataclass(frozen=True, slots=True)
class ConfigExpectations:
    checksum_sha256: str
    default_leverage: float
    max_leverage: float
    risk_per_trade_pct: float
    notional_cap_usdt: float
    symbols: tuple[str, ...]
    max_open_positions: int = 1


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_selected_values(content: bytes) -> tuple[dict[str, str], list[str]]:
    selected: dict[str, str] = {}
    redacted_keys: list[str] = []
    text = content.decode("utf-8")
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, raw_value = line.split("=", 1)
        key = key.strip().upper()
        if not key:
            continue
        if key in SAFE_KEYS:
            selected[key] = _unquote(raw_value)
        elif any(marker in key for marker in SECRET_MARKERS):
            redacted_keys.append(key)
    return selected, sorted(set(redacted_keys))


def _float(values: dict[str, str], key: str, errors: list[str]) -> float | None:
    try:
        return float(values[key])
    except KeyError:
        errors.append(f"missing:{key}")
    except (TypeError, ValueError):
        errors.append(f"invalid_numeric:{key}")
    return None


def _int(values: dict[str, str], key: str, errors: list[str]) -> int | None:
    try:
        return int(values[key])
    except KeyError:
        errors.append(f"missing:{key}")
    except (TypeError, ValueError):
        errors.append(f"invalid_integer:{key}")
    return None


def _bool(values: dict[str, str], key: str, errors: list[str]) -> bool | None:
    raw = values.get(key)
    if raw is None:
        errors.append(f"missing:{key}")
        return None
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    errors.append(f"invalid_boolean:{key}")
    return None


def attest_config_file(path: str | Path, expected: ConfigExpectations) -> dict[str, Any]:
    """Return only allow-listed values, a checksum, redaction state and verdict."""
    content = Path(path).read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    values, redacted_keys = _parse_selected_values(content)
    errors: list[str] = []

    default_leverage = _float(values, "DEFAULT_LEVERAGE", errors)
    max_leverage = _float(values, "MAX_LEVERAGE", errors)
    risk_pct = _float(values, "ACCOUNT_RISK_PER_TRADE_PCT", errors)
    notional_cap = _float(values, "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT", errors)
    max_open = _int(values, "MAX_OPEN_POSITIONS", errors)
    execution_max = _int(values, "EXECUTION_MAX_PER_CYCLE", errors)
    max_symbols = _int(values, "MAX_SYMBOLS", errors)
    auto_refresh = _bool(values, "ALLOW_AUTO_WATCHLIST_REFRESH", errors)
    confirmation = _bool(values, "EXECUTION_REQUIRE_CONFIRMATION", errors)
    try:
        symbols = parse_symbol_allowlist(
            values.get("PRODUCTION_SYMBOL_ALLOWLIST", ""),
            required=True,
        )
    except ValueError:
        symbols = ()
        errors.append("invalid:PRODUCTION_SYMBOL_ALLOWLIST")

    comparisons = {
        "checksum_sha256": hmac.compare_digest(
            checksum.lower(), str(expected.checksum_sha256).lower()
        ),
        "default_leverage": default_leverage == float(expected.default_leverage),
        "max_leverage": max_leverage == float(expected.max_leverage),
        "risk_per_trade_pct": risk_pct == float(expected.risk_per_trade_pct),
        "notional_cap_usdt": notional_cap == float(expected.notional_cap_usdt),
        "max_open_positions": max_open == int(expected.max_open_positions) == 1,
        "execution_max_per_cycle": execution_max == 1,
        "symbols": tuple(symbols) == tuple(expected.symbols),
        "max_symbols": max_symbols == len(symbols),
        "auto_watchlist_refresh_disabled": auto_refresh is False,
        "execution_confirmation_required": confirmation is True,
    }
    for key, matches in comparisons.items():
        if not matches:
            errors.append(f"expectation_mismatch:{key}")

    safe_values: dict[str, Any] = {
        "DEFAULT_LEVERAGE": default_leverage,
        "MAX_LEVERAGE": max_leverage,
        "ACCOUNT_RISK_PER_TRADE_PCT": risk_pct,
        "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT": notional_cap,
        "MAX_OPEN_POSITIONS": max_open,
        "EXECUTION_MAX_PER_CYCLE": execution_max,
        "PRODUCTION_SYMBOL_ALLOWLIST": list(symbols),
        "MAX_SYMBOLS": max_symbols,
        "ALLOW_AUTO_WATCHLIST_REFRESH": auto_refresh,
        "EXECUTION_REQUIRE_CONFIRMATION": confirmation,
    }
    return {
        "attestation_kind": "SAFE_CONFIG_PREDEPLOY",
        "deployment_gate": "PASS" if not errors else "BLOCKED",
        "checksum_sha256": checksum,
        "safe_values": safe_values,
        "redacted": {key: "REDACTED_PRESENT" for key in redacted_keys},
        "comparisons": comparisons,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Attest deployment config without printing secret values."
    )
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-default-leverage", required=True, type=float)
    parser.add_argument("--expected-max-leverage", required=True, type=float)
    parser.add_argument("--expected-risk-pct", required=True, type=float)
    parser.add_argument("--expected-notional-cap", required=True, type=float)
    parser.add_argument("--expected-symbols", required=True)
    args = parser.parse_args()
    expected = ConfigExpectations(
        checksum_sha256=args.expected_sha256,
        default_leverage=args.expected_default_leverage,
        max_leverage=args.expected_max_leverage,
        risk_per_trade_pct=args.expected_risk_pct,
        notional_cap_usdt=args.expected_notional_cap,
        symbols=parse_symbol_allowlist(args.expected_symbols, required=True),
    )
    result = attest_config_file(args.env_file, expected)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["deployment_gate"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ConfigExpectations", "SAFE_KEYS", "attest_config_file"]
