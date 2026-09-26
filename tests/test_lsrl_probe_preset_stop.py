"""The owner-run DEMO probe: it observes how the exchange materialises a preset stop and writes the attestation MICRO_LIVE needs.

A REST-level fake of Bitget stands in for the demo account. Every way the exchange might behave (position-level stop,
per-order stops, ignoring the preset, refusing it, dropping the stop after the settle window, not covering a second fill,
leaving orphans behind) must give a probe result the executor's own gate accepts (PASS) or refuses (anything else).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from lsrl.adapters.process_down import REQUIRED_CHECKS, AttestationRefused, verify_attestation
from lsrl.tools import probe_preset_stop as probe_mod
from lsrl.tools.probe_preset_stop import (ALLOWED, DemoTransport, Probe, ProbeAborted, ProbeHttpError, dry_run_plan, main,
                                          redact)

SHA = "c" * 40
SECRETS = ("KEY-SECRETSECRETSECRETSECRET1", "SEC-SECRETSECRETSECRETSECRET2", "PASS-SECRETSECRETSECRETSECRET3")


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeBitgetDemo:
    """REST-level fake of the demo exchange. `model` selects how a preset stop materialises."""

    def __init__(self, clock: Clock, model: str = "PLAN_PER_ORDER", *, quirk: str | None = None,
                 pos_mode: str = "hedge_mode", symbol: str = "SSOLSUSDT", dirty: bool = False) -> None:
        # `model` = how a preset stop materialises; `quirk` = one extra way the exchange can misbehave (orthogonal to it)
        self.clock, self.model, self.quirk, self.pos_mode, self.symbol = clock, model, quirk, pos_mode, symbol
        self.position = 3.0 if dirty else 0.0
        self.orders: dict[str, dict] = {}
        self.plans: list[dict] = []
        self.calls: list[tuple] = []
        self._n = 0
        self.last, self.ask = 150.0, 150.05

    def _id(self) -> str:
        self._n += 1
        return f"X{self._n:04d}"

    @staticmethod
    def _ok(data):
        return {"code": "00000", "msg": "success", "data": data}

    # ------------------------------------------------------------ the transport
    def request(self, method, path, params=None, body=None):
        self.calls.append((method, path))
        assert (method, path) in ALLOWED, f"the probe left its allow-list: {method} {path}"
        handler = {"/api/v2/mix/market/contracts": self._contracts, "/api/v2/mix/market/ticker": self._ticker,
                   "/api/v2/mix/account/account": self._account, "/api/v2/mix/position/all-position": self._positions,
                   "/api/v2/mix/order/orders-pending": self._pending, "/api/v2/mix/order/orders-plan-pending": self._plan_list,
                   "/api/v2/mix/order/detail": self._detail, "/api/v2/mix/order/place-order": self._place,
                   "/api/v2/mix/order/cancel-order": self._cancel, "/api/v2/mix/order/place-tpsl-order": self._tpsl,
                   "/api/v2/mix/order/place-pos-tpsl": self._pos_tpsl, "/api/v2/mix/order/cancel-plan-order": self._cancel_plan,
                   "/api/v2/mix/order/close-positions": self._close}[path]
        return handler(params or {}, body or {})

    # ------------------------------------------------------------ reads
    def _contracts(self, p, b):
        return self._ok([{"symbol": self.symbol, "baseCoin": "SOL", "pricePlace": "2", "priceEndStep": "1",
                          "volumePlace": "1", "sizeMultiplier": "0.1", "minTradeNum": "0.1", "minTradeUSDT": "5"}])

    def _ticker(self, p, b):
        return self._ok([{"lastPr": str(self.last), "askPr": str(self.ask), "bidPr": str(self.ask - 0.1)}])

    def _account(self, p, b):
        return self._ok({"posMode": self.pos_mode, "marginMode": "isolated"})

    def _positions(self, p, b):
        return self._ok([{"symbol": self.symbol, "holdSide": "long", "total": str(self.position)}] if self.position > 0 else [])

    def _pending(self, p, b):
        return self._ok({"entrustedList": [o for o in self.orders.values() if o["state"] == "live"]})

    def _live_plans(self):
        now = self.clock.time()
        self.plans = [x for x in self.plans if x.get("vanish_at") is None or now < x["vanish_at"]]
        return self.plans

    def _plan_list(self, p, b):
        return self._ok({"entrustedList": [dict(x) for x in self._live_plans()]})

    def _detail(self, p, b):
        o = self.orders.get(p["clientOid"])
        if o is None:
            raise ProbeHttpError("code=43001 msg=order not exist")
        return self._ok({"clientOid": o["clientOid"], "state": o["state"], "presetStopLossPrice": o.get("preset") or "",
                         "presetStopLossType": "fill_price"})

    # ------------------------------------------------------------ writes
    def _place(self, p, b):
        preset = b.get("presetStopLossPrice")
        if preset is not None and self.quirk == "REJECT_PRESET":
            raise ProbeHttpError("code=40017 msg=presetStopLossPrice not supported")
        oid, price, size = b["clientOid"], float(b["price"]), float(b["size"])
        if self.quirk == "DROP_PRESET":                                        # accepted, but the preset is silently dropped
            preset = None
        order = {"clientOid": oid, "orderId": self._id(), "size": size, "price": price, "preset": preset, "state": "live"}
        self.orders[oid] = order
        marketable = b["force"] != "post_only" and price >= self.ask
        if b["force"] == "post_only" and price >= self.ask:
            order["state"] = "canceled"
        if marketable:
            order["state"] = "filled"
            self.position += size
            if preset is not None:
                self._materialise(order)
        return self._ok({"orderId": order["orderId"], "clientOid": oid})

    def _materialise(self, order):
        if self.model == "NONE":
            return
        vanish = self.clock.time() + 1.0 if self.quirk == "VANISH" else None
        if self.model == "POSITION_LEVEL":
            pl = next((x for x in self.plans if x["planType"] == "pos_loss"), None)
            if pl is None:
                self.plans.append({"orderId": self._id(), "planType": "pos_loss", "posSide": "long", "size": str(self.position),
                                   "triggerPrice": str(order["preset"])})
            else:
                pl["size"] = str(self.position)
            return
        if self.quirk == "UNCOVERED_SECOND" and any(x["planType"] == "loss_plan" for x in self.plans):
            return
        self.plans.append({"orderId": self._id(), "planType": "loss_plan", "posSide": "long", "size": str(order["size"]),
                           "triggerPrice": str(order["preset"]), "vanish_at": vanish})

    def _cancel(self, p, b):
        o = self.orders.get(b["clientOid"])
        if o is None or o["state"] != "live":
            raise ProbeHttpError("code=43001 msg=order not exist")
        o["state"] = "canceled"
        if self.quirk == "PRESET_LEAKS_ON_CANCEL" and o.get("preset"):
            self.plans.append({"orderId": self._id(), "planType": "loss_plan", "posSide": "long", "size": str(o["size"]),
                               "triggerPrice": str(o["preset"])})
        return self._ok({})

    def _pos_tpsl(self, p, b):
        assert "stopSurplusTriggerPrice" not in b, "the stop-only move must not send a take-profit leg"
        pl = next((x for x in self.plans if x["planType"] == "pos_loss"), None)
        if pl is None:
            raise ProbeHttpError("code=40774 msg=no position tpsl")
        pl["triggerPrice"] = b["stopLossTriggerPrice"]
        if self.quirk == "MOVE_DUPLICATES":
            self.plans.append({"orderId": self._id(), "planType": "pos_loss", "posSide": "long", "size": str(self.position),
                               "triggerPrice": b["stopLossTriggerPrice"]})
        return self._ok({})

    def _tpsl(self, p, b):
        if any(x["planType"] == "pos_loss" for x in self.plans):                  # the 2026-08-01 incident
            self.plans.append({"orderId": self._id(), "planType": "loss_plan", "posSide": "long", "size": b["size"],
                               "triggerPrice": b["triggerPrice"], "vanish_at": self.clock.time() + 1.0})
        else:
            self.plans.append({"orderId": self._id(), "planType": "loss_plan", "posSide": "long", "size": b["size"],
                               "triggerPrice": b["triggerPrice"]})
        return self._ok({})

    def _cancel_plan(self, p, b):
        before = len(self.plans)
        self.plans = [x for x in self.plans if x["orderId"] != b["orderId"]]
        if len(self.plans) == before:
            raise ProbeHttpError("code=43001 msg=plan not exist")
        return self._ok({})

    def _close(self, p, b):
        self.position = 0.0
        released = ("pos_loss", "pos_profit") if self.quirk != "LEFTOVER_POS_PLAN" else ()
        self.plans = [x for x in self.plans if x["planType"] not in released]           # loss_plans stay until cancelled
        return self._ok({})


def run_probe(model: str = "PLAN_PER_ORDER", **fake_kw):
    clock = Clock()
    fake = FakeBitgetDemo(clock, model, **fake_kw)
    probe = Probe(fake, code_sha=SHA, clean_tree=True, harness_sha256="h" * 64, sleep=clock.sleep, clock=clock.time)
    return probe, fake, clock


def gate(tmp_path, doc, clock):
    """What MICRO_LIVE does with the probe's output: pinned digest, release SHA, expiry, every required check."""
    path = tmp_path / "attestation.json"
    raw = json.dumps(doc, sort_keys=True, default=str).encode()
    path.write_bytes(raw)
    return verify_attestation(path, hashlib.sha256(raw).hexdigest(), code_sha=SHA, now=clock.time())


# ==================================================================================== every good exchange behaviour passes
@pytest.mark.parametrize("model,expected,mechanism", [("PLAN_PER_ORDER", "PLAN_LEVEL", "PLAN_LEVEL_REPLACE"),
                                                      ("POSITION_LEVEL", "POSITION_LEVEL", "POSITION_LEVEL_STOP_ONLY")])
def test_a_well_behaved_exchange_yields_a_pass_the_executors_gate_accepts(tmp_path, model, expected, mechanism):
    probe, fake, clock = run_probe(model)
    doc = probe.run()
    assert doc["decision"] == "PASS" and doc["observed_model"] == expected and doc["environment"] == "BITGET_DEMO"
    assert {c["id"] for c in doc["checks"] if c["passed"]} >= set(REQUIRED_CHECKS)
    assert doc["observations"]["stop_move_mechanism"] == mechanism and doc["observations"]["final_state_clean"] is True
    granted = gate(tmp_path, doc, clock)                                  # the SAME verification MICRO_LIVE performs
    assert granted.observed_model == expected and granted.code_sha == SHA


def test_the_probe_never_sends_a_stop_request_before_it_observes_what_the_exchange_did_by_itself():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    probe.run()
    calls = fake.calls
    first_fill = calls.index(("POST", "/api/v2/mix/order/place-order"), 1)             # after the resting order
    stop_requests = [i for i, c in enumerate(calls) if c[1] in ("/api/v2/mix/order/place-tpsl-order",
                                                                "/api/v2/mix/order/place-pos-tpsl")]
    fill_check_reads = [i for i, c in enumerate(calls) if c[1].endswith("orders-plan-pending")]
    assert stop_requests and min(stop_requests) > first_fill and min(fill_check_reads) < min(stop_requests)
    obs = next(c for c in probe.checks if c["id"] == "stop_exists_after_fill_without_client")["observed"]
    assert obs["client_stop_requests"] == 0


# ==================================================================================== every bad behaviour is a FAIL
@pytest.mark.parametrize("model,quirk,failed", [
    ("NONE", None, "stop_exists_after_fill_without_client"),                 # the exchange ignores the preset
    ("PLAN_PER_ORDER", "REJECT_PRESET", "preset_accepted_on_limit_order"),   # the exchange refuses it
    ("PLAN_PER_ORDER", "DROP_PRESET", "preset_accepted_on_limit_order"),     # ...or accepts the order and drops the preset
    ("PLAN_PER_ORDER", "VANISH", "stop_survives_settle"),                    # accepted, then cancelled about a second later
    ("PLAN_PER_ORDER", "UNCOVERED_SECOND", "stop_covers_every_fill"),        # the second fill is not covered
    ("POSITION_LEVEL", "MOVE_DUPLICATES", "executor_stop_move_coexists"),    # a move leaves two competing stops
    ("PLAN_PER_ORDER", "PRESET_LEAKS_ON_CANCEL", "unfilled_cancel_leaves_no_stop"),
    ("POSITION_LEVEL", "LEFTOVER_POS_PLAN", "cleanup_after_flat"),           # an orphan the executor's own cleanup cannot remove
])
def test_every_way_the_exchange_can_misbehave_is_a_fail_and_the_gate_refuses_it(tmp_path, model, quirk, failed):
    probe, fake, clock = run_probe(model, quirk=quirk)
    doc = probe.run()
    assert doc["decision"] == "FAIL"
    assert failed in {c["id"] for c in doc["checks"] if not c["passed"]}
    with pytest.raises(AttestationRefused):
        gate(tmp_path, doc, clock)


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
    assert fake.position == 0.0 and fake.plans == [] and not [o for o in fake.orders.values() if o["state"] == "live"]


def test_an_unexpected_error_mid_run_is_a_fail_never_a_pass_and_still_cleans_up():
    probe, fake, _ = run_probe("PLAN_PER_ORDER")
    original = fake._tpsl

    def boom(p, b):
        raise RuntimeError("network fell over")
    fake._tpsl = boom
    doc = probe.run()
    assert doc["decision"] == "FAIL" and doc["observations"]["final_state_clean"] is True


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
    assert "preset_accepted_on_limit_order" in capsys.readouterr().out
    assert len(dry_run_plan()) == 10


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
    assert verify_attestation(out, digest, code_sha=SHA, now=clock.time()).observed_model == "PLAN_LEVEL"


def test_a_failed_probe_exits_nonzero_and_the_written_file_is_refused_by_the_gate(tmp_path, monkeypatch, capsys):
    clock = Clock()
    monkeypatch.setattr(probe_mod, "git_state", lambda root: (SHA, True))
    out = tmp_path / "a.json"
    assert main(["--out", str(out)], transport=FakeBitgetDemo(clock, "NONE"), sleep=clock.sleep, clock=clock.time) == 1
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    with pytest.raises(AttestationRefused):
        verify_attestation(out, digest, code_sha=SHA, now=clock.time())


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
    assert all(path.startswith("/api/v2/mix/") for _m, path in ALLOWED)


# ==================================================================================== properties pinned by the mutation campaign
def test_a_run_that_cannot_leave_the_demo_symbol_clean_is_never_a_pass(tmp_path):
    probe, fake, clock = run_probe("PLAN_PER_ORDER")
    original = fake._close

    def stuck(p, b):
        if p.get("stuck"):
            return original(p, b)
        fake.calls.append(("POST", "/api/v2/mix/order/close-positions"))
        raise ProbeHttpError("code=50000 msg=temporarily unavailable")
    fake._close = lambda p, b: stuck({}, b)
    doc = probe.run()
    assert doc["observations"].get("final_state_clean") is False and doc["decision"] == "FAIL"
    with pytest.raises(AttestationRefused):
        gate(tmp_path, doc, clock)


def test_a_check_that_never_ran_is_recorded_as_failed_not_left_out(tmp_path):
    probe, _fake, clock = run_probe("NONE")                       # the fill creates no stop: later checks cannot even start
    doc = probe.run()
    by_id = {c["id"]: c for c in doc["checks"]}
    assert set(REQUIRED_CHECKS) <= set(by_id)
    not_run = [i for i, c in by_id.items() if c["observed"].get("not_run")]
    assert {"stop_covers_every_fill", "stop_survives_settle", "executor_stop_move_coexists"} <= set(not_run)
    assert all(by_id[i]["passed"] is False for i in not_run)


def test_a_symbol_that_is_not_clean_is_never_touched_even_by_cleanup(tmp_path):
    """Precondition refusals must not run cleanup against an account state the probe did not create."""
    probe, fake, _ = run_probe("PLAN_PER_ORDER", dirty=True)
    with pytest.raises(ProbeAborted):
        probe.run()
    assert fake.position == 3.0                                   # the pre-existing position was NOT closed by the cleanup
    assert ("POST", "/api/v2/mix/order/close-positions") not in fake.calls


def test_the_decision_needs_a_clean_finish_and_no_unexpected_error_even_when_every_check_passed():
    for observations in ({"final_state_clean": False}, {"final_state_clean": True, "unexpected_error": "boom"}, {}):
        probe, _fake, _ = run_probe("PLAN_PER_ORDER")
        probe.checks = [{"id": c, "passed": True, "observed": {}} for c in REQUIRED_CHECKS]
        probe.observations = {"plan_types_after_first_fill": ["loss_plan"], **observations}
        assert probe.attestation()["decision"] == "FAIL", observations
    probe, _fake, _ = run_probe("PLAN_PER_ORDER")
    probe.checks = [{"id": c, "passed": True, "observed": {}} for c in REQUIRED_CHECKS]
    probe.observations = {"plan_types_after_first_fill": ["loss_plan"], "final_state_clean": True}
    assert probe.attestation()["decision"] == "PASS"


def test_the_model_is_read_from_what_the_exchange_created_and_unknown_is_never_a_pass():
    probe, _fake, _ = run_probe("PLAN_PER_ORDER")
    probe.checks = [{"id": c, "passed": True, "observed": {}} for c in REQUIRED_CHECKS]
    probe.observations = {"final_state_clean": True, "plan_types_after_first_fill": []}
    doc = probe.attestation()
    assert doc["observed_model"] == "UNKNOWN" and doc["decision"] == "FAIL"
    probe.observations["plan_types_after_first_fill"] = ["pos_loss"]
    assert probe.attestation()["observed_model"] == "POSITION_LEVEL"
