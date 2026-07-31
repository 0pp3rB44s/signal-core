"""Typed position economics and migration helpers.

The persisted position record is still JSON for operational compatibility, but
critical consumers must cross this module before they can use entry price or
position size.  That gives the migration one explicit contract:

* ``planned_avg_entry`` is planning data only;
* ``exchange_avg_entry`` is immutable, exchange-confirmed execution truth;
* ``avg_entry`` is a legacy alias for the planned value;
* ``actual_entry`` is telemetry only and is never promoted to exchange truth.
"""

from __future__ import annotations

import hashlib
import inspect
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any


ZERO = Decimal("0")
HUNDRED = Decimal("100")

EXCHANGE_ACTUAL = "EXCHANGE_ACTUAL"
EXCHANGE_RATE = "EXCHANGE_RATE"
CONFIGURED_FALLBACK = "CONFIGURED_FALLBACK"
LEGACY_FALLBACK = "LEGACY_FALLBACK"

INITIAL_PROTECTION_CONFIRMED = "INITIAL_PROTECTION_CONFIRMED"
BE_PLUS_FEES_PENDING = "BE_PLUS_FEES_PENDING"
BE_PLUS_FEES_CONFIRMED = "BE_PLUS_FEES_CONFIRMED"
PROFIT_LOCK_PENDING = "PROFIT_LOCK_PENDING"
PROFIT_LOCK_CONFIRMED = "PROFIT_LOCK_CONFIRMED"
TRAILING_PENDING = "TRAILING_PENDING"
TRAILING_CONFIRMED = "TRAILING_CONFIRMED"
PROTECTION_UPDATE_FAILED = "PROTECTION_UPDATE_FAILED"

_EXCHANGE_ENTRY_KEYS = (
    "openPriceAvg",
    "averageOpenPrice",
    "avgOpenPrice",
    "openAvgPrice",
    "entryPrice",
    "avgEntryPrice",
)
_EXCHANGE_SIZE_KEYS = (
    "total",
    "size",
    "available",
    "holdVol",
    "positionSize",
    "availableSize",
)
_LEGACY_WARNING_KEYS: set[tuple[str, str, str, str]] = set()


class PositionModelError(RuntimeError):
    """Base error for fail-closed position model decisions."""


class AuthoritativeEntryUnavailable(PositionModelError):
    """The exchange-confirmed entry required by a critical path is unavailable."""


class ConfirmedSizeUnavailable(PositionModelError):
    """No exchange-confirmed or lifecycle-confirmed quantity is available."""


class PositionLifecycleMismatch(PositionModelError):
    """Local state and the current exchange position cannot be the same lifecycle."""


class CriticalLegacyRead(PositionModelError):
    """Development assertion for a legacy price read from a critical module."""


class PlannedPriceOnExecutedPosition(PositionModelError):
    """Development assertion for planned data used after execution confirmation."""


def decimal_value(value: Any, default: Decimal = ZERO) -> Decimal:
    if value in (None, ""):
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return parsed if parsed.is_finite() else default


def decimal_float(value: Decimal, places: int = 12) -> float:
    return round(float(value), places)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def position_lifecycle_id(
    *,
    plan_id: str,
    symbol: str,
    direction: str,
    client_oid: str = "",
    order_id: str = "",
) -> str:
    """Create a non-secret lifecycle identity stable across restarts."""
    material = "|".join(
        (
            "position-lifecycle-v1",
            str(plan_id or "").strip(),
            str(symbol or "").strip().upper(),
            str(direction or "").strip().upper(),
            str(client_oid or "").strip(),
            str(order_id or "").strip(),
        )
    )
    return f"pos-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


@dataclass(frozen=True, slots=True)
class AuthoritativeEntry:
    price: Decimal
    source: str
    confirmed_at: str
    order_id: str
    client_oid: str
    lifecycle_id: str


@dataclass(frozen=True, slots=True)
class PositionPrices:
    planned: Decimal
    executed: AuthoritativeEntry | None

    def require_executed(self) -> AuthoritativeEntry:
        if self.executed is None or self.executed.price <= ZERO:
            raise AuthoritativeEntryUnavailable("exchange_avg_entry is required but unavailable")
        return self.executed

    def planning_price(self, *, development_assertions: bool = False) -> Decimal:
        if development_assertions and self.executed is not None:
            raise PlannedPriceOnExecutedPosition(
                "planned_avg_entry used on a position that already has exchange execution truth"
            )
        return self.planned


@dataclass(frozen=True, slots=True)
class ConfirmedPositionSize:
    quantity: Decimal
    source: str
    estimated: bool = False


@dataclass(frozen=True, slots=True)
class PositionEconomics:
    price_return_pct: Decimal
    margin_roi_pct: Decimal
    estimated_net_return_pct: Decimal
    gross_pnl_usdt: Decimal
    estimated_fees_usdt: Decimal
    estimated_net_pnl_usdt: Decimal


@dataclass(frozen=True, slots=True)
class OpeningFeeSelection:
    amount_usdt: Decimal
    source: str
    rate: Decimal


@dataclass(frozen=True, slots=True)
class BreakEvenResult:
    target: Decimal
    required_recovery_usdt: Decimal
    opening_fee_usdt: Decimal
    expected_closing_fee_usdt: Decimal
    spread_allowance_usdt: Decimal
    slippage_allowance_usdt: Decimal
    extra_safety_allowance_usdt: Decimal
    expected_net_usdt: Decimal
    fee_source: str
    used_legacy_fallback: bool


def development_assertions_enabled(settings: Any) -> bool:
    enabled = bool(getattr(settings, "position_model_dev_assertions", False))
    execution_mode = str(getattr(settings, "execution_mode", "") or "").upper()
    app_env = str(getattr(settings, "app_env", "") or "").lower()
    return enabled and execution_mode != "LIVE" and app_env in {"dev", "development", "test"}


def migrate_planned_entry(position: dict[str, Any]) -> bool:
    """Populate the planning field from the legacy planning alias only.

    ``actual_entry`` is intentionally absent from this migration.
    """
    changed = False
    planned = decimal_value(position.get("planned_avg_entry"))
    legacy_planned = decimal_value(position.get("avg_entry"))
    if planned <= ZERO and legacy_planned > ZERO:
        planned = legacy_planned
        position["planned_avg_entry"] = decimal_float(planned)
        changed = True
    if planned > ZERO and decimal_value(position.get("avg_entry")) != planned:
        position["avg_entry"] = decimal_float(planned)
        changed = True
    return changed


def position_prices(position: dict[str, Any], *, require_executed: bool = False) -> PositionPrices:
    migrate_planned_entry(position)
    planned = decimal_value(position.get("planned_avg_entry"))
    executed_price = decimal_value(position.get("exchange_avg_entry"))
    executed: AuthoritativeEntry | None = None
    if executed_price > ZERO:
        executed = AuthoritativeEntry(
            price=executed_price,
            source=str(position.get("exchange_avg_entry_source") or ""),
            confirmed_at=str(position.get("exchange_avg_entry_confirmed_at") or ""),
            order_id=str(position.get("exchange_entry_order_id") or ""),
            client_oid=str(position.get("exchange_entry_client_oid") or ""),
            lifecycle_id=str(position.get("position_lifecycle_id") or ""),
        )
    prices = PositionPrices(planned=planned, executed=executed)
    if require_executed:
        prices.require_executed()
    return prices


def _live_direction(live_position: dict[str, Any]) -> str:
    raw = str(
        live_position.get("holdSide")
        or live_position.get("posSide")
        or live_position.get("side")
        or live_position.get("direction")
        or ""
    ).lower()
    if "long" in raw or raw == "buy":
        return "LONG"
    if "short" in raw or raw == "sell":
        return "SHORT"
    return ""


def _live_symbol(live_position: dict[str, Any]) -> str:
    for key in ("symbol", "instId", "symbolName", "contractSymbol"):
        value = str(live_position.get(key) or "").upper()
        if value:
            return value
    return ""


def _first_decimal(source: dict[str, Any], keys: tuple[str, ...]) -> Decimal:
    for key in keys:
        value = decimal_value(source.get(key))
        if value > ZERO:
            return value
    return ZERO


def confirm_exchange_position(
    position: dict[str, Any],
    live_position: dict[str, Any],
    *,
    source: str,
    confirmed_at: str | None = None,
    order_id: str = "",
    client_oid: str = "",
) -> AuthoritativeEntry:
    """Validate and persist exchange entry/size without legacy inference."""
    migrate_planned_entry(position)
    local_symbol = str(position.get("symbol") or "").upper()
    live_symbol = _live_symbol(live_position)
    local_direction = str(position.get("direction") or "").upper()
    live_direction = _live_direction(live_position)
    live_size = _first_decimal(live_position, _EXCHANGE_SIZE_KEYS)
    exchange_entry = _first_decimal(live_position, _EXCHANGE_ENTRY_KEYS)

    reasons: list[str] = []
    if not local_symbol or live_symbol != local_symbol:
        reasons.append(f"symbol:{local_symbol or '-'}!={live_symbol or '-'}")
    if local_direction not in {"LONG", "SHORT"} or live_direction != local_direction:
        reasons.append(f"side:{local_direction or '-'}!={live_direction or '-'}")
    if live_size <= ZERO:
        reasons.append("size_missing")
    if exchange_entry <= ZERO:
        reasons.append("openPriceAvg_missing")

    lifecycle_id = str(position.get("position_lifecycle_id") or "")
    live_lifecycle = str(
        live_position.get("positionLifecycleId")
        or live_position.get("position_lifecycle_id")
        or ""
    )
    if live_lifecycle and lifecycle_id and live_lifecycle != lifecycle_id:
        reasons.append("lifecycle_id_mismatch")

    persisted_order_id = str(position.get("exchange_entry_order_id") or "")
    live_order_id = str(
        live_position.get("entryOrderId")
        or live_position.get("orderId")
        or order_id
        or ""
    )
    if persisted_order_id and live_order_id and persisted_order_id != live_order_id:
        reasons.append("entry_order_id_mismatch")

    persisted_client_oid = str(position.get("exchange_entry_client_oid") or "")
    live_client_oid = str(
        live_position.get("entryClientOid")
        or live_position.get("clientOid")
        or client_oid
        or ""
    )
    if persisted_client_oid and live_client_oid and persisted_client_oid != live_client_oid:
        reasons.append("entry_client_oid_mismatch")

    existing_entry = decimal_value(position.get("exchange_avg_entry"))
    if existing_entry > ZERO and exchange_entry > ZERO and existing_entry != exchange_entry:
        reasons.append(f"immutable_entry_changed:{existing_entry}!={exchange_entry}")

    if reasons:
        raise PositionLifecycleMismatch(";".join(reasons))

    if not lifecycle_id:
        plan_id = str(position.get("plan_id") or position.get("candidate_id") or position.get("opened_at") or "")
        lifecycle_id = position_lifecycle_id(
            plan_id=plan_id,
            symbol=local_symbol,
            direction=local_direction,
            client_oid=live_client_oid,
            order_id=live_order_id,
        )

    confirmation_time = str(
        position.get("exchange_avg_entry_confirmed_at")
        or confirmed_at
        or utc_now_iso()
    )
    position["exchange_avg_entry"] = decimal_float(exchange_entry)
    position["exchange_avg_entry_source"] = str(
        position.get("exchange_avg_entry_source") or source
    )
    position["exchange_avg_entry_confirmed_at"] = confirmation_time
    position["exchange_entry_order_id"] = persisted_order_id or live_order_id
    position["exchange_entry_client_oid"] = persisted_client_oid or live_client_oid
    position["position_lifecycle_id"] = lifecycle_id
    position["confirmed_position_size"] = decimal_float(
        max(decimal_value(position.get("confirmed_position_size")), live_size)
    )
    position["confirmed_remaining_size"] = decimal_float(live_size)
    position["confirmed_remaining_size_source"] = "BITGET_OPEN_POSITION"
    position["exchange_live_size"] = decimal_float(live_size)

    return position_prices(position, require_executed=True).require_executed()


def confirmed_position_size(
    position: dict[str, Any],
    *,
    live_position: dict[str, Any] | None = None,
    critical: bool = True,
) -> ConfirmedPositionSize:
    if live_position:
        local_symbol = str(position.get("symbol") or "").upper()
        local_direction = str(position.get("direction") or "").upper()
        if _live_symbol(live_position) != local_symbol or _live_direction(live_position) != local_direction:
            raise PositionLifecycleMismatch("live size belongs to another symbol/side lifecycle")
        live_size = _first_decimal(live_position, _EXCHANGE_SIZE_KEYS)
        if live_size > ZERO:
            return ConfirmedPositionSize(live_size, "CURRENT_EXCHANGE_OPEN_SIZE")

    for key, source in (
        ("confirmed_remaining_size", "PERSISTED_EXCHANGE_REMAINING_SIZE"),
        ("confirmed_fill_quantity", "CONFIRMED_FILL_QUANTITY"),
        ("confirmed_position_size", "PERSISTED_LIFECYCLE_QUANTITY"),
    ):
        quantity = decimal_value(position.get(key))
        if quantity > ZERO:
            return ConfirmedPositionSize(quantity, source)

    if critical:
        raise ConfirmedSizeUnavailable("critical protection size has no exchange-confirmed source")

    planned_notional = decimal_value(position.get("position_notional_usdt"))
    planned_entry = position_prices(position).planned
    if planned_notional > ZERO and planned_entry > ZERO:
        return ConfirmedPositionSize(
            planned_notional / planned_entry,
            "ESTIMATED_PLANNED_NOTIONAL_DIV_PLANNED_ENTRY",
            estimated=True,
        )
    return ConfirmedPositionSize(ZERO, "UNAVAILABLE", estimated=True)


def legacy_avg_entry(
    position: dict[str, Any],
    *,
    module: str,
    function: str,
    logger: logging.Logger,
    critical: bool = False,
    development_assertions: bool = False,
) -> Decimal:
    """Diagnostic-only compatibility read with process-level de-duplication."""
    symbol = str(position.get("symbol") or "UNKNOWN")
    lifecycle_id = str(position.get("position_lifecycle_id") or "UNKNOWN")
    key = (module, function, symbol, lifecycle_id)
    if key not in _LEGACY_WARNING_KEYS:
        caller = inspect.stack()[1]
        logger.warning(
            "LEGACY_AVG_ENTRY_READ | module=%s | function=%s | symbol=%s | lifecycle_id=%s | "
            "exchange_avg_entry_exists=%s | location=%s:%s",
            module,
            function,
            symbol,
            lifecycle_id,
            decimal_value(position.get("exchange_avg_entry")) > ZERO,
            caller.filename,
            caller.lineno,
        )
        _LEGACY_WARNING_KEYS.add(key)
    if critical and development_assertions:
        raise CriticalLegacyRead(f"critical legacy avg_entry read in {module}.{function}")
    return decimal_value(position.get("avg_entry"))


def price_return_pct(direction: str, exchange_entry: Decimal, current_price: Decimal) -> Decimal:
    if exchange_entry <= ZERO or current_price <= ZERO:
        raise AuthoritativeEntryUnavailable("price return requires a positive exchange entry and mark")
    direction = str(direction or "").upper()
    if direction == "LONG":
        return ((current_price - exchange_entry) / exchange_entry) * HUNDRED
    if direction == "SHORT":
        return ((exchange_entry - current_price) / exchange_entry) * HUNDRED
    raise PositionModelError(f"unsupported direction: {direction!r}")


def position_economics(
    *,
    direction: str,
    exchange_entry: Decimal,
    current_price: Decimal,
    remaining_quantity: Decimal,
    leverage: Decimal,
    opening_fee_usdt: Decimal = ZERO,
    expected_closing_fee_usdt: Decimal = ZERO,
) -> PositionEconomics:
    if remaining_quantity <= ZERO:
        raise ConfirmedSizeUnavailable("position economics require confirmed remaining quantity")
    return_pct = price_return_pct(direction, exchange_entry, current_price)
    gross_pnl = (
        (current_price - exchange_entry) * remaining_quantity
        if str(direction).upper() == "LONG"
        else (exchange_entry - current_price) * remaining_quantity
    )
    fees = abs(opening_fee_usdt) + abs(expected_closing_fee_usdt)
    net_pnl = gross_pnl - fees
    entry_notional = exchange_entry * remaining_quantity
    effective_leverage = leverage if leverage > ZERO else Decimal("1")
    margin = entry_notional / effective_leverage
    margin_roi = (net_pnl / margin) * HUNDRED if margin > ZERO else ZERO
    estimated_net_return = (net_pnl / entry_notional) * HUNDRED if entry_notional > ZERO else ZERO
    return PositionEconomics(
        price_return_pct=return_pct,
        margin_roi_pct=margin_roi,
        estimated_net_return_pct=estimated_net_return,
        gross_pnl_usdt=gross_pnl,
        estimated_fees_usdt=fees,
        estimated_net_pnl_usdt=net_pnl,
    )


def select_opening_fee(
    position: dict[str, Any],
    *,
    exchange_entry: Decimal,
    remaining_quantity: Decimal,
    configured_fallback_rate: Decimal,
) -> OpeningFeeSelection:
    actual_fee = decimal_value(position.get("exchange_opening_fee_usdt"))
    actual_source = str(position.get("exchange_opening_fee_source") or "")
    if actual_fee != ZERO and actual_source == EXCHANGE_ACTUAL:
        return OpeningFeeSelection(abs(actual_fee), EXCHANGE_ACTUAL, ZERO)

    persisted_fee = decimal_value(position.get("confirmed_opening_fee_usdt"))
    persisted_source = str(position.get("confirmed_opening_fee_source") or "")
    if persisted_fee != ZERO and persisted_source in {EXCHANGE_ACTUAL, "PERSISTED_CONFIRMED_EXECUTION_FEE"}:
        return OpeningFeeSelection(abs(persisted_fee), EXCHANGE_ACTUAL, ZERO)

    exchange_rate = decimal_value(position.get("exchange_open_fee_rate"))
    if exchange_rate > ZERO:
        return OpeningFeeSelection(
            exchange_entry * remaining_quantity * exchange_rate,
            EXCHANGE_RATE,
            exchange_rate,
        )

    if configured_fallback_rate > ZERO:
        return OpeningFeeSelection(
            exchange_entry * remaining_quantity * configured_fallback_rate,
            CONFIGURED_FALLBACK,
            configured_fallback_rate,
        )

    return OpeningFeeSelection(ZERO, LEGACY_FALLBACK, ZERO)


def _round_to_tick(value: Decimal, tick_size: Decimal, rounding: str) -> Decimal:
    if tick_size <= ZERO:
        raise PositionModelError("exchange tick size must be positive")
    ticks = (value / tick_size).to_integral_value(rounding=rounding)
    return ticks * tick_size


def calculate_break_even_plus_fees(
    *,
    direction: str,
    exchange_entry: Decimal,
    remaining_quantity: Decimal,
    tick_size: Decimal,
    opening_fee: OpeningFeeSelection,
    expected_close_fee_rate: Decimal,
    spread_buffer_pct: Decimal,
    slippage_buffer_pct: Decimal,
    extra_buffer_pct: Decimal,
    legacy_fee_buffer_pct: Decimal,
) -> BreakEvenResult:
    """Return a Decimal, cost-covering stop target.

    Percentage allowances are charged on expected exit notional.  The target is
    therefore solved algebraically (rather than estimating fees from entry
    notional), then rounded in the cost-covering direction.
    """
    direction = str(direction or "").upper()
    if direction not in {"LONG", "SHORT"}:
        raise PositionModelError(f"unsupported direction: {direction!r}")
    if exchange_entry <= ZERO:
        raise AuthoritativeEntryUnavailable("BE+fees requires exchange_avg_entry")
    if remaining_quantity <= ZERO:
        raise ConfirmedSizeUnavailable("BE+fees requires remaining exchange quantity")
    if tick_size <= ZERO:
        raise PositionModelError("BE+fees requires exchange tick size")

    if opening_fee.source == LEGACY_FALLBACK:
        if legacy_fee_buffer_pct <= ZERO:
            raise PositionModelError("itemised costs unavailable and legacy fallback is not configured")
        legacy_rate = legacy_fee_buffer_pct / HUNDRED
        raw_target = (
            exchange_entry * (Decimal("1") + legacy_rate)
            if direction == "LONG"
            else exchange_entry * (Decimal("1") - legacy_rate)
        )
        rounded_target = _round_to_tick(
            raw_target,
            tick_size,
            ROUND_CEILING if direction == "LONG" else ROUND_FLOOR,
        )
        gross = abs(rounded_target - exchange_entry) * remaining_quantity
        return BreakEvenResult(
            target=rounded_target,
            required_recovery_usdt=gross,
            opening_fee_usdt=ZERO,
            expected_closing_fee_usdt=ZERO,
            spread_allowance_usdt=ZERO,
            slippage_allowance_usdt=ZERO,
            extra_safety_allowance_usdt=ZERO,
            expected_net_usdt=ZERO,
            fee_source=LEGACY_FALLBACK,
            used_legacy_fallback=True,
        )

    close_rate = max(expected_close_fee_rate, ZERO)
    spread_rate = max(spread_buffer_pct, ZERO) / HUNDRED
    slippage_rate = max(slippage_buffer_pct, ZERO) / HUNDRED
    extra_rate = max(extra_buffer_pct, ZERO) / HUNDRED
    combined_exit_rate = close_rate + spread_rate + slippage_rate + extra_rate
    opening_per_unit = abs(opening_fee.amount_usdt) / remaining_quantity

    if direction == "LONG":
        denominator = Decimal("1") - combined_exit_rate
        if denominator <= ZERO:
            raise PositionModelError("combined BE+fees rates are invalid")
        raw_target = (exchange_entry + opening_per_unit) / denominator
        rounding = ROUND_CEILING
    else:
        numerator = exchange_entry - opening_per_unit
        denominator = Decimal("1") + combined_exit_rate
        if numerator <= ZERO:
            raise PositionModelError("opening fee exceeds recoverable SHORT notional")
        raw_target = numerator / denominator
        rounding = ROUND_FLOOR

    target = _round_to_tick(raw_target, tick_size, rounding)
    expected_exit_notional = target * remaining_quantity
    closing_fee = expected_exit_notional * close_rate
    spread_allowance = expected_exit_notional * spread_rate
    slippage_allowance = expected_exit_notional * slippage_rate
    extra_allowance = expected_exit_notional * extra_rate
    required = (
        abs(opening_fee.amount_usdt)
        + closing_fee
        + spread_allowance
        + slippage_allowance
        + extra_allowance
    )
    gross_recovery = abs(target - exchange_entry) * remaining_quantity
    expected_net = gross_recovery - required
    if expected_net < ZERO:
        raise PositionModelError("cost-covering rounding invariant failed")

    return BreakEvenResult(
        target=target,
        required_recovery_usdt=required,
        opening_fee_usdt=abs(opening_fee.amount_usdt),
        expected_closing_fee_usdt=closing_fee,
        spread_allowance_usdt=spread_allowance,
        slippage_allowance_usdt=slippage_allowance,
        extra_safety_allowance_usdt=extra_allowance,
        expected_net_usdt=expected_net,
        fee_source=opening_fee.source,
        used_legacy_fallback=False,
    )


def stop_is_legal(
    *,
    direction: str,
    target: Decimal,
    current_mark: Decimal,
    tick_size: Decimal,
    safety_ticks: int,
) -> bool:
    if target <= ZERO or current_mark <= ZERO or tick_size <= ZERO:
        return False
    safety_distance = tick_size * max(0, int(safety_ticks))
    if str(direction or "").upper() == "LONG":
        return target <= current_mark - safety_distance
    if str(direction or "").upper() == "SHORT":
        return target >= current_mark + safety_distance
    return False


def stop_is_monotonic(*, direction: str, previous: Decimal, proposed: Decimal) -> bool:
    if proposed <= ZERO:
        return False
    if previous <= ZERO:
        return True
    if str(direction or "").upper() == "LONG":
        return proposed >= previous
    if str(direction or "").upper() == "SHORT":
        return proposed <= previous
    return False


__all__ = [
    "AuthoritativeEntry",
    "AuthoritativeEntryUnavailable",
    "BE_PLUS_FEES_CONFIRMED",
    "BE_PLUS_FEES_PENDING",
    "BreakEvenResult",
    "CONFIGURED_FALLBACK",
    "ConfirmedPositionSize",
    "ConfirmedSizeUnavailable",
    "CriticalLegacyRead",
    "EXCHANGE_ACTUAL",
    "EXCHANGE_RATE",
    "INITIAL_PROTECTION_CONFIRMED",
    "LEGACY_FALLBACK",
    "OpeningFeeSelection",
    "PROFIT_LOCK_CONFIRMED",
    "PROFIT_LOCK_PENDING",
    "PROTECTION_UPDATE_FAILED",
    "PlannedPriceOnExecutedPosition",
    "PositionEconomics",
    "PositionLifecycleMismatch",
    "PositionModelError",
    "PositionPrices",
    "TRAILING_CONFIRMED",
    "TRAILING_PENDING",
    "calculate_break_even_plus_fees",
    "confirm_exchange_position",
    "confirmed_position_size",
    "decimal_float",
    "decimal_value",
    "development_assertions_enabled",
    "legacy_avg_entry",
    "migrate_planned_entry",
    "position_economics",
    "position_lifecycle_id",
    "position_prices",
    "price_return_pct",
    "select_opening_fee",
    "stop_is_legal",
    "stop_is_monotonic",
    "utc_now_iso",
]
