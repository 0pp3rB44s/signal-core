"""Secret-safe, fail-closed configuration attestation for LIVE deployment."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import subprocess
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.symbol_allowlist import OWNER_APPROVED_PRODUCTION_SYMBOLS, parse_symbol_allowlist
from app.config import LIVE_MAX_EXECUTIONS_PER_CYCLE, LIVE_MAX_OPEN_POSITIONS


SAFE_KEYS = frozenset({
    "APP_ENV",
    "EXECUTION_ENABLED",
    "EXECUTION_MODE",
    "EXECUTION_MARGIN_MODE",
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
    "BREAK_EVEN_OPEN_FEE_FALLBACK_RATE",
    "BREAK_EVEN_EXPECTED_CLOSE_FEE_RATE",
    "BREAK_EVEN_SPREAD_BUFFER_PCT",
    "BREAK_EVEN_SLIPPAGE_BUFFER_PCT",
    "BREAK_EVEN_EXTRA_BUFFER_PCT",
    "BREAK_EVEN_FEE_BUFFER_PCT",
    "BREAK_EVEN_MARK_SAFETY_TICKS",
    "EXECUTOR_ID",
    "HOST_ID",
    "STRATEGY_ISOLATION_ENABLED",
    "ENABLED_STRATEGIES",
    "OLD_STRATEGIES_NEW_ENTRIES_ENABLED",
    "DYNAMIC_GRID_ENABLED",
    "DYNAMIC_GRID_MODE",
    "MAKER_ENTRY_FALLBACK_MARKET",
})
SECRET_MARKERS = (
    "KEY", "SECRET", "PASSWORD", "PASSPHRASE", "TOKEN", "WEBHOOK", "CREDENTIAL",
)
LEGACY_SCOPE_KEYS = ("WATCHLIST", "EXECUTION_CONFIRM_SYMBOLS")
EXPLICIT_BTC_OVERRIDE_KEYS = (
    "BTC_ONLY",
    "BTC_ONLY_MODE",
    "FORCE_BTC_ONLY",
    "LIVE_SYMBOL",
    "EXECUTION_SYMBOL",
    "PRODUCTION_SYMBOL",
)
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$", flags=re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ConfigExpectations:
    checksum_sha256: str
    release_sha: str
    default_leverage: float
    max_leverage: float
    risk_per_trade_pct: float
    notional_cap_usdt: float
    symbols: tuple[str, ...]
    break_even_open_fee_fallback_rate: float
    break_even_expected_close_fee_rate: float
    break_even_spread_buffer_pct: float
    break_even_slippage_buffer_pct: float
    break_even_extra_buffer_pct: float
    break_even_fee_buffer_pct: float
    break_even_mark_safety_ticks: int
    max_open_positions: int = LIVE_MAX_OPEN_POSITIONS
    execution_max_per_cycle: int = LIVE_MAX_EXECUTIONS_PER_CYCLE
    execution_margin_mode: str = "isolated"


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _parse_values(content: bytes) -> tuple[dict[str, str], int]:
    values: dict[str, str] = {}
    redacted_count = 0
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
        values[key] = _unquote(raw_value)
        if key not in SAFE_KEYS and any(marker in key for marker in SECRET_MARKERS):
            redacted_count += 1
    return values, redacted_count


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


def _schema_valid(values: dict[str, str]) -> bool:
    try:
        from app.config import Settings

        # model_validate consumes the supplied mapping only; unlike constructing
        # BaseSettings it never consults the process environment or an env file.
        Settings.model_validate(values)
    except Exception:
        return False
    return True


def _is_truthy(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on", "btc", "btcusdt"}


def _btc_override_absent(values: dict[str, str]) -> bool:
    for key in EXPLICIT_BTC_OVERRIDE_KEYS:
        raw = values.get(key, "")
        if raw and _is_truthy(raw):
            return False
    for key in LEGACY_SCOPE_KEYS:
        raw = values.get(key, "").strip()
        if not raw:
            continue
        try:
            symbols = parse_symbol_allowlist(raw, required=False)
        except ValueError:
            return False
        if symbols == ("BTCUSDT",):
            return False
    return True


def _actual_release_sha() -> str:
    repo = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.strip()


def _break_even_examples(
    *,
    open_rate: float | None,
    close_rate: float | None,
    spread_pct: float | None,
    slippage_pct: float | None,
    extra_pct: float | None,
    legacy_pct: float | None,
) -> dict[str, Any]:
    try:
        from execution.position_model import (
            CONFIGURED_FALLBACK,
            LEGACY_FALLBACK,
            OpeningFeeSelection,
            calculate_break_even_plus_fees,
        )

        supplied = (open_rate, close_rate, spread_pct, slippage_pct, extra_pct, legacy_pct)
        if any(value is None for value in supplied):
            raise ValueError("BE config incomplete")
        entry = Decimal("100")
        quantity = Decimal("1")
        tick = Decimal("0.01")
        opening_rate = Decimal(str(open_rate))
        opening_fee = OpeningFeeSelection(
            entry * quantity * opening_rate,
            CONFIGURED_FALLBACK,
            opening_rate,
        )

        def calculate(direction: str, opening: OpeningFeeSelection):
            return calculate_break_even_plus_fees(
                direction=direction,
                exchange_entry=entry,
                remaining_quantity=quantity,
                tick_size=tick,
                opening_fee=opening,
                expected_close_fee_rate=Decimal(str(close_rate)),
                spread_buffer_pct=Decimal(str(spread_pct)),
                slippage_buffer_pct=Decimal(str(slippage_pct)),
                extra_buffer_pct=Decimal(str(extra_pct)),
                legacy_fee_buffer_pct=Decimal(str(legacy_pct)),
            )

        legacy_opening = OpeningFeeSelection(Decimal("0"), LEGACY_FALLBACK, Decimal("0"))
        legacy_long = calculate("LONG", legacy_opening)
        legacy_short = calculate("SHORT", legacy_opening)
        itemised_long = calculate("LONG", opening_fee)
        itemised_short = calculate("SHORT", opening_fee)
        return {
            "status": "AVAILABLE",
            "semantic": "BE_PLUS_FEES",
            "entry": 100.0,
            "quantity": 1.0,
            "tick_size": 0.01,
            "before_legacy": {
                "long_target": float(legacy_long.target),
                "short_target": float(legacy_short.target),
            },
            "after_itemised": {
                "long_target": float(itemised_long.target),
                "long_expected_net_usdt": float(itemised_long.expected_net_usdt),
                "short_target": float(itemised_short.target),
                "short_expected_net_usdt": float(itemised_short.expected_net_usdt),
                "opening_fee_usdt": float(itemised_long.opening_fee_usdt),
                "components": [
                    "opening_fee",
                    "expected_closing_fee",
                    "spread_allowance",
                    "slippage_allowance",
                    "extra_safety_allowance",
                ],
            },
            "cost_covering": bool(
                itemised_long.expected_net_usdt >= 0
                and itemised_short.expected_net_usdt >= 0
            ),
        }
    except Exception:
        return {"status": "UNAVAILABLE", "semantic": "BE_PLUS_FEES", "cost_covering": False}


def attest_config_file(
    path: str | Path,
    expected: ConfigExpectations,
    *,
    actual_release_sha: str | None = None,
) -> dict[str, Any]:
    """Attest the complete schema while serializing only approved safe fields."""
    content = Path(path).read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    values, redacted_count = _parse_values(content)
    errors: list[str] = []

    release_sha = str(actual_release_sha or _actual_release_sha()).strip().lower()
    expected_sha = str(expected.release_sha).strip().lower()
    default_leverage = _float(values, "DEFAULT_LEVERAGE", errors)
    max_leverage = _float(values, "MAX_LEVERAGE", errors)
    risk_pct = _float(values, "ACCOUNT_RISK_PER_TRADE_PCT", errors)
    notional_cap = _float(values, "EXECUTION_MAX_LIVE_NOTIONAL_PER_TRADE_USDT", errors)
    max_open = _int(values, "MAX_OPEN_POSITIONS", errors)
    execution_max = _int(values, "EXECUTION_MAX_PER_CYCLE", errors)
    max_symbols = _int(values, "MAX_SYMBOLS", errors)
    auto_refresh = _bool(values, "ALLOW_AUTO_WATCHLIST_REFRESH", errors)
    confirmation = _bool(values, "EXECUTION_REQUIRE_CONFIRMATION", errors)
    execution_enabled = _bool(values, "EXECUTION_ENABLED", errors)
    be_open_rate = _float(values, "BREAK_EVEN_OPEN_FEE_FALLBACK_RATE", errors)
    be_close_rate = _float(values, "BREAK_EVEN_EXPECTED_CLOSE_FEE_RATE", errors)
    be_spread_pct = _float(values, "BREAK_EVEN_SPREAD_BUFFER_PCT", errors)
    be_slippage_pct = _float(values, "BREAK_EVEN_SLIPPAGE_BUFFER_PCT", errors)
    be_extra_pct = _float(values, "BREAK_EVEN_EXTRA_BUFFER_PCT", errors)
    be_legacy_pct = _float(values, "BREAK_EVEN_FEE_BUFFER_PCT", errors)
    be_safety_ticks = _int(values, "BREAK_EVEN_MARK_SAFETY_TICKS", errors)
    try:
        symbols = parse_symbol_allowlist(
            values.get("PRODUCTION_SYMBOL_ALLOWLIST", ""),
            required=True,
        )
    except ValueError:
        symbols = ()
        errors.append("invalid:PRODUCTION_SYMBOL_ALLOWLIST")

    schema_valid = _schema_valid(values)
    be_examples = _break_even_examples(
        open_rate=be_open_rate,
        close_rate=be_close_rate,
        spread_pct=be_spread_pct,
        slippage_pct=be_slippage_pct,
        extra_pct=be_extra_pct,
        legacy_pct=be_legacy_pct,
    )
    be_rates_valid = bool(
        be_open_rate is not None
        and be_open_rate > 0
        and be_close_rate is not None
        and be_close_rate > 0
        and be_spread_pct is not None
        and be_spread_pct >= 0
        and be_slippage_pct is not None
        and be_slippage_pct >= 0
        and be_extra_pct is not None
        and be_extra_pct >= 0
        and be_legacy_pct is not None
        and be_legacy_pct > 0
        and be_safety_ticks is not None
        and be_safety_ticks >= 0
        and (
            be_close_rate
            + (be_spread_pct + be_slippage_pct + be_extra_pct) / 100.0
        ) < 1.0
    )
    comparisons = {
        "release_sha_valid": bool(SHA_PATTERN.fullmatch(release_sha)),
        "release_sha": hmac.compare_digest(release_sha, expected_sha),
        "checksum_sha256": hmac.compare_digest(
            checksum.lower(), str(expected.checksum_sha256).lower()
        ),
        "full_settings_schema": schema_valid,
        "app_env_production": values.get("APP_ENV", "").strip().lower() == "production",
        "execution_live": (
            execution_enabled is True
            and values.get("EXECUTION_MODE", "").strip().upper() == "LIVE"
        ),
        "isolated_margin_mode": (
            values.get("EXECUTION_MARGIN_MODE", "").strip().lower()
            == str(expected.execution_margin_mode).strip().lower()
            == "isolated"
        ),
        "default_leverage": default_leverage == float(expected.default_leverage),
        "max_leverage": max_leverage == float(expected.max_leverage),
        "leverage_values_valid": bool(
            default_leverage is not None
            and max_leverage is not None
            and 0 < default_leverage <= max_leverage
        ),
        "risk_per_trade_pct": risk_pct == float(expected.risk_per_trade_pct),
        "risk_per_trade_valid": bool(risk_pct is not None and 0 < risk_pct <= 100),
        "notional_cap_usdt": notional_cap == float(expected.notional_cap_usdt),
        "notional_cap_valid": bool(notional_cap is not None and notional_cap > 0),
        "max_open_positions": (
            max_open == int(expected.max_open_positions) == LIVE_MAX_OPEN_POSITIONS
        ),
        "execution_max_per_cycle": (
            execution_max == int(expected.execution_max_per_cycle)
            == LIVE_MAX_EXECUTIONS_PER_CYCLE
        ),
        "symbols": tuple(symbols) == tuple(expected.symbols),
        "owner_approved_symbols": tuple(symbols) == OWNER_APPROVED_PRODUCTION_SYMBOLS,
        "symbol_count": len(symbols) == len(expected.symbols),
        "max_symbols": max_symbols == len(symbols),
        "canonical_allowlist_runtime_authoritative": bool(symbols),
        "btc_only_override_absent": _btc_override_absent(values),
        "auto_watchlist_refresh_disabled": auto_refresh is False,
        "execution_confirmation_required": confirmation is True,
        "microflow_isolated": (
            values.get("STRATEGY_ISOLATION_ENABLED", "").strip().lower() == "true"
            and values.get("ENABLED_STRATEGIES", "").strip().lower() == "microflow_scalper_v1"
            and values.get("MICROFLOW_SCALPER_ENABLED", "").strip().lower() == "true"
            and tuple(parse_symbol_allowlist(values.get("MICROFLOW_SYMBOLS", "")))
            == OWNER_APPROVED_PRODUCTION_SYMBOLS
            and values.get("OLD_STRATEGIES_NEW_ENTRIES_ENABLED", "").strip().lower() == "false"
            and values.get("DYNAMIC_GRID_ENABLED", "").strip().lower() == "false"
            and values.get("DYNAMIC_GRID_MODE", "").strip().upper() == "OFF"
            and values.get("MAKER_ENTRY_FALLBACK_MARKET", "").strip().lower() == "false"
        ),
        "break_even_open_fee_fallback_rate": (
            be_open_rate == float(expected.break_even_open_fee_fallback_rate)
        ),
        "break_even_expected_close_fee_rate": (
            be_close_rate == float(expected.break_even_expected_close_fee_rate)
        ),
        "break_even_spread_buffer_pct": (
            be_spread_pct == float(expected.break_even_spread_buffer_pct)
        ),
        "break_even_slippage_buffer_pct": (
            be_slippage_pct == float(expected.break_even_slippage_buffer_pct)
        ),
        "break_even_extra_buffer_pct": (
            be_extra_pct == float(expected.break_even_extra_buffer_pct)
        ),
        "break_even_legacy_buffer_pct": (
            be_legacy_pct == float(expected.break_even_fee_buffer_pct)
        ),
        "break_even_mark_safety_ticks": (
            be_safety_ticks == int(expected.break_even_mark_safety_ticks)
        ),
        "break_even_rates_valid": be_rates_valid,
        "break_even_examples_cost_covering": be_examples["cost_covering"] is True,
    }
    for key, matches in comparisons.items():
        if not matches:
            errors.append(f"expectation_mismatch:{key}")

    return {
        "attestation_kind": "SAFE_CONFIG_PREDEPLOY",
        "deployment_gate": "PASS" if not errors else "FAIL",
        "release_sha": release_sha if SHA_PATTERN.fullmatch(release_sha) else "INVALID",
        "checksum_sha256": checksum,
        "secrets_redacted": True,
        "redacted_key_count": redacted_count,
        "allowlist": list(symbols),
        "allowlist_count": len(symbols),
        "portfolio": {
            "default_leverage": default_leverage,
            "max_leverage": max_leverage,
            "risk_per_trade_pct": risk_pct,
            "notional_cap_usdt": notional_cap,
            "max_open_positions": max_open,
            "execution_max_per_cycle": execution_max,
            "margin_mode": values.get("EXECUTION_MARGIN_MODE", "").strip().lower(),
        },
        "break_even": {
            "opening_fee_source_precedence": [
                "exchange_actual",
                "persisted_confirmed",
                "exchange_rate",
                "configured_fallback",
            ],
            "open_fee_fallback_rate": be_open_rate,
            "expected_close_fee_rate": be_close_rate,
            "spread_buffer_pct": be_spread_pct,
            "slippage_buffer_pct": be_slippage_pct,
            "extra_buffer_pct": be_extra_pct,
            "legacy_buffer_pct": be_legacy_pct,
            "mark_safety_ticks": be_safety_ticks,
            "semantic_example": be_examples,
        },
        "comparisons": comparisons,
        "errors": sorted(set(errors)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Attest deployment config without printing secret values."
    )
    parser.add_argument("--env-file", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-default-leverage", required=True, type=float)
    parser.add_argument("--expected-max-leverage", required=True, type=float)
    parser.add_argument("--expected-risk-pct", required=True, type=float)
    parser.add_argument("--expected-notional-cap", required=True, type=float)
    parser.add_argument("--expected-symbols", required=True)
    parser.add_argument("--expected-be-open-fee-rate", required=True, type=float)
    parser.add_argument("--expected-be-close-fee-rate", required=True, type=float)
    parser.add_argument("--expected-be-spread-pct", required=True, type=float)
    parser.add_argument("--expected-be-slippage-pct", required=True, type=float)
    parser.add_argument("--expected-be-extra-pct", required=True, type=float)
    parser.add_argument("--expected-be-legacy-pct", required=True, type=float)
    parser.add_argument("--expected-be-safety-ticks", required=True, type=int)
    args = parser.parse_args()
    expected = ConfigExpectations(
        checksum_sha256=args.expected_sha256,
        release_sha=args.release_sha,
        default_leverage=args.expected_default_leverage,
        max_leverage=args.expected_max_leverage,
        risk_per_trade_pct=args.expected_risk_pct,
        notional_cap_usdt=args.expected_notional_cap,
        symbols=parse_symbol_allowlist(args.expected_symbols, required=True),
        break_even_open_fee_fallback_rate=args.expected_be_open_fee_rate,
        break_even_expected_close_fee_rate=args.expected_be_close_fee_rate,
        break_even_spread_buffer_pct=args.expected_be_spread_pct,
        break_even_slippage_buffer_pct=args.expected_be_slippage_pct,
        break_even_extra_buffer_pct=args.expected_be_extra_pct,
        break_even_fee_buffer_pct=args.expected_be_legacy_pct,
        break_even_mark_safety_ticks=args.expected_be_safety_ticks,
    )
    result = attest_config_file(args.env_file, expected)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["deployment_gate"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["ConfigExpectations", "SAFE_KEYS", "attest_config_file"]
