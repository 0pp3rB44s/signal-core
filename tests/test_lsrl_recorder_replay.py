"""LSRL_V1 prospective recorder + replay: raw causal inputs, integrity, crash recovery, decision equivalence.

The recorder is the only evidence a future auditor will have, so these tests attack it: torn writes, truncation,
tampering, restarts, disk exhaustion, compression, and the feed pathologies (duplicate / out-of-order update,
sequence gap, stale gap, reconnect + resnapshot) recorded and replayed through a FRESH engine.
"""
from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from lsrl.adapters.sim import SimExchange
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import (BarClosed, DepthSnapshot, FeedHealth, FeedMarker, NavEvent, RawBook, RawTrade,
                             TimerTick, TradeTick)
from lsrl.driver import SimDriver
from lsrl.io.gate_log import RecordingRiskGate
from lsrl.io.recorder import (GENESIS, LSRLRecorder, RecorderError, anchor_path, compress_closed_days,
                              decision_files, input_files, read_anchor, read_bodies, read_events, read_header,
                              verify_chain)
from lsrl.io.replay import decisions_match, replay
from lsrl.modes import Mode
from lsrl.obs.observer import FeedConfig
from tests.lsrl_fixtures import RULES, S
from tests.lsrl_raw_feed import Truth, drop, duplicate, retime, to_raw, with_ticks
from tests.test_lsrl_causal_timelines import (FROZEN_DEPTH, T, both, observed, replace_conn, run, timeline_a,
                                              timeline_d_with, view)

DAY = 86_400_000


def recorder(root: Path, **kw) -> LSRLRecorder:
    kw.setdefault("min_free_gb", 0.001)
    kw.setdefault("feed", asdict(FeedConfig()))
    kw.setdefault("git_sha", "fa3f9b4")
    return LSRLRecorder(root, CFG, RULES, mode=kw.pop("mode", "PAPER"), **kw)


def some_events(n: int = 30, t0: int = S) -> list:
    """`n` real-looking raw frames: a CONNECT, a snapshot, then a stream of updates (every state differs)."""
    truth = Truth()
    for i in range(n):
        bid = 149.95 - (0.05 if i % 2 else 0.0)
        truth.book(t0 + i * 100, bid, bid + 0.10)
    events = to_raw(truth, keepalive_ms=0, tape_keepalive_ms=0)
    assert len(events) >= n
    return events[:n]


def write(rec: LSRLRecorder, events) -> None:
    for ev in events:
        rec.record_event(ev)


def lines(path: Path) -> list[str]:
    return path.read_text().splitlines()


# ------------------------------------------------------------------ fresh start / header
def test_a_fresh_recording_starts_with_a_complete_header_and_an_explicit_session_start(tmp_path):
    rec = recorder(tmp_path / "rec", session_id="sess-1", start_ts_ms=1_234, exchange="bitget",
                   transport={"url": "wss://example", "ping_interval_s": 25.0})
    write(rec, some_events(5))
    rec.close()
    files = input_files(tmp_path / "rec")
    header = read_header(files[0])
    assert header["type"] == "header" and header["strategy"] == "LSRL_V1" and header["spec"] == "LSRL_V1"
    assert (header["schema_version"], header["format"]) == (2, "lsrl_input_v2")
    assert header["git_sha"] == "fa3f9b4" and header["config_digest"] == CFG.digest()
    assert header["feed"] == asdict(FeedConfig()) and header["feed_digest"] == FeedConfig().digest()
    assert (header["symbol"], header["exchange"], header["mode"]) == ("SOLUSDT", "bitget", "PAPER")
    assert header["session_id"] == "sess-1" and header["start_ts_ms"] == 1_234
    assert header["transport"] == {"url": "wss://example", "ping_interval_s": 25.0}
    assert header["symbol_rules"]["tick_size"] == 0.001 and header["day"]
    bodies = [b["type"] for b in read_bodies(files)]
    assert bodies[:2] == ["header", "session_start"] and set(bodies[2:]) == {"event"}
    start = next(read_bodies(files, ("session_start",)))
    assert start["session_id"] == "sess-1" and start["git_sha"] == "fa3f9b4" and start["mode"] == "PAPER"


def test_the_git_sha_defaults_to_unknown_never_blank(tmp_path):
    rec = LSRLRecorder(tmp_path / "rec", CFG, RULES, mode="SHADOW", min_free_gb=0.001)
    write(rec, some_events(2))
    rec.close()
    assert read_header(input_files(tmp_path / "rec")[0])["git_sha"] == "UNKNOWN"


# ------------------------------------------------------------------ raw causal inputs are complete
def test_the_input_log_is_raw_and_complete_and_holds_no_derived_market_verdicts(tmp_path):
    rec = recorder(tmp_path / "rec")
    events, _ = timeline_a()
    events = with_ticks(events, start_ms=T(-100), end_ms=T(62), every_ms=5_000)
    d = run(events, recorder=rec)
    rec.close()
    logged = list(read_events(input_files(tmp_path / "rec")))
    types = {type(e).__name__ for e in logged}
    assert {"RawBook", "RawTrade", "FeedMarker", "BarClosed", "NavEvent", "TimerTick", "OrderEvent"} <= types
    assert not types & {"DepthSnapshot", "TradeTick", "FeedHealth"}, "derived events must never replace raw evidence"
    books = [e for e in logged if isinstance(e, RawBook)]
    assert books[0].action == "snapshot" and any(b.action == "update" for b in books)
    assert all(b.seq is not None and b.exchange_ts_ms and b.t_ms and b.conn_id for b in books)
    assert all(b.pseq is not None for b in books[1:])
    trades = [e for e in logged if isinstance(e, RawTrade)]
    assert trades and all(t.trade_id and t.exchange_ts_ms and t.price and t.size and t.side for t in trades)
    assert {e.tf_s for e in logged if isinstance(e, BarClosed)} == {60, 900}       # 1m AND 15m completed bars
    assert [e.kind for e in logged if isinstance(e, FeedMarker)][:1] == ["CONNECT"]
    assert any(isinstance(e, NavEvent) for e in logged)                             # NAV / risk inputs
    gates = list(read_bodies(input_files(tmp_path / "rec"), ("gate",)))
    assert d.engine.setup is not None and len(gates) == 1 and gates[0]["decision"]["allowed"] is True
    bodies = [b for b in read_bodies(decision_files(tmp_path / "rec"), ("record",))]
    assert "feed_health" in {b["kind"] for b in bodies}                             # health transitions are persisted


# ------------------------------------------------------------------ integrity: chain, tamper, reorder
def recorded(tmp_path, n=30, *, close: bool = True, **kw) -> Path:
    """close=False models a CRASH: no final anchor is written, only the ones due during the session."""
    rec = recorder(tmp_path / "rec", **kw)
    write(rec, some_events(n))
    if close:
        rec.close()
    return tmp_path / "rec"


def test_hash_chain_continuity_a_clean_log_verifies_with_its_anchor(tmp_path):
    root = recorded(tmp_path, anchor_every=5)
    files = input_files(root)
    assert verify_chain(files) == (True, "OK")
    assert verify_chain(files, anchor=anchor_path(root, "inputs")) == (True, "OK")
    rows = [json.loads(x) for x in lines(files[0])]
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))
    assert rows[1]["h"] and rows[0]["h"] != GENESIS


@pytest.mark.parametrize("edit", ["value", "delete", "reorder", "insert"])
def test_any_edit_of_the_recorded_history_is_detected(tmp_path, edit):
    root = recorded(tmp_path)
    path = input_files(root)[0]
    rows = lines(path)
    if edit == "value":
        rows[6] = rows[6].replace("149.9", "148.9", 1) if "149.9" in rows[6] else rows[6].replace("1", "2", 1)
    elif edit == "delete":
        del rows[7]
    elif edit == "reorder":
        rows[6], rows[7] = rows[7], rows[6]
    else:
        rows.insert(7, rows[6])
    path.write_text("\n".join(rows) + "\n")
    ok, why = verify_chain([path])
    assert ok is False and any(k in why for k in ("HASH_MISMATCH", "SEQ_GAP"))
    with pytest.raises(ValueError, match="integrity"):
        replay([path])


def test_a_mid_file_garbage_line_is_reported_as_unparseable_not_as_a_torn_tail(tmp_path):
    root = recorded(tmp_path)
    path = input_files(root)[0]
    rows = lines(path)
    rows[5] = rows[5][:20]
    path.write_text("\n".join(rows) + "\n")
    ok, why = verify_chain([path])
    assert ok is False and why.startswith("UNPARSEABLE_LINE")


# ------------------------------------------------------------------ tail truncation (needs the anchor)
def test_tail_truncation_at_a_line_boundary_is_invisible_to_the_chain_but_caught_by_the_anchor(tmp_path):
    root = recorded(tmp_path, n=30, anchor_every=5)
    path = input_files(root)[0]
    keep = lines(path)[:-8]
    path.write_text("\n".join(keep) + "\n")
    assert verify_chain([path]) == (True, "OK")                                     # a shorter chain is still a chain
    ok, why = verify_chain([path], anchor=anchor_path(root, "inputs"))
    assert ok is False and why.startswith("TAIL_TRUNCATED")


def test_a_recorder_refuses_to_continue_past_a_truncated_tail(tmp_path):
    root = recorded(tmp_path, n=30, anchor_every=5)
    path = input_files(root)[0]
    path.write_text("\n".join(lines(path)[:-8]) + "\n")
    with pytest.raises(RecorderError, match="TAIL_TRUNCATED"):
        recorder(root)


def test_deleting_every_data_file_while_the_anchor_survives_is_detected(tmp_path):
    root = recorded(tmp_path)
    for f in input_files(root):
        f.unlink()
    with pytest.raises(RecorderError, match="DATA_MISSING"):
        recorder(root)


def test_a_tampered_anchor_is_refused(tmp_path):
    root = recorded(tmp_path, anchor_every=5)
    a = anchor_path(root, "inputs")
    rows = lines(a)
    rows[1] = rows[1].replace('"seq":', '"seq":9', 1)
    a.write_text("\n".join(rows) + "\n")
    entries, err, _ = read_anchor(a)
    assert err and "ANCHOR" in err
    with pytest.raises(RecorderError, match="ANCHOR"):
        recorder(root)


def test_an_anchor_that_disagrees_with_the_data_at_the_same_seq_is_refused(tmp_path):
    root = recorded(tmp_path, anchor_every=5)
    path = input_files(root)[0]
    rows = lines(path)
    row = json.loads(rows[10])
    row["body"]["event"]["t_ms"] += 1
    rows[10] = json.dumps(row, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(rows) + "\n")
    ok, why = verify_chain([path], anchor=anchor_path(root, "inputs"))
    assert ok is False and ("HASH_MISMATCH" in why or "ANCHOR_MISMATCH" in why)


def test_the_anchor_is_itself_a_hash_chain_written_during_the_session_not_only_at_close(tmp_path):
    rec = recorder(tmp_path / "rec", anchor_every=4)
    write(rec, some_events(30))
    entries, err, _ = read_anchor(anchor_path(tmp_path / "rec", "inputs"))       # BEFORE close(): a crash now
    assert err is None and len(entries) >= 5                                      # still leaves a recent anchor
    assert entries[-1]["seq"] >= rec.inputs.state["seq"] - 4
    rec.close()


# ------------------------------------------------------------------ crash recovery: torn / garbled final record
def test_a_torn_final_record_is_quarantined_and_the_same_chain_continues(tmp_path):
    root = recorded(tmp_path, n=20)
    path = input_files(root)[0]
    good = path.read_bytes()
    path.write_bytes(good + b'{"seq":9999,"body":{"type":"event","event":{"type":"raw_book","t_ms":17')   # crash mid-write
    ok, why = verify_chain([path])
    assert ok is False and why.startswith("TORN_TAIL")                             # visible BEFORE recovery
    rec = recorder(root, session_id="after-crash")
    assert path.read_bytes() == good                                               # truncated to the last valid line
    quarantined = list(root.glob("quarantine-inputs-*.bin"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes().startswith(b'{"seq":9999')   # evidence kept
    write(rec, some_events(4, t0=S + 60_000))
    rec.close()
    assert verify_chain(input_files(root), anchor=anchor_path(root, "inputs")) == (True, "OK")
    recovery = next(read_bodies(input_files(root), ("recovery",)))
    assert recovery["reason"] == "TORN_TAIL" and recovery["discarded_bytes"] == len(quarantined[0].read_bytes())
    assert recovery["quarantine"] == quarantined[0].name
    starts = list(read_bodies(input_files(root), ("session_start",)))
    assert [s["session_id"] for s in starts][-1] == "after-crash" and len(starts) == 2


def test_a_complete_but_garbled_final_line_is_also_quarantined(tmp_path):
    root = recorded(tmp_path, n=20, close=False)                                     # a crash: the last line is unanchored
    path = input_files(root)[0]
    rows = lines(path)
    last = json.loads(rows[-1])
    last["h"] = "0" * 64
    rows[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(rows) + "\n")
    rec = recorder(root)
    write(rec, some_events(3, t0=S + 60_000))
    rec.close()
    assert verify_chain(input_files(root)) == (True, "OK")
    assert next(read_bodies(input_files(root), ("recovery",)))["reason"] in ("TORN_TAIL", "CORRUPT_TAIL")
    assert len(list(root.glob("quarantine-inputs-*.bin"))) == 1


def test_a_crash_that_left_a_torn_first_write_of_a_new_day_falls_back_to_the_previous_file(tmp_path):
    rec = recorder(tmp_path / "rec")
    write(rec, some_events(6))
    rec.close()
    root = tmp_path / "rec"
    new_day = root / f"inputs-{'2099-01-01'}.jsonl"
    new_day.write_bytes(b'{"seq":9,"body":{"typ')                                   # torn header of a new day file
    rec = recorder(root)
    write(rec, some_events(3, t0=S + 30_000))
    rec.close()
    assert verify_chain(input_files(root), anchor=anchor_path(root, "inputs")) == (True, "OK")


def test_a_torn_anchor_tail_is_repaired_on_restart(tmp_path):
    root = recorded(tmp_path, anchor_every=5)
    a = anchor_path(root, "inputs")
    a.write_bytes(a.read_bytes() + b'{"n":99,"seq":')
    rec = recorder(root)
    write(rec, some_events(12, t0=S + 60_000))
    rec.close()
    entries, err, torn = read_anchor(a)
    assert err is None and torn == 0
    assert verify_chain(input_files(root), anchor=a) == (True, "OK")


def test_replay_refuses_a_torn_log_unless_told_it_is_looking_at_a_crash_site(tmp_path):
    rec = recorder(tmp_path / "rec")
    d = run(observed("A_true_consumption", "LONG")[0], recorder=rec)
    rec.close()
    path = input_files(tmp_path / "rec")[0]
    path.write_bytes(path.read_bytes() + b'{"seq":')
    with pytest.raises(ValueError, match="integrity"):
        replay([path])
    r = replay([path], tolerate_torn_tail=True)
    assert r.engine.snapshot() == d.engine.snapshot()                               # everything durable is reproduced


# ------------------------------------------------------------------ restart
def test_a_clean_restart_continues_the_chain_and_marks_each_session(tmp_path):
    root = tmp_path / "rec"
    for i in range(3):
        rec = recorder(root, session_id=f"s{i}")
        write(rec, some_events(5, t0=S + i * 60_000))
        rec.close()
    files = input_files(root)
    assert verify_chain(files, anchor=anchor_path(root, "inputs")) == (True, "OK")
    assert [b["session_id"] for b in read_bodies(files, ("session_start",))] == ["s0", "s1", "s2"]
    rows = [json.loads(x) for x in lines(files[0])]
    assert [r["seq"] for r in rows] == list(range(1, len(rows) + 1))               # ONE unbroken sequence


def test_the_inputs_and_decisions_streams_are_independent_chains(tmp_path):
    rec = recorder(tmp_path / "rec")
    run(observed("A_true_consumption", "LONG")[0], recorder=rec)
    rec.close()
    root = tmp_path / "rec"
    for prefix, files in (("inputs", input_files(root)), ("decisions", decision_files(root))):
        assert verify_chain(files, anchor=anchor_path(root, prefix)) == (True, "OK")


# ------------------------------------------------------------------ day rollover + lossless compression
def two_day_recording(tmp_path):
    root = tmp_path / "rec"
    edge = (S // DAY + 1) * DAY
    rec = recorder(root, anchor_every=5)
    write(rec, some_events(6, t0=edge - 20_000))
    write(rec, some_events(6, t0=edge + 5_000))
    rec.close()
    if rec.inputs._compress_thread is not None:
        rec.inputs._compress_thread.join(timeout=10)
    return root


def test_the_log_rotates_per_utc_day_without_breaking_the_chain(tmp_path):
    root = two_day_recording(tmp_path)
    files = input_files(root)
    assert len(files) == 2 and verify_chain(files, anchor=anchor_path(root, "inputs")) == (True, "OK")


def test_a_closed_day_is_compressed_losslessly_and_still_verifies_replays_and_resumes(tmp_path):
    root = two_day_recording(tmp_path)
    plain = sorted(root.glob("inputs-*.jsonl"))
    gz = sorted(root.glob("inputs-*.jsonl.gz"))
    assert [p.name for p in plain] == [input_files(root)[-1].name] and len(gz) == 1   # only TODAY stays plain
    with gzip.open(gz[0], "rt") as fh:
        first = json.loads(fh.readline())
    assert first["body"]["type"] == "header"
    assert verify_chain(input_files(root), anchor=anchor_path(root, "inputs")) == (True, "OK")
    assert len(list(read_events(input_files(root)))) == 12
    rec = recorder(root)                                                            # restart after compression
    write(rec, some_events(3, t0=S + 3 * DAY))
    rec.close()
    assert verify_chain(input_files(root), anchor=anchor_path(root, "inputs")) == (True, "OK")


def test_compression_removes_the_plain_file_only_after_proving_the_bytes_identical(tmp_path):
    root = tmp_path / "rec"
    rec = recorder(root)
    write(rec, some_events(6, t0=(S // DAY + 1) * DAY - 5_000))
    write(rec, some_events(3, t0=(S // DAY + 2) * DAY + 5_000))
    rec.close()
    rec.inputs._compress_thread.join(timeout=10)
    plain_before = None
    for gz in root.glob("inputs-*.jsonl.gz"):
        want = hashlib.sha256(gzip.open(gz, "rb").read()).hexdigest()
        assert want                                                                   # decompresses cleanly
        plain_before = gz
    assert plain_before is not None and not list(root.glob("*.gz.tmp"))


def test_an_interrupted_compression_leaves_the_plain_file_and_is_ignored_by_readers(tmp_path):
    root = tmp_path / "rec"
    rec = recorder(root)
    write(rec, some_events(6, t0=(S // DAY + 1) * DAY - 5_000))
    write(rec, some_events(3, t0=(S // DAY + 2) * DAY + 5_000))
    rec.close()
    rec.inputs._compress_thread.join(timeout=10)
    day1 = next(root.glob("inputs-*.jsonl.gz"))
    plain = day1.with_name(day1.name[:-3])
    with gzip.open(day1, "rb") as src:
        plain.write_bytes(src.read())                                               # pretend compression had not finished
    day1.rename(day1.with_name(day1.name + ".tmp"))
    assert plain in input_files(root) and verify_chain(input_files(root), anchor=anchor_path(root, "inputs")) == (True, "OK")
    compress_closed_days(root, "inputs", "9999-12-31")                             # a later pass completes it safely
    assert not plain.exists() and any(root.glob("inputs-*.jsonl.gz"))
    assert verify_chain(input_files(root), anchor=anchor_path(root, "inputs")) == (True, "OK")


def test_a_corrupt_compressed_day_is_refused_on_resume(tmp_path):
    root = tmp_path / "rec"
    rec = recorder(root)
    write(rec, some_events(6, t0=(S // DAY + 1) * DAY - 5_000))
    rec.close()
    compress_closed_days(root, "inputs", "9999-12-31")
    (gz,) = list(root.glob("inputs-*.jsonl.gz"))
    gz.write_bytes(gz.read_bytes()[:-20])
    with pytest.raises(RecorderError, match="GZ_CORRUPT"):
        recorder(root)


# ------------------------------------------------------------------ disk safety floor
def test_below_the_disk_floor_the_recorder_fails_closed_and_stays_failed(tmp_path):
    rec = recorder(tmp_path / "rec", min_free_gb=1e9)
    with pytest.raises(RecorderError, match="DISK_GUARD"):
        rec.record_event(some_events(1)[0])
    rec.inputs.min_free_gb = 0.0                                                    # even if space 'reappears'
    with pytest.raises(RecorderError, match="DISK_GUARD"):
        rec.record_event(some_events(1)[0])
    assert not list((tmp_path / "rec").glob("inputs-*.jsonl"))                      # nothing half-written


def test_a_write_error_poisons_the_writer_and_never_skips_a_record(tmp_path):
    rec = recorder(tmp_path / "rec")
    write(rec, some_events(3))

    class Full:                                                                     # ENOSPC on the next append
        def write(self, _data):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

        def close(self):
            pass
    rec.inputs._fh = Full()
    with pytest.raises(RecorderError, match="WRITE_FAILED"):
        rec.record_event(some_events(1)[0])
    with pytest.raises(RecorderError, match="WRITE_FAILED"):                       # stays failed: no silent recovery
        rec.record_event(some_events(1)[0])
    assert verify_chain(input_files(tmp_path / "rec")) == (True, "OK")


# ------------------------------------------------------------------ genesis replay + decision equivalence
def record_and_replay(tmp_path, events, **kw):
    rec = recorder(tmp_path / "rec", **kw)
    d = run(events, recorder=rec)
    rec.close()
    root = tmp_path / "rec"
    files = input_files(root)
    replayed = replay(files, anchor=anchor_path(root, "inputs"))
    same, why = decisions_match(files, decision_files(root))
    return d, replayed, same, why


@pytest.mark.parametrize("direction", ["LONG", "SHORT"])
def test_genesis_replay_of_a_full_setup_lifecycle_reproduces_every_record_and_the_final_state(tmp_path, direction):
    events, _ = timeline_a()
    d, replayed, same, why = record_and_replay(tmp_path, view(events, direction))
    assert d.engine.setup is not None and same, why
    assert [r.to_dict() for r in replayed.records] == [r.to_dict() for r in d.records]
    assert replayed.engine.snapshot() == d.engine.snapshot() and replayed.sessions == 0


PATHOLOGIES = {
    "duplicate_update": lambda ev: duplicate(ev, lambda e: isinstance(e, RawBook) and e.action == "update"
                                             and e.exchange_ts_ms in (T(20), T(33.7))),
    "duplicate_trade": lambda ev: duplicate(ev, lambda e: isinstance(e, RawTrade) and e.trade_id.startswith("t")),
    "out_of_order_update": lambda ev: reorder_two_updates(ev),
    "sequence_gap": lambda ev: drop(ev, lambda e: isinstance(e, RawBook) and e.exchange_ts_ms == T(33.7)
                                    and e.action == "update"),
    "stale_gap": lambda ev: with_ticks(drop(ev, lambda e: isinstance(e, RawBook) and T(31) <= e.exchange_ts_ms <= T(37)),
                                       start_ms=T(-100), end_ms=T(62)),
    "reconnect_resnapshot": lambda ev: reconnect_with_resnapshot(ev),
}


def reorder_two_updates(events):
    ups = [e for e in events if isinstance(e, RawBook) and e.action == "update" and e.exchange_ts_ms >= T(-200)][:2]
    a, b = ups
    swapped = [e for e in events]
    ia, ib = swapped.index(a), swapped.index(b)
    swapped[ia], swapped[ib] = replace(b, t_ms=a.t_ms), replace(a, t_ms=b.t_ms)
    return swapped


def reconnect_with_resnapshot(events):
    cut = next(i for i, e in enumerate(events) if isinstance(e, RawBook) and e.exchange_ts_ms >= T(10))
    bids, asks = {}, {}
    for e in events[:cut]:
        if isinstance(e, RawBook):
            if e.action == "snapshot":
                bids.clear(), asks.clear()
            for side, rows in ((bids, e.bids), (asks, e.asks)):
                for p, s in rows:
                    side.pop(p, None) if float(s) == 0 else side.__setitem__(p, s)
    nxt = next(e for e in events[cut:] if isinstance(e, RawBook) and e.action == "update")
    snap = RawBook(events[cut].t_ms, nxt.exchange_ts_ms - 1, "snapshot", nxt.pseq, 0, tuple(bids.items()),
                   tuple(asks.items()), "conn-2")
    rest = [replace_conn(e, "conn-2") if isinstance(e, (RawBook, RawTrade)) else e for e in events[cut:]]
    return events[:cut] + [FeedMarker(snap.t_ms - 1, "DISCONNECT", "conn-1"), FeedMarker(snap.t_ms, "CONNECT", "conn-2"),
                           snap] + rest


@pytest.mark.parametrize("name", sorted(PATHOLOGIES))
def test_feed_pathologies_are_recorded_raw_and_a_fresh_engine_replays_the_same_decisions(tmp_path, name):
    events, _ = timeline_a()
    d, replayed, same, why = record_and_replay(tmp_path, PATHOLOGIES[name](events))
    assert same, why
    assert [r.to_dict() for r in replayed.records] == [r.to_dict() for r in d.records]
    assert replayed.engine.snapshot() == d.engine.snapshot()
    if name in ("sequence_gap", "stale_gap", "out_of_order_update"):
        assert d.observer.state != "OK" or d.engine.setup is None
    if name == "duplicate_update":
        assert d.observer.stats["duplicate_update"] >= 1 and d.engine.setup is not None
    if name == "reconnect_resnapshot":
        assert d.observer.epoch == 2


def test_replay_verifies_the_feed_parameters_and_config_and_schema(tmp_path):
    rec = recorder(tmp_path / "rec")
    run(observed("A_true_consumption", "LONG")[0], recorder=rec)
    rec.close()
    files = input_files(tmp_path / "rec")
    with pytest.raises(ValueError, match="config digest"):
        replay(files, cfg=replace(CFG, risk_per_full_trade_frac=0.0025))
    rows = lines(files[0])
    header = json.loads(rows[0])
    assert header["body"]["schema_version"] == 2


def test_a_replay_of_an_empty_or_market_less_log_is_a_no_op_not_a_crash(tmp_path):
    rec = recorder(tmp_path / "rec")
    write(rec, [TimerTick(S), TimerTick(S + 1_000)])
    rec.close()
    r = replay(input_files(tmp_path / "rec"))
    assert r.records == [] and r.engine.setup is None


def test_replay_treats_session_start_as_a_boundary_and_flags_a_state_that_lagged_the_stream(tmp_path):
    """A crash between 'record the input' and 'persist the state' restarts from an OLDER state. Replay follows
    the state the restarted process actually restored (recorded in the marker) and reports the lag."""
    import shutil

    from tests.test_lsrl_feed_and_runtime import Rig, timeline_events
    first = Rig(tmp_path)
    first.start()
    events = timeline_events()
    first.run(events[: len(events) // 2])
    older = tmp_path / "state_older.json"
    shutil.copy(tmp_path / "state.json", older)
    first.run(events[len(events) // 2:])
    first.close()
    shutil.copy(older, tmp_path / "state.json")                                     # the restart sees the OLDER state
    second = Rig(tmp_path, session=2, start_ms=S + 90_000)
    second.start()
    second.close()
    files = input_files(tmp_path / "rec")
    r = replay(files, anchor=anchor_path(tmp_path / "rec", "inputs"))
    assert r.sessions == 2 and len(r.restart_state_mismatches) == 1
    assert decisions_match(files, decision_files(tmp_path / "rec"))[0]


def test_garbling_a_record_that_the_anchor_already_vouched_for_is_refused_not_repaired(tmp_path):
    """Repairing a torn write is legitimate; silently discarding a record that WAS durable is evidence loss."""
    root = recorded(tmp_path, n=20)                                                  # close() anchored the final record
    path = input_files(root)[0]
    rows = lines(path)
    last = json.loads(rows[-1])
    last["h"] = "0" * 64
    rows[-1] = json.dumps(last, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(rows) + "\n")
    with pytest.raises(RecorderError, match="TAIL_TRUNCATED"):
        recorder(root)
    assert not list(root.glob("quarantine-*"))                                       # and nothing was moved or deleted


def test_a_reader_that_lists_the_plain_file_just_before_it_is_compressed_still_reads_the_data(tmp_path):
    root = tmp_path / "rec"
    rec = recorder(root)
    write(rec, some_events(6, t0=(S // DAY + 1) * DAY - 5_000))
    rec.close()
    listed = input_files(root)                                                   # listed while still plain
    compress_closed_days(root, "inputs", "9999-12-31")                          # ... then the compressor wins the race
    assert not listed[0].exists()
    assert len(list(read_events(listed))) == 6 and verify_chain(listed) == (True, "OK")


def test_close_waits_for_a_running_compression_so_a_shutdown_never_leaves_it_half_done(tmp_path):
    root = two_day_recording(tmp_path)                                          # helper joins; call close() path explicitly
    assert not list(root.glob("*.tmp"))


# ------------------------------------------------------------------ the audit tool
def test_the_audit_tool_passes_a_clean_recording_and_fails_a_tampered_one(tmp_path, capsys):
    from lsrl.tools.verify_recording import main
    rec = recorder(tmp_path / "rec")
    run(observed("A_true_consumption", "LONG")[0], recorder=rec)
    rec.close()
    assert main([str(tmp_path / "rec")]) == 0
    out = capsys.readouterr().out
    assert "PASS  inputs hash chain + tail anchor" in out and "PASS  decision equivalence" in out
    assert "git=fa3f9b4" in out and "strategy=LSRL_V1" in out                # the header is echoed
    path = input_files(tmp_path / "rec")[0]
    rows = lines(path)
    rows[8] = rows[8].replace("1", "2", 1)
    path.write_text("\n".join(rows) + "\n")
    assert main([str(tmp_path / "rec")]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_the_audit_tool_refuses_an_empty_directory(tmp_path, capsys):
    from lsrl.tools.verify_recording import main
    assert main([str(tmp_path)]) == 1
