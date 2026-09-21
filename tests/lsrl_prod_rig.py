"""VENDORED from Agent C's acceptance evidence (research_lsrl_v1_final_acceptance_2026-09-21/scripts/prod_rig.py,
HEAD 6e0062d), unchanged except this note and the removal of the `sys` path hack. It is the independent auditor's
production-path rig; the permanent regression below drives it.

Agent C production-path rig for LSRL_V1 (HEAD 6e0062d).

Drives the REAL production chain end to end with scripted, deterministic time:

  Bitget-format JSON text frames -> BitgetWsFeed.on_message (real parse_frame, real conn_id handling)
  -> EventBus (real stamping) -> LSRLRuntime.step (real drain, real supervision, real ticks)
  -> SimDriver (real) -> LSRLRecorder (real, hash chained) -> FeedPipeline -> BookObserver -> LSRLEngine -> SimExchange

Only the network socket and the candle REST source are faked. Ground truth is defined by the caller (tests'
Truth/GroundBook helpers are used ONLY to translate ground truth into frames); the outcome is judged against
the frozen spec, not against any expectation written by Agent A.

Reconnects requested by the runtime (observer UNSAFE -> ws.request_reconnect) are honoured: the rig closes the
fake socket, opens a new one and delivers a fresh snapshot of the CURRENT ground-truth book (via `truth_at`).
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from execution.state_store import JsonStateStore
from lsrl.adapters.sim import SimExchange
from lsrl.core.config import DEFAULT_CONFIG as CFG
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import BarClosed, FeedMarker, PlaceEntry, RawBook, RawTrade
from lsrl.driver import SimDriver
from lsrl.io.gate_log import RecordingRiskGate
from lsrl.io.recorder import LSRLRecorder, decision_files, input_files, verify_chain, anchor_path
from lsrl.io.replay import decisions_match, replay
from lsrl.modes import Mode
from lsrl.obs.observer import BookObserver, FeedConfig
from lsrl.runtime import LSRLRuntime
from lsrl.ws import BitgetWsFeed, EventBus
from tests.lsrl_fixtures import RULES, warmup

BOOK_ARG = {"instType": "USDT-FUTURES", "channel": "books", "instId": "SOLUSDT"}
TRADE_ARG = {"instType": "USDT-FUTURES", "channel": "trade", "instId": "SOLUSDT"}


class Clock:
    def __init__(self, t=0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSocket:
    def __init__(self, n):
        self.n, self.closed, self.sent = n, False, []

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        self.closed = True


class ScriptedCandles:
    """History for warm-up + live bars that become available at a scripted time (as REST would return them)."""

    def __init__(self, bus, history, live):
        self.bus, self.history, self.live = bus, history, sorted(live, key=lambda b: b.t_ms)
        self.delivered_live: set = set()
        self._warm: set = set()

    def seed(self, **kw):
        return None

    def bars(self, tf_s, limit):
        if tf_s not in self._warm and self.history is not None:
            self._warm.add(tf_s)
            return [b for b in self.history if b.tf_s == tf_s][-limit:]
        now = self.bus.now()
        out = []
        for b in self.live:
            if b.tf_s == tf_s and b.t_ms <= now and (b.tf_s, b.open_ts_ms) not in self.delivered_live:
                self.delivered_live.add((b.tf_s, b.open_ts_ms))
                out.append(b)
        return out


# ------------------------------------------------------------------------------------------- JSON frames
def book_json(rb: RawBook) -> str:
    item = {"asks": [[p, s] for p, s in rb.asks], "bids": [[p, s] for p, s in rb.bids],
            "ts": str(rb.exchange_ts_ms), "seq": rb.seq, "pseq": rb.pseq}
    return json.dumps({"action": rb.action, "arg": BOOK_ARG, "data": [item], "ts": rb.t_ms})


def trade_json(rts: list[RawTrade]) -> str:
    data = [{"ts": str(x.exchange_ts_ms), "price": x.price, "size": x.size, "side": x.side, "tradeId": x.trade_id}
            for x in rts]
    return json.dumps({"action": rts[0].action, "arg": TRADE_ARG, "data": data, "ts": rts[0].t_ms})


class ProdRig:
    def __init__(self, tmp: Path, *, mode: Mode = Mode.PAPER, feed_cfg: FeedConfig | None = None,
                 truth_at: Callable[[int], tuple[dict, dict]] | None = None, session: int = 1,
                 start_ms: int = 0, git_sha: str = "agentc-test", bars_interval_s: float = 5.0,
                 min_free_gb: float = 0.001, live_bars: list | None = None, history: list | None = None,
                 anchor_every: int = 50, recorder_root: Path | None = None, seq_base: int = 500_000_000_000):
        self.tmp, self.mode = Path(tmp), mode
        self.feed_cfg = feed_cfg or FeedConfig()
        self.truth_at, self.seq_base = truth_at, seq_base
        self.clock, self.mono = Clock(start_ms), Clock(0.0)
        self.bus = EventBus(self.clock)
        self.ws = BitgetWsFeed(self.bus, self.feed_cfg, ping_interval_s=25.0)
        self.sockets: list[FakeSocket] = []
        self.sock: FakeSocket | None = None
        self.recorder_root = recorder_root or (self.tmp / "rec")
        self.recorder = LSRLRecorder(self.recorder_root, CFG, RULES, mode=mode.value, feed=asdict(self.feed_cfg),
                                     transport=self.ws.parameters(), git_sha=git_sha, session_id=f"s{session}",
                                     min_free_gb=min_free_gb, anchor_every=anchor_every)
        holder: dict = {}
        self.engine = LSRLEngine(CFG, RULES, risk_gate=RecordingRiskGate(NullRiskGate(), lambda *a: holder["d"].gate_sink(*a)),
                                 tape_available=True)
        self.observer = BookObserver(self.feed_cfg)
        self.driver = SimDriver(mode, self.engine, SimExchange(RULES, CFG), start_nav_usdt=1_000.0,
                                recorder=self.recorder, observer=self.observer)
        holder["d"] = self.driver
        self.candles = ScriptedCandles(self.bus, history if history is not None else
                                       [e for e in warmup(nav=None) if isinstance(e, BarClosed)], live_bars or [])
        self.rt = LSRLRuntime(mode=mode, engine=self.engine, driver=self.driver, bus=self.bus, ws=self.ws,
                              candles=self.candles, state_store=JsonStateStore(str(self.tmp / "state.json")),
                              git_sha=git_sha, session_id=f"s{session}", bars_interval_s=bars_interval_s,
                              clock=self.mono)
        self.next_step_ms: int | None = None
        self.seq_log: list = []
        self.skip_book_until_ts = -1
        self.reconnects = 0
        self.log: list[str] = []

    # ---------------------------------------------------------------- lifecycle
    def start(self):
        self.rt.restore()
        self.rt.begin_session()
        self.rt._warmup()

    def connect(self, initial: bool = True):
        self.sock = FakeSocket(len(self.sockets) + 1)
        self.sockets.append(self.sock)
        self.ws.on_open(self.sock)
        if not initial:
            self._snapshot_now()

    def _snapshot_now(self):
        """A fresh connection is answered with a full snapshot of the CURRENT true book. Its seq continues the
        scripted chain (the seq of the last scripted update at or before now), so later scripted updates chain on it;
        scripted updates stamped at or before the snapshot belong to the old connection and are skipped."""
        if self.truth_at is None:
            return
        bids, asks = self.truth_at(self.clock.t)
        self.clock.t += 60
        snap_ts = self.clock.t - 40
        seq = self.seq_base
        for ts, sq in self.seq_log:
            if ts <= snap_ts:
                seq = sq
        snap = RawBook(t_ms=self.clock.t, exchange_ts_ms=snap_ts, action="snapshot", seq=seq, pseq=0,
                       bids=tuple((f"{p:.3f}".rstrip("0").rstrip("."), f"{s:g}") for p, s in sorted(bids.items(), reverse=True)),
                       asks=tuple((f"{p:.3f}".rstrip("0").rstrip("."), f"{s:g}") for p, s in sorted(asks.items())))
        self.skip_book_until_ts = snap_ts
        self.deliver_text(book_json(snap))

    def deliver_text(self, text: str):
        assert self.sock is not None
        self.ws.on_message(self.sock, text)

    def step(self):
        self.mono.t += 1.0
        self.rt.step()
        if self.sock is not None and self.sock.closed:
            self._reconnect()

    def _reconnect(self):
        """The runtime asked the transport for a fresh connection: close, reopen, deliver a snapshot of the truth."""
        old = self.sock
        self.reconnects += 1
        self.ws.on_close(old, 1000, "requested")
        self.connect(initial=False)

    # ---------------------------------------------------------------- run a scripted arrival list
    def run(self, events: list, *, tail_ms: int = 20_000, close: bool = True, resume_at_ms: int | None = None,
            seq_events: list | None = None):
        """`events` = raw events in ARRIVAL order (RawBook/RawTrade/FeedMarker/BarClosed) as produced by to_raw.
        close=False leaves the recorder open (a process kill). resume_at_ms: this is a RESTARTED process: it
        connects fresh (snapshot of the current truth) instead of waiting for a scripted CONNECT."""
        live_bars = [e for e in events if isinstance(e, BarClosed)]
        self.candles.live = sorted(live_bars, key=lambda b: b.t_ms)
        self.seq_log = sorted((e.exchange_ts_ms, e.seq) for e in (seq_events or events)
                              if isinstance(e, RawBook) and e.exchange_ts_ms is not None and e.seq is not None)
        first_connect_done = False
        if resume_at_ms is not None:
            self.clock.t = resume_at_ms
        self.start()
        if resume_at_ms is not None:
            self.connect(initial=False)
            first_connect_done = True
        pending_trades: list[RawTrade] = []

        def flush_trades():
            nonlocal pending_trades
            if pending_trades:
                self.deliver_text(trade_json(pending_trades))
                pending_trades = []

        end_t = events[-1].t_ms + tail_ms
        for ev in events:
            if isinstance(ev, BarClosed):
                continue
            if self.next_step_ms is None:
                self.next_step_ms = (ev.t_ms // 1000) * 1000 + 1000
            while self.next_step_ms <= ev.t_ms:
                flush_trades()
                self.clock.t = max(self.clock.t, self.next_step_ms)
                self.step()
                self.next_step_ms += 1000
            self.clock.t = max(self.clock.t, ev.t_ms)
            if isinstance(ev, FeedMarker):
                flush_trades()
                if ev.kind == "CONNECT":
                    self.connect(initial=not first_connect_done)
                    first_connect_done = True
                elif ev.kind == "DISCONNECT" and self.sock is not None:
                    self.ws.on_close(self.sock, 1006, "network")
                continue
            if isinstance(ev, RawBook):
                flush_trades()
                if ev.action == "update" and ev.exchange_ts_ms is not None and ev.exchange_ts_ms <= self.skip_book_until_ts:
                    continue                                   # belongs to the connection that was replaced
                self.deliver_text(book_json(ev))
            elif isinstance(ev, RawTrade):
                if pending_trades and (pending_trades[0].t_ms != ev.t_ms or pending_trades[0].action != ev.action):
                    flush_trades()
                pending_trades.append(ev)
        flush_trades()
        while self.next_step_ms is not None and self.next_step_ms <= end_t:
            self.clock.t = max(self.clock.t, self.next_step_ms)
            self.step()
            self.next_step_ms += 1000
        if close:
            self.recorder.close()
        return self

    # ---------------------------------------------------------------- results
    def reasons(self) -> list[str]:
        return [r.data.get("reason") for r in self.driver.records if r.kind == "setup_rejected"]

    def health(self) -> list[tuple]:
        return [(r.data.get("ok"), r.data.get("reason")) for r in self.driver.records if r.kind == "feed_health"]

    def anomalies(self) -> list[str]:
        return [r.data.get("reason") for r in self.driver.records if r.kind == "anomaly"]

    def ladders(self) -> int:
        return len([i for i in self.driver.intents if isinstance(i, PlaceEntry)])

    def sweep_confirmed(self) -> bool:
        return any(r.data.get("event") == "SWEEP_CONFIRMED" for r in self.driver.records if r.kind == "lifecycle")

    def consumption(self):
        for r in self.driver.records:
            if r.kind == "lifecycle" and r.data.get("event") == "SWEEP_CONFIRMED":
                return r.data.get("consumption")
        for r in self.driver.records:
            if r.kind == "setup_rejected" and r.data.get("consumption"):
                return r.data.get("consumption")
        return None

    def verify(self) -> dict:
        files_in, files_out = input_files(self.recorder_root), decision_files(self.recorder_root)
        ok_in, why_in = verify_chain(files_in, anchor=anchor_path(self.recorder_root, "inputs"))
        ok_out, why_out = verify_chain(files_out, anchor=anchor_path(self.recorder_root, "decisions"))
        eq, why_eq = decisions_match(files_in, files_out)
        return {"inputs_chain": why_in if not ok_in else "OK", "decisions_chain": why_out if not ok_out else "OK",
                "genesis_replay": "EQUIVALENT" if eq else why_eq[:160]}
