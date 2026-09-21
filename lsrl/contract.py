"""SOLUSDT exchange constraints and startup VERIFICATION of exchange semantics (fail closed).

Nothing here is a test value: tick, quantity step, minimums, position mode and fee rates come from the
EXCHANGE (public contract config + the authenticated READ-ONLY account endpoints) and anything that cannot
be established stops the executor instead of being assumed.

Official semantics this module encodes (Bitget classic futures API, verified against the live public
contract endpoint on 2026-09-20; docs: Futures -> Market -> Get Contract Config, Account -> Get Single
Account, Trade -> Place Order):
  * tick size  = priceEndStep x 10**-pricePlace        (SOLUSDT: 1 x 10**-3 = 0.001, NOT 0.01)
  * order size = a multiple of sizeMultiplier and >= minTradeNum; notional >= minTradeUSDT
  * symbolStatus must be `normal` (limit_open / maintain / restrictedAPI / off refuse order placement)
  * posMode is `hedge_mode` or `one_way_mode`. The LSRL executor sends `tradeSide` + `holdSide`
    (hedge semantics); in one-way mode `tradeSide` is ignored and `reduceOnly` (documented as applicable
    ONLY in one-way mode) would change meaning, so anything but hedge mode is refused.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from lsrl.core.types import SymbolRules

REQUIRED_POS_MODE = "hedge_mode"
REQUIRED_MARGIN_MODE = "isolated"


class ExchangeFactsError(RuntimeError):
    """The exchange state needed to trade safely could not be established."""


def _dec(value: Any) -> Decimal | None:
    try:
        out = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return out if out.is_finite() else None


def tick_from(price_place: int, price_end_step: Decimal) -> float:
    return float(price_end_step * Decimal(1).scaleb(-int(price_place)))


def rules_from_contract_row(row: dict[str, Any], symbol: str) -> SymbolRules:
    """A raw Bitget contract-config row -> SymbolRules. Any unusable field refuses to run."""
    if str(row.get("symbol") or "").upper() != symbol.upper():
        raise ExchangeFactsError(f"contract row is for {row.get('symbol')!r}, not {symbol}")
    place = _dec(row.get("pricePlace"))
    step = _dec(row.get("priceEndStep"))
    size_mult = _dec(row.get("sizeMultiplier"))
    min_num = _dec(row.get("minTradeNum"))
    min_usdt = _dec(row.get("minTradeUSDT"))
    if None in (place, step, size_mult, min_num) or step <= 0 or size_mult <= 0 or min_num <= 0:
        raise ExchangeFactsError(f"unusable contract metadata for {symbol}: {sorted(row)}")
    if min_usdt is None or min_usdt <= 0:
        raise ExchangeFactsError(f"minTradeUSDT unavailable for {symbol}")
    return SymbolRules(symbol=symbol.upper(), tick_size=tick_from(int(place), step), qty_step=float(size_mult),
                       min_qty=float(min_num), min_notional_usdt=float(min_usdt))


def rules_from_contract_spec(spec: Any, symbol: str) -> SymbolRules:
    """`clients.bitget_precision.ContractSpec` -> SymbolRules (same signature the runtime always used).

    Unknown metadata refuses to run. The tick honours `priceEndStep`."""
    if spec is None:
        raise RuntimeError("CONTRACT_METADATA_UNAVAILABLE: refusing to run LSRL without exchange constraints")
    step = getattr(spec, "price_end_step", Decimal(1))
    min_usdt = getattr(spec, "min_trade_usdt", None)
    return SymbolRules(symbol=symbol.upper(), tick_size=tick_from(int(spec.price_place), Decimal(str(step))),
                       qty_step=float(spec.size_multiplier), min_qty=float(spec.min_trade_num),
                       min_notional_usdt=float(min_usdt) if min_usdt is not None else 5.0)


@dataclass(frozen=True, slots=True)
class ExchangeFacts:
    symbol: str
    rules: SymbolRules
    symbol_status: str
    pos_mode: str
    margin_mode: str
    maker_fee_rate: float | None
    taker_fee_rate: float | None
    max_order_qty: float | None
    verified_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "tick_size": self.rules.tick_size, "qty_step": self.rules.qty_step,
                "min_qty": self.rules.min_qty, "min_notional_usdt": self.rules.min_notional_usdt,
                "symbol_status": self.symbol_status, "pos_mode": self.pos_mode, "margin_mode": self.margin_mode,
                "maker_fee_rate": self.maker_fee_rate, "taker_fee_rate": self.taker_fee_rate,
                "max_order_qty": self.max_order_qty, "verified_at_ms": self.verified_at_ms}


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-12


def verify_exchange_facts(client: Any, *, symbol: str, product_type: str, expected: SymbolRules | None,
                          margin_coin: str = "USDT", now_ms: int | None = None) -> ExchangeFacts:
    """Read-only checks (GET only). Raises ExchangeFactsError on anything unproven.

    Established here: contract metadata == the rules the engine sizes with; symbol tradable; account is a
    classic futures account in HEDGE mode with ISOLATED margin for the symbol; actual fee rates.
    """
    try:
        payload = client.get_contracts(product_type, symbol=symbol.upper())
    except Exception as exc:                                   # noqa: BLE001
        raise ExchangeFactsError(f"CONTRACT_CONFIG_UNREADABLE:{type(exc).__name__}:{exc}") from exc
    rows = [r for r in (payload or {}).get("data") or [] if str(r.get("symbol") or "").upper() == symbol.upper()]
    if len(rows) != 1:
        raise ExchangeFactsError(f"CONTRACT_CONFIG_NOT_UNIQUE:{len(rows)} rows for {symbol}")
    row = rows[0]
    rules = rules_from_contract_row(row, symbol)
    status = str(row.get("symbolStatus") or "").lower()
    if status != "normal":
        raise ExchangeFactsError(f"SYMBOL_NOT_TRADABLE:symbolStatus={status!r}")
    if str(row.get("offTime") or "-1") != "-1" or str(row.get("limitOpenTime") or "-1") != "-1":
        raise ExchangeFactsError(f"SYMBOL_MAINTENANCE_SCHEDULED:offTime={row.get('offTime')} "
                                 f"limitOpenTime={row.get('limitOpenTime')}")
    if expected is not None:
        for field in ("tick_size", "qty_step", "min_qty", "min_notional_usdt"):
            if not _same(getattr(rules, field), getattr(expected, field)):
                raise ExchangeFactsError(f"CONTRACT_METADATA_DRIFT:{field} exchange={getattr(rules, field)} "
                                         f"engine={getattr(expected, field)}")
    try:
        acct = (client.get_symbol_account(symbol, product_type=product_type, margin_coin=margin_coin) or {}).get("data") or {}
    except Exception as exc:                                   # noqa: BLE001
        raise ExchangeFactsError(f"ACCOUNT_CONFIG_UNREADABLE:{type(exc).__name__}:{exc}") from exc
    pos_mode = str(acct.get("posMode") or "").lower()
    margin_mode = str(acct.get("marginMode") or "").lower()
    if pos_mode != REQUIRED_POS_MODE:
        raise ExchangeFactsError(f"POSITION_MODE_NOT_HEDGE:posMode={pos_mode!r}")
    if margin_mode != REQUIRED_MARGIN_MODE:
        raise ExchangeFactsError(f"MARGIN_MODE_NOT_ISOLATED:marginMode={margin_mode!r}")
    maker = taker = None
    try:
        fee = ((client.get_trade_fee_rate(symbol) or {}).get("data") or {})
        maker = float(fee["makerFeeRate"]) if fee.get("makerFeeRate") not in (None, "") else None
        taker = float(fee["takerFeeRate"]) if fee.get("takerFeeRate") not in (None, "") else None
    except Exception:                                          # fee rates are advisory: contract defaults apply
        maker = _f(row.get("makerFeeRate"))
        taker = _f(row.get("takerFeeRate"))
    return ExchangeFacts(symbol=symbol.upper(), rules=rules, symbol_status=status, pos_mode=pos_mode,
                         margin_mode=margin_mode, maker_fee_rate=maker, taker_fee_rate=taker,
                         max_order_qty=_f(row.get("maxOrderQty")),
                         verified_at_ms=int(time.time() * 1000) if now_ms is None else now_ms)


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
