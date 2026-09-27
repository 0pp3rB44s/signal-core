"""OWNER-RUN, DEMO-ONLY probe of how Bitget materialises an order-level preset stop. Writes the process-down ATTESTATION.

    BITGET_DEMO_API_KEY=... BITGET_DEMO_API_SECRET=... BITGET_DEMO_API_PASSPHRASE=... \\
        .venv/bin/python -m lsrl.tools.probe_preset_stop --out attestation.json
    .venv/bin/python -m lsrl.tools.probe_preset_stop --dry-run                       # prints the plan, contacts nothing
    ... -m lsrl.tools.probe_preset_stop --verify-ids attestation.json                # read-only re-query of every recorded id

WHY. LSRL's process-down protection (`lsrl.adapters.process_down`) needs facts the Bitget documentation does not give and
this repository's 2026-08-01 incident shows are dangerous to guess: what a `presetStopLossPrice` becomes at a fill
(position-level `pos_loss` or per-order `loss_plan`), whether a partial fill and a second fill stay covered, whether the stop
survives the settle window, whether it can be MOVED with the mechanism the executor uses (and whether the new stop can be
placed BEFORE the old one is cancelled), what a rung that fills after the move does to a moved stop, and whether anything is
left behind. This tool observes exactly that, ON BOTH SIDES (LSRL trades both), on a Bitget DEMO account and writes a
machine-checkable attestation; MICRO_LIVE refuses to construct its executor without one that is bound to the release SHA and
pinned by a digest supplied outside the file.

WHAT IT CAN AND CANNOT DO.
  * DEMO ONLY. It reads only BITGET_DEMO_* variables (never the live ones), always sends the `paptrading: 1` header, and
    refuses any symbol that is not a demo symbol (`S...` under SUSDT-FUTURES). There is no flag or code path for a live
    endpoint. Demo orders spend virtual funds; still, nothing here is run by an agent: the OWNER runs it.
  * It operates on ONE symbol that must start out completely clean (no position, no order, no plan order) and leaves it
    clean: a `finally` cancels its own orders, its plan orders and closes its position.
  * Minimum size, except the partial-fill observation: producing a partial fill needs an order larger than the top of the book,
    so that one order is sized just above it, capped by PARTIAL_FILL_MAX_NOTIONAL_USDT of VIRTUAL demo funds. If the book is
    too thick for the cap the check is FAILED as `not_exercised`; it is never assumed.
  * It never prints a credential; errors are redacted.
  * The attestation embeds the raw exchange evidence (order ids, client ids, fill rows, plan rows, exchange timestamps) so a
    reviewer can re-query the exchange read-only (`--verify-ids`). It is unsigned JSON: its authority comes only from the
    digest pinned OUTSIDE the file and the reviewer's own re-query.
  * Demo semantics equal live semantics only as far as Bitget makes them so; the attestation records its environment and the
    release policy (an owner decision) decides whether BITGET_DEMO is acceptable or a minimal live test is required.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from lsrl.adapters.process_down import (ATTESTATION_KIND, ATTESTATION_SCHEMA, MAX_VALIDITY_S, REQUIRED_CHECKS,
                                        SIDES as SIDE_NAMES)

DEMO_PRODUCT_TYPE = "SUSDT-FUTURES"
DEMO_MARGIN_COIN = "SUSDT"
BASE_URL = "https://api.bitget.com"
ENV_KEY, ENV_SECRET, ENV_PASS = "BITGET_DEMO_API_KEY", "BITGET_DEMO_API_SECRET", "BITGET_DEMO_API_PASSPHRASE"
VALIDITY_S = 7 * 24 * 3600
MAX_PROBE_NOTIONAL_USDT = 60.0
#: Only the partial-fill observation may exceed the minimum lot (it must out-size the top of the book); VIRTUAL demo funds only.
PARTIAL_FILL_MAX_NOTIONAL_USDT = 3000.0
PARTIAL_FILL_ATTEMPTS = 3
#: Stop distances (in ticks) from the entry price. The strategy's stop is 0.15 x ATR(1m) beyond the sweep extreme, and the E5 rung
#: IS the extreme, so real preset stops sit a handful of ticks from the resting entry: 1 tick is the tightest a stop can be.
GEOMETRY_TICKS = (1, 2, 5, 20, 60)
_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")

#: The complete list of endpoints this tool may touch. The tests fail if any request leaves this list.
ALLOWED = {
    ("GET", "/api/v2/mix/market/contracts"), ("GET", "/api/v2/mix/market/ticker"),
    ("GET", "/api/v2/mix/account/account"), ("GET", "/api/v2/mix/position/all-position"),
    ("GET", "/api/v2/mix/order/orders-pending"), ("GET", "/api/v2/mix/order/orders-plan-pending"),
    ("GET", "/api/v2/mix/order/detail"), ("GET", "/api/v2/mix/order/fills"),
    ("POST", "/api/v2/mix/order/place-order"), ("POST", "/api/v2/mix/order/cancel-order"),
    ("POST", "/api/v2/mix/order/place-tpsl-order"), ("POST", "/api/v2/mix/order/place-pos-tpsl"),
    ("POST", "/api/v2/mix/order/cancel-plan-order"), ("POST", "/api/v2/mix/order/close-positions"),
}
#: `--verify-ids` may only READ two endpoints. Nothing else.
READONLY_ALLOWED = {("GET", "/api/v2/mix/order/detail"), ("GET", "/api/v2/mix/order/fills")}


class ProbeAborted(RuntimeError):
    """A precondition failed: nothing was placed and no attestation is written."""


class ProbeHttpError(RuntimeError):
    pass


def redact(text: object) -> str:
    return _TOKEN.sub("<redacted>", str(text))[:240]


class Transport(Protocol):
    def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> dict[str, Any]: ...


class DemoTransport:
    """Signed REST against Bitget with the demo header. Uses the repository's own signer."""

    def __init__(self, key: str, secret: str, passphrase: str, *, timeout: float = 15.0, session: Any = None,
                 allowed: set[tuple[str, str]] | None = None) -> None:
        self._key, self._secret, self._pass, self._timeout = key, secret, passphrase, timeout
        self._allowed = ALLOWED if allowed is None else allowed
        if session is None:
            import requests
            session = requests
        self._session = session

    def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> dict[str, Any]:
        if (method.upper(), path) not in self._allowed:
            raise ProbeAborted(f"endpoint not allow-listed: {method} {path}")
        from clients.bitget_auth import build_headers
        raw = None if body is None else json.dumps(body, separators=(",", ":"))
        headers = build_headers(api_key=self._key, api_secret=self._secret, passphrase=self._pass, method=method,
                                request_path=path, params=params, body=raw)
        headers["paptrading"] = "1"                                   # the DEMO environment, always
        kwargs: dict[str, Any] = {"method": method.upper(), "url": BASE_URL + path, "params": params,
                                  "headers": headers, "timeout": self._timeout}
        if raw is not None:
            kwargs["data"] = raw
        try:
            resp = self._session.request(**kwargs)
            payload = resp.json()
        except Exception as exc:                                     # noqa: BLE001
            raise ProbeHttpError(f"{type(exc).__name__}: {redact(exc)}") from None
        if not isinstance(payload, dict) or str(payload.get("code")) != "00000":
            code = payload.get("code") if isinstance(payload, dict) else "?"
            msg = payload.get("msg") if isinstance(payload, dict) else str(payload)
            raise ProbeHttpError(f"code={code} msg={redact(msg)}")
        return payload


def _rows(payload: Any) -> list[dict[str, Any]]:
    data = (payload or {}).get("data")
    if isinstance(data, dict):
        for key in ("entrustedList", "list", "orderList", "planList", "fillList"):
            if isinstance(data.get(key), list):
                return [r for r in data[key] if isinstance(r, dict)]
        return []
    return [r for r in (data or []) if isinstance(r, dict)]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def git_state(root: Path) -> tuple[str, bool]:
    def git(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=root, timeout=10).decode().strip()
    return git("rev-parse", "HEAD"), not git("status", "--porcelain", "--untracked-files=no")


@dataclass(frozen=True)
class Side:
    """LSRL trades both directions: every observation is made once per side. d = +1 long, -1 short."""
    name: str
    open_side: str
    d: int

    def away(self, price: float, frac: float) -> float:          # the passive side of `price`: below for a long, above for a short
        return price * (1 - self.d * frac)

    def cross(self, price: float, frac: float) -> float:         # marketable: through the opposite best price
        return price * (1 + self.d * frac)


LONG, SHORT = Side("long", "buy", 1), Side("short", "sell", -1)
SIDE_DEFS = (LONG, SHORT)
assert tuple(s.name for s in SIDE_DEFS) == SIDE_NAMES


class Probe:
    def __init__(self, transport: Transport, *, code_sha: str, clean_tree: bool, harness_sha256: str,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 settle_s: float = 3.0, appear_timeout_s: float = 12.0, poll_s: float = 0.5) -> None:
        self.t, self.code_sha, self.clean_tree, self.harness_sha256 = transport, code_sha, clean_tree, harness_sha256
        self._sleep, self._clock = sleep, clock
        self.settle_s, self.appear_timeout_s, self.poll_s = settle_s, appear_timeout_s, poll_s
        self.checks: list[dict[str, Any]] = []
        self.observations: dict[str, Any] = {}
        self.evidence: dict[str, Any] = {"orders": [], "plans": [], "fills": []}
        self.models: dict[str, str] = {}                # side -> POSITION_LEVEL / PLAN_LEVEL, as CREATED BY THE EXCHANGE
        self.place_first: dict[str, bool] = {}          # side -> a second loss_plan placed beside the live one survived
        self.sides_done: list[str] = []
        self.symbol = ""
        self.contract: dict[str, Any] = {}
        self._created: list[str] = []
        self._ids: dict[str, str] = {}
        self._run = uuid.uuid4().hex[:8]
        self._started = 0.0
        self._owned = False        # True only once the symbol has been PROVEN clean: from then on everything on it is ours

    # ----------------------------------------------------------------- transport helpers
    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return self.t.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.t.request("POST", path, body=body)

    def _plans(self) -> list[dict[str, Any]]:
        return [p for p in _rows(self._get("/api/v2/mix/order/orders-plan-pending", productType=DEMO_PRODUCT_TYPE,
                                           symbol=self.symbol, planType="profit_loss"))]

    def _pending(self) -> list[dict[str, Any]]:
        return _rows(self._get("/api/v2/mix/order/orders-pending", productType=DEMO_PRODUCT_TYPE, symbol=self.symbol))

    @staticmethod
    def _ptype(plan: dict[str, Any]) -> str:
        return str(plan.get("planType") or plan.get("orderType") or "").lower()

    def _losses(self, side: str = "long") -> list[dict[str, Any]]:
        out = []
        for p in self._plans():
            hold = str(p.get("posSide") or p.get("holdSide") or "").lower()
            if "loss" in self._ptype(p) and (not hold or hold == side):
                out.append(p)
        return out

    @staticmethod
    def _trigger(plan: dict[str, Any]) -> float:
        return _f(plan.get("triggerPrice") or plan.get("planTriggerPrice") or plan.get("stopLossTriggerPrice"), -1.0)

    @staticmethod
    def _pid(plan: dict[str, Any]) -> str:
        return str(plan.get("orderId") or plan.get("planOrderId") or "")

    def _position(self, side: str = "long") -> float:
        rows = _rows(self._get("/api/v2/mix/position/all-position", productType=DEMO_PRODUCT_TYPE,
                               marginCoin=DEMO_MARGIN_COIN))
        return sum(_f(r.get("total") or r.get("available")) for r in rows
                   if str(r.get("symbol") or "").upper() == self.symbol and str(r.get("holdSide") or "").lower() == side)

    def _near(self, a: float, b: float) -> bool:
        return abs(a - b) <= self.contract["tick"] * 2

    def _wait(self, predicate: Callable[[], Any], timeout_s: float | None = None) -> tuple[Any, float]:
        start, limit = self._clock(), self.appear_timeout_s if timeout_s is None else timeout_s
        while True:
            value = predicate()
            if value:
                return value, self._clock() - start
            if self._clock() - start >= limit:
                return value, self._clock() - start
            self._sleep(self.poll_s)

    def _check(self, base: str, side: Side | None, passed: bool, observed: dict[str, Any]) -> bool:
        check_id = base if side is None else f"{base}:{side.name}"
        self.checks.append({"id": check_id, "passed": bool(passed), "observed": observed})
        return bool(passed)

    # ----------------------------------------------------------------- the raw exchange evidence
    def _detail(self, oid: str) -> dict[str, Any]:
        return (self._get("/api/v2/mix/order/detail", symbol=self.symbol, productType=DEMO_PRODUCT_TYPE,
                          clientOid=oid).get("data")) or {}

    def _record_order(self, oid: str, purpose: str, row: dict[str, Any] | None) -> None:
        row = row or {}
        self.evidence["orders"].append({
            "purpose": purpose, "clientOid": oid, "orderId": self._ids.get(oid) or str(row.get("orderId") or ""),
            "symbol": self.symbol, "state": row.get("state"), "side": row.get("side"), "tradeSide": row.get("tradeSide"),
            "price": row.get("price"), "size": row.get("size"), "priceAvg": row.get("priceAvg"),
            "baseVolume": row.get("baseVolume"), "presetStopLossPrice": row.get("presetStopLossPrice"),
            "presetStopLossType": row.get("presetStopLossType"), "cTime": row.get("cTime"), "uTime": row.get("uTime")})

    def _snapshot(self, oid: str, purpose: str) -> dict[str, Any]:
        try:
            row = self._detail(oid)
        except ProbeHttpError as exc:
            self._record_order(oid, purpose, {})
            self.evidence["orders"][-1]["error"] = str(exc)
            return {}
        self._record_order(oid, purpose, row)
        return row

    def _note_plans(self, step: str, plans: list[dict[str, Any]]) -> None:
        for p in plans:
            self.evidence["plans"].append({"step": step, "orderId": self._pid(p), "planType": self._ptype(p),
                                           "holdSide": p.get("posSide") or p.get("holdSide"),
                                           "triggerPrice": self._trigger(p), "size": p.get("size"), "cTime": p.get("cTime")})

    def _note_fills(self, oid: str) -> None:
        order_id = self._ids.get(oid, "")
        if not order_id:
            return
        try:
            rows = _rows(self._get("/api/v2/mix/order/fills", symbol=self.symbol, productType=DEMO_PRODUCT_TYPE,
                                   orderId=order_id))
        except ProbeHttpError as exc:
            self.evidence["fills"].append({"orderId": order_id, "error": str(exc)})
            return
        for r in rows:
            self.evidence["fills"].append({"orderId": order_id, "tradeId": r.get("tradeId"), "price": r.get("price"),
                                           "baseVolume": r.get("baseVolume"), "cTime": r.get("cTime")})

    # ----------------------------------------------------------------- preconditions (abort, place nothing)
    def preconditions(self) -> None:
        if not self.clean_tree or len(self.code_sha) != 40:
            raise ProbeAborted("the working tree is not a clean commit: an attestation must bind to a clean release SHA")
        rows = _rows(self._get("/api/v2/mix/market/contracts", productType=DEMO_PRODUCT_TYPE))
        sol = [r for r in rows if str(r.get("baseCoin") or "").upper() == "SOL"]
        if len(sol) != 1:
            raise ProbeAborted(f"expected exactly one SOL demo contract under {DEMO_PRODUCT_TYPE}, found {len(sol)}")
        row = sol[0]
        self.symbol = str(row.get("symbol") or "").upper()
        if not self.symbol.startswith("S") or self.symbol == "SOLUSDT":
            raise ProbeAborted(f"{self.symbol!r} does not look like a demo symbol; refusing")
        place = int(_f(row.get("pricePlace"), 0))
        self.contract = {"symbol": self.symbol, "tick": _f(row.get("priceEndStep"), 1.0) * 10 ** -place,
                         "step": _f(row.get("sizeMultiplier"), 0.0) or 10 ** -int(_f(row.get("volumePlace"), 1)),
                         "min_qty": _f(row.get("minTradeNum")), "min_notional": _f(row.get("minTradeUSDT"), 5.0),
                         "price_place": place, "volume_place": int(_f(row.get("volumePlace"), 1))}
        acct = ((self._get("/api/v2/mix/account/account", symbol=self.symbol, productType=DEMO_PRODUCT_TYPE,
                           marginCoin=DEMO_MARGIN_COIN).get("data")) or {})
        if str(acct.get("posMode") or "").lower() != "hedge_mode":
            raise ProbeAborted(f"the demo account is not in hedge mode (posMode={acct.get('posMode')!r}); LSRL needs hedge mode")
        if str(acct.get("marginMode") or "").lower() != "isolated":
            raise ProbeAborted(f"the demo account is not isolated margin (marginMode={acct.get('marginMode')!r})")
        if (self._position("long") + self._position("short") > 0 or self._pending() or self._plans()):
            raise ProbeAborted(f"{self.symbol} is not clean (position, order or plan order exists): refusing to operate on it")

    # ----------------------------------------------------------------- pricing
    def _snap(self, price: float) -> float:
        tick = self.contract["tick"]
        return round(round(price / tick) * tick, self.contract["price_place"])

    def _ceil_step(self, qty: float) -> float:
        step = self.contract["step"]
        return round(math.ceil(qty / step - 1e-9) * step, self.contract["volume_place"])

    def _qty(self, price: float) -> float:
        need = max(self.contract["min_qty"], (self.contract["min_notional"] * 1.3) / price)
        qty = self._ceil_step(need)
        if qty * price > MAX_PROBE_NOTIONAL_USDT:
            raise ProbeAborted(f"the minimum probe order would be {qty * price:.2f} USDT (limit {MAX_PROBE_NOTIONAL_USDT})")
        return qty

    def _ticker(self) -> dict[str, float]:
        row = (_rows(self._get("/api/v2/mix/market/ticker", symbol=self.symbol, productType=DEMO_PRODUCT_TYPE)) or [{}])[0]
        last = _f(row.get("lastPr") or row.get("last"))
        if last <= 0:
            raise ProbeAborted("no ticker price")
        return {"last": last, "ask": _f(row.get("askPr") or row.get("bestAsk"), last),
                "bid": _f(row.get("bidPr") or row.get("bestBid"), last),
                "ask_sz": _f(row.get("askSz")), "bid_sz": _f(row.get("bidSz"))}

    def _order(self, s: Side, tag: str, price: float, qty: float, preset: float | None, *, tif: str) -> str:
        oid = f"lsrlprobe-{self._run}-{s.name[0]}-{tag}"
        body: dict[str, Any] = {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginMode": "isolated",
                                "marginCoin": DEMO_MARGIN_COIN, "size": str(qty), "price": str(price), "side": s.open_side,
                                "tradeSide": "open", "orderType": "limit", "force": tif, "clientOid": oid}
        if preset is not None:
            body["presetStopLossPrice"] = str(preset)
        res = self._post("/api/v2/mix/order/place-order", body)
        self._created.append(oid)
        self._ids[oid] = str(((res or {}).get("data") or {}).get("orderId") or "")
        return oid

    def _close_order(self, s: Side, tag: str, price: float, qty: float) -> str:
        """A reduce-only post-only close: exactly what the executor rests as a take-profit (`place_futures_limit_close_order`)."""
        from clients.bitget_order_client import close_side_for_hold_side
        oid = f"lsrlprobe-{self._run}-{s.name[0]}-{tag}"
        body = {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN,
                "marginMode": "isolated", "size": str(qty), "price": str(price), "side": close_side_for_hold_side(s.name),
                "tradeSide": "close", "orderType": "limit", "force": "post_only", "holdSide": s.name,
                "reduceOnly": "YES", "clientOid": oid}
        res = self._post("/api/v2/mix/order/place-order", body)
        self._created.append(oid)
        self._ids[oid] = str(((res or {}).get("data") or {}).get("orderId") or "")
        return oid

    def _cancel_one(self, oid: str) -> None:
        self._post("/api/v2/mix/order/cancel-order", {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE,
                                                      "marginCoin": DEMO_MARGIN_COIN, "clientOid": oid})

    # ----------------------------------------------------------------- the observations
    def run(self) -> dict[str, Any]:
        self._started = self._clock()
        try:
            self.preconditions()
            self._owned = True
            for s in SIDE_DEFS:
                try:
                    self._run_side(s)
                    self.sides_done.append(s.name)
                except ProbeAborted:
                    raise
                except Exception as exc:                             # noqa: BLE001 - one side failing must not hide the other
                    text = f"{type(exc).__name__}: {redact(exc)}"
                    self.observations.setdefault("side_errors", {})[s.name] = text
                    self.observations.setdefault("unexpected_error", text)
                    self._flatten(s)
            self._check_model_same_on_both_sides()
        except ProbeAborted:
            self._final_cleanup()
            raise
        except Exception as exc:                                     # noqa: BLE001 - an unexpected failure is a FAIL, never a pass
            self.observations["unexpected_error"] = f"{type(exc).__name__}: {redact(exc)}"
        finally:
            self._final_cleanup()
        return self.attestation()

    def _run_side(self, s: Side) -> None:
        tk = self._ticker()
        qty = self._qty(tk["last"])
        self.observations.setdefault("reference", {})[s.name] = {"last": tk["last"], "qty": qty}
        self._check_preset_accepted_and_unfilled_cancel(s, tk["last"], qty)
        self._check_geometry(s, tk["last"], qty)
        self._check_partial_fill(s)
        fresh = self._ticker()
        ref = fresh["ask"] if s.d > 0 else fresh["bid"]
        preset = self._snap(s.away(ref, 0.05))
        model = self._check_fill_creates_stop(s, ref, qty, preset)
        if model:
            self.models[s.name] = model
            self._check_second_fill_covered(s, ref, qty, preset)
            self._check_survives_settle(s, preset)
            new = self._snap(s.away(fresh["last"], 0.04))              # a better (tighter) stop than the preset
            self._check_stop_move(s, model, fresh["last"], qty, new)
            self._check_later_fill(s, model, ref, qty, preset, new)
        self._check_cleanup(s)

    def _check_preset_accepted_and_unfilled_cancel(self, s: Side, last: float, qty: float) -> None:
        resting_price, stop = self._snap(s.away(last, 0.10)), self._snap(s.away(last, 0.15))
        try:
            oid = self._order(s, "resting", resting_price, qty, stop, tif="post_only")
            detail = self._snapshot(oid, "preset_accepted_on_limit_order")
            echoed = _f(detail.get("presetStopLossPrice"), -1.0)
            accepted = abs(echoed - stop) <= self.contract["tick"] and str(detail.get("state")).lower() in ("live", "new")
            self._check("preset_accepted_on_limit_order", s, accepted,
                        {"sent": stop, "echoed": echoed, "state": detail.get("state"), "type": detail.get("presetStopLossType")})
        except ProbeHttpError as exc:
            self._check("preset_accepted_on_limit_order", s, False, {"error": str(exc)})
            self._check("unfilled_cancel_leaves_no_stop", s, False, {"skipped": "order not placed"})
            return
        try:
            self._cancel_one(oid)
            self._sleep(self.settle_s)
            left = self._plans()
            self._note_plans(f"unfilled_cancel:{s.name}", left)
            self._check("unfilled_cancel_leaves_no_stop", s, not left,
                        {"plans_after_cancel": [(self._ptype(p), self._trigger(p)) for p in left]})
        except ProbeHttpError as exc:
            self._check("unfilled_cancel_leaves_no_stop", s, False, {"error": str(exc)})

    def _check_geometry(self, s: Side, last: float, qty: float) -> None:
        """A preset stop this close to its own entry is what the strategy sends for the rung at the sweep extreme. A rejection is
        fail-safe (no entry rests) but would silently thin the ladder, so every distance has to be accepted."""
        entry = self._snap(s.away(last, 0.005))                   # passive: post-only can never cross
        accepted: list[int] = []
        rejected: list[dict[str, Any]] = []
        for ticks in GEOMETRY_TICKS:
            stop = self._snap(entry - s.d * ticks * self.contract["tick"])
            try:
                oid = self._order(s, f"geo{ticks}", entry, qty, stop, tif="post_only")
            except ProbeHttpError as exc:
                rejected.append({"ticks": ticks, "error": str(exc)})
                continue
            row = self._snapshot(oid, f"geometry_{ticks}_ticks")
            if abs(_f(row.get("presetStopLossPrice"), -1.0) - stop) <= self.contract["tick"] / 2 \
                    and str(row.get("state")).lower() in ("live", "new"):
                accepted.append(ticks)
            else:
                rejected.append({"ticks": ticks, "echoed": row.get("presetStopLossPrice"), "state": row.get("state")})
            try:
                self._cancel_one(oid)
            except ProbeHttpError:
                pass
        self._sleep(self.settle_s)
        left = self._plans()
        self._note_plans(f"geometry:{s.name}", left)
        self._check("preset_accepted_at_strategy_geometry", s, not rejected and not left,
                    {"entry": entry, "accepted_ticks": accepted, "rejected": rejected,
                     "min_accepted_ticks": min(accepted) if accepted else None,
                     "plans_left": [(self._ptype(p), self._trigger(p)) for p in left]})

    # ------------------------------------------------------------------ partial fill
    def _partial_attempt(self, s: Side, attempt: int) -> dict[str, Any]:
        """One try: an order sized JUST ABOVE the top of the book at the best opposing price fills the top level and rests the rest."""
        tk = self._ticker()
        ref, top = (tk["ask"], tk["ask_sz"]) if s.d > 0 else (tk["bid"], tk["bid_sz"])
        if top <= 0:
            return {"exercised": False, "reason": "the ticker gave no top-of-book size"}
        qty = self._ceil_step(top * 1.05 + self.contract["step"])
        if qty * ref > PARTIAL_FILL_MAX_NOTIONAL_USDT:
            return {"exercised": False, "reason": "the book is too thick for the demo cap", "top_of_book_size": top,
                    "needed_notional": round(qty * ref, 2), "cap": PARTIAL_FILL_MAX_NOTIONAL_USDT}
        preset = self._snap(s.away(ref, 0.05))
        oid = self._order(s, f"part{attempt}", self._snap(ref), qty, preset, tif="gtc")
        if not self._wait(lambda: str(self._detail(oid).get("state")).lower() == "partially_filled")[0]:
            return {"exercised": False, "reason": "the order did not end up partially filled", "top_of_book_size": top,
                    "state": str(self._detail(oid).get("state"))}
        position = self._position(s.name)
        stops, waited = self._wait(lambda: [p for p in self._losses(s.name) if self._near(self._trigger(p), preset)])
        types = sorted({self._ptype(p) for p in stops})
        covered = bool(stops) and (any(t.startswith("pos_") for t in types)
                                   or sum(_f(p.get("size")) for p in stops) >= position - self.contract["step"] / 2)
        row_partial = self._snapshot(oid, "partial_fill_while_resting")
        self._note_plans(f"partial_fill:{s.name}", stops)
        try:
            self._cancel_one(oid)
        except ProbeHttpError as exc:
            return {"exercised": False, "reason": f"the remainder could not be cancelled (filled meanwhile?): {exc}"}
        self._sleep(self.settle_s)
        final_position = self._position(s.name)
        after = [p for p in self._losses(s.name)]
        after_covered = bool(after) and all(self._near(self._trigger(p), preset) for p in after) and (
            any(self._ptype(p).startswith("pos_") for p in after)
            or sum(_f(p.get("size")) for p in after) >= final_position - self.contract["step"] / 2)
        self._snapshot(oid, "partial_fill_after_cancel")
        self._note_fills(oid)
        self._note_plans(f"partial_fill_after_cancel:{s.name}", after)
        return {"exercised": True, "order_size": qty, "top_of_book_size": top, "filled_when_partial": position,
                "filled_final": final_position, "stops_at_partial": [(self._ptype(p), self._trigger(p), _f(p.get("size")))
                                                                    for p in stops],
                "stops_after_cancel": [(self._ptype(p), self._trigger(p), _f(p.get("size"))) for p in after],
                "seconds_until_stop_visible": round(waited, 2), "state_seen": row_partial.get("state"),
                "covers_filled": covered, "covers_after_cancel": after_covered and 0 < final_position < qty}

    def _check_partial_fill(self, s: Side) -> None:
        result: dict[str, Any] = {"exercised": False, "reason": "not attempted"}
        for attempt in range(PARTIAL_FILL_ATTEMPTS):
            try:
                result = self._partial_attempt(s, attempt)
            except ProbeHttpError as exc:
                result = {"exercised": False, "reason": f"exchange refused the partial-fill order: {exc}"}
            self._flatten(s)                                              # every attempt leaves the side flat
            if result.get("exercised") or "too thick" in str(result.get("reason")):
                break
        self._check("partial_fill_stop_covers_filled", s, bool(result.get("exercised") and result.get("covers_filled")),
                    result if result.get("exercised") else {"not_exercised": True, **result})
        self._check("partial_fill_cancel_keeps_stop_for_filled_size", s,
                    bool(result.get("exercised") and result.get("covers_after_cancel")),
                    result if result.get("exercised") else {"not_exercised": True, **result})

    # ------------------------------------------------------------------ full fills
    def _check_fill_creates_stop(self, s: Side, ref: float, qty: float, preset: float) -> str | None:
        """A marketable limit fills at once. NO stop request is made by this tool: whatever exists afterwards was created by
        the exchange from the order's own preset."""
        oid = self._order(s, "fill1", self._snap(s.cross(ref, 0.003)), qty, preset, tif="gtc")
        filled, _t = self._wait(lambda: self._position(s.name) >= qty - self.contract["step"] / 2)
        self._snapshot(oid, "fill1")
        self._note_fills(oid)
        if not filled:
            self._check("stop_exists_after_fill_without_client", s, False, {"reason": "the probe order did not fill"})
            return None
        stops, waited = self._wait(lambda: [p for p in self._losses(s.name) if self._near(self._trigger(p), preset)])
        types = sorted({self._ptype(p) for p in stops})
        model = None
        if stops:
            model = "POSITION_LEVEL" if any(t.startswith("pos_") for t in types) else "PLAN_LEVEL"
        self.observations.setdefault("plan_types_after_first_fill", {})[s.name] = types
        self.observations.setdefault("seconds_until_stop_visible", {})[s.name] = round(waited, 2)
        self._note_plans(f"first_fill:{s.name}", stops)
        self._check("stop_exists_after_fill_without_client", s, bool(stops),
                    {"plan_types": types, "seconds": round(waited, 2), "count": len(stops),
                     "sizes": [_f(p.get("size")) for p in stops], "client_stop_requests": 0})
        return model

    def _covered(self, s: Side, preset: float, position: float) -> tuple[bool, dict[str, Any]]:
        losses = self._losses(s.name)
        if not losses:
            return False, {"plans": 0}
        if any(not self._near(self._trigger(p), preset) for p in losses):
            return False, {"plans": len(losses), "off_price": True}
        if any(self._ptype(p).startswith("pos_") for p in losses):
            return True, {"plans": len(losses), "position_level": True}
        covered = sum(_f(p.get("size")) for p in losses)
        return covered >= position - self.contract["step"] / 2, {"plans": len(losses), "covered": covered, "position": position}

    def _check_second_fill_covered(self, s: Side, ref: float, qty: float, preset: float) -> None:
        oid = self._order(s, "fill2", self._snap(s.cross(ref, 0.003)), qty, preset, tif="gtc")
        filled, _t = self._wait(lambda: self._position(s.name) >= 2 * qty - self.contract["step"] / 2)
        self._snapshot(oid, "fill2")
        self._note_fills(oid)
        if not filled:
            self._check("stop_covers_every_fill", s, False, {"reason": "the second probe order did not fill"})
            return
        ok, _t = self._wait(lambda: self._covered(s, preset, self._position(s.name))[0])
        _covered, detail = self._covered(s, preset, self._position(s.name))
        self._note_plans(f"second_fill:{s.name}", self._losses(s.name))
        self._check("stop_covers_every_fill", s, bool(ok), detail)

    def _check_survives_settle(self, s: Side, preset: float) -> None:
        before = sorted(self._pid(p) for p in self._losses(s.name))
        self._sleep(self.settle_s)
        losses = self._losses(s.name)
        after = sorted(self._pid(p) for p in losses)
        still = bool(losses) and all(self._near(self._trigger(p), preset) for p in losses)
        self._note_plans(f"after_settle:{s.name}", losses)
        self._check("stop_survives_settle", s, still and before == after, {"before": before, "after": after})

    # ------------------------------------------------------------------ the stop move (the executor's own mechanisms)
    def _converge_stop(self, s: Side, model: str, target: float, position: float) -> str:
        """Make the exchange hold exactly ONE stop at `target` covering the position, with the mechanism the executor uses.
        POSITION_LEVEL: rewrite the lone position-level stop IN PLACE (stop leg only, mark_price). PLAN_LEVEL: place the new
        loss_plan BESIDE the old ones first; only if the newcomer AND every old stop survive the settle window is the old set
        then cancelled (never a moment without a stop). If the exchange does not tolerate that, the legacy cancel-then-place is
        used and the capability is recorded as absent."""
        common = {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN}
        if model == "POSITION_LEVEL":
            self._post("/api/v2/mix/order/place-pos-tpsl", {**common, "marginMode": "isolated", "holdSide": s.name,
                                                            "stopLossTriggerPrice": str(target),
                                                            "stopLossTriggerType": "mark_price"})
            return "POSITION_LEVEL_STOP_ONLY"
        step = self.contract["step"]
        losses = self._losses(s.name)
        good = [p for p in losses if self._near(self._trigger(p), target) and _f(p.get("size")) >= position - step / 2]
        place = {**common, "marginMode": "isolated", "planType": "loss_plan", "triggerPrice": str(target),
                 "triggerType": "mark_price", "executePrice": "0", "holdSide": s.name, "size": str(position)}

        def cancel(plans: list[dict[str, Any]]) -> None:
            for plan in plans:
                self._post("/api/v2/mix/order/cancel-plan-order", {**common, "planType": "loss_plan", "orderId": self._pid(plan)})

        if good:                                                     # already there: only the worse extras go
            cancel([p for p in losses if self._pid(p) != self._pid(good[0])])
            return "PLAN_LEVEL_CANCEL_EXTRAS"
        old_ids = {self._pid(p) for p in losses}
        survived = False
        try:
            self._post("/api/v2/mix/order/place-tpsl-order", place)
            self._sleep(self.settle_s)
            now = self._losses(s.name)
            now_ids = {self._pid(p) for p in now}
            survived = any(self._near(self._trigger(p), target) and self._pid(p) not in old_ids for p in now) and old_ids <= now_ids
        except ProbeHttpError:
            survived = False
        self.place_first[s.name] = survived
        if survived:
            cancel([p for p in self._losses(s.name) if self._pid(p) in old_ids])
            return "PLAN_LEVEL_PLACE_FIRST"
        cancel(self._losses(s.name))                                 # the newcomer (if any) and every old stop
        self._post("/api/v2/mix/order/place-tpsl-order", place)
        return "PLAN_LEVEL_REPLACE"

    def _stop_state(self, s: Side, model: str, target: float, position: float) -> tuple[bool, list[Any]]:
        losses = self._losses(s.name)
        one = len(losses) == 1 and self._near(self._trigger(losses[0]), target)
        kind_ok = (self._ptype(losses[0]).startswith("pos_") == (model == "POSITION_LEVEL")) if losses else False
        size_ok = model == "POSITION_LEVEL" or (bool(losses) and _f(losses[0].get("size")) >= position - self.contract["step"] / 2)
        self._note_plans(f"stop_state:{s.name}", losses)
        return one and kind_ok and size_ok, [(self._ptype(p), self._trigger(p), _f(p.get("size"))) for p in losses]

    def _check_stop_move(self, s: Side, model: str, last: float, qty: float, new: float) -> None:
        position = self._position(s.name)
        tp_oid = None
        try:
            tp_oid = self._close_order(s, "tp", self._snap(s.cross(last, 0.10)), qty)          # a resting reduce-only take-profit
            has_tp_plan = any("profit" in self._ptype(p) for p in self._plans())
            mechanism = self._converge_stop(s, model, new, position)
            self._sleep(self.settle_s)                               # the 2026-08-01 lesson: read AFTER the settle window
            ok, plans_after = self._stop_state(s, model, new, position)
            tp_row = self._snapshot(tp_oid, "take_profit_after_stop_move")
            tp_ok = str(tp_row.get("state")).lower() in ("live", "new")
            self.observations.setdefault("stop_move_mechanism", {})[s.name] = mechanism
            self._check("executor_stop_move_coexists", s, ok and tp_ok,
                        {"mechanism": mechanism, "target": new, "plans_after": plans_after, "take_profit_survives": tp_ok,
                         "position_level_take_profit_plan_present": has_tp_plan, "place_first_observed": self.place_first.get(s.name)})
        except ProbeHttpError as exc:
            self._check("executor_stop_move_coexists", s, False, {"error": str(exc), "take_profit_placed": tp_oid is not None})

    def _check_later_fill(self, s: Side, model: str, ref: float, qty: float, old_preset: float, new: float) -> None:
        """A rung filling AFTER the move still carries its OLD (worse) preset. Whatever the exchange makes of it, the position
        must stay covered, and the executor's own move must be able to restore the moved stop."""
        try:
            before = [(self._ptype(p), self._trigger(p)) for p in self._losses(s.name)]
            oid = self._order(s, "fill3", self._snap(s.cross(ref, 0.003)), qty, old_preset, tif="gtc")
            self._wait(lambda: self._position(s.name) >= 3 * qty - self.contract["step"] / 2)
            self._snapshot(oid, "fill3_with_old_preset")
            self._note_fills(oid)
            self._sleep(self.settle_s)
            position = self._position(s.name)
            losses = self._losses(s.name)
            self._note_plans(f"late_fill:{s.name}", losses)
            after = [(self._ptype(p), self._trigger(p), _f(p.get("size"))) for p in losses]
            covered = bool(losses) and (any(self._ptype(p).startswith("pos_") for p in losses)
                                        or sum(_f(p.get("size")) for p in losses) >= position - self.contract["step"] / 2)
            worse_present = any(self._near(self._trigger(p), old_preset) for p in losses)
            mechanism = self._converge_stop(s, model, new, position)
            self._sleep(self.settle_s)
            repaired, plans_repaired = self._stop_state(s, model, new, position)
            self._check("later_fill_with_old_preset_is_repairable", s, covered and repaired,
                        {"stops_before": before, "stops_after_late_fill": after, "covered_after_late_fill": covered,
                         "worse_stop_present_after_late_fill": worse_present, "repair_mechanism": mechanism,
                         "stops_after_repair": plans_repaired, "repaired": repaired})
        except ProbeHttpError as exc:
            self._check("later_fill_with_old_preset_is_repairable", s, False, {"error": str(exc)})

    def _check_cleanup(self, s: Side) -> None:
        try:
            if self._position(s.name) > 0:
                self._post("/api/v2/mix/order/close-positions", {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE,
                                                                 "holdSide": s.name})
            flat, _t = self._wait(lambda: self._position(s.name) <= 0)
            self._sleep(self.settle_s)
            after_close = [(self._ptype(p), self._trigger(p)) for p in self._plans()]
            for plan in self._plans():                              # the executor's own cleanup: loss_plan / profit_plan only
                kind = self._ptype(plan)
                if kind in ("loss_plan", "profit_plan"):
                    self._post("/api/v2/mix/order/cancel-plan-order", {
                        "symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN,
                        "planType": kind, "orderId": self._pid(plan)})
            self._cancel_orders()                                    # ... and its own orphan reduce-only take-profit
            self._sleep(self.poll_s)
            remaining = [(self._ptype(p), self._trigger(p)) for p in self._plans()]
            orders_left = [str(o.get("clientOid")) for o in self._pending()]
            self._note_plans(f"after_cleanup:{s.name}", self._plans())
            self._check("cleanup_after_flat", s, bool(flat) and not remaining and not orders_left,
                        {"plans_after_close": after_close, "plans_after_executor_cleanup": remaining,
                         "orders_after_executor_cleanup": orders_left})
        except ProbeHttpError as exc:
            self._check("cleanup_after_flat", s, False, {"error": str(exc)})

    def _check_model_same_on_both_sides(self) -> None:
        long_m, short_m = self.models.get("long"), self.models.get("short")
        self._check("model_same_on_both_sides", None, bool(long_m) and long_m == short_m, {"long": long_m, "short": short_m})

    # ------------------------------------------------------------------ cleanup
    def _flatten(self, s: Side) -> None:
        """Leave one side flat and free of its stops (between sub-tests). Only ever called on a symbol proven clean at the start."""
        try:
            self._cancel_orders()                                    # no further fills; the position stays protected until closed
            if self._position(s.name) > 0:
                self._post("/api/v2/mix/order/close-positions", {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE,
                                                                 "holdSide": s.name})
                self._wait(lambda: self._position(s.name) <= 0)
                self._sleep(self.settle_s)
            self._cancel_plans()
        except Exception as exc:                                     # noqa: BLE001
            self.observations.setdefault("cleanup_errors", []).append(f"flatten: {redact(exc)}")

    def _final_cleanup(self) -> None:
        """Whatever happened, leave the demo symbol as it was found: clean. Never runs against a symbol that was not proven
        clean first: a precondition refusal must not close a position or cancel an order the probe did not create."""
        if not self.symbol or not self._owned:
            return
        for step in (self._cancel_orders, self._close, self._cancel_plans):          # close BEFORE dropping the stops
            try:
                step()
            except Exception as exc:                                 # noqa: BLE001
                self.observations.setdefault("cleanup_errors", []).append(f"{step.__name__}: {redact(exc)}")
        try:
            self.observations["final_state_clean"] = (
                self._position("long") + self._position("short") <= 0 and not self._pending() and not self._plans())
        except Exception as exc:                                     # noqa: BLE001
            self.observations["final_state_clean"] = False
            self.observations.setdefault("cleanup_errors", []).append(f"verify: {redact(exc)}")

    def _cancel_orders(self) -> None:
        for oid in self._created:
            try:
                self._cancel_one(oid)
            except ProbeHttpError:
                pass                                                 # filled or already gone

    def _cancel_plans(self) -> None:
        for plan in self._plans():
            self._post("/api/v2/mix/order/cancel-plan-order", {
                "symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN,
                "planType": self._ptype(plan) or "loss_plan", "orderId": self._pid(plan)})

    def _close(self) -> None:
        for side in SIDE_NAMES:
            if self._position(side) > 0:
                self._post("/api/v2/mix/order/close-positions", {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE,
                                                                 "holdSide": side})
                self._wait(lambda side=side: self._position(side) <= 0)

    # ----------------------------------------------------------------- the attestation
    def attestation(self) -> dict[str, Any]:
        by_id = {c["id"]: c for c in self.checks}
        missing = [c for c in REQUIRED_CHECKS if c not in by_id]
        for check_id in missing:                                     # a check that never ran is a FAILED check, never absent
            self.checks.append({"id": check_id, "passed": False, "observed": {"not_run": True}})
        passed = all(c["passed"] for c in self.checks if c["id"] in REQUIRED_CHECKS)
        clean = self.observations.get("final_state_clean") is True and "unexpected_error" not in self.observations
        both = list(self.sides_done) == list(SIDE_NAMES)
        models = {self.models.get(side) for side in SIDE_NAMES}
        model = models.pop() if len(models) == 1 and None not in models else "UNKNOWN"
        capabilities = {"plan_place_first_ok": bool(model == "PLAN_LEVEL" and both
                                                    and all(self.place_first.get(side) is True for side in SIDE_NAMES))}
        has_evidence = bool(self.evidence["orders"]) and all(o.get("orderId") for o in self.evidence["orders"])
        now = self._clock()
        return {"kind": ATTESTATION_KIND, "schema": ATTESTATION_SCHEMA,
                "decision": "PASS" if (passed and clean and both and model != "UNKNOWN" and has_evidence) else "FAIL",
                "environment": "BITGET_DEMO", "code_sha": self.code_sha, "harness_sha256": self.harness_sha256,
                "created_at": now, "expires_at": now + min(VALIDITY_S, MAX_VALIDITY_S), "observed_model": model,
                "models_by_side": dict(self.models), "sides_tested": list(self.sides_done), "capabilities": capabilities,
                "symbol_tested": self.symbol, "contract": self.contract, "checks": self.checks,
                "observations": self.observations,
                "evidence": {"run_id": self._run, "started_at": self._started, "finished_at": now,
                             "orders": self.evidence["orders"], "plans": self.evidence["plans"],
                             "fills": self.evidence["fills"]}}


# ---------------------------------------------------------------------------------------------------- reviewer re-query
IMMUTABLE_ORDER_FIELDS = ("clientOid", "symbol", "side", "tradeSide", "price", "size", "presetStopLossPrice", "cTime")
TERMINAL_ORDER_FIELDS = ("state", "priceAvg", "baseVolume")


def verify_ids(doc: dict[str, Any], transport: Transport) -> dict[str, Any]:
    """READ-ONLY: re-query every order id the attestation recorded and compare what the exchange says now with what the probe
    recorded then. Only the immutable fields (and, for an order that had already filled, its fill fields) are compared."""
    if doc.get("environment") != "BITGET_DEMO":
        raise ProbeAborted("--verify-ids re-queries a DEMO account only")
    symbol = doc.get("symbol_tested")
    checked, mismatches, missing = 0, [], []
    for rec in (doc.get("evidence") or {}).get("orders", []):
        order_id = rec.get("orderId")
        if not order_id:
            missing.append({"clientOid": rec.get("clientOid"), "reason": "no orderId recorded"})
            continue
        try:
            row = ((transport.request("GET", "/api/v2/mix/order/detail",
                                      params={"symbol": symbol, "productType": DEMO_PRODUCT_TYPE, "orderId": order_id})
                    .get("data")) or {})
        except ProbeHttpError as exc:
            missing.append({"orderId": order_id, "reason": str(exc)})
            continue
        checked += 1
        fields = IMMUTABLE_ORDER_FIELDS + (TERMINAL_ORDER_FIELDS if str(rec.get("state")).lower() == "filled" else ())
        for field in fields:
            if rec.get(field) is not None and str(row.get(field)) != str(rec.get(field)):
                mismatches.append({"orderId": order_id, "field": field, "recorded": rec.get(field), "exchange": row.get(field)})
    return {"orders_recorded": len((doc.get("evidence") or {}).get("orders", [])), "orders_checked": checked,
            "mismatches": mismatches, "not_found": missing, "ok": checked > 0 and not mismatches and not missing}


def dry_run_plan() -> list[str]:
    return [
        "0. preconditions: clean git commit; demo SOL contract under SUSDT-FUTURES; hedge mode + isolated margin; the symbol has no position, order or plan order",
        "   EVERYTHING BELOW RUNS ONCE FOR THE LONG SIDE AND ONCE FOR THE SHORT SIDE",
        "1. preset_accepted_on_limit_order: post-only entry far from market WITH presetStopLossPrice; read Get Order Detail; check the echoed value",
        "2. unfilled_cancel_leaves_no_stop: cancel it; no plan order may exist",
        "3. preset_accepted_at_strategy_geometry: resting entries whose stop is 1, 2, 5, 20 and 60 ticks from the entry (the strategy's real, tight geometry); all must be accepted",
        "4. partial_fill_stop_covers_filled + partial_fill_cancel_keeps_stop_for_filled_size: an order just larger than the top of the book at the best price partly fills; the stop must cover the filled part, and still does after the remainder is cancelled",
        "5. stop_exists_after_fill_without_client: marketable entry with the preset; NO stop request is sent; read plan orders",
        "6. stop_covers_every_fill: a second marketable entry with the same preset; the whole position stays covered",
        "7. stop_survives_settle: wait the settle window; the same stop(s) must still be there",
        "8. executor_stop_move_coexists: with a resting reduce-only take-profit on the position, move the stop with the mechanism the executor will use (plan-level: new stop placed BESIDE the old first); exactly one stop at the new price after settle; the take-profit survives",
        "9. later_fill_with_old_preset_is_repairable: a rung that fills after the move carries its OLD preset; the position stays covered and the executor's own move restores the moved stop",
        "10. cleanup_after_flat: flash-close; no plan order and no order may remain after the executor's own cleanup",
        "11. after both sides: model_same_on_both_sides; finally cancel everything, close positions, verify the symbol is clean; write the attestation with every order/plan/fill id",
    ]


def main(argv: list[str] | None = None, *, transport: Transport | None = None, root: Path | None = None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, help="where to write the attestation (must not exist)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be done and contact nothing")
    ap.add_argument("--verify-ids", type=Path, help="READ-ONLY: re-query every order id recorded in this attestation")
    args = ap.parse_args(argv)
    if args.dry_run:
        print("\n".join(dry_run_plan()))
        return 0
    if args.verify_ids is not None:
        try:
            doc = json.loads(args.verify_ids.read_text())
        except (OSError, ValueError):
            print("REFUSED: the attestation cannot be read", file=sys.stderr)
            return 2
        if transport is None:
            key, secret, phrase = (os.environ.get(n, "") for n in (ENV_KEY, ENV_SECRET, ENV_PASS))
            if not (key and secret and phrase):
                print(f"REFUSED: {ENV_KEY}, {ENV_SECRET} and {ENV_PASS} (a Bitget DEMO API key) are required", file=sys.stderr)
                return 2
            transport = DemoTransport(key, secret, phrase, allowed=READONLY_ALLOWED)
        try:
            result = verify_ids(doc, transport)
        except (ProbeAborted, ProbeHttpError) as exc:
            print(f"ABORTED: {redact(exc)}", file=sys.stderr)
            return 3
        print(json.dumps(result, indent=1))
        return 0 if result["ok"] else 1
    if args.out is None:
        print("REFUSED: --out is required", file=sys.stderr)
        return 2
    if args.out.exists():
        print("REFUSED: the output file already exists; an attestation is never overwritten", file=sys.stderr)
        return 2
    if transport is None:
        key, secret, phrase = (os.environ.get(n, "") for n in (ENV_KEY, ENV_SECRET, ENV_PASS))
        if not (key and secret and phrase):
            print(f"REFUSED: {ENV_KEY}, {ENV_SECRET} and {ENV_PASS} (a Bitget DEMO API key) are required", file=sys.stderr)
            return 2
        transport = DemoTransport(key, secret, phrase)
    here = root or Path(__file__).resolve().parents[2]
    try:
        sha, clean = git_state(here)
    except Exception:                                               # noqa: BLE001
        sha, clean = "UNKNOWN", False
    harness = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    probe = Probe(transport, code_sha=sha, clean_tree=clean, harness_sha256=harness, sleep=sleep, clock=clock)
    try:
        doc = probe.run()
    except ProbeAborted as exc:
        print(f"ABORTED (nothing was written): {redact(exc)}", file=sys.stderr)
        return 3
    except ProbeHttpError as exc:
        print(f"ABORTED (nothing was written): {redact(exc)}", file=sys.stderr)
        return 3
    args.out.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str))
    digest = hashlib.sha256(args.out.read_bytes()).hexdigest()
    print(json.dumps({"decision": doc["decision"], "observed_model": doc["observed_model"], "attestation": str(args.out),
                      "sha256": digest, "capabilities": doc["capabilities"],
                      "failed_checks": [c["id"] for c in doc["checks"] if not c["passed"]]}, indent=1))
    print("Pin the sha256 above in the reviewed release policy (LSRL_PROCESS_DOWN_ATTESTATION_SHA256); it is NOT read from the file.")
    return 0 if doc["decision"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
