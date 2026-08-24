"""Production wiring: PositionManager.sync() dispatches OPEN
adaptive_trend_tsmom_v1 positions to _sync_adaptive_trend_position and
completely bypasses every MicroFlow-specific mechanism for them.

Binds against the real PositionManager/AdaptiveTrendSyncMixin -- not a
helper double -- so if the dispatch is removed from sync() or the
exchange-stop-first restart contract is broken, these go red.
"""

from __future__ import annotations

import logging

import pytest

from execution.position_manager import PositionManager
from strategies.adaptive_trend_tsmom import STRATEGY_VERSION

SIX_H = 6 * 60 * 60 * 1000


class FakeClient:
    def __init__(self, candles_rows, protect_should_fail=False):
        self._candles_rows = candles_rows
        self.protect_calls = []
        self.protect_should_fail = protect_should_fail

    def get_candles(self, symbol, product_type, granularity="6h", limit=200):
        return {"data": self._candles_rows}

    def place_futures_protection_orders(self, **kwargs):
        if self.protect_should_fail:
            raise RuntimeError("exchange rejected protection update")
        self.protect_calls.append(kwargs)
        return {"stop_loss": kwargs["stop_loss"]}


class Harness(PositionManager):
    """Real PositionManager, only I/O stubbed."""

    def __init__(self, client):
        self.client = client
        self.settings = type("S", (), {"bitget_product_type": "USDT-FUTURES"})()
        self.log = logging.getLogger("harness")


def candle_row(open_ms, close):
    return [open_ms, close, close + 1.0, close - 1.0, close, "1", "1"]


def _anchor_start_ms(num_candles):
    import time
    now_ms = int(time.time() * 1000)
    boundary = (now_ms // SIX_H) * SIX_H
    return boundary - (num_candles - 1) * SIX_H


def make_candles(closes, start_ms=None):
    if start_ms is None:
        start_ms = _anchor_start_ms(len(closes))
    return [candle_row(start_ms + i * SIX_H, c) for i, c in enumerate(closes)]


def base_position(**overrides):
    position = {
        "symbol": "BTCUSDT",
        "status": "OPEN",
        "strategy": STRATEGY_VERSION,
        "direction": "LONG",
        "stop_loss": 90.0,
        "last_price": 100.0,
        "adaptive_trend_last_processed_close_ms": None,
    }
    position.update(overrides)
    return position


def live_position(symbol="BTCUSDT", stop_loss=90.0, hold_side="long", size="1.0"):
    return {
        "symbol": symbol, "stopLoss": str(stop_loss), "holdSide": hold_side,
        "total": size, "averageOpenPrice": "95.0",
    }


def test_adaptive_trend_position_never_touches_microflow_logic():
    """A MicroFlow-only path (e.g. TP1/BE/profit-lock attribute access) would
    raise on this minimal position dict -- reaching the end without error
    proves the dispatch-and-continue actually isolates the two strategies."""
    from clients.schemas import MarketSnapshot

    closes = [100.0] * 16
    client = FakeClient(make_candles(closes))
    manager = Harness(client)
    position = base_position()

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position()], price_map={"BTCUSDT": 100.0},
        updates=updates, events=events,
    )
    assert len(updates) == 1
    assert updates[0].symbol == "BTCUSDT"


def test_no_trustworthy_snapshot_never_touches_exchange_stop():
    client = FakeClient(make_candles([100.0] * 16))
    manager = Harness(client)
    position = base_position(stop_loss=90.0)

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=False, bitget_open_symbols=set(),
        positions_live=[], price_map={"BTCUSDT": 100.0},
        updates=updates, events=events,
    )
    assert client.protect_calls == []
    assert position["stop_loss"] == 90.0


def test_unprotected_position_defers_to_protection_repair_not_trailing():
    client = FakeClient(make_candles([100.0] * 16))
    manager = Harness(client)
    position = base_position()

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position(stop_loss=0.0)], price_map={"BTCUSDT": 100.0},
        updates=updates, events=events,
    )
    assert client.protect_calls == []
    assert "protection repair" in updates[0].note


def test_restart_uses_exchange_reported_stop_not_local_cache():
    """Local cache says 50.0 (stale/wrong); exchange says 95.0. The ratchet
    must continue from 95.0, proving the restart-safety contract holds at
    the real wiring layer, not just inside evaluate_trail's own unit tests."""
    closes = [100.0] * 15 + [96.0]
    client = FakeClient(make_candles(closes))
    manager = Harness(client)
    position = base_position(stop_loss=50.0)  # stale local value

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position(stop_loss=95.0)], price_map={"BTCUSDT": 96.0},
        updates=updates, events=events,
    )
    # candidate = 96.0 - 2.5*1.0 = 93.5, below exchange's 95.0 -> UNCHANGED at 95.0
    assert client.protect_calls == []
    assert position["stop_loss"] == 95.0


def test_favorable_move_places_updated_protection_and_advances_local_state():
    closes = [100.0] * 15 + [120.0]
    client = FakeClient(make_candles(closes))
    manager = Harness(client)
    position = base_position(stop_loss=90.0)

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position(stop_loss=90.0)], price_map={"BTCUSDT": 120.0},
        updates=updates, events=events,
    )
    assert len(client.protect_calls) == 1
    call = client.protect_calls[0]
    assert call["symbol"] == "BTCUSDT"
    assert call["stop_loss"] > 90.0
    assert position["stop_loss"] == call["stop_loss"]
    assert position["adaptive_trend_last_processed_close_ms"] is not None


def test_exchange_rejection_leaves_local_state_and_exchange_stop_untouched():
    """If place_futures_protection_orders raises, local state must NOT be
    advanced -- the next cycle must re-attempt against the same candle."""
    closes = [100.0] * 15 + [120.0]
    client = FakeClient(make_candles(closes), protect_should_fail=True)
    manager = Harness(client)
    position = base_position(stop_loss=90.0)

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position(stop_loss=90.0)], price_map={"BTCUSDT": 120.0},
        updates=updates, events=events,
    )
    assert position["stop_loss"] == 90.0
    assert position.get("adaptive_trend_last_processed_close_ms") is None
    assert "FAILED" in updates[0].note


def test_same_candle_processed_twice_is_a_pure_noop():
    """Race scenario 1 at the real wiring layer: second sync() call for the
    same closed candle must not touch the exchange at all."""
    closes = [100.0] * 15 + [120.0]
    client = FakeClient(make_candles(closes))
    manager = Harness(client)
    position = base_position(stop_loss=90.0)

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position(stop_loss=90.0)], price_map={"BTCUSDT": 120.0},
        updates=updates, events=events,
    )
    assert len(client.protect_calls) == 1
    first_stop = position["stop_loss"]

    # Second cycle: exchange now reports the stop we just placed.
    updates2, events2 = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position(stop_loss=first_stop)], price_map={"BTCUSDT": 120.0},
        updates=updates2, events=events2,
    )
    assert len(client.protect_calls) == 1  # unchanged -- no second call
    assert position["stop_loss"] == first_stop


def test_short_position_direction_is_respected():
    closes = [100.0] * 15 + [80.0]
    client = FakeClient(make_candles(closes))
    manager = Harness(client)
    position = base_position(direction="SHORT", stop_loss=110.0)

    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position(stop_loss=110.0, hold_side="short")],
        price_map={"BTCUSDT": 80.0}, updates=updates, events=events,
    )
    assert len(client.protect_calls) == 1
    assert client.protect_calls[0]["stop_loss"] < 110.0
    assert client.protect_calls[0]["direction"] == "SHORT"


def test_sync_dispatches_adaptive_trend_positions_and_skips_microflow_branch(monkeypatch):
    """End-to-end through the real sync() entrypoint: an OPEN
    adaptive_trend_tsmom_v1 position must reach _sync_adaptive_trend_position
    and never fall into the MicroFlow `if not bitget_sync_ok:` legacy branch
    below it (which would raise on this minimal position schema)."""
    from clients.schemas import MarketSnapshot

    called = {}

    class DispatchHarness(Harness):
        def _sync_adaptive_trend_position(self, position, **kwargs):
            called["dispatched"] = True
            kwargs["updates"].append(object())

        def recover_provisional_close_rows(self):
            return {}

        def fetch_exchange_snapshot(self):
            from execution.position_manager import ExchangeSnapshot
            import time
            return ExchangeSnapshot(
                positions_live=[live_position()], open_symbols={"BTCUSDT"},
                ok=True, fetched_at_ms=time.time() * 1000,
            )

        def _heal_missing_protection_from_fallback(self, position):
            pass

    manager = DispatchHarness(FakeClient(make_candles([100.0] * 16)))
    manager.store = type("Store", (), {
        "load": lambda self, default=None: [base_position()],
        "save": lambda self, data: None,
    })()
    manager.event_store = type("Store", (), {
        "load": lambda self, default=None: [],
        "save": lambda self, data: None,
    })()

    snapshot = MarketSnapshot.__new__(MarketSnapshot)
    object.__setattr__(snapshot, "symbol", "BTCUSDT") if hasattr(MarketSnapshot, "__slots__") else None

    class FakePrimary:
        latest_close = 100.0
        candles = []

    class FakeSnap:
        symbol = "BTCUSDT"
        primary = FakePrimary()

    manager.sync([FakeSnap()])
    assert called.get("dispatched") is True


def test_restart_mid_flight_after_exchange_confirmed_is_still_idempotent():
    """Race scenario: exchange accepts a stop update but the process crashes
    before the sync loop's own local save flushes `adaptive_trend_last_processed_close_ms`.
    On restart, `current_stop` is read fresh from the exchange (already the
    NEW value -- Bitget is the source of truth, not our local record), so the
    ratchet recomputes the same candidate and lands on UNCHANGED. No second
    exchange call, no double-application, no drift."""
    closes = [100.0] * 15 + [120.0]
    client = FakeClient(make_candles(closes))
    manager = Harness(client)

    # Simulates the crash: local state never advanced past the OLD stop, but
    # the exchange already reflects the NEW one (as if a save was interrupted
    # right after the successful protection call).
    position = base_position(stop_loss=90.0)  # stale, as-if-never-saved
    updates, events = [], []
    manager._sync_adaptive_trend_position(
        position, bitget_sync_ok=True, bitget_open_symbols={"BTCUSDT"},
        positions_live=[live_position(stop_loss=125.0)],  # exchange already moved
        price_map={"BTCUSDT": 120.0}, updates=updates, events=events,
    )
    assert client.protect_calls == []  # candidate (<=120-2.5*atr) can't beat 125 -> no call
    assert position["stop_loss"] == 125.0  # local state reconciles to exchange truth


def test_recovered_position_never_reaches_adaptive_trend_sync():
    """A position discovered on the exchange with no prior local record is
    always labeled `recovered_exchange_position` (fail-closed attribution,
    execution/position_reconciler.py), never guessed as adaptive_trend_tsmom_v1
    -- so PositionManager.sync()'s strategy-equality dispatch check can never
    route it here by construction. This test documents that invariant at the
    boundary this module owns."""
    from execution.position_reconciler import RECOVERED_EXCHANGE_POSITION_STRATEGY

    assert RECOVERED_EXCHANGE_POSITION_STRATEGY != STRATEGY_VERSION
