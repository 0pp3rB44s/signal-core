"""OWNER-RUN, DEMO-ONLY probe of how Bitget materialises an order-level preset stop. Writes the process-down ATTESTATION.

    BITGET_DEMO_API_KEY=... BITGET_DEMO_API_SECRET=... BITGET_DEMO_API_PASSPHRASE=... \\
        .venv/bin/python -m lsrl.tools.probe_preset_stop --out attestation.json
    .venv/bin/python -m lsrl.tools.probe_preset_stop --dry-run          # prints the plan, contacts nothing

WHY. LSRL's process-down protection (`lsrl.adapters.process_down`) needs facts the Bitget documentation does not give and
this repository's 2026-08-01 incident shows are dangerous to guess: what a `presetStopLossPrice` becomes at a fill
(position-level `pos_loss` or per-order `loss_plan`), whether a second fill stays covered, whether the stop survives the
settle window, whether the stop can be MOVED with the mechanism the executor uses, and whether anything is left behind.
This tool observes exactly that on a Bitget DEMO account and writes a machine-checkable attestation; MICRO_LIVE refuses to
construct its executor without one that is bound to the release SHA and pinned by a digest supplied outside the file.

WHAT IT CAN AND CANNOT DO.
  * DEMO ONLY. It reads only BITGET_DEMO_* variables (never the live ones), always sends the `paptrading: 1` header, and
    refuses any symbol that is not a demo symbol (`S...` under SUSDT-FUTURES). There is no flag or code path for a live
    endpoint. Demo orders spend virtual funds; still, nothing here is run by an agent: the OWNER runs it.
  * It operates on ONE symbol that must start out completely clean (no position, no order, no plan order) and leaves it
    clean: a `finally` cancels its own orders, its plan orders and closes its position.
  * Minimum size only. It never prints a credential; errors are redacted.
  * Demo semantics equal live semantics only as far as Bitget makes them so; the attestation records its environment and the
    release policy decides whether BITGET_DEMO is acceptable or a minimal live test is required.
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
from pathlib import Path
from typing import Any, Callable, Protocol

from lsrl.adapters.process_down import ATTESTATION_KIND, ATTESTATION_SCHEMA, MAX_VALIDITY_S, REQUIRED_CHECKS

DEMO_PRODUCT_TYPE = "SUSDT-FUTURES"
DEMO_MARGIN_COIN = "SUSDT"
BASE_URL = "https://api.bitget.com"
ENV_KEY, ENV_SECRET, ENV_PASS = "BITGET_DEMO_API_KEY", "BITGET_DEMO_API_SECRET", "BITGET_DEMO_API_PASSPHRASE"
VALIDITY_S = 7 * 24 * 3600
MAX_PROBE_NOTIONAL_USDT = 60.0
_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")

#: The complete list of endpoints this tool may touch. The tests fail if any request leaves this list.
ALLOWED = {
    ("GET", "/api/v2/mix/market/contracts"), ("GET", "/api/v2/mix/market/ticker"),
    ("GET", "/api/v2/mix/account/account"), ("GET", "/api/v2/mix/position/all-position"),
    ("GET", "/api/v2/mix/order/orders-pending"), ("GET", "/api/v2/mix/order/orders-plan-pending"),
    ("GET", "/api/v2/mix/order/detail"),
    ("POST", "/api/v2/mix/order/place-order"), ("POST", "/api/v2/mix/order/cancel-order"),
    ("POST", "/api/v2/mix/order/place-tpsl-order"), ("POST", "/api/v2/mix/order/place-pos-tpsl"),
    ("POST", "/api/v2/mix/order/cancel-plan-order"), ("POST", "/api/v2/mix/order/close-positions"),
}


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

    def __init__(self, key: str, secret: str, passphrase: str, *, timeout: float = 15.0, session: Any = None) -> None:
        self._key, self._secret, self._pass, self._timeout = key, secret, passphrase, timeout
        if session is None:
            import requests
            session = requests
        self._session = session

    def request(self, method: str, path: str, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> dict[str, Any]:
        if (method.upper(), path) not in ALLOWED:
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
        for key in ("entrustedList", "list", "orderList", "planList"):
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


class Probe:
    def __init__(self, transport: Transport, *, code_sha: str, clean_tree: bool, harness_sha256: str,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 settle_s: float = 3.0, appear_timeout_s: float = 12.0, poll_s: float = 0.5) -> None:
        self.t, self.code_sha, self.clean_tree, self.harness_sha256 = transport, code_sha, clean_tree, harness_sha256
        self._sleep, self._clock = sleep, clock
        self.settle_s, self.appear_timeout_s, self.poll_s = settle_s, appear_timeout_s, poll_s
        self.checks: list[dict[str, Any]] = []
        self.observations: dict[str, Any] = {}
        self.symbol = ""
        self.contract: dict[str, Any] = {}
        self._created: list[str] = []
        self._run = uuid.uuid4().hex[:8]
        self._owned = False        # True only once the symbol has been PROVEN clean: from then on everything on it is ours

    # ----------------------------------------------------------------- transport helpers
    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return self.t.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self.t.request("POST", path, body=body)

    def _plans(self) -> list[dict[str, Any]]:
        return [p for p in _rows(self._get("/api/v2/mix/order/orders-plan-pending", productType=DEMO_PRODUCT_TYPE,
                                           symbol=self.symbol, planType="profit_loss"))]

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

    def _position(self, side: str = "long") -> float:
        rows = _rows(self._get("/api/v2/mix/position/all-position", productType=DEMO_PRODUCT_TYPE,
                               marginCoin=DEMO_MARGIN_COIN))
        return sum(_f(r.get("total") or r.get("available")) for r in rows
                   if str(r.get("symbol") or "").upper() == self.symbol and str(r.get("holdSide") or "").lower() == side)

    def _wait(self, predicate: Callable[[], Any], timeout_s: float | None = None) -> tuple[Any, float]:
        start, limit = self._clock(), self.appear_timeout_s if timeout_s is None else timeout_s
        while True:
            value = predicate()
            if value:
                return value, self._clock() - start
            if self._clock() - start >= limit:
                return value, self._clock() - start
            self._sleep(self.poll_s)

    def _check(self, check_id: str, passed: bool, observed: dict[str, Any]) -> bool:
        self.checks.append({"id": check_id, "passed": bool(passed), "observed": observed})
        return bool(passed)

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
        if (self._position("long") + self._position("short") > 0
                or _rows(self._get("/api/v2/mix/order/orders-pending", productType=DEMO_PRODUCT_TYPE, symbol=self.symbol))
                or self._plans()):
            raise ProbeAborted(f"{self.symbol} is not clean (position, order or plan order exists): refusing to operate on it")

    # ----------------------------------------------------------------- pricing
    def _snap(self, price: float) -> float:
        tick = self.contract["tick"]
        return round(round(price / tick) * tick, self.contract["price_place"])

    def _qty(self, price: float) -> float:
        step = self.contract["step"]
        need = max(self.contract["min_qty"], (self.contract["min_notional"] * 1.3) / price)
        qty = round(math.ceil(need / step - 1e-9) * step, self.contract["volume_place"])
        if qty * price > MAX_PROBE_NOTIONAL_USDT:
            raise ProbeAborted(f"the minimum probe order would be {qty * price:.2f} USDT (limit {MAX_PROBE_NOTIONAL_USDT})")
        return qty

    def _order(self, tag: str, price: float, qty: float, preset: float | None, *, post_only: bool) -> str:
        oid = f"lsrlprobe-{self._run}-{tag}"
        body: dict[str, Any] = {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginMode": "isolated",
                                "marginCoin": DEMO_MARGIN_COIN, "size": str(qty), "price": str(price), "side": "buy",
                                "tradeSide": "open", "orderType": "limit", "force": "post_only" if post_only else "gtc",
                                "clientOid": oid}
        if preset is not None:
            body["presetStopLossPrice"] = str(preset)
        self._post("/api/v2/mix/order/place-order", body)
        self._created.append(oid)
        return oid

    # ----------------------------------------------------------------- the observations
    def run(self) -> dict[str, Any]:
        try:
            self.preconditions()
            self._owned = True
            tick = self._get("/api/v2/mix/market/ticker", symbol=self.symbol, productType=DEMO_PRODUCT_TYPE)
            row = (_rows(tick) or [{}])[0]
            last = _f(row.get("lastPr") or row.get("last"))
            ask = _f(row.get("askPr") or row.get("bestAsk"), last)
            if last <= 0:
                raise ProbeAborted("no ticker price")
            qty = self._qty(last)
            preset = self._snap(last * 0.95)
            self.observations["reference_price"], self.observations["qty"], self.observations["preset_stop"] = last, qty, preset
            self._check_preset_accepted_and_unfilled_cancel(last, qty)
            model = self._check_fill_creates_stop(ask, qty, preset)
            if model:
                self._check_second_fill_covered(ask, qty, preset, model)
                self._check_survives_settle(preset)
                self._check_stop_move(model, last, preset)
                self._observe_later_fill_with_old_preset(ask, qty, preset)
            self._check_cleanup()
        except ProbeAborted:
            self._final_cleanup()
            raise
        except Exception as exc:                                     # noqa: BLE001 - an unexpected failure is a FAIL, never a pass
            self.observations["unexpected_error"] = f"{type(exc).__name__}: {redact(exc)}"
        finally:
            self._final_cleanup()
        return self.attestation()

    def _check_preset_accepted_and_unfilled_cancel(self, last: float, qty: float) -> None:
        resting_price, stop = self._snap(last * 0.90), self._snap(last * 0.85)
        try:
            oid = self._order("resting", resting_price, qty, stop, post_only=True)
            detail = ((self._get("/api/v2/mix/order/detail", symbol=self.symbol, productType=DEMO_PRODUCT_TYPE,
                                 clientOid=oid).get("data")) or {})
            echoed = _f(detail.get("presetStopLossPrice"), -1.0)
            accepted = abs(echoed - stop) <= self.contract["tick"] and str(detail.get("state")).lower() in ("live", "new")
            self._check("preset_accepted_on_limit_order", accepted,
                        {"sent": stop, "echoed": echoed, "state": detail.get("state"),
                         "type": detail.get("presetStopLossType")})
        except ProbeHttpError as exc:
            self._check("preset_accepted_on_limit_order", False, {"error": str(exc)})
            self._check("unfilled_cancel_leaves_no_stop", False, {"skipped": "order not placed"})
            return
        try:
            self._post("/api/v2/mix/order/cancel-order", {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE,
                                                          "marginCoin": DEMO_MARGIN_COIN, "clientOid": oid})
            self._sleep(self.settle_s)
            left = self._plans()
            self._check("unfilled_cancel_leaves_no_stop", not left,
                        {"plans_after_cancel": [(self._ptype(p), self._trigger(p)) for p in left]})
        except ProbeHttpError as exc:
            self._check("unfilled_cancel_leaves_no_stop", False, {"error": str(exc)})

    def _check_fill_creates_stop(self, ask: float, qty: float, preset: float) -> str | None:
        """A marketable limit fills at once. NO stop request is made by this tool: whatever exists afterwards was created by
        the exchange from the order's own preset."""
        self._order("fill1", self._snap(ask * 1.003), qty, preset, post_only=False)
        filled, _t = self._wait(lambda: self._position() >= qty - self.contract["step"] / 2)
        if not filled:
            self._check("stop_exists_after_fill_without_client", False, {"reason": "the probe order did not fill"})
            return None
        stops, waited = self._wait(lambda: [p for p in self._losses() if abs(self._trigger(p) - preset) <= self.contract["tick"] * 2])
        types = sorted({self._ptype(p) for p in stops})
        model = None
        if stops:
            model = "POSITION_LEVEL" if any(t.startswith("pos_") for t in types) else "PLAN_LEVEL"
        self.observations["plan_types_after_first_fill"] = types
        self.observations["seconds_until_stop_visible"] = round(waited, 2)
        self._check("stop_exists_after_fill_without_client", bool(stops),
                    {"plan_types": types, "seconds": round(waited, 2), "count": len(stops),
                     "sizes": [_f(p.get("size")) for p in stops], "client_stop_requests": 0})
        return model

    def _covered(self, model: str, preset: float, position: float) -> tuple[bool, dict[str, Any]]:
        losses = self._losses()
        if not losses:
            return False, {"plans": 0}
        if any(abs(self._trigger(p) - preset) > self.contract["tick"] * 2 for p in losses):
            return False, {"plans": len(losses), "off_price": True}
        if any(self._ptype(p).startswith("pos_") for p in losses):
            return True, {"plans": len(losses), "position_level": True}
        covered = sum(_f(p.get("size")) for p in losses)
        return covered >= position - self.contract["step"] / 2, {"plans": len(losses), "covered": covered, "position": position}

    def _check_second_fill_covered(self, ask: float, qty: float, preset: float, model: str) -> None:
        self._order("fill2", self._snap(ask * 1.003), qty, preset, post_only=False)
        filled, _t = self._wait(lambda: self._position() >= 2 * qty - self.contract["step"] / 2)
        if not filled:
            self._check("stop_covers_every_fill", False, {"reason": "the second probe order did not fill"})
            return
        ok, _t = self._wait(lambda: self._covered(model, preset, self._position())[0])
        _covered, detail = self._covered(model, preset, self._position())
        self._check("stop_covers_every_fill", bool(ok), detail)

    def _check_survives_settle(self, preset: float) -> None:
        before = sorted(str(p.get("orderId") or p.get("planOrderId")) for p in self._losses())
        self._sleep(self.settle_s)
        losses = self._losses()
        after = sorted(str(p.get("orderId") or p.get("planOrderId")) for p in losses)
        still = bool(losses) and all(abs(self._trigger(p) - preset) <= self.contract["tick"] * 2 for p in losses)
        self._check("stop_survives_settle", still and before == after, {"before": before, "after": after})

    def _check_stop_move(self, model: str, last: float, preset: float) -> None:
        """The executor's own two mechanisms, reproduced request-for-request: PLAN_LEVEL = cancel the loss plans then place one
        loss_plan for the whole position (shared client `move_futures_stop_loss`); POSITION_LEVEL = rewrite the lone
        position-level stop in place with place-pos-tpsl, stop leg only (`move_position_level_stop_only`)."""
        new = self._snap(last * 0.96)
        position = self._position()
        try:
            if model == "POSITION_LEVEL":
                mechanism = "POSITION_LEVEL_STOP_ONLY"
                has_tp = any("profit" in self._ptype(p) for p in self._plans())
                self._post("/api/v2/mix/order/place-pos-tpsl", {
                    "symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN,
                    "marginMode": "isolated", "holdSide": "long", "stopLossTriggerPrice": str(new),
                    "stopLossTriggerType": "mark_price"})
            else:
                mechanism, has_tp = "PLAN_LEVEL_REPLACE", False
                for plan in self._losses():
                    oid = plan.get("orderId") or plan.get("planOrderId")
                    self._post("/api/v2/mix/order/cancel-plan-order", {
                        "symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN,
                        "planType": "loss_plan", "orderId": str(oid)})
                self._post("/api/v2/mix/order/place-tpsl-order", {
                    "symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN,
                    "marginMode": "isolated", "planType": "loss_plan", "triggerPrice": str(new),
                    "triggerType": "mark_price", "executePrice": "0", "holdSide": "long", "size": str(position)})
            self._sleep(self.settle_s)                               # the 2026-08-01 lesson: read AFTER the settle window
            losses = self._losses()
            one = len(losses) == 1 and abs(self._trigger(losses[0]) - new) <= self.contract["tick"] * 2
            kind_ok = (self._ptype(losses[0]).startswith("pos_") == (model == "POSITION_LEVEL")) if losses else False
            size_ok = model == "POSITION_LEVEL" or (bool(losses) and _f(losses[0].get("size")) >= position - self.contract["step"] / 2)
            self.observations["stop_move_mechanism"] = mechanism
            self._check("executor_stop_move_coexists", one and kind_ok and size_ok,
                        {"mechanism": mechanism, "target": new, "plans_after": [(self._ptype(p), self._trigger(p),
                         _f(p.get("size"))) for p in losses], "position_level_take_profit_present": has_tp})
        except ProbeHttpError as exc:
            self._check("executor_stop_move_coexists", False, {"error": str(exc)})

    def _observe_later_fill_with_old_preset(self, ask: float, qty: float, old_preset: float) -> None:
        """INFORMATION, not a gate: a rung that fills after the stop was moved still carries its OLD preset. Bitget may apply
        it (the moved stop reverts to the old, worse price) - the executor's per-poll audit repairs that, but the fact is
        recorded so a reviewer can see it."""
        try:
            before = sorted((self._ptype(p), self._trigger(p)) for p in self._losses())
            self._order("fill3", self._snap(ask * 1.003), qty, old_preset, post_only=False)
            self._wait(lambda: self._position() >= 3 * qty - self.contract["step"] / 2)
            self._sleep(self.settle_s)
            after = sorted((self._ptype(p), self._trigger(p)) for p in self._losses())
            self.observations["later_fill_with_old_preset"] = {"stops_before": before, "stops_after": after,
                                                               "changed": before != after}
        except ProbeHttpError as exc:
            self.observations["later_fill_with_old_preset"] = {"error": str(exc)}

    def _check_cleanup(self) -> None:
        try:
            if self._position() > 0:
                self._post("/api/v2/mix/order/close-positions", {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE,
                                                                 "holdSide": "long"})
            flat, _t = self._wait(lambda: self._position() <= 0)
            self._sleep(self.settle_s)
            after_close = [(self._ptype(p), self._trigger(p)) for p in self._plans()]
            for plan in self._plans():                              # the executor's own cleanup: loss_plan / profit_plan only
                kind = self._ptype(plan)
                if kind in ("loss_plan", "profit_plan"):
                    self._post("/api/v2/mix/order/cancel-plan-order", {
                        "symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN,
                        "planType": kind, "orderId": str(plan.get("orderId") or plan.get("planOrderId"))})
            self._sleep(self.poll_s)
            remaining = [(self._ptype(p), self._trigger(p)) for p in self._plans()]
            self._check("cleanup_after_flat", bool(flat) and not remaining,
                        {"plans_after_close": after_close, "plans_after_executor_cleanup": remaining})
        except ProbeHttpError as exc:
            self._check("cleanup_after_flat", False, {"error": str(exc)})

    def _final_cleanup(self) -> None:
        """Whatever happened, leave the demo symbol as it was found: clean. Never runs against a symbol that was not proven
        clean first: a precondition refusal must not close a position or cancel an order the probe did not create."""
        if not self.symbol or not self._owned:
            return
        for step in (self._cancel_orders, self._cancel_plans, self._close):
            try:
                step()
            except Exception as exc:                                 # noqa: BLE001
                self.observations.setdefault("cleanup_errors", []).append(f"{step.__name__}: {redact(exc)}")
        try:
            self.observations["final_state_clean"] = (
                self._position("long") + self._position("short") <= 0
                and not _rows(self._get("/api/v2/mix/order/orders-pending", productType=DEMO_PRODUCT_TYPE, symbol=self.symbol))
                and not self._plans())
        except Exception as exc:                                     # noqa: BLE001
            self.observations["final_state_clean"] = False
            self.observations.setdefault("cleanup_errors", []).append(f"verify: {redact(exc)}")

    def _cancel_orders(self) -> None:
        for oid in self._created:
            try:
                self._post("/api/v2/mix/order/cancel-order", {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE,
                                                              "marginCoin": DEMO_MARGIN_COIN, "clientOid": oid})
            except ProbeHttpError:
                pass                                                 # filled or already gone

    def _cancel_plans(self) -> None:
        for plan in self._plans():
            self._post("/api/v2/mix/order/cancel-plan-order", {
                "symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE, "marginCoin": DEMO_MARGIN_COIN,
                "planType": self._ptype(plan) or "loss_plan", "orderId": str(plan.get("orderId") or plan.get("planOrderId"))})

    def _close(self) -> None:
        if self._position("long") > 0:
            self._post("/api/v2/mix/order/close-positions", {"symbol": self.symbol, "productType": DEMO_PRODUCT_TYPE,
                                                             "holdSide": "long"})
            self._wait(lambda: self._position() <= 0)

    # ----------------------------------------------------------------- the attestation
    def attestation(self) -> dict[str, Any]:
        by_id = {c["id"]: c for c in self.checks}
        missing = [c for c in REQUIRED_CHECKS if c not in by_id]
        for check_id in missing:                                     # a check that never ran is a FAILED check, never absent
            self.checks.append({"id": check_id, "passed": False, "observed": {"not_run": True}})
        passed = all(c["passed"] for c in self.checks if c["id"] in REQUIRED_CHECKS)
        clean = self.observations.get("final_state_clean") is True and "unexpected_error" not in self.observations
        types = self.observations.get("plan_types_after_first_fill") or []
        model = ("POSITION_LEVEL" if any(str(t).startswith("pos_") for t in types)
                 else "PLAN_LEVEL" if types else "UNKNOWN")
        now = self._clock()
        return {"kind": ATTESTATION_KIND, "schema": ATTESTATION_SCHEMA,
                "decision": "PASS" if (passed and clean and model != "UNKNOWN") else "FAIL",
                "environment": "BITGET_DEMO", "code_sha": self.code_sha, "harness_sha256": self.harness_sha256,
                "created_at": now, "expires_at": now + min(VALIDITY_S, MAX_VALIDITY_S), "observed_model": model,
                "symbol_tested": self.symbol, "contract": self.contract, "checks": self.checks,
                "observations": self.observations}


def dry_run_plan() -> list[str]:
    return [
        "1. preconditions: clean git commit; demo SOL contract under SUSDT-FUTURES; hedge mode + isolated margin; the symbol has no position, order or plan order",
        "2. preset_accepted_on_limit_order: post-only BUY far below market WITH presetStopLossPrice; read Get Order Detail; check the echoed value",
        "3. unfilled_cancel_leaves_no_stop: cancel it; no plan order may exist",
        "4. stop_exists_after_fill_without_client: marketable BUY with the preset; NO stop request is sent; read plan orders",
        "5. stop_covers_every_fill: a second marketable BUY with the same preset; the whole position stays covered",
        "6. stop_survives_settle: wait the settle window; the same stop(s) must still be there",
        "7. executor_stop_move_coexists: move the stop with the mechanism the executor will use; exactly one stop at the new price after settle",
        "8. (information) a later fill carrying the OLD preset after the move: does the stop revert?",
        "9. cleanup_after_flat: flash-close; no plan order may remain after the executor's own cleanup",
        "10. finally: cancel its orders and plan orders, close the position, verify the symbol is clean; write the attestation",
    ]


def main(argv: list[str] | None = None, *, transport: Transport | None = None, root: Path | None = None,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, help="where to write the attestation (must not exist)")
    ap.add_argument("--dry-run", action="store_true", help="print what would be done and contact nothing")
    args = ap.parse_args(argv)
    if args.dry_run:
        print("\n".join(dry_run_plan()))
        return 0
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
                      "sha256": digest, "failed_checks": [c["id"] for c in doc["checks"] if not c["passed"]]}, indent=1))
    print("Pin the sha256 above in the reviewed release policy (LSRL_PROCESS_DOWN_ATTESTATION_SHA256); it is NOT read from the file.")
    return 0 if doc["decision"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
