"""Maker-entry experiment (2026-07-08).

Fees waren 197% van de bruto-edge. Een post-only limit-entry betaalt maker-
i.p.v. taker-fee (Bitget ~0,02% vs 0,06%), wat de fee-drag ruwweg halveert.

Discipline: vult de limit niet binnen het wachtvenster, dan annuleren en de
trade SKIPPEN — geen taker-fallback (dat zou de besparing tenietdoen). Een
gemiste entry is geen verlies; een dure entry wel.

De prijsberekening is een pure functie (getest); de plaatsing/poll/cancel
zit in attempt_maker_entry en praat met de order-client.
"""

from __future__ import annotations

import time
from typing import Any

from execution.entry_submitter import RESULT_ACCEPTED, RESULT_ADOPTED
from execution.order_identity import ENTRY_LEG_MAKER


def compute_limit_price(direction: str, anchor_price: float, offset_bps: float) -> float:
    """Post-only limit net binnen de markt.

    LONG: iets ONDER de marktprijs (koper wacht op een dip) -> maker.
    SHORT: iets BOVEN de marktprijs (verkoper wacht op een tik omhoog) -> maker.
    Zo kruist de order de spread niet en vult hij als maker.
    """
    direction = str(direction).upper()
    if anchor_price <= 0:
        return 0.0
    factor = offset_bps / 10000.0
    if direction == "LONG":
        return round(anchor_price * (1.0 - factor), 10)
    return round(anchor_price * (1.0 + factor), 10)


def _fill_state(metrics: dict[str, Any]) -> tuple[float, str]:
    """(gevulde hoeveelheid, state) uit extract_fill_metrics-achtige dict."""
    try:
        qty = float(metrics.get("filled_qty") or 0.0)
    except (TypeError, ValueError):
        qty = 0.0
    return qty, str(metrics.get("state") or "").lower()


def attempt_maker_entry(
    client, settings, symbol, direction, size, anchor_price, hold_side, log, submit=None
) -> dict[str, Any]:
    """Plaats een post-only limit en wacht kort op fill.

    Returns dict: {status, filled_qty, fill_entry, order_id, payload, client_oid}.
    status in {'FILLED','UNFILLED_CANCELLED','ERROR','BLOCKED_UNKNOWN'}.

    ``submit`` is de idempotente inzendlaag (EntryOrderSubmitter.submit_entry):
    hij krijgt de plaatsingsfunctie, voorziet die van een deterministische
    clientOid en verzoent na een dubbelzinnig antwoord met de exchange in plaats
    van blind opnieuw in te zenden. Zonder ``submit`` valt deze functie terug op
    een rechtstreekse plaatsing (alleen gebruikt in geïsoleerde tests).
    """
    offset_bps = float(getattr(settings, "maker_entry_offset_bps", 1.0))
    wait_s = float(getattr(settings, "maker_entry_wait_seconds", 4.0))
    poll_s = max(0.25, float(getattr(settings, "maker_entry_poll_seconds", 1.0)))
    limit_price = compute_limit_price(direction, anchor_price, offset_bps)

    result = {
        "status": "ERROR", "filled_qty": 0.0, "fill_entry": 0.0,
        "order_id": "", "payload": None, "client_oid": "", "message": "",
    }
    if limit_price <= 0:
        return result

    def _place(client_oid: str | None = None):
        return client.place_futures_limit_order(
            symbol=symbol, direction=direction, size=size, price=limit_price,
            margin_mode="isolated", post_only=True, client_oid=client_oid,
        )

    if submit is not None:
        submission = submit(_place)
        result["client_oid"] = submission.client_oid
        result["payload"] = submission.payload
        result["order_id"] = submission.order_id or ""
        result["message"] = submission.message
        if submission.blocks_new_entries:
            result["status"] = "BLOCKED_UNKNOWN"
            return result
        if not submission.has_live_order:
            log.warning(
                "MAKER_ENTRY_NOT_CREATED | %s | status=%s | classification=%s | client_oid=%s",
                symbol, submission.status, submission.classification, submission.client_oid,
            )
            result["status"] = "ERROR"
            return result
        order_id = submission.order_id
    else:
        try:
            payload = _place()
            order_id = client.extract_order_id(payload)
            result["order_id"] = order_id or ""
            result["payload"] = payload
            if not order_id:
                log.warning("MAKER_ENTRY_NO_ORDER_ID | %s | payload=%s", symbol, payload)
                result["status"] = "ERROR"
                return result
        except Exception as exc:
            log.warning("MAKER_ENTRY_PLACE_FAILED | %s | error=%s", symbol, exc)
            return result

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            detail = client.get_order_detail(symbol=symbol, order_id=order_id)
            metrics = client.extract_fill_metrics(detail)
            qty, state = _fill_state(metrics)
            if qty > 0 and state in ("filled", "full-fill", "partially_filled", "partial-fill"):
                fill_entry = 0.0
                try:
                    fill_entry = float(metrics.get("avg_price") or 0.0)
                except (TypeError, ValueError):
                    fill_entry = 0.0
                log.warning(
                    "MAKER_ENTRY_FILLED | %s | order_id=%s | qty=%s | avg_price=%s | state=%s",
                    symbol, order_id, qty, fill_entry, state,
                )
                result.update(status="FILLED", filled_qty=qty, fill_entry=fill_entry)
                return result
        except Exception as exc:
            log.warning("MAKER_ENTRY_POLL_FAILED | %s | order_id=%s | error=%s", symbol, order_id, exc)

    # Niet (volledig) gevuld binnen het venster -> annuleren.
    try:
        client.cancel_futures_order(symbol=symbol, order_id=order_id)
        log.warning("MAKER_ENTRY_UNFILLED_CANCELLED | %s | order_id=%s | wait_s=%.1f", symbol, order_id, wait_s)
    except Exception as exc:
        # Cancel kan falen (bv. code 43001 'order bestaat niet') als de order
        # net vulde in de race tussen laatste poll en cancel. Dan staat er een
        # ONBESCHERMDE positie open -> die MOETEN we beschermen, niet skippen.
        log.warning("MAKER_ENTRY_CANCEL_FAILED | %s | order_id=%s | error=%s", symbol, order_id, exc)

    # Safety-net: verifieer altijd of er tóch een positie is ontstaan.
    try:
        positions = (client.get_all_positions().get("data") or [])
        for p in positions:
            if str(p.get("symbol") or "") != str(symbol):
                continue
            live_size = 0.0
            for k in ("total", "size", "available", "holdVol", "positionSize"):
                try:
                    live_size = float(p.get(k) or 0.0)
                except (TypeError, ValueError):
                    live_size = 0.0
                if live_size > 0:
                    break
            if live_size > 0:
                fill_entry = 0.0
                for k in ("openPriceAvg", "averageOpenPrice", "openAvgPrice", "avgOpenPrice", "openPrice"):
                    try:
                        fill_entry = float(p.get(k) or 0.0)
                    except (TypeError, ValueError):
                        fill_entry = 0.0
                    if fill_entry > 0:
                        break
                log.critical(
                    "MAKER_ENTRY_FILLED_DURING_CANCEL | %s | order_id=%s | size=%s | avg=%s -> beschermen i.p.v. skippen",
                    symbol, order_id, live_size, fill_entry,
                )
                result.update(status="FILLED", filled_qty=live_size, fill_entry=fill_entry)
                return result
    except Exception as exc:
        log.warning("MAKER_ENTRY_POSTCANCEL_VERIFY_FAILED | %s | error=%s", symbol, exc)

    result["status"] = "UNFILLED_CANCELLED"
    return result
# Long-lived maker grids use the same write-ahead/idempotent submitter as every
# other production entry. This helper lives here so the raw transport call
# remains confined to the repository's audited maker-entry boundary.
def submit_persistent_maker_entry(*, submitter, client, plan, size, price, notional_usdt):
    result = submitter.submit_entry(
        plan=plan,
        size=size,
        side="buy",
        order_type="limit",
        leg=ENTRY_LEG_MAKER,
        place=lambda client_oid: client.place_futures_limit_order(
            symbol=plan.symbol,
            direction="LONG",
            size=size,
            price=price,
            margin_mode="isolated",
            post_only=True,
            client_oid=client_oid,
        ),
        notional_usdt=notional_usdt,
    )
    if result.status not in {RESULT_ACCEPTED, RESULT_ADOPTED}:
        raise RuntimeError(
            f"maker entry blocked: status={result.status} classification={result.classification}"
        )
    return result
