"""LiveDriver: engine + the MICRO_LIVE executor (+ recorder). Mirrors SimDriver's structure.

Project-coupled only through the executor it is handed; unarmed unless the executor could be built.
"""
from __future__ import annotations

from typing import Any, Callable

from lsrl.core.engine import LSRLEngine
from lsrl.core.types import BarClosed, NavEvent, Record
from lsrl.io.recorder import LSRLRecorder, RecorderError
from lsrl.obs.observer import BookObserver
from lsrl.pipeline import RESTORE, FeedPipeline


class LiveDriver:
    def __init__(self, engine: LSRLEngine, executor: Any, *, nav_fn: Callable[[], float],
                 recorder: LSRLRecorder | None = None, observer: BookObserver | None = None,
                 history_limit: int | None = None) -> None:
        self.engine, self.executor, self.nav_fn, self.recorder = engine, executor, nav_fn, recorder
        self.observer = observer or BookObserver()
        self.pipeline = FeedPipeline(engine, self.observer, session_mode=RESTORE)
        self.history_limit = history_limit          # a long-running runtime keeps only a tail; the recorder has it all
        self.records: list[Record] = []
        self.intents: list[Any] = []
        self.recorder_error: str | None = None
        self.nav_error: str | None = None

    def _guard(self, fn, *args) -> None:
        if self.recorder is None or self.recorder_error:
            return
        try:
            fn(*args)
        except RecorderError as exc:
            self.recorder_error = str(exc)
            self.engine.halt(str(exc))

    def gate_sink(self, t_ms: int, setup_id: str, decision: dict) -> None:
        if self.recorder is not None:
            self._guard(self.recorder.record_gate, t_ms, setup_id, decision)

    def _process(self, ev: Any) -> None:
        if self.recorder is not None:
            self._guard(self.recorder.record_event, ev)
        intents, records = self.pipeline.process(ev)
        if self.recorder is not None:
            self._guard(self.recorder.record_outputs, ev.t_ms, intents, records)
        self.records.extend(records)
        self.intents.extend(intents)
        if self.history_limit:
            del self.records[:-self.history_limit]
            del self.intents[:-self.history_limit]
        for out in self.executor.apply(intents, ev.t_ms):
            self._process(out)
        self._tell_the_executor_which_setups_ended(records, ev.t_ms)
        if self.executor.halt_reason:
            self.engine.halt(f"EXECUTOR:{self.executor.halt_reason}")

    def _tell_the_executor_which_setups_ended(self, records: list[Record], t_ms: int) -> None:
        """The engine ends a setup without any stop intent when it never had a position (an entry was rejected, the ladder
        expired). An entry of that setup the executor holds but the engine no longer knows would rest for ever and its fill
        would be unmanaged: the executor must hear about it."""
        finished = getattr(self.executor, "engine_finished", None)
        if finished is None:
            return
        for record in records:
            if record.kind in ("setup_rejected", "setup_closed"):
                setup_id = (record.data or {}).get("setup_id")
                if setup_id:
                    finished(str(setup_id), t_ms)

    def feed(self, ev: Any, *, nav: bool = True) -> None:
        if nav and isinstance(ev, BarClosed) and ev.tf_s == self.engine.cfg.trigger_tf_s:
            try:
                self._process(NavEvent(t_ms=ev.t_ms, nav_usdt=float(self.nav_fn()), source="AUTHENTICATED_EQUITY"))
                self.nav_error = None
            except Exception as exc:             # NAV goes stale -> NAV_UNAVAILABLE: fail closed
                self.nav_error = f"{type(exc).__name__}:{exc}"
        self._process(ev)

    def state_snapshot(self) -> dict:
        return {}                                   # the executor persists its own order/position state

    def state_restore(self, state: dict) -> None:
        return None

    def poll_events(self, t_ms: int) -> list[Any]:
        """Executor reconciliation events, NOT yet processed. The runtime queues them on the same stamping
        bus as every other input so the engine sees one monotonic timeline."""
        events = list(self.executor.poll(t_ms))
        if self.executor.halt_reason:
            self.engine.halt(f"EXECUTOR:{self.executor.halt_reason}")
        return events

    def poll(self, t_ms: int) -> None:
        for out in self.poll_events(t_ms):
            self._process(out)
