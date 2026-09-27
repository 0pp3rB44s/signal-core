"""READ-ONLY, ACCOUNT-WIDE Bitget exposure audit. GET requests only. It can never place, cancel, modify or close anything.

    set -a; . ./.env.live; set +a; .venv/bin/python -m lsrl.tools.exposure_audit                    # the owner's environment
    ... -m lsrl.tools.exposure_audit --expect-file expected.json                                    # reconcile against a declared state

WHY. `lsrl.tools.verify_exchange` looks at ONE symbol. Before any future arming the whole account must be shown to hold no exposure the
pilot did not create: a position or a resting/plan order on ANOTHER symbol (or another kind of plan order) would be counted by the exchange's
margin and could be triggered or filled behind the pilot's back. This tool reads, account-wide and to the end of every page:

  REAL_POSITIONS     non-zero positions, every symbol, every hold side
  REAL_ORDERS        resting orders (limit/market pending), every symbol
  REAL_PLAN_ORDERS   plan orders of EVERY type: profit_loss (TP/SL, position-level and per-order), normal_plan (trigger), track_plan (trailing)

and reports the three counts. EXCHANGE_EXPOSURE_GATE=PASS only when all three are zero, OR when a declared `--expect-file` lists exactly what is
there (an item missing or extra is a FAIL). Anything not read to completion (an error, an unparseable response, a page limit reached, a
cursor that did not advance) makes the audit INCOMPLETE and therefore FAIL: unknown is never zero.

HONEST SCOPE. Covered: USDT-FUTURES and USDC-FUTURES (linear). NOT covered (listed in the report, never silently omitted): COIN-FUTURES, spot,
margin, earn, sub-accounts, and anything the classic mix endpoints do not show. The dedicated micro-live account is expected to hold nothing else.

READ-ONLY BY CONSTRUCTION. The client is wrapped in `verify_exchange.ReadOnlyClient`: only the three allow-listed GET methods can be called; the
tests assert every one is a GET against the client source. No secret, balance or equity is ever printed.
Exit code: 0 = gate PASS; 2 = read completely but the account is not as expected; 3 = incomplete / credentials missing (nothing is claimed).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from lsrl.tools.verify_exchange import ReadOnlyClient, _credentials_configured, _num, _rows, redact

PRODUCT_TYPES = ("USDT-FUTURES", "USDC-FUTURES")
NOT_COVERED = ("COIN-FUTURES", "SPOT", "MARGIN", "EARN", "SUB_ACCOUNTS")
PLAN_TYPES = ("profit_loss", "normal_plan", "track_plan")
PAGE_LIMIT = 100
MAX_PAGES = 20


class AuditIncomplete(RuntimeError):
    """Something could not be read to the end: nothing may be concluded from a partial read."""


def _cursor(payload: Any) -> str:
    data = (payload or {}).get("data")
    return str(data.get("endId") or "") if isinstance(data, dict) else ""


def _paged(read: Callable[[str | None], Any], what: str) -> list[dict[str, Any]]:
    """Every row of a paginated read. A full page needs a NEW cursor to continue; anything else that is not a clean end is incomplete."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen: set[str] = set()
    for _page in range(MAX_PAGES):
        payload = read(cursor)
        if not isinstance(payload, dict) or "data" not in payload:
            raise AuditIncomplete(f"{what}: response without a data field")
        data = payload["data"]
        if data is not None and not isinstance(data, (dict, list)):
            raise AuditIncomplete(f"{what}: unparseable data")
        page = _rows(payload)
        if isinstance(data, list) and len(page) != len(data):
            raise AuditIncomplete(f"{what}: a non-object row in the response")
        rows.extend(page)
        if len(page) < PAGE_LIMIT:
            return rows                                            # a short page is the clean end
        nxt = _cursor(payload)
        if not nxt or nxt in seen or nxt == cursor:
            raise AuditIncomplete(f"{what}: a full page without a usable next cursor")
        seen.add(nxt)
        cursor = nxt
    raise AuditIncomplete(f"{what}: more than {MAX_PAGES} pages")


def _position_rows(client: Any, product_type: str) -> list[dict[str, Any]]:
    payload = client.get_all_positions(product_type=product_type, margin_coin="USDC" if product_type.startswith("USDC") else "USDT")
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise AuditIncomplete(f"positions[{product_type}]: response without a data list")
    out = []
    for row in payload["data"]:
        if not isinstance(row, dict):
            raise AuditIncomplete(f"positions[{product_type}]: a non-object row")
        size = _num(row.get("total"))
        if size is None:
            size = _num(row.get("size"))
        if size is None:
            raise AuditIncomplete(f"positions[{product_type}]: a row without a readable size")
        if size != 0:
            out.append({"product_type": product_type, "symbol": str(row.get("symbol") or "").upper(),
                        "side": str(row.get("holdSide") or "").lower(), "size": size})
    return out


def _order_row(row: dict[str, Any], product_type: str) -> dict[str, Any]:
    return {"product_type": product_type, "symbol": str(row.get("symbol") or "").upper(), "side": str(row.get("side") or "").lower(),
            "trade_side": str(row.get("tradeSide") or "").lower(), "order_id": str(row.get("orderId") or ""),
            "client_oid": str(row.get("clientOid") or ""), "size": _num(row.get("size")), "price": _num(row.get("price")),
            "reduce_only": str(row.get("reduceOnly") or "").upper()}


def _plan_row(row: dict[str, Any], product_type: str, queried: str) -> dict[str, Any]:
    return {"product_type": product_type, "queried_plan_type": queried, "symbol": str(row.get("symbol") or "").upper(),
            "plan_type": str(row.get("planType") or row.get("orderType") or "").lower(),
            "side": str(row.get("posSide") or row.get("holdSide") or "").lower(),
            "order_id": str(row.get("orderId") or row.get("planOrderId") or ""), "client_oid": str(row.get("clientOid") or ""),
            "trigger_price": _num(row.get("triggerPrice") or row.get("planTriggerPrice")), "size": _num(row.get("size"))}


def _key(kind: str, row: dict[str, Any]) -> str:
    if kind == "positions":
        return f"{row['product_type']}:{row['symbol']}:{row['side']}"
    return f"{row['product_type']}:{row['symbol']}:{row.get('order_id')}"


def load_expectation(path: Path) -> dict[str, set[str]]:
    """A declared state: {"positions": ["USDT-FUTURES:SOLUSDT:long"], "orders": ["USDT-FUTURES:SOLUSDT:<orderId>"], "plan_orders": [...]}.
    Ids only: a pattern or a prefix would let an unexpected order hide behind an expected name."""
    doc = json.loads(path.read_text())
    if not isinstance(doc, dict) or set(doc) - {"positions", "orders", "plan_orders"}:
        raise ValueError("the expectation file must be an object with only positions/orders/plan_orders")
    out: dict[str, set[str]] = {}
    for kind in ("positions", "orders", "plan_orders"):
        items = doc.get(kind, [])
        if not isinstance(items, list) or not all(isinstance(i, str) and i.count(":") == 2 and "*" not in i for i in items):
            raise ValueError(f"{kind}: every item must be an exact 'PRODUCT:SYMBOL:id-or-side' string")
        out[kind] = set(items)
    return out


def audit(client: Any, *, expected: dict[str, set[str]] | None = None, product_types: tuple[str, ...] = PRODUCT_TYPES,
          now_ms: int | None = None) -> dict[str, Any]:
    ro = client if isinstance(client, ReadOnlyClient) else ReadOnlyClient(client)
    report: dict[str, Any] = {"tool": "lsrl.tools.exposure_audit", "read_only": True,
                              "utc_ms": int(time.time() * 1000) if now_ms is None else now_ms,
                              "scope": {"product_types": list(product_types), "NOT_COVERED": list(NOT_COVERED)},
                              "positions": [], "orders": [], "plan_orders": [], "errors": []}
    for pt in product_types:
        for name, read in (
                ("positions", lambda pt=pt: _position_rows(ro, pt)),
                ("orders", lambda pt=pt: [_order_row(r, pt) for r in _paged(
                    lambda cur: ro.get_pending_orders(product_type=pt, limit=PAGE_LIMIT, id_less_than=cur), f"orders[{pt}]")]),
                ("plan_orders", lambda pt=pt: [_plan_row(r, pt, q) for q in PLAN_TYPES for r in _paged(
                    lambda cur, q=q: ro.get_tpsl_orders(product_type=pt, plan_type=q, limit=PAGE_LIMIT, id_less_than=cur),
                    f"plan_orders[{pt}/{q}]")])):
            try:
                report[name].extend(read())
            except AuditIncomplete as exc:
                report["errors"].append(redact(exc))
            except Exception as exc:                                  # noqa: BLE001 - reported, never raised
                report["errors"].append(f"{name}[{pt}]: {type(exc).__name__}: {redact(exc)}")
    report["complete"] = not report["errors"]
    report["REAL_POSITIONS"] = len(report["positions"])
    report["REAL_ORDERS"] = len(report["orders"])
    report["REAL_PLAN_ORDERS"] = len(report["plan_orders"])
    if expected is None:
        clean = report["REAL_POSITIONS"] == report["REAL_ORDERS"] == report["REAL_PLAN_ORDERS"] == 0
        report["reconciliation"] = None
        reason = "" if clean else "the account is not flat and clean and no expectation was declared"
    else:
        diff: dict[str, Any] = {}
        clean = True
        for kind in ("positions", "orders", "plan_orders"):
            actual = {_key(kind, r) for r in report[kind]}
            unexpected, missing = sorted(actual - expected[kind]), sorted(expected[kind] - actual)
            diff[kind] = {"unexpected": unexpected, "missing": missing}
            clean = clean and not unexpected and not missing
        report["reconciliation"] = diff
        reason = "" if clean else "the account does not match the declared expectation"
    passed = report["complete"] and clean
    report["EXCHANGE_EXPOSURE_GATE"] = "PASS" if passed else "FAIL"
    report["reason"] = "" if passed else ("audit incomplete: " + "; ".join(report["errors"]) if not report["complete"] else reason)
    report["read_only_calls_made"] = sorted(set(getattr(ro, "calls", [])))
    return report


def exit_code(report: dict[str, Any]) -> int:
    if not report.get("complete"):
        return 3
    return 0 if report.get("EXCHANGE_EXPOSURE_GATE") == "PASS" else 2


def main(argv: list[str] | None = None, *, client_factory: Callable[[Any], Any] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only, account-wide exposure audit (positions, orders, all plan-order types)")
    ap.add_argument("--expect-file", type=Path, help="reconcile against a declared state (exact ids); default = expect nothing")
    args = ap.parse_args(argv)
    from app.config import Settings
    settings = Settings()
    if not _credentials_configured(settings):
        print(json.dumps({"read_only": True, "credentials_configured": False, "complete": False, "EXCHANGE_EXPOSURE_GATE": "FAIL",
                          "note": "no credentials in this environment; nothing was read and no fact is claimed"}, indent=1))
        return 3
    expected = None
    if args.expect_file is not None:
        try:
            expected = load_expectation(args.expect_file)
        except (OSError, ValueError) as exc:
            print(json.dumps({"EXCHANGE_EXPOSURE_GATE": "FAIL", "error": redact(exc)}))
            return 3
    if client_factory is not None:
        client = client_factory(settings)
    else:
        from clients.bitget_rest import BitgetRestClient
        client = BitgetRestClient(settings=settings)
    report = audit(client, expected=expected)
    print(json.dumps(report, indent=1, sort_keys=True, default=str))
    return exit_code(report)


if __name__ == "__main__":
    sys.exit(main())
