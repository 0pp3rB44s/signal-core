"""The capacity + heartbeat monitor Agent C's `CAPACITY_GATE=PASS` ruling depends on.

Ruling conditions: (i) restart under measurement with a free-space alarm and a STOP rule if the drain exceeds 100 MB/h for 30 minutes;
(ii) the recorder floor stays 1.0 GB. Plus the gap the first epoch exposed: a poisoned recorder left the process alive and silent.
Time, disk, process and kill are all injected: nothing here waits, writes to a real volume, or signals a real process.
"""
from __future__ import annotations

import json
import os
import signal
from datetime import datetime, timezone

import pytest

from lsrl.tools.capacity_monitor import GB, MB, Monitor, allocated_bytes, main

T0 = 1_800_000_000.0


class World:
    """A fake host: a clock, free space, the size of LSRL's data dir, a process table and a status file."""

    def __init__(self, tmp_path, **kw):
        self.now, self.free, self.lsrl, self.alive, self.killed = T0, 30 * GB, 1 * GB, True, []
        self.data = tmp_path / "paper"
        self.data.mkdir()
        (self.data / "prospective.pid").write_text("4242")
        self.status = {"recorder_error": None, "engine_halted": None, "feed": {"state": "OK"}}
        self.notified = []
        self.mon = Monitor(data_dir=self.data, log_dir=tmp_path / "mon", clock=lambda: self.now, sleep=self.sleep,
                           disk_free=lambda v: int(self.free), dir_bytes=lambda p: int(self.lsrl),
                           pid_alive=lambda pid: self.alive, kill=lambda pid, sig: self.killed.append((pid, sig)),
                           notifier=self.notified.append, **kw)
        self.write_status()

    def sleep(self, s):
        self.now += s

    def write_status(self, age_s: float = 0.0):
        saved = datetime.fromtimestamp(self.now - age_s, timezone.utc).isoformat()
        (self.data / "status.json").write_text(json.dumps({"_state_metadata": {"saved_at": saved}, "data": self.status}))

    def tick(self, minutes: float = 1.0, *, drain_mb_h: float = 0.0, lsrl_mb_h: float = 0.0):
        """Advance time; free space falls by `drain_mb_h` (external) + `lsrl_mb_h` (LSRL's own writes, which also grow its dir)."""
        h = minutes / 60.0
        self.now += minutes * 60
        self.free -= (drain_mb_h + lsrl_mb_h) * MB * h
        self.lsrl += lsrl_mb_h * MB * h
        self.write_status()
        return self.mon.check()

    def run(self, minutes: int, **kw):
        out = None
        for _ in range(minutes):
            out = self.tick(1.0, **kw)
        return out


def alarms(world) -> set[str]:
    return set(world.mon.active)


# ------------------------------------------------------------------------------------------------ healthy
def test_a_healthy_collection_raises_nothing_and_writes_its_own_heartbeat(tmp_path):
    w = World(tmp_path)
    w.run(45, lsrl_mb_h=35)                                              # LSRL writes 35 MB/h into its own dir: not a drain
    assert alarms(w) == set() and w.killed == [] and not (tmp_path / "mon" / "ALARM.jsonl").exists()
    beat = json.loads((tmp_path / "mon" / "monitor_status.json").read_text())
    assert beat["alive"] is True and beat["active_alarms"] == [] and beat["free_gb"] > 29
    assert len((tmp_path / "mon" / "samples.jsonl").read_text().splitlines()) == 45


# ------------------------------------------------------------------------------------------------ free space
def test_low_free_space_alarms_once_stops_only_below_the_stop_level_and_clears(tmp_path):
    w = World(tmp_path, stop_on_alarm=True, notify=True, free_alarm_gb=8, free_stop_gb=3)
    w.free = 7.5 * GB
    for _ in range(5):
        w.tick()
    assert alarms(w) == {"FREE_SPACE_LOW"} and w.killed == []                       # an alarm, not a stop
    assert [n for n in w.notified if "FREE_SPACE_LOW" in n] and len(w.notified) == 1  # notified when RAISED, not every minute
    w.free = 2.9 * GB
    w.tick()
    assert "FREE_SPACE_STOP_LEVEL" in alarms(w) and w.killed == [(4242, signal.SIGTERM)]
    w.tick()
    assert len(w.killed) == 1                                                        # SIGTERM once, never a hammer
    w.free = 20 * GB
    w.alive = True
    w.tick()
    log = [json.loads(l) for l in (tmp_path / "mon" / "ALARM.jsonl").read_text().splitlines()]
    assert {"RAISED", "CLEARED", "STOP_SENT"} <= {e["event"] for e in log}


def test_without_the_stop_flag_nothing_is_ever_signalled(tmp_path):
    w = World(tmp_path, stop_on_alarm=False)
    w.free = 1 * GB
    w.tick()
    assert "FREE_SPACE_STOP_LEVEL" in alarms(w) and w.killed == []


# ------------------------------------------------------------------------------------------------ drain
def test_a_sustained_drain_above_the_limit_alarms_then_stops_after_the_confirmations(tmp_path):
    w = World(tmp_path, stop_on_alarm=True, drain_limit_mb_h=100, drain_window_min=30, drain_confirmations=5)
    w.run(31, drain_mb_h=150)                                                        # the window is covered: 150 MB/h > 100
    assert "DRAIN_RATE" in alarms(w) and w.killed == []                              # alarm at once, no stop yet
    w.run(5, drain_mb_h=150)
    assert w.killed == [(4242, signal.SIGTERM)]


def test_a_drain_below_the_limit_never_alarms(tmp_path):
    w = World(tmp_path, stop_on_alarm=True)
    w.run(90, drain_mb_h=60)
    assert "DRAIN_RATE" not in alarms(w) and w.killed == []


def test_the_drain_is_not_judged_until_the_whole_window_has_been_observed(tmp_path):
    w = World(tmp_path, stop_on_alarm=True)
    w.run(20, drain_mb_h=5_000)                                                      # a huge rate, but only 20 of 30 minutes observed
    assert "DRAIN_RATE" not in alarms(w) and w.mon.drain_rate_mb_h() is None


def test_lsrls_own_writes_are_not_a_drain_and_compressing_a_day_is_not_a_drain(tmp_path):
    w = World(tmp_path, stop_on_alarm=True)
    w.run(40, lsrl_mb_h=400)                                                         # free falls 400 MB/h but ALL of it is LSRL's own dir
    assert "DRAIN_RATE" not in alarms(w)
    w.lsrl -= 300 * MB                                                               # the closed day is gzipped: dir shrinks ...
    w.free += 300 * MB                                                               # ... and free space rises by the same
    w.run(35)
    assert "DRAIN_RATE" not in alarms(w) and w.killed == []


def test_one_transient_dip_at_the_edge_of_the_window_alarms_but_never_stops(tmp_path):
    w = World(tmp_path, stop_on_alarm=True, drain_confirmations=5)
    w.run(31, drain_mb_h=0)
    w.free -= 400 * MB                                                               # a temp file appears ...
    w.tick()
    w.free += 400 * MB                                                               # ... and is gone
    w.run(10)
    assert w.killed == []                                                            # a transient must never cost the evidence its continuity


def test_the_drain_rate_is_computed_from_the_free_space_and_lsrl_growth_arithmetic(tmp_path):
    w = World(tmp_path)
    w.run(35, drain_mb_h=240, lsrl_mb_h=30)
    assert w.mon.drain_rate_mb_h() == pytest.approx(240, rel=0.06)


# ------------------------------------------------------------------------------------------------ heartbeat
def test_a_dead_process_is_an_alarm_and_a_missing_pidfile_too(tmp_path):
    w = World(tmp_path)
    w.alive = False
    w.tick()
    assert "PROCESS_DEAD" in alarms(w)
    (w.data / "prospective.pid").unlink()
    w.tick()
    assert "NO_PIDFILE" in alarms(w)


def test_a_stale_or_unreadable_status_file_is_an_alarm(tmp_path):
    w = World(tmp_path, stale_status_s=120)
    w.now += 60
    w.write_status(age_s=300)
    assert "STATUS_STALE" in set(w.mon.check()["alarms"])
    (w.data / "status.json").write_text("{not json")
    assert "STATUS_UNREADABLE" in set(w.mon.check()["alarms"])
    (w.data / "status.json").unlink()
    assert "STATUS_UNREADABLE" in set(w.mon.check()["alarms"])


def test_a_poisoned_recorder_is_an_alarm_and_a_stop_because_it_can_never_recover_in_process(tmp_path):
    """The first epoch: the recorder hit its floor and the process stayed alive, halted and silent for 5 h 47 min."""
    w = World(tmp_path, stop_on_alarm=True)
    w.status["recorder_error"] = "RECORDER_DISK_GUARD: 1.00 GB free < floor 1.0 GB"
    w.tick()
    assert "RECORDER_ERROR" in alarms(w) and w.killed == [(4242, signal.SIGTERM)]


def test_a_halted_engine_is_an_alarm_but_not_a_stop(tmp_path):
    w = World(tmp_path, stop_on_alarm=True)
    w.status["engine_halted"] = "RUNTIME_ERROR:OSError"
    w.tick()
    assert "ENGINE_HALTED" in alarms(w) and w.killed == []


def test_a_feed_stuck_unsafe_for_too_long_is_an_alarm_and_a_recovery_clears_it(tmp_path):
    w = World(tmp_path, unsafe_feed_alarm_s=600)
    w.status["feed"] = {"state": "UNSAFE"}
    w.run(9)
    assert "FEED_UNSAFE" not in alarms(w)                                            # a reconnect takes seconds, not minutes
    w.run(3)
    assert "FEED_UNSAFE" in alarms(w)
    w.status["feed"] = {"state": "OK"}
    w.tick()
    assert "FEED_UNSAFE" not in alarms(w)


def test_a_notifier_that_fails_never_breaks_the_monitor(tmp_path):
    w = World(tmp_path, notify=True)
    w.mon._notifier = lambda m: (_ for _ in ()).throw(RuntimeError("no display"))
    w.alive = False
    w.tick()
    assert "PROCESS_DEAD" in alarms(w)


def test_a_kill_that_fails_is_recorded_and_does_not_crash(tmp_path):
    w = World(tmp_path, stop_on_alarm=True)
    w.mon._kill = lambda pid, sig: (_ for _ in ()).throw(ProcessLookupError("gone"))
    w.free = 1 * GB
    w.tick()
    events = [json.loads(l)["event"] for l in (tmp_path / "mon" / "ALARM.jsonl").read_text().splitlines()]
    assert "STOP_FAILED" in events


# ------------------------------------------------------------------------------------------------ mechanics
def test_allocated_bytes_counts_what_a_sparse_file_holds_not_its_apparent_size(tmp_path):
    sparse = tmp_path / "sparse.out"
    with open(sparse, "wb") as fh:
        fh.truncate(500 * 1024 * 1024)
        fh.seek(0)
        fh.write(b"x" * 4096)
    assert os.path.getsize(sparse) == 500 * 1024 * 1024
    assert allocated_bytes(tmp_path) < 5 * 1024 * 1024


def test_the_command_once_exits_1_on_an_alarm_and_0_when_clean_and_writes_nothing_else(tmp_path, capsys):
    data = tmp_path / "paper"
    data.mkdir()
    saved = datetime.now(timezone.utc).isoformat()
    (data / "status.json").write_text(json.dumps({"_state_metadata": {"saved_at": saved},
                                                  "data": {"recorder_error": None, "engine_halted": None, "feed": {"state": "OK"}}}))
    (data / "prospective.pid").write_text(str(os.getpid()))
    args = ["--data-dir", str(data), "--log-dir", str(tmp_path / "mon"), "--volume", str(tmp_path), "--once"]
    assert main(args + ["--free-alarm-gb", "0.000001"]) == 0
    assert json.loads(capsys.readouterr().out)["alarms"] == {}
    assert main(args + ["--free-alarm-gb", "100000"]) == 1
    assert "FREE_SPACE_LOW" in json.loads(capsys.readouterr().out)["alarms"]
    assert not list(data.glob("*.tmp"))                                              # it never writes into the collection's dir


def test_a_small_transient_in_one_sample_is_smoothed_by_the_three_sample_endpoints_and_raises_nothing(tmp_path):
    """One 100 MB dip in the last sample would read as 200 MB/h on a single-sample endpoint; the mean of three reads 67 MB/h."""
    w = World(tmp_path)
    w.run(31)
    w.free -= 100 * MB
    w.tick()
    assert "DRAIN_RATE" not in alarms(w)
    assert w.mon.drain_rate_mb_h() < 100
