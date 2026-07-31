"""Precision helpers driven by Bitget contract metadata.

Replaces a hardcoded minimum-size table that was stricter than the exchange and
silently blocked every order this account could afford:

    _min_size("BTCUSDT") -> 0.001      # exchange minTradeNum is 0.0001

On 2026-07-30 five genuine LIVE entries (0.0002 and 0.0004 BTC, 12.91 and 26.79
USDT) were refused pre-transport by that constant. The exchange would have
accepted all of them: minTradeNum=0.0001, sizeMultiplier=0.0001, volumePlace=4,
minTradeUSDT=5.

Three defects are fixed together because they share one root — nothing read the
contract spec:

  1. the minimum was guessed from a substring match on the symbol name;
  2. `round()` was used to fit volumePlace, which rounds *up* half the time and
     can therefore inflate a quantity past the account risk budget and the live
     notional cap;
  3. every formatting call issued its own /contracts request, so a single order
     cost two to three HTTP round-trips and each was a fresh chance to fail.

Quantities are now quantized DOWN with Decimal to the exchange size increment.
Rounding down can only ever reduce exposure, so it cannot breach a risk ceiling.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

#: Contract metadata TTL. Contract specs change rarely (a listing change or a
#: precision change), so minutes are ample, while a bounded TTL still means a
#: genuine change is picked up without a restart.
SPEC_TTL_SECONDS = 900.0

#: Rejection reasons. These strings are asserted by tests and appear in logs.
REASON_BELOW_EXCHANGE_MIN = "EXCHANGE_MINIMUM_INCOMPATIBLE_WITH_RISK_LIMITS"
REASON_BELOW_MIN_NOTIONAL = "EXCHANGE_MIN_NOTIONAL_INCOMPATIBLE_WITH_RISK_LIMITS"
REASON_METADATA_UNAVAILABLE = "CONTRACT_METADATA_UNAVAILABLE"
REASON_INVALID_REFERENCE_PRICE = "INVALID_VALIDATION_PRICE"
REASON_PRODUCT_TYPE_MISMATCH = "CONTRACT_PRODUCT_TYPE_MISMATCH"

#: Decimal precision fallback used ONLY for formatting, never for minimum-size
#: enforcement. A metadata outage must not prevent closing an open position, so
#: formatting degrades to a conservative decimal count; enforcement fails closed
#: instead (see `min_size_or_reason`).
_FORMAT_FALLBACK_PLACES = 3


@dataclass(frozen=True)
class ContractSpec:
    """One symbol's exchange-declared trading constraints."""

    symbol: str
    product_type: str
    min_trade_num: Decimal
    size_multiplier: Decimal
    volume_place: int
    price_place: int
    min_trade_usdt: Decimal | None
    source: str
    retrieved_at: float
    fallback_used: bool = False

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.retrieved_at)

    def log_fields(self) -> str:
        mn = "n/a" if self.min_trade_usdt is None else f"{self.min_trade_usdt}"
        return (
            f"symbol={self.symbol} | product_type={self.product_type} "
            f"| metadata_source={self.source} | metadata_age={self.age_seconds:.1f}s "
            f"| fallback_used={str(self.fallback_used).lower()} "
            f"| min_trade_num={self.min_trade_num} | size_multiplier={self.size_multiplier} "
            f"| volume_place={self.volume_place} | min_trade_usdt={mn}"
        )


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


class _SpecCache:
    """Bounded, process-safe cache keyed by (product_type, symbol).

    `validated` keeps the last spec that actually came from the exchange. It is
    the only permitted fallback during an outage: a generic constant must never
    override metadata we have previously seen.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fresh: dict[tuple[str, str], ContractSpec] = {}
        self._validated: dict[tuple[str, str], ContractSpec] = {}

    def get_fresh(self, key: tuple[str, str]) -> ContractSpec | None:
        with self._lock:
            spec = self._fresh.get(key)
            if spec is None:
                return None
            if spec.age_seconds >= SPEC_TTL_SECONDS:
                # Expired: drop it so the next lookup refetches. Stale metadata
                # is never handed out as if it were fresh.
                self._fresh.pop(key, None)
                return None
            return spec

    def put(self, key: tuple[str, str], spec: ContractSpec) -> None:
        with self._lock:
            self._fresh[key] = spec
            if not spec.fallback_used:
                self._validated[key] = spec

    def get_validated(self, key: tuple[str, str]) -> ContractSpec | None:
        with self._lock:
            return self._validated.get(key)

    def clear(self) -> None:
        with self._lock:
            self._fresh.clear()
            self._validated.clear()


_CACHE = _SpecCache()


def reset_spec_cache() -> None:
    """Test seam; also lets an operator force a metadata refresh."""
    _CACHE.clear()


class BitgetPrecisionMixin:
    """Contract-spec lookup, size normalization and trigger-price formatting."""

    def _assert_order_transport_allowed(self) -> None:
        """Fail fast when the runtime may not place orders at all.

        Mirrors the central guard in BitgetBaseClient._request so an order path
        refuses before any network call, including the public contract-metadata
        lookup. Without this, a forward-paper runtime would report a size or
        metadata error rather than the actual prohibition.
        """
        if getattr(self.settings, "forward_paper_only", False):
            from clients.bitget_base_client import PrivateExchangeCallBlocked

            raise PrivateExchangeCallBlocked(
                "Private exchange call blocked: FORWARD_PAPER_ONLY is active"
            )

    # --- metadata ---------------------------------------------------------

    def _spec_key(self, symbol: str) -> tuple[str, str]:
        return (str(self.settings.bitget_product_type).upper(), symbol.upper())

    def _parse_contract(self, contract: dict[str, Any], product_type: str,
                        symbol: str) -> ContractSpec | None:
        """Build a spec, or None when the payload is unusable.

        Malformed metadata is never accepted: without a usable minTradeNum and
        size increment there is nothing to validate against, and guessing is
        exactly the defect being repaired.
        """
        min_trade = _dec(contract.get("minTradeNum"))
        multiplier = _dec(contract.get("sizeMultiplier"))

        volume_place = contract.get("volumePlace", contract.get("sizePlace"))
        price_place = contract.get("pricePlace", contract.get("priceScale"))
        try:
            volume_place_int = int(volume_place)
            price_place_int = int(price_place)
        except (TypeError, ValueError):
            return None

        if min_trade is None or min_trade <= 0:
            return None
        if multiplier is None or multiplier <= 0:
            # Without an increment we cannot align; fall back to the smallest
            # step volumePlace can express rather than inventing one.
            multiplier = Decimal(1).scaleb(-volume_place_int)
        if volume_place_int < 0 or price_place_int < 0:
            return None

        return ContractSpec(
            symbol=symbol.upper(), product_type=product_type,
            min_trade_num=min_trade, size_multiplier=multiplier,
            volume_place=volume_place_int, price_place=price_place_int,
            min_trade_usdt=_dec(contract.get("minTradeUSDT")),
            source="/api/v2/mix/market/contracts",
            retrieved_at=time.monotonic(), fallback_used=False,
        )

    def _contract_spec(self, symbol: str, *, force_refresh: bool = False) -> ContractSpec | None:
        """Fresh spec, else the last validated one (flagged), else None."""
        product_type = str(self.settings.bitget_product_type).upper()
        key = self._spec_key(symbol)

        if not force_refresh:
            cached = _CACHE.get_fresh(key)
            if cached is not None:
                return cached

        try:
            payload = self.get_contracts(product_type, symbol=symbol.upper())
            data = payload.get("data") or []
            contract = next(
                (c for c in data if str(c.get("symbol", "")).upper() == symbol.upper()),
                data[0] if len(data) == 1 else None,
            )
            if contract is None:
                raise ValueError(f"{symbol} absent from {product_type} contracts")

            # The response must describe the same market orders are sent to.
            # Bitget echoes productType on some payloads; when present it is
            # authoritative and a mismatch is a hard error, not a warning.
            echoed = str(contract.get("productType") or product_type).upper()
            if echoed != product_type:
                self.log.error(
                    "CONTRACT_METADATA_REJECTED | %s | reason=%s | expected=%s | received=%s",
                    symbol.upper(), REASON_PRODUCT_TYPE_MISMATCH, product_type, echoed,
                )
                return None

            spec = self._parse_contract(contract, product_type, symbol)
            if spec is None:
                self.log.error(
                    "CONTRACT_METADATA_REJECTED | %s | reason=malformed_metadata "
                    "| product_type=%s | keys=%s",
                    symbol.upper(), product_type, ",".join(sorted(contract)[:12]),
                )
            else:
                _CACHE.put(key, spec)
                self.log.info("CONTRACT_METADATA_OK | %s", spec.log_fields())
                return spec
        except Exception as exc:
            self.log.warning(
                "CONTRACT_METADATA_FETCH_FAILED | %s | product_type=%s | error_type=%s | error=%s",
                symbol.upper(), product_type, type(exc).__name__, exc,
            )

        # Outage path: reuse the last spec that genuinely came from the exchange.
        validated = _CACHE.get_validated(key)
        if validated is not None:
            fallback = ContractSpec(
                symbol=validated.symbol, product_type=validated.product_type,
                min_trade_num=validated.min_trade_num,
                size_multiplier=validated.size_multiplier,
                volume_place=validated.volume_place, price_place=validated.price_place,
                min_trade_usdt=validated.min_trade_usdt,
                source=validated.source + " (cached)",
                retrieved_at=validated.retrieved_at, fallback_used=True,
            )
            self.log.warning("CONTRACT_METADATA_FALLBACK | %s", fallback.log_fields())
            return fallback

        return None

    # --- size normalization ----------------------------------------------

    @staticmethod
    def _quantize_down(size: Decimal, increment: Decimal, places: int) -> Decimal:
        """Align DOWN to the increment, then clamp to the declared decimals.

        Down-only is the safety property: the result is never larger than the
        risk-derived quantity, so it cannot breach the risk budget, the notional
        cap, the leverage limit or the sizing limit.
        """
        if increment > 0:
            steps = (size / increment).to_integral_value(rounding=ROUND_DOWN)
            size = steps * increment
        quantum = Decimal(1).scaleb(-places) if places > 0 else Decimal(1)
        return size.quantize(quantum, rounding=ROUND_DOWN)

    def _normalize_size(self, symbol: str, size: float) -> Decimal:
        raw = _dec(size) or Decimal(0)
        if raw <= 0:
            return Decimal(0)
        spec = self._contract_spec(symbol)
        if spec is None:
            # Formatting only. Enforcement is handled by `min_size_or_reason`,
            # which fails closed in exactly this situation.
            return self._quantize_down(raw, Decimal(0), _FORMAT_FALLBACK_PLACES)
        return self._quantize_down(raw, spec.size_multiplier, spec.volume_place)

    def _format_size(self, symbol: str, size: float) -> float:
        return float(self._normalize_size(symbol, size))

    # --- minimums ---------------------------------------------------------

    def min_size_or_reason(self, symbol: str) -> tuple[Decimal | None, str | None]:
        """(minimum, None) on success, (None, reason) when it cannot be known.

        Never falls back to a generic constant: an unknown minimum fails closed
        so an entry is refused before transport instead of being sized against a
        guess.
        """
        spec = self._contract_spec(symbol)
        if spec is None:
            return None, REASON_METADATA_UNAVAILABLE
        return spec.min_trade_num, None

    def _min_size(self, symbol: str) -> float:
        """Back-compatible accessor. Returns the exchange minimum, or +inf when
        metadata is unavailable so any comparison against it refuses the order."""
        minimum, reason = self.min_size_or_reason(symbol)
        if minimum is None:
            return float("inf")
        return float(minimum)

    def _min_notional(self, symbol: str) -> Decimal | None:
        spec = self._contract_spec(symbol)
        return None if spec is None else spec.min_trade_usdt

    def validate_entry_size(
        self, symbol: str, size: float, reference_price: float | None = None,
    ) -> tuple[Decimal, str | None]:
        """Normalize DOWN and validate against the exchange floors.

        Returns (normalized_size, reason). `reason` is None when the order may be
        submitted. The quantity is never inflated to reach a minimum — if the
        risk-derived size cannot satisfy the exchange, the order is refused and
        the caller reports why.
        """
        spec = self._contract_spec(symbol)
        if spec is None:
            return Decimal(0), REASON_METADATA_UNAVAILABLE

        normalized = self._quantize_down(
            _dec(size) or Decimal(0), spec.size_multiplier, spec.volume_place)

        if normalized <= 0 or normalized < spec.min_trade_num:
            return normalized, REASON_BELOW_EXCHANGE_MIN

        if reference_price is not None:
            price = _dec(reference_price)
            if price is None or price <= 0:
                # A price was offered but is unusable. Validating against zero
                # would reject everything and validating against nothing would
                # skip the floor silently, so treat it as a malformed plan.
                return normalized, REASON_INVALID_REFERENCE_PRICE
            if spec.min_trade_usdt is not None:
                if (normalized * price) < spec.min_trade_usdt:
                    return normalized, REASON_BELOW_MIN_NOTIONAL

        return normalized, None

    # --- price ------------------------------------------------------------

    def _contract_price_scale(self, symbol: str, *, force_refresh: bool = False) -> int | None:
        spec = self._contract_spec(symbol, force_refresh=force_refresh)
        return None if spec is None else spec.price_place

    def _contract_volume_scale(self, symbol: str) -> int | None:
        spec = self._contract_spec(symbol)
        return None if spec is None else spec.volume_place

    def _format_trigger_price(self, symbol: str, price: float) -> float:
        raw = _dec(price)
        if raw is None:
            return 0.0
        scale = self._contract_price_scale(symbol)
        places = _FORMAT_FALLBACK_PLACES if scale is None else scale
        quantum = Decimal(1).scaleb(-places) if places > 0 else Decimal(1)
        # Prices are rounded half-even rather than down: a trigger is not an
        # exposure, and biasing it downward would systematically shift stops and
        # targets in one direction.
        return float(raw.quantize(quantum))


__all__ = [
    "REASON_BELOW_EXCHANGE_MIN", "REASON_BELOW_MIN_NOTIONAL",
    "REASON_INVALID_REFERENCE_PRICE",
    "REASON_METADATA_UNAVAILABLE", "REASON_PRODUCT_TYPE_MISMATCH",
    "SPEC_TTL_SECONDS", "BitgetPrecisionMixin", "ContractSpec", "reset_spec_cache",
]
