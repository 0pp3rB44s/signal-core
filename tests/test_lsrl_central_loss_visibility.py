"""Every finalized LSRL trade reaches the CANONICAL central close record, so the daily-loss, consecutive-loss and
weekly-freeze kill-switches can see an LSRL loss. No parallel ledger: the rows are written by the same writer, with
the same exchange-truth contract, as every other bot-initiated close."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pytest

import risk.risk_manager as risk_manager_module
import telemetry.dataset_builder as dataset_builder
from app.config import Settings
from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.core.types import Direction, PlaceEntry, TimerTick
from risk.risk_manager import RiskManager
from telemetry.close_record_sources import is_economic_close
from tests.lsrl_fixtures import long_sweep
from tests.test_lsrl_executor_hardening import entries
from tests.test_lsrl_live_executor import Rig
from tests.test_lsrl_risk_integration import _evaluate, _isolate  # noqa: F401  (autouse isolation fixture)


def read_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def history_row(position: dict, *, net_loss: float = 40.0, position_id: str = "POS-1") -> dict:
    """A Bitget `history-position` row for this lifecycle (netProfit = pnl - fees + funding, all exchange numbers)."""
    qty, open_p, close_p = position["confirmed_position_size"], position["exchange_avg_entry"], position["exit"]
    gross = -(net_loss - 1.4)
    return {"positionId": position_id, "symbol": "SOLUSDT", "holdSide": "long", "pnl": str(gross),
            "netProfit": str(gross - 0.5 - 0.9), "openFee": "-0.5", "closeFee": "-0.9", "totalFunding": "0",
            "closeTotalPos": str(qty), "openTotalPos": str(qty), "openAvgPrice": str(open_p),
            "closeAvgPrice": str(close_p), "ctime": str(position["opened_at_ms"] - 400),
            "utime": str(position["closed_at_ms"] - 400)}


def stopped_out_rig(tmp_path, **kw) -> Rig:
    rig = Rig(tmp_path, **kw).run(long_sweep())
    for oid in entries(rig):
        rig.ex.fill(oid)
    rig.poll()
    rig.ex.trigger_stop(slippage=0.05)
    rig.poll(6_000)
    return rig


def later(rig: Rig, ms: int = 6_000) -> None:
    """Let engine/exchange time pass, then reconcile (the central retry is rate limited to one try per 5 s)."""
    rig.feed(TimerTick(t_ms=rig.engine.now_ms + ms))
    rig.poll(0)


def only_item(rig: Rig) -> dict:
    (item,) = rig.exec.pending_central.values()
    return item


# ------------------------------------------------------------------ the record is written the canonical way
def test_a_finalized_loss_is_written_provisional_first_then_economic_from_exchange_truth(tmp_path):
    rig = stopped_out_rig(tmp_path)
    item = only_item(rig)
    rows = read_rows(rig.dataset)
    assert [r["event_type"] for r in rows] == ["CLOSE_PROVISIONAL"]               # visible immediately, money-free
    assert rows[0]["net_pnl"] == "" and rows[0]["strategy"] == "lsrl_v1"
    assert not is_economic_close(rows[0])                                          # a provisional row counts nowhere
    rig.ex.position_history.append(history_row(item["position"]))
    later(rig)
    assert rig.exec.pending_central == {}
    rows = read_rows(rig.dataset)
    econ = [r for r in rows if r["event_type"] == "CLOSE"]
    assert len(econ) == 1 and is_economic_close(econ[0])
    assert econ[0]["sync_source"] == "bitget_position_history" and econ[0]["strategy"] == "lsrl_v1"
    assert float(econ[0]["net_pnl"]) == pytest.approx(-40.0 - 0.0)                 # copied from the exchange, not derived
    assert econ[0]["position_lifecycle_id"].startswith("lsrl-") and econ[0]["exchange_position_id"] == "POS-1"
    assert econ[0]["data_confidence"] == "EXCHANGE_TRUTH"


def test_recording_is_idempotent_a_second_pass_never_writes_a_duplicate_close(tmp_path):
    rig = stopped_out_rig(tmp_path)
    item = only_item(rig)
    rig.ex.position_history.append(history_row(item["position"]))
    later(rig)
    n = len(read_rows(rig.dataset))
    assert rig.exec.central.reconcile_once(item["position"], item["close_reason"]) == "ALREADY"
    assert len(read_rows(rig.dataset)) == n


def test_new_entries_stay_blocked_until_the_loss_is_economically_recorded(tmp_path):
    rig = stopped_out_rig(tmp_path)
    probe = PlaceEntry("next", "next-e1", 1, Direction.LONG, 149.5, 1.0, 1)
    assert rig.exec._entry_cycle_gate(probe, rig.engine.now_ms) == "CENTRAL_CLOSE_RECORD_PENDING"
    later(rig)                                                                     # the exchange has not published it yet
    assert rig.exec._entry_cycle_gate(probe, rig.engine.now_ms) == "CENTRAL_CLOSE_RECORD_PENDING"
    rig.ex.position_history.append(history_row(only_item(rig)["position"]))
    later(rig)
    assert rig.exec._entry_cycle_gate(probe, rig.engine.now_ms) is None


def test_a_close_the_exchange_never_publishes_halts_the_executor_instead_of_hiding_the_loss(tmp_path, monkeypatch):
    import lsrl.adapters.live as live
    monkeypatch.setattr(live, "CENTRAL_MAX_ATTEMPTS", 3)
    rig = stopped_out_rig(tmp_path)
    for _ in range(4):
        later(rig)
    assert rig.exec.halt_reason and "CENTRAL_CLOSE_RECORD_UNRECONCILED" in rig.exec.halt_reason
    assert "CENTRAL_CLOSE_UNRECONCILED" in [i["kind"] for i in rig.exec.incidents]
    assert [r["event_type"] for r in read_rows(rig.dataset)] == ["CLOSE_PROVISIONAL"]     # never a fabricated zero


def test_the_pending_record_survives_a_restart(tmp_path):
    rig = stopped_out_rig(tmp_path)
    restarted = rig.build_executor()
    assert restarted.pending_central.keys() == rig.exec.pending_central.keys()
    rig.exec = restarted
    rig.ex.position_history.append(history_row(only_item(rig)["position"]))
    later(rig)
    assert rig.exec.pending_central == {}
    assert len([r for r in read_rows(rig.dataset) if r["event_type"] == "CLOSE"]) == 1


# ------------------------------------------------------------------ the central protections actually SEE it
def _fixture_now(monkeypatch, ms: int) -> None:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(ms / 1000, tz or timezone.utc)
    monkeypatch.setattr(dataset_builder, "datetime", FrozenDatetime)


def test_the_daily_and_weekly_kill_switches_block_the_next_lsrl_setup_after_an_lsrl_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(risk_manager_module, "BASE_PATH", tmp_path)
    (tmp_path / "logs").mkdir()
    rig = stopped_out_rig(tmp_path, dataset=tmp_path / "logs" / "trade_dataset_v2.csv")
    rig.ex.position_history.append(history_row(only_item(rig)["position"], net_loss=40.0))
    later(rig)
    rows = read_rows(rig.dataset)

    # 1. the WEEKLY freeze meter reads the dataset directly
    RiskManager._weekly_pnl_cache = None
    settings = Settings(_env_file=None, WEEKLY_FREEZE_LOSS_PCT=3.0, HARD_DAILY_STOP_PCT=2.0)
    rm = RiskManager(settings=settings)
    rm._latest_backtest_summary = lambda: {}
    rm._latest_strategy_expectancy = lambda: {}
    rm._latest_agent_decisions = staticmethod(lambda: {"decisions": []})
    rm._load_open_positions = staticmethod(lambda: [])
    assert rm._weekly_realized_pnl() == pytest.approx(-40.0)

    # 2. the DAILY report is derived from the very same rows (the runner refreshes it with build_dataset())
    _fixture_now(monkeypatch, rig.engine.now_ms + 3_600_000)
    report = dataset_builder._daily_learning_report(rows, {})
    assert report["daily_total_net_pnl"] == pytest.approx(-40.0) and report["consecutive_losses"] == 1
    report_path = tmp_path / "data_store" / "trades" / "daily_learning_report.json"
    report_path.parent.mkdir(parents=True)
    import json
    report_path.write_text(json.dumps(report))
    assert RiskManager._daily_defensive_status()["daily_total_net_pnl"] == pytest.approx(-40.0)

    # 3. ... and the central gate now refuses the next LSRL setup for BOTH reasons
    RiskManager._weekly_pnl_cache = None
    _gate, decision = _evaluate(rm)
    assert not decision.allowed
    joined = " | ".join(decision.reasons)
    assert "kill-switch: daily defensive mode active" in joined and "kill-switch: weekly freeze active" in joined


def test_the_provisional_row_alone_does_not_move_any_meter(tmp_path, monkeypatch):
    """A percentage-free, money-free provisional row can never be mistaken for a loss (or a win)."""
    monkeypatch.setattr(risk_manager_module, "BASE_PATH", tmp_path)
    (tmp_path / "logs").mkdir()
    rig = stopped_out_rig(tmp_path, dataset=tmp_path / "logs" / "trade_dataset_v2.csv")
    RiskManager._weekly_pnl_cache = None
    rm = RiskManager(settings=Settings(_env_file=None))
    assert rm._weekly_realized_pnl() == 0.0
    assert [r["event_type"] for r in read_rows(rig.dataset)] == ["CLOSE_PROVISIONAL"]


def test_three_consecutive_lsrl_losses_trip_the_central_consecutive_loss_limit(tmp_path, monkeypatch):
    import json

    from lsrl.adapters.central_ledger import CentralCloseRecorder
    from tests.lsrl_fake_exchange import FakeExchange
    from tests.lsrl_fixtures import RULES

    monkeypatch.setattr(risk_manager_module, "BASE_PATH", tmp_path)
    dataset = tmp_path / "logs" / "trade_dataset_v2.csv"
    dataset.parent.mkdir()
    recorder = CentralCloseRecorder(client=FakeExchange(rules=RULES), dataset_path=str(dataset))
    base = 1_800_002_000_000
    for i in range(3):
        opened, closed = base + i * 600_000, base + i * 600_000 + 300_000
        position = recorder.position_record({
            "setup_id": f"s{i}", "symbol": "SOLUSDT", "direction": "LONG", "entered_qty": 1.6, "entry_notional": 240.0,
            "exit_qty": 1.6, "exit_notional": 236.8, "opened_at_ms": opened, "closed_at_ms": closed,
            "stop_price": 148.0, "leverage": 5, "take_profits": [151.0], "close_reason": "STOP_LOSS"})
        recorder.write_provisional(position, "STOP_LOSS")
        row = {"positionId": f"P{i}", "symbol": "SOLUSDT", "holdSide": "long", "pnl": "-3.2", "netProfit": "-4.4",
               "openFee": "-0.5", "closeFee": "-0.7", "totalFunding": "0", "closeTotalPos": "1.6", "openTotalPos": "1.6",
               "openAvgPrice": "150", "closeAvgPrice": "148", "ctime": str(opened - 400), "utime": str(closed - 400)}
        assert recorder.reconcile_once(position, "STOP_LOSS", fetch_history=lambda r=row: [r]) == "RECONCILED"
    rows = read_rows(dataset)
    _fixture_now(monkeypatch, base + 3_600_000)
    report = dataset_builder._daily_learning_report(rows, {})
    assert report["consecutive_losses"] == 3 and report["daily_losses"] == 3
    (tmp_path / "data_store" / "trades").mkdir(parents=True)
    (tmp_path / "data_store" / "trades" / "daily_learning_report.json").write_text(json.dumps(report))
    rm = RiskManager(settings=Settings(_env_file=None))
    rm._latest_backtest_summary = lambda: {}
    rm._latest_strategy_expectancy = lambda: {}
    rm._latest_agent_decisions = staticmethod(lambda: {"decisions": []})
    rm._load_open_positions = staticmethod(lambda: [])
    RiskManager._weekly_pnl_cache = None
    _gate, decision = _evaluate(rm)
    assert not decision.allowed and any("consecutive loss limit" in r for r in decision.reasons)
