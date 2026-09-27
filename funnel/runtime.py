"""Per-symbol PAPER/SHADOW runtime for SIGNAL_CORE_FUNNEL_V1, reusing LSRL_V1's runtime shell unmodified.

lsrl.runtime.LSRLRuntime is generic over its engine (it only calls engine.snapshot()/.restore()/.cfg.symbol/
.cfg.trigger_tf_s/.signal_allowed/.halted_reason - see lsrl/driver.py::SimDriver and lsrl/pipeline.py::
FeedPipeline, both already strategy-agnostic). This module supplies exactly what LSRL_V1's own
lsrl.runtime.build_runtime() supplies for PAPER/SHADOW, with FunnelEngine in place of LSRLEngine, and adds
build_funnel_fleet() to build one runtime PER SYMBOL sharing a single RiskManager instance, matching the
original funnel bot's cross-symbol MAX_OPEN_POSITIONS/cluster-exposure caps (see
tests/test_funnel_restoration_dry_run.py::test_two_symbols_share_one_risk_manager_...).

MICRO_LIVE is deliberately NOT wired here. Restoring a second strategy onto the current hardened engine is
Section 3 of the mission; arming it for real capital is explicitly out of scope ("Do NOT arm production
yet") and would need its own LiveAuthority/ceremony/capital-policy binding, admit_strategy() call, and
Agent C review - exactly the same governance LSRL_V1 already goes through, not a shortcut around it.
"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable

from lsrl.adapters.riskgate import CentralRiskGate
from lsrl.adapters.sim import SimExchange
from lsrl.core.gates import NullRiskGate
from lsrl.core.types import SymbolRules
from lsrl.driver import SimDriver
from lsrl.feed import CandleFeed
from lsrl.io.recorder import LSRLRecorder
from lsrl.modes import Mode
from lsrl.obs.observer import BookObserver, FeedConfig
from lsrl.runtime import HISTORY_LIMIT, LSRLRuntime, load_rules, resolve_git_sha
from lsrl.ws import BitgetWsFeed, EventBus
from execution.state_store import JsonStateStore

from funnel.core.config import DEFAULT_CONFIG, FunnelConfig, OWNER_APPROVED_PRODUCTION_SYMBOLS
from funnel.core.engine import FunnelEngine


def build_funnel_runtime(settings: Any, *, symbol: str, client: Any, risk_manager: Any, mode: Mode = Mode.PAPER,
                         cfg: FunnelConfig | None = None, ws_factory: Callable[..., Any] | None = None,
                         project_dir: str | Path | None = None) -> LSRLRuntime:
    """One symbol's PAPER/SHADOW runtime. `risk_manager` is the SAME instance passed to every other symbol
    in the fleet (see build_funnel_fleet) so exposure/cluster/max-open-positions checks see the whole book,
    not just this symbol's slice of it. `mode` is an explicit parameter (not read from settings.lsrl_mode,
    which is LSRL_V1's own on/off switch and semantically unrelated to this restoration)."""
    if mode not in (Mode.SHADOW, Mode.PAPER):
        raise ValueError(f"build_funnel_runtime only builds SHADOW/PAPER (got {mode}); "
                         "MICRO_LIVE is out of scope for this restoration")
    cfg = FunnelConfig(symbol=symbol) if cfg is None else cfg
    if cfg.symbol != symbol:
        raise ValueError(f"cfg.symbol {cfg.symbol} does not match requested symbol {symbol}")
    rules: SymbolRules = load_rules(client, symbol, str(settings.bitget_product_type))
    feed_cfg = FeedConfig(symbol=symbol, inst_type=str(settings.bitget_product_type))
    root = Path(getattr(settings, "lsrl_data_dir", "./funnel_data")) / "signal_core_funnel_v1" / mode.value.lower() / symbol
    git_sha = resolve_git_sha(project_dir or Path(__file__).resolve().parents[1])
    session_id = uuid.uuid4().hex
    bus = EventBus()
    ws = BitgetWsFeed(bus, feed_cfg, ws_factory=ws_factory)
    recorder = LSRLRecorder(root / "recorded", cfg, rules, mode=mode.value, feed=None, transport=ws.parameters(),
                            git_sha=git_sha, session_id=session_id,
                            min_free_gb=float(getattr(settings, "lsrl_min_free_gb", 5.0)))
    state = JsonStateStore(str(root / "state.json"))
    gate = CentralRiskGate(risk_manager, settings) if risk_manager is not None else NullRiskGate()
    engine = FunnelEngine(cfg, rules, risk_gate=gate, settings=settings)
    observer = BookObserver(feed_cfg)
    candles = CandleFeed(client, symbol, str(settings.bitget_product_type), now_ms=bus.now)
    driver = SimDriver(mode, engine, SimExchange(rules, cfg), start_nav_usdt=float(getattr(settings, "lsrl_shadow_nav_usdt", 1000.0)),
                       recorder=recorder, observer=observer, history_limit=HISTORY_LIMIT)
    return LSRLRuntime(mode=mode, engine=engine, driver=driver, bus=bus, ws=ws, candles=candles, state_store=state,
                       git_sha=git_sha, session_id=session_id, status_store=JsonStateStore(str(root / "status.json")))


def build_funnel_fleet(settings: Any, *, client: Any, risk_manager: Any, symbols: tuple[str, ...] | None = None,
                      mode: Mode = Mode.PAPER, ws_factory: Callable[..., Any] | None = None,
                      project_dir: str | Path | None = None) -> dict[str, LSRLRuntime]:
    """One runtime per symbol (default: the owner-approved 8-symbol universe from the last real-money
    session), all sharing ONE RiskManager instance. Nothing here starts a thread or opens a socket -
    callers decide when to call .start() on each, exactly like a single-symbol LSRL_V1 runtime."""
    symbols = symbols or OWNER_APPROVED_PRODUCTION_SYMBOLS
    return {sym: build_funnel_runtime(settings, symbol=sym, client=client, risk_manager=risk_manager, mode=mode,
                                      ws_factory=ws_factory, project_dir=project_dir)
           for sym in symbols}
