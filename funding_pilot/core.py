"""Order-transport-neutral implementation of the frozen funding pilot.

No Bitget client is imported here. A deployment adapter must route mutations
through the canonical RiskManager/PositionManager ownership path. Until such an
adapter exists and is independently verified, ``orders_enabled`` must remain
false and this component cannot send an order.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

FROZEN_SPEC_SHA256 = "cda7ecb21e6fd089ef98abb8047d409285825a37d6a2e87510d73cf653ea2e13"
STRATEGY = "funding_crowding_continuation_24h"
OID_PREFIX = "cgc-fcp-"
REQUIRED_EXECUTION_TELEMETRY = frozenset({
    "signal_timestamp", "symbol", "side", "signal_inputs", "pilot_nav_before",
    "target_notional", "actual_notional", "order_timestamp", "order_type",
    "requested_price", "bid", "ask", "fill_price", "fill_timestamp", "spread",
    "estimated_slippage", "realized_slippage", "fees", "funding", "stop_order_id",
    "stop_price", "exit_reason", "exit_price", "realized_pnl", "pilot_nav_after",
    "pilot_drawdown", "exchange_account_equity", "available_margin",
})


class FailClosed(RuntimeError):
    pass


@dataclass(frozen=True)
class PilotConfig:
    spec_path: Path
    state_path: Path
    starting_equity: float = 27.44
    single_position_fraction: float = 0.10
    gross_fraction: float = 0.20
    max_positions: int = 2
    leverage: float = 1.0
    stop_fraction: float = 0.10
    kill_drawdown_fraction: float = 0.05
    orders_enabled: bool = False

    def __post_init__(self) -> None:
        exact = (27.44, .10, .20, 2, 1.0, .10, .05)
        actual = (self.starting_equity, self.single_position_fraction, self.gross_fraction,
                  self.max_positions, self.leverage, self.stop_fraction, self.kill_drawdown_fraction)
        if actual != exact:
            raise ValueError(f"frozen pilot constraints changed: {actual}")


@dataclass(frozen=True)
class PilotSignal:
    signal_id: str
    timestamp_ms: int
    symbol: str
    side: str
    reference_price: float
    signal_inputs: dict


@dataclass(frozen=True)
class ExchangeTruth:
    connected: bool
    account_equity: float | None
    available_margin: float | None
    pilot_positions: tuple[dict, ...]
    pilot_working_orders: tuple[dict, ...]
    pilot_stops: tuple[dict, ...]
    non_pilot_reserved_margin: float | None


class ExchangePort(Protocol):
    def truth(self) -> ExchangeTruth: ...
    def min_notional(self, symbol: str) -> float | None: ...
    def decision_book(self, symbol: str) -> dict: ...
    def submit_entry(self, *, signal: PilotSignal, notional: float, client_oid: str) -> dict: ...
    def place_native_stop(self, *, position: dict, stop_price: float) -> dict: ...
    def verify_native_stop(self, *, symbol: str, side: str, stop_price: float) -> dict: ...
    def cancel_working_order(self, order: dict) -> None: ...
    def close_reduce_only(self, position: dict, reason: str) -> dict: ...
    def cancel_stop(self, stop: dict) -> None: ...


class PilotLedger:
    """SQLite strategy ledger; restart-safe and isolated from AdaptiveTrend."""

    def __init__(self, path: Path, starting_equity: float = 27.44) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_ms INTEGER NOT NULL,
              kind TEXT NOT NULL, signal_id TEXT, symbol TEXT, payload TEXT NOT NULL);
        """)
        self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('starting_equity',?)", (str(starting_equity),))
        self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('high_water_mark',?)", (str(starting_equity),))
        self.db.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('status','ACTIVE')")
        self.db.commit()

    def append(self, kind: str, payload: dict, *, signal_id: str = "", symbol: str = "") -> None:
        self.db.execute(
            "INSERT INTO events(timestamp_ms,kind,signal_id,symbol,payload) VALUES(?,?,?,?,?)",
            (int(time.time() * 1000), kind, signal_id, symbol.upper(), json.dumps(payload, sort_keys=True)),
        )
        self.db.commit()

    def events(self, kind: str | None = None) -> list[dict]:
        query, args = "SELECT * FROM events", ()
        if kind:
            query, args = query + " WHERE kind=?", (kind,)
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in self.db.execute(query, args)]

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def set(self, key: str, value: str | float) -> None:
        self.db.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        self.db.commit()

    def economics(self, truth: ExchangeTruth) -> dict:
        totals = {"realized_pnl": 0.0, "fees": 0.0, "funding": 0.0, "other_costs": 0.0}
        for row in self.events("ECONOMICS"):
            for key in totals:
                value = row["payload"].get(key)
                if value is None or not math.isfinite(float(value)):
                    raise FailClosed(f"unknown pilot economics: {key}")
                totals[key] += float(value)
        unrealized = 0.0
        for position in truth.pilot_positions:
            value = position.get("unrealized_pnl")
            if value is None or not math.isfinite(float(value)):
                raise FailClosed("pilot unrealized PnL unknown")
            unrealized += float(value)
        starting = float(self.get("starting_equity") or 0)
        nav = starting + totals["realized_pnl"] + unrealized - totals["fees"] - totals["funding"] - totals["other_costs"]
        if not math.isfinite(nav) or nav <= 0:
            raise FailClosed("pilot NAV unavailable or depleted")
        return {**totals, "unrealized_pnl": unrealized, "nav": nav}


def verify_spec(path: Path) -> None:
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != FROZEN_SPEC_SHA256:
        raise FailClosed("frozen spec integrity mismatch")


class PilotRuntime:
    def __init__(self, config: PilotConfig, ledger: PilotLedger, exchange: ExchangePort) -> None:
        self.config, self.ledger, self.exchange = config, ledger, exchange
        verify_spec(config.spec_path)

    def reconcile(self) -> dict:
        truth = self.exchange.truth()
        if not truth.connected:
            raise FailClosed("exchange connectivity unknown")
        if truth.account_equity is None or truth.available_margin is None or truth.non_pilot_reserved_margin is None:
            raise FailClosed("account or margin truth unknown")
        if any(not str(position.get("client_oid") or "").startswith(OID_PREFIX) for position in truth.pilot_positions):
            raise FailClosed("pilot position identity uncertain")
        if any(not str(order.get("client_oid") or "").startswith(OID_PREFIX) for order in truth.pilot_working_orders):
            raise FailClosed("pilot working-order identity uncertain")
        if any(not str(stop.get("client_oid") or "").startswith(OID_PREFIX) for stop in truth.pilot_stops):
            raise FailClosed("pilot stop identity uncertain")
        economics = self.ledger.economics(truth)
        stop_by_symbol = {}
        for stop in truth.pilot_stops:
            symbol = str(stop.get("symbol") or "").upper()
            if symbol in stop_by_symbol:
                raise FailClosed(f"duplicate pilot stop: {symbol}")
            stop_by_symbol[symbol] = stop
        for position in truth.pilot_positions:
            symbol = str(position.get("symbol") or "").upper()
            if symbol not in stop_by_symbol:
                if self.config.orders_enabled:
                    self.exchange.close_reduce_only(position, "reconciliation_unprotected_position")
                    after_close = self.exchange.truth()
                    if any(str(row.get("symbol") or "").upper() == symbol for row in after_close.pilot_positions):
                        raise FailClosed(f"unprotected pilot position flatten unconfirmed: {symbol}")
                raise FailClosed(f"orphan/unprotected pilot position: {symbol}")
        for symbol in stop_by_symbol:
            if symbol not in {str(p.get("symbol") or "").upper() for p in truth.pilot_positions}:
                raise FailClosed(f"orphan pilot stop: {symbol}")
        hwm = max(float(self.ledger.get("high_water_mark") or 0), economics["nav"])
        self.ledger.set("high_water_mark", hwm)
        self.ledger.append("RECONCILIATION", {
            "nav": economics["nav"], "realized_pnl": economics["realized_pnl"],
            "unrealized_pnl": economics["unrealized_pnl"], "fees": economics["fees"],
            "funding": economics["funding"], "positions": list(truth.pilot_positions),
            "working_orders": list(truth.pilot_working_orders), "native_stops": list(truth.pilot_stops),
            "high_water_mark": hwm, "kill_switch_state": self.ledger.get("status"),
        })
        if economics["nav"] <= hwm * (1 - self.config.kill_drawdown_fraction):
            self._kill(truth, economics["nav"], hwm)
        return {"truth": truth, "economics": economics, "high_water_mark": hwm}

    def size(self, signal: PilotSignal, state: dict) -> float:
        truth, nav = state["truth"], state["economics"]["nav"]
        if self.ledger.get("status") != "ACTIVE":
            raise FailClosed("pilot not ACTIVE")
        if len(truth.pilot_positions) >= self.config.max_positions:
            raise FailClosed("maximum pilot positions reached")
        gross = sum(float(position.get("notional") or 0) for position in truth.pilot_positions)
        if any(str(position.get("symbol") or "").upper() == signal.symbol.upper() for position in truth.pilot_positions):
            raise FailClosed("pilot symbol already open")
        single_cap, gross_cap = nav * self.config.single_position_fraction, nav * self.config.gross_fraction
        safe_margin = float(truth.available_margin) - float(truth.non_pilot_reserved_margin)
        notional = min(single_cap, gross_cap - gross, safe_margin * self.config.leverage)
        minimum = self.exchange.min_notional(signal.symbol)
        if minimum is None:
            raise FailClosed("exchange minimum notional unknown")
        if notional + 1e-12 < float(minimum):
            raise FailClosed("SKIP_MIN_NOTIONAL")
        if notional <= 0 or notional > single_cap + 1e-12 or gross + notional > gross_cap + 1e-12:
            raise FailClosed("pilot sizing invariant failed")
        return notional

    def process_signal(self, signal: PilotSignal) -> dict:
        verify_spec(self.config.spec_path)
        state = self.reconcile()
        notional = self.size(signal, state)
        book = self.exchange.decision_book(signal.symbol)
        required = ("bid", "ask", "timestamp_ms")
        if any(book.get(key) is None for key in required):
            raise FailClosed("decision book unknown")
        telemetry = {"signal_timestamp": signal.timestamp_ms, "symbol": signal.symbol,
                     "side": signal.side, "signal_inputs": signal.signal_inputs,
                     "pilot_nav_before": state["economics"]["nav"], "target_notional": notional,
                     "bid": book["bid"], "ask": book["ask"], "exchange_account_equity": state["truth"].account_equity,
                     "available_margin": state["truth"].available_margin}
        self.ledger.append("SIGNAL", telemetry, signal_id=signal.signal_id, symbol=signal.symbol)
        if not self.config.orders_enabled:
            self.ledger.append("NO_ORDER_DECISION", {**telemetry, "reason": "orders_disabled"}, signal_id=signal.signal_id, symbol=signal.symbol)
            return {"status": "NO_ORDER", "notional": notional}
        raise FailClosed("live adapter not independently verified")

    def record_execution_telemetry(self, payload: dict, *, signal_id: str) -> None:
        missing = sorted(key for key in REQUIRED_EXECUTION_TELEMETRY if payload.get(key) is None)
        if missing:
            raise FailClosed("execution telemetry incomplete: " + ",".join(missing))
        self.ledger.append("EXECUTION_TELEMETRY", payload, signal_id=signal_id,
                           symbol=str(payload["symbol"]))

    def protect_filled_position(self, position: dict) -> dict:
        """Install exactly one native stop or fail closed and flatten the fill."""
        if not self.config.orders_enabled:
            raise FailClosed("orders disabled")
        symbol = str(position.get("symbol") or "").upper()
        side = str(position.get("side") or "").upper()
        entry = position.get("entry_price")
        if not symbol or side not in {"LONG", "SHORT"} or entry is None:
            raise FailClosed("filled position identity unknown")
        entry = float(entry)
        stop_price = entry * (1 - self.config.stop_fraction if side == "LONG" else 1 + self.config.stop_fraction)
        truth = self.exchange.truth()
        existing = [stop for stop in truth.pilot_stops if str(stop.get("symbol") or "").upper() == symbol]
        if len(existing) > 1:
            raise FailClosed("duplicate stop before placement")
        if len(existing) == 1:
            verified = self.exchange.verify_native_stop(symbol=symbol, side=side, stop_price=stop_price)
            if not verified.get("verified"):
                raise FailClosed("existing native stop mismatch")
            return {"status": "ALREADY_VERIFIED", "stop": existing[0]}
        placed = self.exchange.place_native_stop(position=position, stop_price=stop_price)
        verified = self.exchange.verify_native_stop(symbol=symbol, side=side, stop_price=stop_price)
        self.ledger.append("STOP_LIFECYCLE", {"symbol": symbol, "stop_price": stop_price,
                                              "stop_order_id": placed.get("order_id"),
                                              "acknowledged": bool(placed.get("order_id")),
                                              "verified": bool(verified.get("verified"))}, symbol=symbol)
        if not placed.get("order_id") or not verified.get("verified"):
            self.exchange.close_reduce_only(position, "native_stop_unverified")
            raise FailClosed("native stop not acknowledged and verified")
        return {"status": "VERIFIED", "stop": placed, "stop_price": stop_price}

    def normal_time_exit(self, position: dict) -> dict:
        if not self.config.orders_enabled:
            raise FailClosed("orders disabled")
        symbol = str(position.get("symbol") or "").upper()
        result = self.exchange.close_reduce_only(position, "frozen_24h_time_exit")
        after = self.exchange.truth()
        if any(str(row.get("symbol") or "").upper() == symbol for row in after.pilot_positions):
            raise FailClosed("normal exit not confirmed flat")
        for stop in after.pilot_stops:
            if str(stop.get("symbol") or "").upper() == symbol:
                self.exchange.cancel_stop(stop)
        final = self.exchange.truth()
        if any(str(row.get("symbol") or "").upper() == symbol for row in final.pilot_stops):
            raise FailClosed("stop cancel on normal exit not verified")
        self.ledger.append("NORMAL_EXIT", {"symbol": symbol, "exit_reason": "frozen_24h_time_exit",
                                           "exchange_result": result}, symbol=symbol)
        return result

    def _kill(self, truth: ExchangeTruth, nav: float, hwm: float) -> None:
        self.ledger.set("status", "HALTED")
        self.ledger.append("KILL_SWITCH", {"nav": nav, "high_water_mark": hwm,
                                           "drawdown": nav / hwm - 1, "latched": True})
        if not self.config.orders_enabled:
            return
        for order in truth.pilot_working_orders:
            self.exchange.cancel_working_order(order)
        for position in truth.pilot_positions:
            self.exchange.close_reduce_only(position, "pilot_drawdown_kill_switch")
        post = self.exchange.truth()
        if not post.connected or post.pilot_positions or post.pilot_working_orders:
            raise FailClosed("kill-switch flatten could not be verified")
        for stop in post.pilot_stops:
            self.exchange.cancel_stop(stop)
        final = self.exchange.truth()
        if final.pilot_positions or final.pilot_working_orders or final.pilot_stops:
            raise FailClosed("kill-switch residual exposure")
