"""The owner-run DEMO probe: it observes how the exchange materialises a preset stop and writes the attestation MICRO_LIVE needs.

A REST-level fake of Bitget stands in for the demo account. Every way the exchange might behave (position-level stop,
per-order stops, ignoring the preset, refusing it, dropping the stop after the settle window, not covering a second fill or a
partial fill, refusing tight stops, leaving orphans behind, misbehaving on ONE side only) must give a probe result the
executor's own gate accepts (PASS) or refuses (anything else).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from lsrl.adapters.process_down import REQUIRED_CHECKS, AttestationRefused, verify_attestation
from lsrl.tools import probe_preset_stop as probe_mod
from lsrl.tools.probe_preset_stop import (ALLOWED, GEOMETRY_TICKS, LONG, READONLY_ALLOWED, SHORT, DemoTransport, Probe,
                                          ProbeAborted, ProbeHttpError, dry_run_plan, main, redact, verify_ids)

SHA = "c" * 40
SECRETS = ("KEY-SECRETSECRETSECRETSECRET1", "SEC-SECRETSECRETSECRETSECRET2", "PASS-SECRETSECRETSECRETSECRET3")
SIDES = ("long", "short")


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeBitgetDemo:
    """REST-level fake of the demo exchange, both sides. `model` selects how a preset stop materialises; `quirks` (a set) are
    extra ways the exchange can misbehave, optionally only on `quirk_side`."""

    def __init__(self, clock: Clock, model: str = "PLAN_PER_ORDER", *, quirk: str | set | None = None,
                 quirk_side: str | None = None, pos_mode: str = "hedge_mode", symbol: str = "SSOLSUSDT", dirty: bool = False,
                 top_size: float = 5.0) -> None:
        self.clock, self.model, self.pos_mode, self.symbol = clock, model, pos_mode, symbol
        self.quirks = set() if quirk is None else ({quirk} if isinstance(quirk, str) else set(quirk))
        self.quirk_side = quirk_side
        self.pos = {"long": 3.0 if dirty else 0.0, "short": 0.0}
        self.orders: dict[str, dict] = {}
        self.plans: list[dict] = []
        self.calls: list[tuple] = []
        self.bodies: list[tuple[str, dict]] = []
        self.zero_stop_moments = 0
        self._n = 0
        self.last, self.ask, self.bid, self.top_size = 150.0, 150.05, 149.95, top_size

    # ------------------------------------------------------------ helpers
    def _id(self) -> str:
        self._n += 1
        return f"X{self._n:04d}"

    @staticmethod
    def _ok(data):
        return {"code": "00000", "msg": "success", "data": data}

    def _q(self, name: str, hold: str | None) -> bool:
        return name in self.quirks and (self.quirk_side is None or hold == self.quirk_side)

    @property
    def position(self) -> float:                                             # convenience for the single-position tests
        return self.pos["long"] + self.pos["short"]

    # ------------------------------------------------------------ the transport
    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        if body is not None:
            self.bodies.append((path, dict(body)))
        assert (method, path) in ALLOWED, f"the probe left its allow-list: {method} {path}"
        handler = {"/api/v2/mix/market/contracts": self._contracts, "/api/v2/mix/market/ticker": self._ticker,
                   "/api/v2/mix/account/account": self._account, "/api/v2/mix/position/all-position": self._positions,
                   "/api/v2/mix/order/orders-pending": self._pending, "/api/v2/mix/order/orders-plan-pending": self._plan_list,
                   "/api/v2/mix/order/detail": self._detail, "/api/v2/mix/order/fills": self._fills,
                   "/api/v2/mix/order/place-order": self._place, "/api/v2/mix/order/cancel-order": self._cancel,
                   "/api/v2/mix/order/place-tpsl-order": self._tpsl, "/api/v2/mix/order/place-pos-tpsl": self._pos_tpsl,
                   "/api/v2/mix/order/cancel-plan-order": self._cancel_plan,
                   "/api/v2/mix/order/close-positions": self._close}[path]
        return handler(params or {}, body or {})

    # ------------------------------------------------------------ reads
    def _contracts(self, p, b):
        return self._ok([{"symbol": self.symbol, "baseCoin": "SOL", "pricePlace": "2", "priceEndStep": "1",
                          "volumePlace": "1", "sizeMultiplier": "0.1", "minTradeNum": "0.1", "minTradeUSDT": "5"}])

    def _ticker(self, p, b):
        return self._ok([{"lastPr": str(self.last), "askPr": str(self.ask), "bidPr": str(self.bid),
                          "askSz": str(self.top_size), "bidSz": str(self.top_size)}])

    def _account(self, p, b):
        return self._ok({"posMode": self.pos_mode, "marginMode": "isolated"})

    def _positions(self, p, b):
        return self._ok([{"symbol": self.symbol, "holdSide": h, "total": str(round(v, 6))} for h, v in self.pos.items() if v > 0])

    def _pending(self, p, b):
        return self._ok({"entrustedList": [self._row(o) for o in self.orders.values() if o["state"] in ("live", "partially_filled")]})

    def _live_plans(self):
        now = self.clock.time()
        self.plans = [x for x in self.plans if x.get("vanish_at") is None or now < x["vanish_at"]]
        return self.plans

    def _plan_list(self, p, b):
        return self._ok({"entrustedList": [{k: v for k, v in x.items() if k != "vanish_at"} for x in self._live_plans()]})

    @staticmethod
    def _row(o):
        return {"clientOid": o["clientOid"], "orderId": o["orderId"], "state": o["state"], "price": str(o["price"]),
                "size": str(o["size"]), "baseVolume": str(o["filled"]), "priceAvg": str(o["price"] if o["filled"] else 0),
                "side": o["side"], "tradeSide": o["tradeSide"], "symbol": o["symbol"], "cTime": o["cTime"],
                "presetStopLossPrice": str(o["preset"] or ""), "presetStopLossType": "fill_price"}

    def _find(self, p):
        if "clientOid" in p:
            o = self.orders.get(p["clientOid"])
        else:
            o = next((x for x in self.orders.values() if x["orderId"] == p.get("orderId")), None)
        if o is None:
            raise ProbeHttpError("code=43001 msg=order not exist")
        return o

    def _detail(self, p, b):
        return self._ok(self._row(self._find(p)))

    def _fills(self, p, b):
        o = self._find(p)
        rows = [{"tradeId": f"T{o['orderId']}", "price": str(o["price"]), "baseVolume": str(o["filled"]), "cTime": o["cTime"]}] \
            if o["filled"] else []
        return self._ok({"fillList": rows})

    # ------------------------------------------------------------ writes
    def _place(self, p, b):
        opening = b["tradeSide"] == "open"
        hold = ("long" if b["side"] == "buy" else "short") if opening else b["holdSide"]
        preset = b.get("presetStopLossPrice")
        tick = 0.01
        if preset is not None and self._q("REJECT_PRESET", hold):
            raise ProbeHttpError("code=40017 msg=presetStopLossPrice not supported")
        if preset is not None and self._q("REJECT_TIGHT_PRESET", hold) and abs(float(b["price"]) - float(preset)) < 2.5 * tick:
            raise ProbeHttpError("code=40017 msg=stop too close to the entry")
        if preset is not None and self._q("DROP_PRESET", hold):                   # accepted, but the preset is silently dropped
            preset = None
        price, size = float(b["price"]), float(b["size"])
        order = {"clientOid": b["clientOid"], "orderId": self._id(), "size": size, "price": price, "preset": preset,
                 "state": "live", "filled": 0.0, "hold": hold, "opening": opening, "side": b["side"],
                 "tradeSide": b["tradeSide"], "symbol": self.symbol, "cTime": str(int(self.clock.time() * 1000)),
                 "post_only": b["force"] == "post_only"}
        self.orders[b["clientOid"]] = order
        if not opening:
            return self._ok({"orderId": order["orderId"], "clientOid": order["clientOid"]})
        marketable = price >= self.ask if hold == "long" else price <= self.bid
        if marketable and order["post_only"]:
            order["state"] = "canceled"
        elif marketable:
            fill = min(size, self.top_size) if abs(price - (self.ask if hold == "long" else self.bid)) < 1e-9 else size
            order["filled"] = fill
            order["state"] = "filled" if fill >= size - 1e-9 else "partially_filled"
            self.pos[hold] += fill
            if preset is not None:
                self._materialise(order, fill)
        return self._ok({"orderId": order["orderId"], "clientOid": order["clientOid"]})

    def _materialise(self, order, filled):
        hold = order["hold"]
        if self.model == "NONE":
            return
        partial = order["state"] == "partially_filled"
        if partial and self._q("PARTIAL_UNCOVERED", hold):
            return
        vanish = self.clock.time() + 1.0 if self._q("VANISH", hold) else None
        if self.model == "POSITION_LEVEL":
            pl = next((x for x in self.plans if x["planType"] == "pos_loss" and x["posSide"] == hold), None)
            if pl is None:
                self.plans.append({"orderId": self._id(), "planType": "pos_loss", "posSide": hold, "size": str(self.pos[hold]),
                                   "triggerPrice": str(order["preset"]), "cTime": order["cTime"]})
            else:
                pl["size"] = str(self.pos[hold])
                if self._q("LATE_PRESET_OVERWRITES", hold) and "fill3" in order["clientOid"]:
                    pl["triggerPrice"] = str(order["preset"])                  # the older, worse price comes back
            return
        if self._q("UNCOVERED_SECOND", hold) and any(x["planType"] == "loss_plan" and x["posSide"] == hold for x in self.plans):
            return
        if self._q("LATE_FILL_UNCOVERED", hold) and "fill3" in order["clientOid"]:
            return
        self.plans.append({"orderId": self._id(), "planType": "loss_plan", "posSide": hold, "size": str(filled),
                           "triggerPrice": str(order["preset"]), "vanish_at": vanish, "cTime": order["cTime"]})

    def _cancel(self, p, b):
        o = self.orders.get(b["clientOid"])
        if o is None or o["state"] not in ("live", "partially_filled"):
            raise ProbeHttpError("code=43001 msg=order not exist")
        was_partial = o["state"] == "partially_filled"
        o["state"] = "canceled"
        if self._q("PRESET_LEAKS_ON_CANCEL", o["hold"]) and o.get("preset") and not was_partial and o["opening"]:
            self.plans.append({"orderId": self._id(), "planType": "loss_plan", "posSide": o["hold"], "size": str(o["size"]),
                               "triggerPrice": str(o["preset"])})
        if was_partial and self._q("PARTIAL_CANCEL_DROPS_STOP", o["hold"]):
            self.plans = [x for x in self.plans if x["posSide"] != o["hold"]]
        return self._ok({})

    def _kill_take_profit(self, hold):
        if self._q("MOVE_KILLS_TP", hold):
            for o in self.orders.values():
                if not o["opening"] and o["hold"] == hold and o["state"] == "live":
                    o["state"] = "canceled"

    def _pos_tpsl(self, p, b):
        assert "stopSurplusTriggerPrice" not in b, "the stop-only move must not send a take-profit leg"
        hold = b["holdSide"]
        pl = next((x for x in self.plans if x["planType"] == "pos_loss" and x["posSide"] == hold), None)
        if pl is None:
            raise ProbeHttpError("code=40774 msg=no position tpsl")
        pl["triggerPrice"] = b["stopLossTriggerPrice"]
        if self._q("MOVE_DUPLICATES", hold):
            self.plans.append({"orderId": self._id(), "planType": "pos_loss", "posSide": hold, "size": str(self.pos[hold]),
                               "triggerPrice": b["stopLossTriggerPrice"]})
        self._kill_take_profit(hold)
        return self._ok({})

    def _tpsl(self, p, b):
        hold = b["holdSide"]
        entry = {"orderId": self._id(), "planType": "loss_plan", "posSide": hold, "size": b["size"],
                 "triggerPrice": b["triggerPrice"], "cTime": str(int(self.clock.time() * 1000))}
        if any(x["planType"] == "pos_loss" and x["posSide"] == hold for x in self.plans):          # the 2026-08-01 incident
            entry["vanish_at"] = self.clock.time() + 1.0
        elif self._q("NO_PLACE_FIRST", hold) and any(x["planType"] == "loss_plan" and x["posSide"] == hold for x in self.plans):
            entry["vanish_at"] = self.clock.time() + 1.0                                          # the newcomer is cancelled
        self.plans.append(entry)
        self._kill_take_profit(hold)
        return self._ok({})

    def _cancel_plan(self, p, b):
        before = len(self.plans)
        self.plans = [x for x in self.plans if x["orderId"] != b["orderId"]]
        if len(self.plans) == before:
            raise ProbeHttpError("code=43001 msg=plan not exist")
        for hold in SIDES:
            if self.pos[hold] > 0 and not [x for x in self._live_plans() if x["posSide"] == hold and "loss" in x["planType"]]:
                self.zero_stop_moments += 1                                # a moment when an open position had NO stop
        return self._ok({})

    def _close(self, p, b):
        hold = b["holdSide"]
        self.pos[hold] = 0.0
        released = ("pos_loss", "pos_profit") if not self._q("LEFTOVER_POS_PLAN", hold) else ()
        self.plans = [x for x in self.plans if not (x["planType"] in released and x["posSide"] == hold)]   # loss_plans stay
        for o in self.orders.values():                                                # reduce-only orders die with the position
            if not o["opening"] and o["hold"] == hold and o["state"] == "live":
                o["state"] = "canceled"
        return self._ok({})


def run_probe(model: str = "PLAN_PER_ORDER", **fake_kw):
    clock = Clock()
    fake = FakeBitgetDemo(clock, model, **fake_kw)
    probe = Probe(fake, code_sha=SHA, clean_tree=True, harness_sha256="h" * 64, sleep=clock.sleep, clock=clock.time)
    return probe, fake, clock


def gate(tmp_path, doc, clock, *, accept_demo=True):
    """What MICRO_LIVE does with the probe's output: pinned digest, release SHA, expiry, every required check."""
    path = tmp_path / "attestation.json"
    raw = json.dumps(doc, sort_keys=True, default=str).encode()
    path.write_bytes(raw)
    return verify_attestation(path, hashlib.sha256(raw).hexdigest(), code_sha=SHA, now=clock.time(), accept_demo=accept_demo)


def failed_ids(doc):
    return {c["id"] for c in doc["checks"] if not c["passed"]}


# ==================================================================================== every good exchange behaviour passes
@pytest.mark.parametrize("model,expected,mechanism", [("PLAN_PER_ORDER", "PLAN_LEVEL", "PLAN_LEVEL_PLACE_FIRST"),
                                                      ("POSITION_LEVEL", "POSITION_LEVEL", "POSITION_LEVEL_STOP_ONLY")])
def test_a_well_behaved_exchange_yields_a_pass_the_executors_gate_accepts(tmp_path, model, expected, mechanism):
    probe, fake, clock = run_probe(model)
    doc = probe.run()
    assert doc["decision"] == "PASS" and doc["observed_model"] == expected and doc["environment"] == "BITGET_DEMO"
    assert doc["sides_tested"] == ["long", "short"] and doc["models_by_side"] == {"long": expected, "short": expected}
    assert {c["id"] for c in doc["checks"] if c["passed"]} >= set(REQUIRED_CHECKS)
    assert doc["observations"]["stop_move_mechanism"] == {"long": mechanism, "short": mechanism}
    assert doc["observations"]["final_state_clean"] is True
    granted = gate(tmp_path, doc, clock)                                  # the SAME verification MICRO_LIVE performs
    assert granted.observed_model == expected and granted.code_sha == SHA
    assert granted.plan_place_first_ok is (expected == "PLAN_LEVEL")


def test_the_probe_really_exercises_the_short_side_with_short_orders_and_short_closes():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    opens = [b for path, b in fake.bodies if path.endswith("place-order") and b["tradeSide"] == "open"]
    assert {b["side"] for b in opens} == {"buy", "sell"}
    sells = [b for b in opens if b["side"] == "sell"]
    assert len(sells) >= 9 and all(float(b.get("presetStopLossPrice", "inf")) > float(b["price"]) for b in sells), \
        "a short's preset stop must sit ABOVE its entry"
    buys = [b for b in opens if b["side"] == "buy"]
    assert all(float(b["presetStopLossPrice"]) < float(b["price"]) for b in buys)
    closes = [b for path, b in fake.bodies if path.endswith("close-positions")]
    assert {b["holdSide"] for b in closes} == {"long", "short"}
    take_profits = [b for path, b in fake.bodies if path.endswith("place-order") and b["tradeSide"] == "close"]
    assert {(b["holdSide"], b["side"]) for b in take_profits} == {("long", "buy"), ("short", "sell")}     # the executor's table
    assert all(b["reduceOnly"] == "YES" and b["force"] == "post_only" for b in take_profits)
    assert {c["id"] for c in doc["checks"] if c["passed"]} >= {f"cleanup_after_flat:{s}" for s in SIDES}


def test_the_probe_never_sends_a_stop_request_before_it_observes_what_the_exchange_did_by_itself():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    probe.run()
    for side in SIDES:
        obs = next(c for c in probe.checks if c["id"] == f"stop_exists_after_fill_without_client:{side}")["observed"]
        assert obs["client_stop_requests"] == 0
    calls = fake.calls
    stop_requests = [i for i, c in enumerate(calls) if c[1] in ("/api/v2/mix/order/place-tpsl-order", "/api/v2/mix/order/place-pos-tpsl")]
    first_fill_read = next(i for i, c in enumerate(calls) if c[1].endswith("orders-plan-pending") and i > calls.index(
        ("POST", "/api/v2/mix/order/place-order"), 1))
    assert stop_requests and min(stop_requests) > first_fill_read


# ==================================================================================== every bad behaviour is a FAIL
@pytest.mark.parametrize("model,quirk,failed", [
    ("NONE", None, "stop_exists_after_fill_without_client"),                 # the exchange ignores the preset
    ("PLAN_PER_ORDER", "REJECT_PRESET", "preset_accepted_on_limit_order"),   # the exchange refuses it
    ("PLAN_PER_ORDER", "DROP_PRESET", "preset_accepted_on_limit_order"),     # ...or accepts the order and drops the preset
    ("PLAN_PER_ORDER", "VANISH", "stop_survives_settle"),                    # accepted, then cancelled about a second later
    ("PLAN_PER_ORDER", "UNCOVERED_SECOND", "stop_covers_every_fill"),        # the second fill is not covered
    ("POSITION_LEVEL", "MOVE_DUPLICATES", "executor_stop_move_coexists"),    # a move leaves two competing stops
    ("PLAN_PER_ORDER", "MOVE_KILLS_TP", "executor_stop_move_coexists"),      # a stop move cancels the resting take-profit
    ("POSITION_LEVEL", "MOVE_KILLS_TP", "executor_stop_move_coexists"),
    ("PLAN_PER_ORDER", "PRESET_LEAKS_ON_CANCEL", "unfilled_cancel_leaves_no_stop"),
    ("POSITION_LEVEL", "LEFTOVER_POS_PLAN", "cleanup_after_flat"),           # an orphan the executor's own cleanup cannot remove
    ("PLAN_PER_ORDER", "REJECT_TIGHT_PRESET", "preset_accepted_at_strategy_geometry"),   # thin ladder live, silently
    ("PLAN_PER_ORDER", "PARTIAL_UNCOVERED", "partial_fill_stop_covers_filled"),
    ("POSITION_LEVEL", "PARTIAL_UNCOVERED", "partial_fill_stop_covers_filled"),
    ("PLAN_PER_ORDER", "PARTIAL_CANCEL_DROPS_STOP", "partial_fill_cancel_keeps_stop_for_filled_size"),
    ("PLAN_PER_ORDER", "LATE_FILL_UNCOVERED", "later_fill_with_old_preset_is_repairable"),
])
@pytest.mark.parametrize("side", SIDES)
def test_every_way_the_exchange_can_misbehave_on_either_side_is_a_fail_and_the_gate_refuses_it(tmp_path, model, quirk, failed, side):
    probe, fake, clock = run_probe(model, quirk=quirk, quirk_side=side if quirk else None)
    doc = probe.run()
    assert doc["decision"] == "FAIL"
    ids = failed_ids(doc)
    assert f"{failed}:{side}" in ids if quirk else any(i.startswith(failed) for i in ids)
    if quirk:
        other = "long" if side == "short" else "short"
        assert not any(i.endswith(f":{other}") and i.startswith(failed) for i in ids), "the misbehaviour was on ONE side only"
    with pytest.raises(AttestationRefused):
        gate(tmp_path, doc, clock)


def test_a_model_that_differs_between_the_sides_is_never_a_pass(tmp_path):
    probe, fake, clock = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    probe.models["short"] = "POSITION_LEVEL"
    probe.checks = [c for c in probe.checks if c["id"] != "model_same_on_both_sides"]
    probe._check_model_same_on_both_sides()
    assert not next(c for c in probe.checks if c["id"] == "model_same_on_both_sides")["passed"]
    assert probe.attestation()["decision"] == "FAIL" and probe.attestation()["observed_model"] == "UNKNOWN"


def test_the_incident_two_stop_models_at_once_is_caught_when_the_wrong_mechanism_is_used():
    """If the exchange makes a position-level stop and a plan-level stop is placed beside it, the exchange cancels the
    newcomer: the probe's plan-level mechanism would see no coexistence. It never uses that mechanism here."""
    probe, fake, _ = run_probe("POSITION_LEVEL")
    probe.run()
    assert ("POST", "/api/v2/mix/order/place-tpsl-order") not in fake.calls


def test_the_probe_leaves_the_demo_symbol_clean_even_when_a_check_fails():
    probe, fake, _ = run_probe("PLAN_PER_ORDER", quirk="VANISH")
    doc = probe.run()
    assert doc["observations"]["final_state_clean"] is True
    assert fake.position == 0.0 and fake.plans == [] and not [o for o in fake.orders.values() if o["state"] in ("live", "partially_filled")]


def test_an_unexpected_error_mid_run_is_a_fail_never_a_pass_and_still_cleans_up():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")

    def boom(p, b):
        raise RuntimeError("network fell over")
    fake._tpsl = boom
    doc = probe.run()
    assert doc["decision"] == "FAIL" and doc["observations"]["final_state_clean"] is True


def test_a_crash_after_the_long_side_leaves_the_short_side_unrun_and_therefore_failed(tmp_path):
    probe, fake, clock = run_probe("PLAN_PER_ORDER")
    real = probe._run_side

    def only_long(side):
        if side.name == "short":
            raise RuntimeError("fell over on the short side")
        real(side)
    probe._run_side = only_long
    doc = probe.run()
    assert doc["sides_tested"] == ["long"] and doc["decision"] == "FAIL"
    assert {c["id"] for c in doc["checks"] if c["observed"].get("not_run")} >= {f"stop_covers_every_fill:short"}
    with pytest.raises(AttestationRefused):
        gate(tmp_path, doc, clock)


# ==================================================================================== the partial fill is REALLY produced
def test_the_partial_fill_is_produced_by_an_order_just_above_the_top_of_the_book_and_the_stop_covers_the_filled_part():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    for side in SIDES:
        obs = next(c for c in doc["checks"] if c["id"] == f"partial_fill_stop_covers_filled:{side}")["observed"]
        assert obs["exercised"] is True and obs["state_seen"] == "partially_filled"
        assert obs["order_size"] > obs["top_of_book_size"] and 0 < obs["filled_when_partial"] < obs["order_size"]
        assert obs["filled_final"] == obs["filled_when_partial"]                     # the remainder was cancelled, not filled
        after = next(c for c in doc["checks"] if c["id"] == f"partial_fill_cancel_keeps_stop_for_filled_size:{side}")["observed"]
        assert after["covers_after_cancel"] is True


def test_a_book_too_thick_to_out_size_is_reported_not_exercised_and_is_a_fail_never_assumed(tmp_path):
    probe, fake, clock = run_probe("PLAN_PER_ORDER", top_size=10_000.0)
    doc = probe.run()
    assert doc["decision"] == "FAIL"
    for side in SIDES:
        c = next(c for c in doc["checks"] if c["id"] == f"partial_fill_stop_covers_filled:{side}")
        assert c["passed"] is False and c["observed"]["not_exercised"] is True and "too thick" in c["observed"]["reason"]
    assert not [o for o in fake.orders.values() if o["clientOid"].endswith("-part0")]      # nothing oversized was ever placed
    with pytest.raises(AttestationRefused):
        gate(tmp_path, doc, clock)


def test_the_partial_fill_order_is_the_only_order_allowed_above_the_minimum_lot_and_is_capped():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    probe.run()
    caps = probe_mod.PARTIAL_FILL_MAX_NOTIONAL_USDT
    for path, body in fake.bodies:
        if path.endswith("place-order") and body["tradeSide"] == "open":
            notional = float(body["size"]) * float(body["price"])
            if "-part" in body["clientOid"]:
                assert notional <= caps
            else:
                assert notional <= probe_mod.MAX_PROBE_NOTIONAL_USDT
    assert caps == 3000.0 and probe_mod.MAX_PROBE_NOTIONAL_USDT == 60.0


def test_an_order_that_fills_completely_instead_of_partially_is_retried_then_reported_not_exercised():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    real = fake._place

    def deep(p, b):                                       # the book deepens while the order is in flight: it fills whole
        if "-part" in b["clientOid"]:
            fake.top_size = 10_000.0
            out = real(p, b)
            fake.top_size = 5.0
            return out
        return real(p, b)
    fake._place = deep
    doc = probe.run()
    obs = next(c for c in doc["checks"] if c["id"] == "partial_fill_stop_covers_filled:long")
    assert obs["passed"] is False and obs["observed"]["not_exercised"] is True
    assert sum(1 for path, b in fake.bodies if path.endswith("place-order") and "-l-part" in b.get("clientOid", "")) == probe_mod.PARTIAL_FILL_ATTEMPTS
    assert doc["observations"]["final_state_clean"] is True


# ==================================================================================== the strategy's real (tight) geometry
def test_every_stop_distance_from_one_tick_to_sixty_is_tried_on_both_sides_and_all_must_be_accepted():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    assert GEOMETRY_TICKS[0] == 1                          # the E5 rung's stop is 0.15 x ATR(1m) beyond it: as tight as a tick
    for side in SIDES:
        obs = next(c for c in doc["checks"] if c["id"] == f"preset_accepted_at_strategy_geometry:{side}")
        assert obs["passed"] and obs["observed"]["accepted_ticks"] == list(GEOMETRY_TICKS)
    for path, b in fake.bodies:
        if path.endswith("place-order") and "-geo" in b["clientOid"]:
            k = int(b["clientOid"].split("-geo")[1])
            assert round(abs(float(b["price"]) - float(b["presetStopLossPrice"])) / 0.01) == k


def test_a_rejected_tight_distance_is_reported_with_the_smallest_accepted_one():
    probe, fake, _ = run_probe("PLAN_PER_ORDER", quirk="REJECT_TIGHT_PRESET")
    doc = probe.run()
    obs = next(c for c in doc["checks"] if c["id"] == "preset_accepted_at_strategy_geometry:long")
    assert obs["passed"] is False and obs["observed"]["min_accepted_ticks"] == 5
    assert [r["ticks"] for r in obs["observed"]["rejected"]] == [1, 2]


# ==================================================================================== the stop move
def test_a_plan_level_move_places_the_new_stop_before_cancelling_the_old_one_and_never_leaves_the_position_bare():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    assert fake.zero_stop_moments == 0, "with place-first no cancel may ever leave an open position without a stop"
    assert doc["capabilities"] == {"plan_place_first_ok": True}


def test_when_the_exchange_does_not_tolerate_a_second_stop_the_move_falls_back_to_replace_and_the_capability_is_absent(tmp_path):
    probe, fake, clock = run_probe("PLAN_PER_ORDER", quirk="NO_PLACE_FIRST")
    doc = probe.run()
    assert doc["decision"] == "PASS"                                      # the fallback works: only the capability is missing
    assert doc["observations"]["stop_move_mechanism"] == {"long": "PLAN_LEVEL_REPLACE", "short": "PLAN_LEVEL_REPLACE"}
    assert doc["capabilities"] == {"plan_place_first_ok": False}
    assert gate(tmp_path, doc, clock).plan_place_first_ok is False
    assert fake.zero_stop_moments >= 1                                    # the known residual of cancel-then-place, on record


def test_the_capability_needs_both_sides_and_is_never_granted_to_a_position_level_exchange():
    probe, fake, _ = run_probe("PLAN_PER_ORDER", quirk="NO_PLACE_FIRST", quirk_side="short")
    assert probe.run()["capabilities"] == {"plan_place_first_ok": False}
    probe, fake, _ = run_probe("POSITION_LEVEL")
    assert probe.run()["capabilities"] == {"plan_place_first_ok": False}


def test_the_late_fill_carries_the_old_preset_and_the_moved_stop_is_restored():
    probe, fake, _ = run_probe("POSITION_LEVEL", quirk="LATE_PRESET_OVERWRITES")
    doc = probe.run()
    for side in SIDES:
        obs = next(c for c in doc["checks"] if c["id"] == f"later_fill_with_old_preset_is_repairable:{side}")
        assert obs["passed"] and obs["observed"]["worse_stop_present_after_late_fill"] is True
        assert obs["observed"]["repaired"] is True and obs["observed"]["repair_mechanism"] == "POSITION_LEVEL_STOP_ONLY"
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    obs = next(c for c in probe.run()["checks"] if c["id"] == "later_fill_with_old_preset_is_repairable:short")
    assert obs["passed"] and obs["observed"]["worse_stop_present_after_late_fill"] is True   # a per-order stop at the old price


def test_the_move_is_made_with_a_resting_reduce_only_take_profit_and_it_must_survive():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    for side in SIDES:
        obs = next(c for c in doc["checks"] if c["id"] == f"executor_stop_move_coexists:{side}")["observed"]
        assert obs["take_profit_survives"] is True


# ==================================================================================== the evidence a reviewer can re-query
def test_every_order_the_probe_relied_on_is_recorded_with_its_exchange_id_and_a_reviewer_can_requery_it_read_only():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    ev = doc["evidence"]
    assert ev["run_id"] and ev["finished_at"] >= ev["started_at"]
    ids = {o["orderId"] for o in ev["orders"]}
    assert ids and all(o["orderId"] and o["clientOid"].startswith("lsrlprobe-") for o in ev["orders"])
    purposes = {o["purpose"] for o in ev["orders"]}
    assert {"fill1", "fill2", "fill3_with_old_preset", "partial_fill_while_resting", "take_profit_after_stop_move"} <= purposes
    assert ev["fills"] and all(f["orderId"] in {o["orderId"] for o in fake.orders.values()} for f in ev["fills"])
    assert ev["plans"] and all(p["orderId"] for p in ev["plans"])
    result = verify_ids(doc, fake)
    assert result["ok"] and result["orders_checked"] == len(ev["orders"]) and result["mismatches"] == []
    assert {c[1] for c in fake.calls[-len(ev["orders"]):]} == {"/api/v2/mix/order/detail"}       # only detail reads


def test_a_tampered_or_invented_order_is_caught_by_the_read_only_requery():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    forged = json.loads(json.dumps(doc))
    fill1 = next(o for o in forged["evidence"]["orders"] if o["purpose"] == "fill1")
    fill1["priceAvg"] = "1.00"                                             # a fill price the exchange never reported
    assert verify_ids(forged, fake)["mismatches"][0]["field"] == "priceAvg"
    invented = json.loads(json.dumps(doc))
    invented["evidence"]["orders"][0]["orderId"] = "does-not-exist"
    result = verify_ids(invented, fake)
    assert result["ok"] is False and result["not_found"]
    empty = json.loads(json.dumps(doc))
    empty["evidence"]["orders"] = []
    assert verify_ids(empty, fake)["ok"] is False                          # nothing checked is never OK
    live = json.loads(json.dumps(doc))
    live["environment"] = "BITGET_LIVE_MINIMAL"
    with pytest.raises(ProbeAborted, match="DEMO"):
        verify_ids(live, fake)


def test_the_read_only_requery_transport_cannot_place_or_cancel_anything():
    session = FakeSession({"code": "00000", "data": {}})
    transport = DemoTransport("k", "s", "p", session=session, allowed=READONLY_ALLOWED)
    for method, path in (("POST", "/api/v2/mix/order/place-order"), ("POST", "/api/v2/mix/order/cancel-order"),
                         ("GET", "/api/v2/mix/position/all-position")):
        with pytest.raises(ProbeAborted, match="allow-listed"):
            transport.request(method, path)
    assert session.sent == [] and READONLY_ALLOWED == {("GET", "/api/v2/mix/order/detail"), ("GET", "/api/v2/mix/order/fills")}


def test_verify_ids_command_needs_demo_credentials_and_reports_a_forgery(tmp_path, monkeypatch, capsys):
    probe, fake, clock = run_probe("PLAN_PER_ORDER")
    doc = probe.run()
    path = tmp_path / "a.json"
    path.write_text(json.dumps(doc, default=str))
    for name in ("BITGET_DEMO_API_KEY", "BITGET_DEMO_API_SECRET", "BITGET_DEMO_API_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    assert main(["--verify-ids", str(path)]) == 2 and "DEMO" in capsys.readouterr().err
    assert main(["--verify-ids", str(path)], transport=fake) == 0
    doc["evidence"]["orders"][0]["clientOid"] = "lsrlprobe-forged"
    path.write_text(json.dumps(doc, default=str))
    assert main(["--verify-ids", str(path)], transport=fake) == 1


# ==================================================================================== preconditions abort, placing nothing
@pytest.mark.parametrize("kw,fragment", [
    ({"pos_mode": "one_way_mode"}, "hedge mode"),
    ({"dirty": True}, "not clean"),
    ({"symbol": "SOLUSDT"}, "demo symbol"),                     # a LIVE symbol name is refused outright
])
def test_a_precondition_failure_aborts_before_any_order(kw, fragment):
    probe, fake, _ = run_probe(**kw)
    with pytest.raises(ProbeAborted, match=fragment):
        probe.run()
    assert not [c for c in fake.calls if c[0] == "POST" and c[1].endswith("place-order")]


def test_a_dirty_working_tree_refuses_to_bind_an_attestation():
    clock = Clock()
    fake = FakeBitgetDemo(clock)
    probe = Probe(fake, code_sha=SHA, clean_tree=False, harness_sha256="h" * 64, sleep=clock.sleep, clock=clock.time)
    with pytest.raises(ProbeAborted, match="clean commit"):
        probe.run()
    assert fake.calls == []                                                            # not one request


# ==================================================================================== the command and the transport
def test_dry_run_prints_the_plan_and_contacts_nothing(capsys):
    assert main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    base = {r.split(":")[0] for r in REQUIRED_CHECKS}
    assert all(b in out for b in base if b != "model_same_on_both_sides")
    assert "SHORT SIDE" in out and "partial" in out and "1, 2, 5, 20 and 60 ticks" in out
    assert len(dry_run_plan()) == 13


def test_the_command_refuses_without_demo_credentials_and_never_reads_the_live_ones(tmp_path, monkeypatch, capsys):
    for name in ("BITGET_DEMO_API_KEY", "BITGET_DEMO_API_SECRET", "BITGET_DEMO_API_PASSPHRASE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BITGET_API_KEY", "LIVE-KEY-MUST-NOT-BE-USED-000000")
    assert main(["--out", str(tmp_path / "a.json")]) == 2
    assert "DEMO" in capsys.readouterr().err
    assert "LIVE-KEY" not in Path(probe_mod.__file__).read_text()


def test_the_command_never_overwrites_an_existing_attestation(tmp_path, monkeypatch, capsys):
    clock = Clock()
    fake = FakeBitgetDemo(clock, "PLAN_PER_ORDER")
    monkeypatch.setattr(probe_mod, "git_state", lambda root: (SHA, True))
    out = tmp_path / "a.json"
    out.write_text("{}")
    assert main(["--out", str(out)], transport=fake, sleep=clock.sleep, clock=clock.time) == 2
    assert out.read_text() == "{}" and fake.calls == []                    # refused before a single request


def test_the_command_writes_the_attestation_prints_the_digest_and_no_secret(tmp_path, monkeypatch, capsys):
    clock = Clock()
    fake = FakeBitgetDemo(clock, "PLAN_PER_ORDER")
    monkeypatch.setattr(probe_mod, "git_state", lambda root: (SHA, True))
    monkeypatch.setenv("BITGET_DEMO_API_KEY", SECRETS[0])
    monkeypatch.setenv("BITGET_DEMO_API_SECRET", SECRETS[1])
    monkeypatch.setenv("BITGET_DEMO_API_PASSPHRASE", SECRETS[2])
    out = tmp_path / "a.json"
    assert main(["--out", str(out)], transport=fake, sleep=clock.sleep, clock=clock.time) == 0
    printed = capsys.readouterr().out
    digest = json.loads(printed.split("Pin the sha256")[0])["sha256"]
    assert digest == hashlib.sha256(out.read_bytes()).hexdigest()
    assert not any(s in printed or s in out.read_text() for s in SECRETS)
    granted = verify_attestation(out, digest, code_sha=SHA, now=clock.time(), accept_demo=True)
    assert granted.observed_model == "PLAN_LEVEL"
    with pytest.raises(AttestationRefused, match="DEMO_NOT_ACCEPTED"):          # the owner's policy decision is required
        verify_attestation(out, digest, code_sha=SHA, now=clock.time())


def test_a_failed_probe_exits_nonzero_and_the_written_file_is_refused_by_the_gate(tmp_path, monkeypatch, capsys):
    clock = Clock()
    monkeypatch.setattr(probe_mod, "git_state", lambda root: (SHA, True))
    out = tmp_path / "a.json"
    assert main(["--out", str(out)], transport=FakeBitgetDemo(clock, "NONE"), sleep=clock.sleep, clock=clock.time) == 1
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    with pytest.raises(AttestationRefused):
        verify_attestation(out, digest, code_sha=SHA, now=clock.time(), accept_demo=True)


class FakeSession:
    def __init__(self, payload):
        self.payload, self.sent = payload, []

    def request(self, **kw):
        self.sent.append(kw)
        session = self

        class R:
            def json(self):
                return session.payload
        return R()


def test_the_real_transport_always_sends_the_demo_header_and_signs_with_the_repo_signer():
    session = FakeSession({"code": "00000", "data": []})
    DemoTransport("k", "s", "p", session=session).request("GET", "/api/v2/mix/market/contracts",
                                                          params={"productType": "SUSDT-FUTURES"})
    headers = session.sent[0]["headers"]
    assert headers["paptrading"] == "1" and headers["ACCESS-KEY"] == "k" and headers["ACCESS-SIGN"]
    assert session.sent[0]["url"].startswith("https://api.bitget.com/")


def test_the_real_transport_refuses_an_endpoint_outside_the_allow_list_before_any_request():
    session = FakeSession({"code": "00000", "data": []})
    for method, path in (("POST", "/api/v2/mix/order/place-order-live"), ("GET", "/api/v2/spot/wallet/withdrawal"),
                         ("POST", "/api/v2/mix/account/set-leverage")):
        with pytest.raises(ProbeAborted, match="allow-listed"):
            DemoTransport("k", "s", "p", session=session).request(method, path)
    assert session.sent == []


def test_a_non_success_exchange_response_and_its_secrets_are_redacted():
    session = FakeSession({"code": "40018", "msg": "Invalid apikey " + SECRETS[0]})
    with pytest.raises(ProbeHttpError) as exc:
        DemoTransport(*SECRETS, session=session).request("GET", "/api/v2/mix/market/contracts")
    assert SECRETS[0] not in str(exc.value) and "<redacted>" in str(exc.value)
    assert redact("short text stays") == "short text stays"


def test_the_tool_source_has_no_live_endpoint_no_live_credential_name_and_the_demo_header_is_unconditional():
    text = Path(probe_mod.__file__).read_text()
    assert text.count('"SOLUSDT"') == 1 and 'self.symbol == "SOLUSDT"' in text          # named once: only to REFUSE it
    assert not re.search(r"BITGET_API_(KEY|SECRET|PASSPHRASE)", text)
    assert 'headers["paptrading"] = "1"' in text
    assert all(path.startswith("/api/v2/mix/") for _m, path in ALLOWED | READONLY_ALLOWED)
    assert READONLY_ALLOWED <= ALLOWED


# ==================================================================================== properties pinned by the mutation campaign
def test_a_run_that_cannot_leave_the_demo_symbol_clean_is_never_a_pass(tmp_path):
    probe, fake, clock = run_probe("PLAN_PER_ORDER")
    original = fake._close
    state = {"n": 0}

    def stuck(p, b):
        state["n"] += 1
        if state["n"] <= 3:                                          # every close during the run AND the final one fail
            fake.calls.append(("POST", "/api/v2/mix/order/close-positions"))
            raise ProbeHttpError("code=50000 msg=temporarily unavailable")
        return original(p, b)
    fake._close = stuck
    doc = probe.run()
    assert doc["decision"] == "FAIL"
    with pytest.raises(AttestationRefused):
        gate(tmp_path, doc, clock)


def test_a_check_that_never_ran_is_recorded_as_failed_not_left_out(tmp_path):
    probe, _fake, clock = run_probe("NONE")                       # the fill creates no stop: later checks cannot even start
    doc = probe.run()
    by_id = {c["id"]: c for c in doc["checks"]}
    assert set(REQUIRED_CHECKS) <= set(by_id)
    not_run = [i for i, c in by_id.items() if c["observed"].get("not_run")]
    assert {"stop_covers_every_fill:long", "stop_survives_settle:short", "executor_stop_move_coexists:long",
            "later_fill_with_old_preset_is_repairable:short"} <= set(not_run)
    assert all(by_id[i]["passed"] is False for i in not_run)


def test_a_symbol_that_is_not_clean_is_never_touched_even_by_cleanup(tmp_path):
    """Precondition refusals must not run cleanup against an account state the probe did not create."""
    probe, fake, _ = run_probe("PLAN_PER_ORDER", dirty=True)
    with pytest.raises(ProbeAborted):
        probe.run()
    assert fake.position == 3.0                                   # the pre-existing position was NOT closed by the cleanup
    assert ("POST", "/api/v2/mix/order/close-positions") not in fake.calls


def _synthetic(probe, **overrides):
    probe.checks = [{"id": c, "passed": True, "observed": {}} for c in REQUIRED_CHECKS]
    probe.models = {"long": "PLAN_LEVEL", "short": "PLAN_LEVEL"}
    probe.sides_done = ["long", "short"]
    probe.evidence["orders"] = [{"orderId": "1", "clientOid": "c"}]
    probe.observations = {"final_state_clean": True}
    for key, value in overrides.items():
        setattr(probe, key, value)
    return probe.attestation()


def test_the_decision_needs_a_clean_finish_both_sides_evidence_and_no_unexpected_error_even_when_every_check_passed():
    assert _synthetic(run_probe()[0])["decision"] == "PASS"
    for name, value in (("observations", {"final_state_clean": False}),
                        ("observations", {"final_state_clean": True, "unexpected_error": "boom"}),
                        ("observations", {}), ("sides_done", ["long"]), ("sides_done", ["short", "long"]),
                        ("models", {"long": "PLAN_LEVEL", "short": "POSITION_LEVEL"}), ("models", {"long": "PLAN_LEVEL"}),
                        ("evidence", {"orders": [], "plans": [], "fills": []}),
                        ("evidence", {"orders": [{"clientOid": "c"}], "plans": [], "fills": []})):
        assert _synthetic(run_probe()[0], **{name: value})["decision"] == "FAIL", (name, value)


def test_the_model_is_read_from_what_the_exchange_created_and_unknown_is_never_a_pass():
    doc = _synthetic(run_probe()[0], models={})
    assert doc["observed_model"] == "UNKNOWN" and doc["decision"] == "FAIL"
    assert _synthetic(run_probe()[0], models={"long": "POSITION_LEVEL", "short": "POSITION_LEVEL"})["observed_model"] == "POSITION_LEVEL"


def test_the_probe_refuses_to_place_an_order_larger_than_its_notional_cap():
    """The minimum lot on an expensive symbol can exceed the cap: the probe aborts BEFORE any order."""
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    fake.last, fake.ask, fake.bid = 2000.0, 2000.05, 1999.95                       # min lot 0.1 -> 200 USDT
    with pytest.raises(ProbeAborted, match="limit"):
        probe.run()
    assert not [c for c in fake.calls if c[0] == "POST" and c[1].endswith("place-order")]
    assert probe_mod.MAX_PROBE_NOTIONAL_USDT == 60.0


def test_the_probes_stop_only_request_has_exactly_the_shape_the_executor_sends():
    """C05: the probe must exercise the SAME request as `move_position_level_stop_only` (same fields, same trigger reference)."""
    from tests.test_tpsl_position_stop_only import STOP_ONLY_BODY_KEYS
    probe, fake, _ = run_probe("POSITION_LEVEL")
    probe.run()
    bodies = [b for path, b in fake.bodies if path.endswith("place-pos-tpsl")]
    assert len(bodies) == 4                                              # a move and a repair, per side
    for body in bodies:
        assert set(body) == STOP_ONLY_BODY_KEYS and body["stopLossTriggerType"] == "mark_price"
    assert {b["holdSide"] for b in bodies} == {"long", "short"}


def test_an_abort_with_an_open_position_closes_it_before_dropping_its_stops():
    """The final cleanup must never leave an open position without its stop: close first, cancel the stops afterwards."""
    probe, fake, _ = run_probe("PLAN_PER_ORDER")

    def abort(*a, **k):
        raise ProbeAborted("the owner interrupted after the first fill")
    probe._check_second_fill_covered = abort
    with pytest.raises(ProbeAborted):
        probe.run()
    assert fake.position == 0.0 and fake.plans == []
    assert fake.zero_stop_moments == 0                        # no cancel ever left an OPEN position without a stop
