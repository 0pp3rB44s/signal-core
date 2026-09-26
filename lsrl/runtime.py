"""LSRL_V1 runtime: WebSocket feed -> stamping bus -> driver (FeedPipeline) -> persistence, plus mode wiring.

Data path (sweep classification evidence comes ONLY from here): Bitget public WebSocket `books` (full depth,
seq/pseq chain) + `trade` (taker side tape) -> raw events on the `EventBus` -> recorder (raw) -> BookObserver
(causal reconstruction, fail-closed health) -> engine. REST supplies completed 1m/15m candles and startup
metadata only; there is no REST depth or REST trade path in this module.

Every input (WS frame, REST bar, executor reconciliation, timer tick) is stamped and queued through ONE bus,
so the engine sees a single non-decreasing timeline. A `SESSION_START` marker (carrying the restored engine
state) is the first event of every process: it is what lets replay treat a restart as a boundary.

Everything is OFF unless `LSRL_ENABLED=true` and `LSRL_MODE` is set (Settings validators). The runner only
constructs this when enabled, and `MICRO_LIVE` additionally needs the global LIVE gate, `LSRL_MICROLIVE_ARMED`,
and an `lsrl_v1` entry in the production allow-list that the launch ceremony currently REFUSES -- so this module
cannot reach a real order today.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable

from lsrl.contract import rules_from_contract_row, rules_from_contract_spec
from lsrl.core.config import DEFAULT_CONFIG, LSRLConfig
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import FeedMarker, TimerTick
from lsrl.modes import Mode
from lsrl.obs.observer import NO_BOOK, OK, UNSAFE, FeedConfig

WARMUP_BARS_1M = 120
WARMUP_BARS_15M = 60
NO_SNAPSHOT_RECONNECT_S = 20.0
HISTORY_LIMIT = 1_000            # in-memory tail of records/intents kept by the drivers of a long-running process


def resolve_git_sha(project_dir: str | Path, run: Callable[..., Any] = subprocess.run) -> str:
    """HEAD of the code that is running, `+dirty` if the tracked tree differs. `UNKNOWN` if it cannot be
    established (recorded as such: never silently blank)."""
    try:
        head = run(["git", "rev-parse", "HEAD"], cwd=str(project_dir), capture_output=True, text=True, timeout=5)
        sha = head.stdout.strip()
        if head.returncode != 0 or len(sha) < 7:
            return "UNKNOWN"
        dirty = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=str(project_dir),
                    capture_output=True, text=True, timeout=5)
        return sha + ("+dirty" if dirty.returncode == 0 and dirty.stdout.strip() else "")
    except Exception:
        return "UNKNOWN"


class LSRLRuntime:
    def __init__(self, *, mode: Mode, engine: LSRLEngine, driver: Any, bus: Any, ws: Any, candles: Any,
                 state_store: Any, git_sha: str = "UNKNOWN", session_id: str | None = None,
                 bars_interval_s: float = 5.0, poll_interval_s: float = 3.0, tick_interval_s: float = 1.0,
                 status_store: Any = None, clock: Callable[[], float] = time.monotonic,
                 log: logging.Logger | None = None) -> None:
        self.mode, self.engine, self.driver, self.bus, self.ws, self.candles = mode, engine, driver, bus, ws, candles
        self.state, self.status_store = state_store, status_store
        self.git_sha, self.session_id = git_sha, session_id or uuid.uuid4().hex
        self.bars_interval_s, self.poll_interval_s, self.tick_interval_s = bars_interval_s, poll_interval_s, tick_interval_s
        self.clock = clock
        self.log = log or logging.getLogger("lsrl_runtime")
        self._last_bars = self._last_poll = self._last_tick = self._last_status = -1e18
        self._unhealthy_since: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    # ------------------------------------------------------------------ lifecycle
    def restore(self) -> None:
        saved = self.state.load({}) or {}
        if saved.get("engine"):
            if saved.get("mode") != self.mode.value:
                raise RuntimeError(f"LSRL state was written by mode {saved.get('mode')}, not {self.mode.value}")
            self.engine.restore(saved["engine"])          # ValueError on a config mismatch: refuse to start
            self.driver.state_restore(saved.get("driver") or self.driver.state_snapshot())
        self.candles.seed(last_open_1m=self.engine.last_bar1m_open_ts, last_open_15m=self.engine.last_bar15_open_ts)
        self.bus.floor(self.engine.now_ms)               # a backwards wall clock must not make new events look old

    def begin_session(self) -> None:
        """Process start = replay boundary: the marker carries the state this process restored."""
        detail = json.dumps({"session_id": self.session_id, "git_sha": self.git_sha, "mode": self.mode.value,
                             "engine_state": self.engine.snapshot()}, sort_keys=True, separators=(",", ":"))
        self.bus.push(lambda t: FeedMarker(t, "SESSION_START", "", detail))
        self._drain(nav=False)

    def start(self) -> None:
        self.restore()
        self.begin_session()
        self._warmup()
        self.ws.start()
        self._thread = threading.Thread(target=self._loop, name="lsrl-runtime", daemon=True)
        self._thread.start()
        self.log.warning("LSRL_RUNTIME_STARTED | mode=%s | symbol=%s | git=%s | session=%s | orders_possible=%s",
                         self.mode.value, self.engine.cfg.symbol, self.git_sha, self.session_id,
                         self.mode is Mode.MICRO_LIVE)

    def stop(self) -> None:
        self._stop.set()
        try:
            self.ws.stop()
        except Exception:
            self.log.exception("LSRL_WS_STOP_FAILED")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:                        # never let the loop die; new setups halt
                self.last_error = f"{type(exc).__name__}:{exc}"
                self.log.exception("LSRL_RUNTIME_STEP_FAILED | %s", self.last_error)
                self.engine.halt(f"RUNTIME_ERROR:{self.last_error}"[:200])
            self._stop.wait(0.25)

    # ------------------------------------------------------------------ inputs
    def _safe(self, fn, what: str):
        try:
            return fn()
        except Exception as exc:                            # a transient data failure is handled by staleness rules
            self.last_error = f"{what}:{type(exc).__name__}:{exc}"
            self.log.warning("LSRL_FEED_ERROR | %s", self.last_error)
            return None

    def _queue_bars(self, tf_s: int, limit: int, what: str) -> None:
        for b in self._safe(lambda: self.candles.bars(tf_s, limit), what) or []:
            self.bus.push(lambda t, b=b: replace(b, t_ms=t))

    def _warmup(self) -> None:
        """History only warms the ATRs: it is processed without NAV fetches and cannot trigger decisions."""
        self._queue_bars(900, WARMUP_BARS_15M, "warmup_15m")
        self._queue_bars(60, WARMUP_BARS_1M, "warmup_1m")
        self._drain(nav=False)

    def _drain(self, *, nav: bool = True) -> None:
        for ev in self.bus.drain():
            self.driver.feed(ev, nav=nav)

    # ------------------------------------------------------------------ one iteration (testable)
    def step(self) -> None:
        now = self.clock()
        if now - self._last_bars >= self.bars_interval_s:
            self._last_bars = now
            self._queue_bars(900, 3, "bars_15m")
            self._queue_bars(60, 5, "bars_1m")
        if self.mode is Mode.MICRO_LIVE and now - self._last_poll >= self.poll_interval_s:
            self._last_poll = now
            stamp = self.bus.now()
            for out in self._safe(lambda: self.driver.poll_events(stamp), "executor_poll") or []:
                self.bus.push(lambda t, e=out: replace(e, t_ms=t))
        if now - self._last_tick >= self.tick_interval_s:
            self._last_tick = now
            self.bus.push(lambda t: TimerTick(t))
        self._drain()
        self._supervise_feed(now)
        self._persist()
        if self.status_store is not None and now - self._last_status >= 10.0:
            self._last_status = now
            self._safe(lambda: self.status_store.save(self.status()), "status")

    def _supervise_feed(self, now: float) -> None:
        """The observer decides the view is unsafe; the transport's job is to get a fresh snapshot."""
        observer = self.driver.observer
        if observer.state == OK:
            self._unhealthy_since = None
            return
        if self._unhealthy_since is None:
            self._unhealthy_since = now
        if not self.ws.connected:
            return
        if observer.state == UNSAFE:
            self.ws.request_reconnect(observer.unsafe_reason)
        elif observer.state == NO_BOOK and now - self._unhealthy_since > NO_SNAPSHOT_RECONNECT_S:
            self.ws.request_reconnect("NO_SNAPSHOT_AFTER_CONNECT")
            self._unhealthy_since = now

    def _persist(self) -> None:
        self.state.save({"mode": self.mode.value, "engine": self.engine.snapshot(),
                         "driver": self.driver.state_snapshot()})

    def status(self) -> dict[str, Any]:
        obs = self.driver.observer
        return {"mode": self.mode.value, "git_sha": self.git_sha, "session_id": self.session_id,
                "signal_allowed": self.engine.signal_allowed,
                "LSRL_SIGNAL_ALLOWED": "YES" if self.engine.signal_allowed else "NO",
                "engine_halted": self.engine.halted_reason,
                "recorder_error": getattr(self.driver, "recorder_error", None), "last_error": self.last_error,
                "feed": {"state": obs.state, "epoch": obs.epoch, "unsafe_reason": obs.unsafe_reason,
                         "stats": dict(obs.stats), "window": obs.window_width()},
                "ws": self.ws.status(), "queue": len(self.bus)}


# ---------------------------------------------------------------------- mode wiring
def load_rules(client: Any, symbol: str, product_type: str):
    """Exchange constraints, never test values. The repository's `ContractSpec` does not carry `priceEndStep`
    (tick = priceEndStep x 10**-pricePlace), so when the client can return the raw contract row it is parsed
    with full fidelity AND must agree with the spec; any disagreement or missing row refuses to run."""
    rules = rules_from_contract_spec(client.get_contract_spec(symbol), symbol)
    get_contracts = getattr(client, "get_contracts", None)
    if get_contracts is None:
        return rules
    payload = get_contracts(product_type, symbol=symbol)
    row = next((c for c in (payload.get("data") or []) if str(c.get("symbol", "")).upper() == symbol.upper()), None)
    if row is None:
        raise RuntimeError(f"CONTRACT_METADATA_UNAVAILABLE: no raw contract row for {symbol}")
    exact = rules_from_contract_row(row, symbol)
    if exact != rules:
        raise RuntimeError(f"CONTRACT_METADATA_MISMATCH: spec={rules} raw_row={exact}")
    return exact


def build_runtime(settings: Any, *, client: Any, execution_service: Any, risk_manager: Any,
                  cfg: LSRLConfig = DEFAULT_CONFIG, ws_factory: Callable[..., Any] | None = None,
                  project_dir: str | Path | None = None) -> LSRLRuntime:
    """Assemble the runtime for `settings.lsrl_mode`. Raises rather than degrade: an unknown
    contract spec, a missing execution service (MICRO_LIVE), or an unarmed executor refuses to start."""
    from execution.state_store import JsonStateStore
    from lsrl.adapters.riskgate import CentralRiskGate
    from lsrl.adapters.sim import SimExchange
    from lsrl.driver import SimDriver
    from lsrl.feed import CandleFeed
    from lsrl.io.gate_log import RecordingRiskGate
    from lsrl.io.recorder import LSRLRecorder
    from lsrl.obs.observer import BookObserver
    from lsrl.ws import BitgetWsFeed, EventBus

    mode = Mode(settings.lsrl_mode.strip().upper())
    if mode is Mode.BACKTEST:
        raise ValueError("BACKTEST runs from recorded inputs (lsrl.backtest), not from the live runtime")
    symbol = settings.lsrl_symbol.strip().upper()
    rules = load_rules(client, symbol, str(settings.bitget_product_type))
    feed_cfg = FeedConfig(symbol=symbol, inst_type=str(settings.bitget_product_type))
    root = Path(settings.lsrl_data_dir) / mode.value.lower()
    git_sha = resolve_git_sha(project_dir or Path(__file__).resolve().parents[1])
    session_id = uuid.uuid4().hex
    bus = EventBus()
    ws = BitgetWsFeed(bus, feed_cfg, ws_factory=ws_factory)
    recorder = LSRLRecorder(root / "recorded", cfg, rules, mode=mode.value, feed=asdict(feed_cfg),
                            transport=ws.parameters(), git_sha=git_sha, session_id=session_id,
                            min_free_gb=float(settings.lsrl_min_free_gb))
    state = JsonStateStore(str(root / "state.json"))
    gate = CentralRiskGate(risk_manager, settings) if risk_manager is not None else NullRiskGate()
    holder: dict[str, Any] = {}
    recording_gate = RecordingRiskGate(gate, lambda *a: holder["driver"].gate_sink(*a))
    engine = LSRLEngine(cfg, rules, risk_gate=recording_gate, tape_available=True)
    observer = BookObserver(feed_cfg)
    candles = CandleFeed(client, symbol, str(settings.bitget_product_type), now_ms=bus.now)
    if mode in (Mode.SHADOW, Mode.PAPER):
        driver = SimDriver(mode, engine, SimExchange(rules, cfg), start_nav_usdt=float(settings.lsrl_shadow_nav_usdt),
                           recorder=recorder, observer=observer, history_limit=HISTORY_LIMIT)
    else:
        if execution_service is None:
            raise RuntimeError("MICRO_LIVE requires the execution service (not forward-paper-only)")
        from execution.entry_submitter import EntryOrderSubmitter
        from execution.order_intent_store import OrderIntentStore
        from lsrl.adapters.live import LSRLLiveExecutor, assert_armed
        from lsrl.adapters.live_driver import LiveDriver
        from lsrl.adapters.process_down import load_from_settings
        from risk.mandatory_authorization import RiskAuthorizationStore
        submitter = EntryOrderSubmitter(
            client=client, intent_store=OrderIntentStore(str(root / "order_intents.json")),
            session_id=f"lsrl-{int(time.time())}", execution_mode="LIVE")
        # Refuses (AttestationRefused) unless the process-down attestation is present, pinned, unexpired and bound to THIS
        # clean release SHA: a MICRO_LIVE executor that could rest an entry without an exchange-side stop cannot exist.
        assert_armed(settings)                              # unarmed stays a LiveNotArmed refusal, before anything else
        process_down = load_from_settings(settings, code_sha=git_sha)
        executor = LSRLLiveExecutor(settings=settings, client=client, submitter=submitter,
                                    auth_store=RiskAuthorizationStore(), gate=gate,
                                    state_store=JsonStateStore(str(root / "executor_state.json")), rules=rules, cfg=cfg,
                                    process_down=process_down)

        def authenticated_equity() -> float:
            for row in (client.get_accounts().get("data") or []):
                if str(row.get("marginCoin") or "").upper() == "USDT":
                    value = float(row.get("accountEquity") or row.get("usdtEquity") or row.get("equity") or 0)
                    if value > 0:
                        return value
            raise RuntimeError("authenticated USDT account equity unavailable")

        driver = LiveDriver(engine, executor, nav_fn=authenticated_equity, recorder=recorder, observer=observer,
                            history_limit=HISTORY_LIMIT)
    holder["driver"] = driver
    return LSRLRuntime(mode=mode, engine=engine, driver=driver, bus=bus, ws=ws, candles=candles, state_store=state,
                       git_sha=git_sha, session_id=session_id, status_store=JsonStateStore(str(root / "status.json")))
