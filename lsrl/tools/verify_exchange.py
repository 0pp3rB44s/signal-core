"""Read-only verification of the exchange facts LSRL_V1 relies on (GET requests only).

    python -m lsrl.tools.verify_exchange --public-only     # contract metadata (no credentials needed)
    python -m lsrl.tools.verify_exchange                   # + account posMode / marginMode / fee rates (credentials)

The account part uses only `get_symbol_account` and `get_trade_fee_rate` (both GET, both read-only). Nothing here
can place, modify or cancel anything, and no credential is ever printed. The MICRO_LIVE executor performs the
same verification itself before its first entry and refuses to trade unless every fact holds.
"""
from __future__ import annotations

import argparse
import json
import sys

from lsrl.contract import ExchangeFactsError, rules_from_contract_row, verify_exchange_facts

READ_ONLY_CALLS = ("get_contracts", "get_symbol_account", "get_trade_fee_rate")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--public-only", action="store_true", help="contract metadata only (no credentials)")
    parser.add_argument("--symbol", default="SOLUSDT")
    args = parser.parse_args(argv)

    from app.config import Settings

    settings = Settings()
    product_type = str(settings.bitget_product_type).upper()
    try:
        if args.public_only:
            from clients.bitget_public_client import BitgetPublicClient

            client = BitgetPublicClient(settings=settings)
            rows = [r for r in (client.get_contracts(product_type, symbol=args.symbol) or {}).get("data") or []
                    if str(r.get("symbol") or "").upper() == args.symbol]
            if len(rows) != 1:
                raise ExchangeFactsError(f"CONTRACT_CONFIG_NOT_UNIQUE:{len(rows)}")
            rules = rules_from_contract_row(rows[0], args.symbol)
            out = {"mode": "public-only", "symbol": rules.symbol, "tick_size": rules.tick_size,
                   "qty_step": rules.qty_step, "min_qty": rules.min_qty,
                   "min_notional_usdt": rules.min_notional_usdt, "symbol_status": rows[0].get("symbolStatus"),
                   "price_place": rows[0].get("pricePlace"), "price_end_step": rows[0].get("priceEndStep"),
                   "read_only_calls": ["get_contracts"]}
        else:
            from clients.bitget_rest import BitgetRestClient

            client = BitgetRestClient(settings=settings)
            facts = verify_exchange_facts(client, symbol=args.symbol, product_type=product_type, expected=None)
            out = {"mode": "authenticated-read-only", **facts.to_dict(), "read_only_calls": list(READ_ONLY_CALLS)}
    except ExchangeFactsError as exc:
        print(json.dumps({"verified": False, "reason": str(exc)}))
        return 2
    print(json.dumps({"verified": True, **out}, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
