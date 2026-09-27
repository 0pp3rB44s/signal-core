"""OWNER READ-ONLY EXCHANGE VERIFICATION. GET requests only. One command, no side effect, no secret in the output.

    set -a; . ./.env.live; set +a; .venv/bin/python -m lsrl.tools.verify_exchange           # the owner's environment
    .venv/bin/python -m lsrl.tools.verify_exchange --public-only                             # contract metadata, no credentials

What it reports (each fact carries its OWN `read_ok`; nothing is marked verified unless the read actually succeeded AND the
value is the one the executor assumes):

  * SOLUSDT contract metadata: tick (= priceEndStep x 10^-pricePlace), quantity step, minimum quantity / notional,
    symbolStatus, scheduled maintenance;
  * account modes the executor assumes: posMode (hedge_mode) and marginMode (isolated), plus assetMode, the leverage the
    account holds for the symbol and the maker/taker fee rates;
  * the state the executor assumes is CLEAN before its first entry: SOLUSDT position sizes per side, pending orders on the
    symbol, and stop plans on the symbol (the stop client cancels loss plans on the side LSRL owns).

What it NEVER does: place, cancel, modify or close anything; change a leverage, margin or position mode; print an API key,
passphrase or secret (it never prints settings, headers or environment); print balances or equity. The client it uses is
wrapped so that ANY attribute outside the read allow-list below raises before a request can be built, and every allow-listed
method is a GET (asserted by tests against the client source).

Exit code: 0 = every expectation verified; 2 = something read but not as expected; 3 = a read failed or credentials are
not configured (no fact is claimed). The same facts are verified by the MICRO_LIVE executor itself before its first entry.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any, Callable

from lsrl.contract import ExchangeFactsError, rules_from_contract_row

# every method the tool may call, and the GET endpoint behind it (asserted against the client source by the tests)
ALLOWED_READS = {
    "get_contracts": "/api/v2/mix/market/contracts",
    "get_symbol_account": "/api/v2/mix/account/account",
    "get_trade_fee_rate": "/api/v2/common/trade-rate",
    "get_all_positions": "/api/v2/mix/position/all-position",
    "get_pending_orders": "/api/v2/mix/order/orders-pending",
    "get_tpsl_orders": "/api/v2/mix/order/orders-plan-pending",
}
EXPECTED_POS_MODE = "hedge_mode"
EXPECTED_MARGIN_MODE = "isolated"
_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


class ReadOnlyViolation(RuntimeError):
    pass


class ReadOnlyClient:
    """Exposes ONLY the allow-listed GET methods of the wrapped client. Anything else is refused before it can run."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        if name not in ALLOWED_READS:
            raise ReadOnlyViolation(f"{name} is not an allowed read-only call")
        target = getattr(self._client, name)

        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            return target(*args, **kwargs)
        return call


def redact(text: object) -> str:
    """Error text is reported, but anything that could be a key, secret or signature is masked."""
    return _TOKEN.sub("<redacted>", str(text))[:200]


def _rows(payload: Any) -> list[dict[str, Any]]:
    data = (payload or {}).get("data")
    if isinstance(data, dict):
        for key in ("entrustedList", "list", "orderList", "planList"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        return []
    return [r for r in (data or []) if isinstance(r, dict)]


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _attempt(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return {"read_ok": True, **fn()}
    except ExchangeFactsError as exc:
        return {"read_ok": False, "error": redact(exc)}
    except Exception as exc:                                     # noqa: BLE001 - reported, never raised
        return {"read_ok": False, "error": f"{type(exc).__name__}: {redact(exc)}"}


def read_contract(client: Any, symbol: str, product_type: str) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        rows = [r for r in _rows(client.get_contracts(product_type, symbol=symbol)) if str(r.get("symbol") or "").upper() == symbol]
        if len(rows) != 1:
            raise ExchangeFactsError(f"CONTRACT_CONFIG_NOT_UNIQUE:{len(rows)}")
        row = rows[0]
        rules = rules_from_contract_row(row, symbol)
        status = str(row.get("symbolStatus") or "").lower()
        maintenance = str(row.get("offTime") or "-1") != "-1" or str(row.get("limitOpenTime") or "-1") != "-1"
        return {"symbol": rules.symbol, "tick_size": rules.tick_size, "qty_step": rules.qty_step, "min_qty": rules.min_qty,
                "min_notional_usdt": rules.min_notional_usdt, "price_place": row.get("pricePlace"),
                "price_end_step": row.get("priceEndStep"), "symbol_status": status, "maintenance_scheduled": maintenance,
                "tradable": status == "normal" and not maintenance}
    return _attempt(go)


def read_account(client: Any, symbol: str, product_type: str) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        acct = ((client.get_symbol_account(symbol, product_type=product_type, margin_coin="USDT") or {}).get("data")) or {}
        if not acct:
            raise ExchangeFactsError("ACCOUNT_CONFIG_EMPTY")
        pos, margin = str(acct.get("posMode") or "").lower(), str(acct.get("marginMode") or "").lower()
        return {"pos_mode": pos or None, "margin_mode": margin or None, "asset_mode": acct.get("assetMode"),
                "isolated_long_leverage": _num(acct.get("isolatedLongLever")),
                "isolated_short_leverage": _num(acct.get("isolatedShortLever")),
                "crossed_margin_leverage": _num(acct.get("crossedMarginLeverage")),
                "hedge_mode_verified": pos == EXPECTED_POS_MODE, "isolated_margin_verified": margin == EXPECTED_MARGIN_MODE}
    return _attempt(go)


def read_fees(client: Any, symbol: str) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        data = ((client.get_trade_fee_rate(symbol) or {}).get("data")) or {}
        maker, taker = _num(data.get("makerFeeRate")), _num(data.get("takerFeeRate"))
        if maker is None or taker is None:
            raise ExchangeFactsError("FEE_RATES_NOT_RETURNED")
        return {"maker_fee_rate": maker, "taker_fee_rate": taker}
    return _attempt(go)


def read_symbol_state(client: Any, symbol: str, product_type: str) -> dict[str, Any]:
    def go() -> dict[str, Any]:
        sizes = {"long": 0.0, "short": 0.0}
        for row in ((client.get_all_positions(product_type=product_type) or {}).get("data")) or []:
            if str(row.get("symbol") or "").upper() == symbol:
                side = str(row.get("holdSide") or "").lower()
                if side in sizes:
                    sizes[side] += _num(row.get("total") or row.get("size")) or 0.0
        pending = [r for r in _rows(client.get_pending_orders(symbol=symbol, product_type=product_type))]
        plans = _rows(client.get_tpsl_orders(product_type=product_type, symbol=symbol, plan_type="profit_loss"))
        loss = {"long": 0, "short": 0}
        for plan in plans:
            if "loss" in str(plan.get("planType") or plan.get("orderType") or "").lower():
                side = str(plan.get("posSide") or plan.get("holdSide") or "").lower()
                if side in loss:
                    loss[side] += 1
        return {"position_long_size": sizes["long"], "position_short_size": sizes["short"],
                "pending_orders": len(pending), "loss_plans_long": loss["long"], "loss_plans_short": loss["short"],
                "flat_and_clean": not any(sizes.values()) and not pending and not any(loss.values())}
    return _attempt(go)


def build_report(client: Any, *, symbol: str, product_type: str, public_only: bool, now_ms: int | None = None) -> dict[str, Any]:
    ro = client if isinstance(client, ReadOnlyClient) else ReadOnlyClient(client)
    report: dict[str, Any] = {"tool": "lsrl.tools.verify_exchange", "read_only": True,
                              "mode": "public-only" if public_only else "authenticated-read-only",
                              "symbol": symbol, "product_type": product_type,
                              "utc_ms": int(time.time() * 1000) if now_ms is None else now_ms}
    report["contract"] = read_contract(ro, symbol, product_type)
    if not public_only:
        report["account"] = read_account(ro, symbol, product_type)
        report["fees"] = read_fees(ro, symbol)
        report["symbol_state"] = read_symbol_state(ro, symbol, product_type)
    c, a, f, s = (report.get(k) or {} for k in ("contract", "account", "fees", "symbol_state"))
    verified = {
        "CONTRACT_METADATA_VERIFIED": bool(c.get("read_ok")) and bool(c.get("tradable")),
    }
    if not public_only:
        verified.update({
            "POSITION_MODE_HEDGE_VERIFIED": bool(a.get("read_ok")) and bool(a.get("hedge_mode_verified")),
            "MARGIN_MODE_ISOLATED_VERIFIED": bool(a.get("read_ok")) and bool(a.get("isolated_margin_verified")),
            "FEE_RATES_READ": bool(f.get("read_ok")),
            "SYMBOL_FLAT_AND_CLEAN": bool(s.get("read_ok")) and bool(s.get("flat_and_clean")),
        })
    report["verified"] = verified
    reads = [x.get("read_ok") for x in (c, a, f, s) if x]
    report["all_reads_ok"] = all(reads) and bool(reads)
    report["all_verified"] = report["all_reads_ok"] and all(verified.values())
    report["read_only_calls_made"] = list(getattr(ro, "calls", []))
    return report


def exit_code(report: dict[str, Any]) -> int:
    if not report.get("all_reads_ok"):
        return 3
    return 0 if report.get("all_verified") else 2


def _credentials_configured(settings: Any) -> bool:
    """True when all three credential fields are non-empty. Only emptiness is tested; the values are never read out."""
    for name in ("bitget_api_key", "bitget_api_secret", "bitget_api_passphrase"):
        value = getattr(settings, name, "")
        raw = value.get_secret_value() if hasattr(value, "get_secret_value") else value
        if not str(raw or "").strip():
            return False
    return True


def main(argv: list[str] | None = None, *, client_factory: Callable[[Any, bool], Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only (GET) verification of the exchange facts LSRL_V1 relies on")
    parser.add_argument("--public-only", action="store_true", help="contract metadata only (no credentials needed)")
    parser.add_argument("--symbol", default="SOLUSDT")
    args = parser.parse_args(argv)
    symbol = args.symbol.upper()

    from app.config import Settings

    settings = Settings()
    product_type = str(settings.bitget_product_type).upper()
    if not args.public_only:
        if not _credentials_configured(settings):
            print(json.dumps({"read_only": True, "mode": "authenticated-read-only", "credentials_configured": False,
                              "all_reads_ok": False, "all_verified": False,
                              "note": "no credentials in this environment; nothing was read and no fact is claimed"}, indent=1))
            return 3
    if client_factory is not None:
        client = client_factory(settings, args.public_only)
    elif args.public_only:
        from clients.bitget_public_client import BitgetPublicClient
        client = BitgetPublicClient(settings=settings)
    else:
        from clients.bitget_rest import BitgetRestClient
        client = BitgetRestClient(settings=settings)
    report = build_report(client, symbol=symbol, product_type=product_type, public_only=args.public_only)
    print(json.dumps(report, indent=1, sort_keys=True, default=str))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
