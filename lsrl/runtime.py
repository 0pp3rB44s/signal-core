"""LSRL_V1 runtime: feed -> driver (SimDriver | LiveDriver) -> persistence, plus mode wiring.

Everything is OFF unless `LSRL_ENABLED=true` and `LSRL_MODE` is set (Settings validators). The
runner only constructs this when enabled, and `MICRO_LIVE` additionally needs the global LIVE
gate, `LSRL_MICROLIVE_ARMED`, and an `lsrl_v1` entry in the production allow-list that the launch
ceremony currently REFUSES -- so this module cannot reach a real order today.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable

from lsrl.core.config import DEFAULT_CONFIG, LSRLConfig
from lsrl.core.engine import LSRLEngine
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import BarClosed, SymbolRules, TimerTick
from lsrl.modes import Mode

WARMUP_BARS_1M = 120
WARMUP_BARS_15M = 60


def rules_from_contract_spec(spec: Any, symbol: str) -> SymbolRules:
    """Exchange constraints -> SymbolRules. Unknown metadata = refuse to run (fail closed)."""
    if spec is None:
        raise RuntimeError("CONTRACT_METADATA_UNAVAILABLE: refusing to run LSRL without exchange constraints")
    tick = 10.0 ** (-int(spec.price_place))                      # same convention as the rest of the code base
    min_notional = float(spec.min_trade_usdt) if spec.min_trade_usdt is not None else 5.0
    return SymbolRules(symbol=symbol.upper(), tick_size=tick, qty_step=float(spec.size_multiplier),
                       min_qty=float(spec.min_trade_num), min_notional_usdt=min_notional)


class LSRLRuntime:
    def __init__(self, *, mode: Mode, engine: LSRLEngine, driver: Any, feed: Any, state_store: Any,
                 depth_interval_s: float = 10.0, poll_interval_s: float = 3.0,
                 clock: Callable[[], float] = time.monotonic, log: logging.Logger | None = None) -> None:
        self.mode, self.engine, self.driver, self.feed, self.state = mode, engine, driver, feed, state_store
        self.depth_interval_s, self.poll_interval_s = depth_interval_s, poll_interval_s
        self.clock = clock
        self.log = log or logging.getLogger("lsrl_runtime")
        self._last_depth = self._last_poll = self._last_bars = -1e18
        self._warmed = False
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

    def start(self) -> None:
        self.restore()
        self._thread = threading.Thread(target=self._loop, name="lsrl-runtime", daemon=True)
        self._thread.start()
        self.log.warning("LSRL_RUNTIME_STARTED | mode=%s | symbol=%s | orders_possible=%s", self.mode.value,
                         self.engine.cfg.symbol, self.mode is Mode.MICRO_LIVE)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:                        # never let the loop die; new setups halt
                self.last_error = f"{type(exc).__name__}:{exc}"
                self.log.exception("LSRL_RUNTIME_STEP_FAILED | %s", self.last_error)
                self.engine.halt(f"RUNTIME_ERROR:{self.last_error}"[:200])
            self._stop.wait(1.0)

    # ------------------------------------------------------------------ one iteration (testable)
    def _safe(self, fn, what: str):
        try:
            return fn()
        except Exception as exc:                            # a transient data failure is handled by staleness rules
            self.last_error = f"{what}:{type(exc).__name__}:{exc}"
            self.log.warning("LSRL_FEED_ERROR | %s", self.last_error)
            return None

    def _warmup(self) -> None:
        bars = list(self._safe(lambda: self.feed.bars(900, WARMUP_BARS_15M), "warmup_15m") or [])
        bars += list(self._safe(lambda: self.feed.bars(60, WARMUP_BARS_1M), "warmup_1m") or [])
        for b in sorted(bars, key=lambda x: (x.close_ts_ms, x.tf_s)):
            self.driver.feed(b, nav=False)          # history only warms ATR: no NAV fetch, no decisions
        self._warmed = True

    def step(self) -> None:
        now = self.clock()
        if not self._warmed:
            self._warmup()
        if now - self._last_depth >= self.depth_interval_s:
            self._last_depth = now
            snap = self._safe(self.feed.depth, "depth")
            if snap is not None:
                self.driver.feed(snap)
        if now - self._last_bars >= 5.0:
            self._last_bars = now
            bars = list(self._safe(lambda: self.feed.bars(900, 3), "bars_15m") or [])
            bars += list(self._safe(lambda: self.feed.bars(60, 5), "bars_1m") or [])
            for b in sorted(bars, key=lambda x: (x.close_ts_ms, x.tf_s)):
                self.driver.feed(b)
        if self.mode is Mode.MICRO_LIVE and now - self._last_poll >= self.poll_interval_s:
            self._last_poll = now
            self.driver.poll(self.engine.now_ms)
        self.driver.feed(TimerTick(t_ms=max(self.engine.now_ms, self.feed.now())))
        self._persist()

    def _persist(self) -> None:
        self.state.save({"mode": self.mode.value, "engine": self.engine.snapshot()})


# ---------------------------------------------------------------------- mode wiring
def build_runtime(settings: Any, *, client: Any, execution_service: Any, risk_manager: Any,
                  cfg: LSRLConfig = DEFAULT_CONFIG) -> LSRLRuntime:
    """Assemble the runtime for `settings.lsrl_mode`. Raises rather than degrade: an unknown
    contract spec, a missing execution service (MICRO_LIVE), or an unarmed executor refuses to start."""
    from execution.state_store import JsonStateStore
    from lsrl.adapters.riskgate import CentralRiskGate
    from lsrl.adapters.sim import SimExchange
    from lsrl.driver import SimDriver
    from lsrl.feed import LiveFeed
    from lsrl.io.gate_log import RecordingRiskGate
    from lsrl.io.recorder import LSRLRecorder

    mode = Mode(settings.lsrl_mode.strip().upper())
    if mode is Mode.BACKTEST:
        raise ValueError("BACKTEST runs from recorded inputs (lsrl.backtest), not from the live runtime")
    symbol = settings.lsrl_symbol.strip().upper()
    rules = rules_from_contract_spec(client.get_contract_spec(symbol), symbol)
    root = Path(settings.lsrl_data_dir) / mode.value.lower()
    recorder = LSRLRecorder(root / "recorded", cfg, rules, mode=mode.value, tape_available=False,
                            min_free_gb=float(settings.lsrl_min_free_gb))
    state = JsonStateStore(str(root / "state.json"))
    gate = CentralRiskGate(risk_manager, settings) if risk_manager is not None else NullRiskGate()
    holder: dict[str, Any] = {}
    recording_gate = RecordingRiskGate(gate, lambda *a: holder["driver"].gate_sink(*a))
    engine = LSRLEngine(cfg, rules, risk_gate=recording_gate, tape_available=False)
    feed = LiveFeed(client, symbol, str(settings.bitget_product_type))
    if mode in (Mode.SHADOW, Mode.PAPER):
        driver = SimDriver(mode, engine, SimExchange(rules, cfg), start_nav_usdt=float(settings.lsrl_shadow_nav_usdt),
                           recorder=recorder)
    else:
        if execution_service is None:
            raise RuntimeError("MICRO_LIVE requires the execution service (not forward-paper-only)")
        from execution.entry_submitter import EntryOrderSubmitter
        from execution.order_intent_store import OrderIntentStore
        from lsrl.adapters.live import LSRLLiveExecutor
        from lsrl.adapters.live_driver import LiveDriver
        from risk.mandatory_authorization import RiskAuthorizationStore
        submitter = EntryOrderSubmitter(
            client=client, intent_store=OrderIntentStore(str(root / "order_intents.json")),
            session_id=f"lsrl-{int(time.time())}", execution_mode="LIVE")
        executor = LSRLLiveExecutor(settings=settings, client=client, submitter=submitter,
                                    auth_store=RiskAuthorizationStore(), gate=gate,
                                    state_store=JsonStateStore(str(root / "executor_state.json")), rules=rules, cfg=cfg)

        def authenticated_equity() -> float:
            for row in (client.get_accounts().get("data") or []):
                if str(row.get("marginCoin") or "").upper() == "USDT":
                    value = float(row.get("accountEquity") or row.get("usdtEquity") or row.get("equity") or 0)
                    if value > 0:
                        return value
            raise RuntimeError("authenticated USDT account equity unavailable")

        driver = LiveDriver(engine, executor, nav_fn=authenticated_equity, recorder=recorder)
    holder["driver"] = driver
    return LSRLRuntime(mode=mode, engine=engine, driver=driver, feed=feed, state_store=state,
                       depth_interval_s=float(settings.lsrl_depth_interval_s))
