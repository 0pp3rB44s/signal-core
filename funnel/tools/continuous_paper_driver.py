"""Continuous, real-live-data SIGNAL_CORE_FUNNEL_V1 driver: polls real Bitget public REST data on a
timer and feeds it through the SAME step_variant() path already proven in Tuning Round 1
(funnel.tools.tuning_round1_ab), instead of a historical replay timeline.

DISCLOSED FINDING (2026-09-27): funnel/runtime.py's build_funnel_runtime/build_funnel_fleet - built in
Phase 1 to reuse lsrl.runtime.LSRLRuntime unmodified - does NOT actually work for the funnel as-is.
LSRLRuntime.restore() reads engine.last_bar1m_open_ts/last_bar15_open_ts (LSRLEngine-only attributes;
FunnelEngine raises AttributeError), and more fundamentally lsrl.feed.CandleFeed/LSRLRuntime.step() are
hardcoded to tf_s in {60, 900} (LSRL_V1's own 1m/15m structure) with no path for the 1h/4H/1D bars the
funnel's HTF confirmation and regime classification genuinely need (see
FUNNEL_MARKET_INPUT_RECONSTRUCTION_2026-09-27.md). Extending lsrl/runtime.py or lsrl/feed.py to add
timeframes would mean editing LSRL_V1's own shared runtime - explicitly out of scope ("never touch
LSRL_V1"). This module is therefore a SEPARATE, funnel-specific continuous driver: REST-polling (not
WS), built entirely from already-tested pieces (funnel.tools.live_data_run's fetch helper,
funnel.tools.tuning_round1_ab's make_variant/step_variant/_write_open_positions - the exact,
ordering-fixed harness the concentration-mechanism audit just verified), not a new decision path.

What this does NOT inherit from LSRLRuntime, disclosed rather than silently assumed: WebSocket
depth/trade ingestion (the funnel's own detectors only ever consumed candles + one orderbook snapshot,
never WS depth/trade - see the Phase 2 market-input reconstruction), the LSRLRecorder's hash-chained
day-file format (replaced here by a plain append-only JSONL decision/trade log plus a periodic JSON
state snapshot - restart reconciliation still works via FunnelEngine.snapshot()/.restore() and
SimExchange.snapshot()/.restore(), just not through LSRL_V1's own recorder), and LSRL's WS-reconnect
supervision (replaced by a simple per-symbol consecutive-failure counter that degrades to
FEED_UNHEALTHY without ever crashing the process). Disk-capacity protection is a fresh, minimal
shutil.disk_usage floor check - NOT lsrl.tools.capacity_monitor's shared monitor_status.json/ALARM.json
(reading those would couple this run to LSRL_V1's own operational monitor process, which is explicitly
out of scope to touch).

Real public Bitget REST market data only (candles + orderbook + contracts). No credentials read or
required anywhere on this path. SimExchange fills only - never a real order.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from clients.bitget_rest import BitgetRestClient
from clients.schemas import Candle
from lsrl.core.types import SymbolRules
from market_data.htf_regime import classify_htf_regime
from market_features.engine import LiveMarketContext, build_market_snapshot

from funnel.core.config import OWNER_APPROVED_PRODUCTION_SYMBOLS
from funnel.tools.live_data_run import INTERVAL_MS, _closed_as_of, _fetch_symbol_inputs
from funnel.tools.tuning_round1_ab import VariantState, _write_open_positions, make_variant, step_variant

POLL_INTERVAL_S = 60.0
STATE_SNAPSHOT_EVERY_S = 300.0
DISK_FREE_FLOOR_GB = 2.0
MAX_CONSECUTIVE_FEED_FAILURES = 10           # after this many, mark FEED_UNHEALTHY (never crashes)

REPO_ROOT = Path(__file__).resolve().parents[2]

VARIANTS = {
    "baseline": {"cap_pct": 120.0, "label": "ORIGINAL_120_BASELINE", "compounding": True, "start_nav_usdt": 100.0},
    "shadow": {"cap_pct": 40.0, "label": "TUNED_40_SHADOW", "compounding": False, "start_nav_usdt": 100.0},
}


def _paths(variant: str) -> dict[str, Path]:
    root = REPO_ROOT / "data_store" / "continuous_paper" / variant
    return {"root": root, "decisions": root / "decisions.jsonl", "trades": root / "trades.jsonl",
           "state": root / "state.json", "status": root / "status.json"}


def _disk_free_gb() -> float:
    return shutil.disk_usage(REPO_ROOT).free / (1024 ** 3)


def _append_jsonl(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(doc, sort_keys=True, default=str) + "\n")


def _build_market(inputs: dict, symbol: str, as_of_ms: int, candle: Candle):
    primary, confirm, h4, d1, orderbook, contract = (
        inputs["primary"], inputs["confirm"], inputs["h4"], inputs["d1"], inputs["orderbook"], inputs["contract"])
    i = primary.index(candle)
    h4_closed = _closed_as_of(h4, "4H", as_of_ms)
    d1_closed = _closed_as_of(d1, "1D", as_of_ms)
    htf = classify_htf_regime(h4_closed, d1_closed) if h4_closed and d1_closed else {
        "regime_1d": "neutral", "regime_4h": "neutral", "htf_regime": "neutral"}
    return build_market_snapshot(
        symbol, primary[:i + 1], confirm, as_of_timestamp_ms=as_of_ms,
        primary_granularity="15m", confirmation_granularity="1h",
        inputs=LiveMarketContext(orderbook=orderbook, htf_context=htf, contract=contract))


def _fetch_all_rules(client: BitgetRestClient, settings: Settings, symbols: tuple[str, ...]) -> dict[str, SymbolRules]:
    rules: dict[str, SymbolRules] = {}
    for symbol in symbols:
        inputs = _fetch_symbol_inputs(client, settings, symbol)
        contract = inputs["contract"]
        rules[symbol] = (
            SymbolRules(symbol=symbol, tick_size=10 ** (-contract.price_place), qty_step=contract.size_multiplier or 0.0001,
                       min_qty=contract.min_trade_num or 0.0001, min_notional_usdt=5.0)
            if contract is not None and contract.price_place is not None else
            SymbolRules(symbol=symbol, tick_size=0.0001, qty_step=0.0001, min_qty=0.0001, min_notional_usdt=5.0))
    return rules


class ContinuousDriver:
    def __init__(self, variant: str, symbols: tuple[str, ...] = OWNER_APPROVED_PRODUCTION_SYMBOLS) -> None:
        spec = VARIANTS[variant]
        self.variant, self.symbols = variant, symbols
        self.cap_pct: float = spec["cap_pct"]
        self.compounding: bool = spec["compounding"]
        self.nav = spec["start_nav_usdt"]
        self.paths = _paths(variant)
        self.settings = Settings(MAX_CLUSTER_EXPOSURE_PCT=self.cap_pct)
        if self.settings.max_cluster_exposure_pct != self.cap_pct:
            raise RuntimeError("Settings alias override failed for MAX_CLUSTER_EXPOSURE_PCT")
        self.client = BitgetRestClient(self.settings)
        rules_by_symbol = _fetch_all_rules(self.client, self.settings, symbols)
        self.variant_state: VariantState = make_variant(
            spec["label"], max_cluster_exposure_pct=self.cap_pct, rules_by_symbol=rules_by_symbol)
        self.last_processed_open_ts: dict[str, int | None] = {s: None for s in symbols}
        self.consecutive_failures: dict[str, int] = {s: 0 for s in symbols}
        self.last_success_utc: dict[str, str | None] = {s: None for s in symbols}
        self._trades_seen = 0
        self.started_at = time.time()
        self._restore_if_present()

    # ------------------------------------------------------------------ persistence / restart
    def _restore_if_present(self) -> None:
        if not self.paths["state"].exists():
            return
        try:
            saved = json.loads(self.paths["state"].read_text())
        except (OSError, ValueError):
            print(f"CONTINUOUS_STATE_RESTORE_FAILED | variant={self.variant} | unreadable state.json, starting fresh",
                 file=sys.stderr, flush=True)
            return
        self.nav = saved.get("nav", self.nav)
        self.last_processed_open_ts = {**self.last_processed_open_ts, **saved.get("last_processed_open_ts", {})}
        for symbol, eng_state in (saved.get("engines") or {}).items():
            if symbol in self.variant_state.engines:
                try:
                    self.variant_state.engines[symbol].restore(eng_state)
                except ValueError as exc:
                    print(f"CONTINUOUS_ENGINE_RESTORE_REFUSED | symbol={symbol} | {exc} | starting that symbol fresh",
                         file=sys.stderr, flush=True)
        for symbol, sim_state in (saved.get("sims") or {}).items():
            if symbol in self.variant_state.sims:
                self.variant_state.sims[symbol].restore(sim_state)
        print(f"CONTINUOUS_STATE_RESTORED | variant={self.variant} | nav={self.nav} | "
             f"last_processed_open_ts={self.last_processed_open_ts}", flush=True)

    def _persist_state(self) -> None:
        doc = {
            "variant": self.variant, "nav": self.nav, "last_processed_open_ts": self.last_processed_open_ts,
            "engines": {s: e.snapshot() for s, e in self.variant_state.engines.items()},
            "sims": {s: sim.snapshot() for s, sim in self.variant_state.sims.items()},
            "saved_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.paths["state"].parent.mkdir(parents=True, exist_ok=True)
        tmp = self.paths["state"].with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, default=str))
        tmp.replace(self.paths["state"])

    def _persist_status(self) -> None:
        doc = {
            "variant": self.variant, "label": VARIANTS[self.variant]["label"], "cap_pct": self.cap_pct,
            "compounding": self.compounding, "nav": self.nav, "symbols": list(self.symbols),
            "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
            "uptime_s": round(time.time() - self.started_at, 1),
            "pid": os.getpid(), "n_trades_closed": len(self.variant_state.trades),
            "n_accepted": len(self.variant_state.accepted), "n_signals": len(self.variant_state.signals),
            "last_processed_open_ts": self.last_processed_open_ts,
            "consecutive_failures": self.consecutive_failures,
            "last_success_utc": self.last_success_utc,
            "feed_health": {s: ("FEED_UNHEALTHY" if n >= MAX_CONSECUTIVE_FEED_FAILURES else "OK")
                           for s, n in self.consecutive_failures.items()},
            "disk_free_gb": round(_disk_free_gb(), 2),
        }
        self.paths["status"].parent.mkdir(parents=True, exist_ok=True)
        tmp = self.paths["status"].with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=2, default=str))
        tmp.replace(self.paths["status"])

    def _log_new_trades(self) -> None:
        new = self.variant_state.trades[self._trades_seen:]
        for t in new:
            _append_jsonl(self.paths["trades"], t)
            if self.compounding:
                self.nav += float(t["net_pnl"])
        self._trades_seen = len(self.variant_state.trades)

    # ------------------------------------------------------------------ one symbol, one cycle
    def _cycle_symbol(self, symbol: str, *, capacity_blocked: bool) -> None:
        try:
            inputs = _fetch_symbol_inputs(self.client, self.settings, symbol)
        except Exception as exc:
            self.consecutive_failures[symbol] += 1
            _append_jsonl(self.paths["decisions"], {
                "symbol": symbol, "t_ms": int(time.time() * 1000), "event": "FEED_ERROR", "error": str(exc)})
            return
        primary = inputs["primary"]
        if len(primary) < 30:
            self.consecutive_failures[symbol] += 1
            return
        now_ms = int(time.time() * 1000)
        closed = _closed_as_of(primary, "15m", now_ms)   # Bitget's candle endpoint can include the still-
        if not closed:                                  # forming bar as the last row; only ever act on
            return                                       # bars that have genuinely closed (see smoke test).
        candle = closed[-1]
        as_of_ms = candle.timestamp_ms + INTERVAL_MS["15m"] + 1
        last = self.last_processed_open_ts[symbol]
        if last is not None and candle.timestamp_ms <= last:
            return                                    # already processed this closed bar
        if capacity_blocked:
            _append_jsonl(self.paths["decisions"], {
                "symbol": symbol, "t_ms": as_of_ms, "event": "CAPACITY_BLOCKED_NEW_ENTRY",
                "disk_free_gb": round(_disk_free_gb(), 2)})
            # Never blocks protection: still let step_variant run so existing setups' stops/targets/fills
            # are processed; a capacity block only means "no NEW entries" in spirit, and step_variant's
            # own RiskManager/CentralRiskGate path already independently gates every new entry regardless.
        try:
            market = self._build_market_safe(inputs, symbol, as_of_ms, candle)
        except Exception as exc:
            self.consecutive_failures[symbol] += 1
            _append_jsonl(self.paths["decisions"], {
                "symbol": symbol, "t_ms": as_of_ms, "event": "MARKET_SNAPSHOT_ERROR", "error": str(exc)})
            return
        outcome = step_variant(self.variant_state, symbol, market, t_ms=as_of_ms, nav_usdt=self.nav, candle=candle)
        self.consecutive_failures[symbol] = 0
        self.last_success_utc[symbol] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.last_processed_open_ts[symbol] = candle.timestamp_ms
        _append_jsonl(self.paths["decisions"], {"symbol": symbol, "t_ms": as_of_ms, "event": "DECISION_CYCLE",
                                                "nav_usdt": self.nav, **outcome})
        self._log_new_trades()

    def _build_market_safe(self, inputs, symbol, as_of_ms, candle):
        return _build_market(inputs, symbol, as_of_ms, candle)

    # ------------------------------------------------------------------ main loop
    def run_forever(self) -> None:
        stop = {"flag": False}

        def _on_signal(signum, _frame):
            stop["flag"] = True

        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)

        last_state_snapshot = 0.0
        print(f"CONTINUOUS_DRIVER_STARTED | variant={self.variant} | cap_pct={self.cap_pct} | "
             f"symbols={list(self.symbols)} | nav={self.nav}", flush=True)
        while not stop["flag"]:
            cycle_start = time.time()
            capacity_blocked = _disk_free_gb() < DISK_FREE_FLOOR_GB
            for symbol in self.symbols:
                if stop["flag"]:
                    break
                self._cycle_symbol(symbol, capacity_blocked=capacity_blocked)
            self._persist_status()
            if time.time() - last_state_snapshot >= STATE_SNAPSHOT_EVERY_S:
                self._persist_state()
                last_state_snapshot = time.time()
            elapsed = time.time() - cycle_start
            stop_wait = max(1.0, POLL_INTERVAL_S - elapsed)
            for _ in range(int(stop_wait)):
                if stop["flag"]:
                    break
                time.sleep(1.0)
        self._persist_state()
        self._persist_status()
        print(f"CONTINUOUS_DRIVER_STOPPED | variant={self.variant}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=sorted(VARIANTS), required=True)
    args = parser.parse_args(argv)
    driver = ContinuousDriver(args.variant)
    driver.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
