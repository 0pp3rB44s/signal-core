"""End-to-end: ExecutionService -> real client -> readback -> economics gate.

The other fail-safe tests hand `_fail_safe_close` a ready-made
`{"status": "CLOSED"}`. That verifies the gate *after* the client but assumes
the status means something. These drive the real
`BitgetOrderClient.close_futures_position_full`, with only `get_all_positions`
and the order POST mocked, so the remaining-size decision is made by production
code — and disabling that decision turns these red.
"""

from __future__ import annotations

import logging

import pytest

from clients.bitget_account_client import BitgetAccountClientMixin
from clients.bitget_order_client import BitgetOrderClientMixin

OPEN_MS = 1_785_700_000_000
SYMBOL = "BTCUSDT"


def position_row(size="0.0003"):
    return {"symbol": SYMBOL, "holdSide": "short", "total": size, "available": size,
            "openPriceAvg": "62900.0", "markPrice": "62950.0", "cTime": OPEN_MS,
            "positionId": "P1"}


def history_row():
    return {"symbol": SYMBOL, "holdSide": "short", "ctime": OPEN_MS,
            "utime": OPEN_MS + 600_000, "openTotalPos": "0.0003",
            "closeTotalPos": "0.0003", "openAvgPrice": "62900.0",
            "closeAvgPrice": "62950.0", "pnl": "-0.03619",
            "netProfit": "-0.06631471", "openFee": "-0.01505149",
            "closeFee": "-0.01507321", "totalFunding": "0", "positionId": "P1"}


class Client(BitgetOrderClientMixin, BitgetAccountClientMixin):
    """Real close_futures_position_full; only the exchange edge is stubbed."""

    def __init__(self, readbacks):
        self.log = logging.getLogger("client")
        self._readbacks = list(readbacks)
        self.cancelled_tpsl = []
        self.close_orders = []

    # -- exchange edge -----------------------------------------------------
    def get_all_positions(self, *a, **k):
        rows = self._readbacks.pop(0) if len(self._readbacks) > 1 else self._readbacks[0]
        return {"data": rows}

    def close_futures_position(self, **kw):
        self.close_orders.append(kw)
        return {"code": "00000", "msg": "success", "data": {"orderId": "C1"}}

    def cancel_all_futures_tpsl_orders(self, **kw):
        self.cancelled_tpsl.append(kw)
        return {"status": "CANCELLED"}

    def _request(self, *a, **k):
        return {"data": {"list": [history_row()]}}


class Service:
    """The real _fail_safe_close and its economics hop, on a stub service."""

    from execution.execution_service import ExecutionService as _ES

    _fail_safe_close = _ES._fail_safe_close
    _record_fail_safe_close_economics = _ES._record_fail_safe_close_economics
    _fetch_closed_position_history = _ES._fetch_closed_position_history
    _verify_no_live_position_after_fail_safe = _ES._verify_no_live_position_after_fail_safe

    def __init__(self, client, tmp_path):
        self.client = client
        self.log = logging.getLogger("svc")
        self.settings = type("S", (), {"bitget_product_type": "USDT-FUTURES"})()
        self.recorder_calls = []
        self.written = []
        self._dataset = str(tmp_path / "trade_dataset_v2.csv")
        open(self._dataset, "w").write(
            "event_type,symbol,direction,opened_at,net_pnl,fees,sync_source,"
            "position_lifecycle_id,confirmed_position_size\n"
        )

    # capture the shared-recorder hop without changing production behaviour
    def _record_fail_safe_close_economics(self, close_result, lifecycle_identity):
        from execution.closed_lifecycle_recorder import reconcile_fail_safe_close

        self.recorder_calls.append((close_result, lifecycle_identity))
        return reconcile_fail_safe_close(
            lifecycle_identity=lifecycle_identity,
            close_result=close_result,
            dataset_path=self._dataset,
            fetch_history=lambda: [history_row()],
            write_economic_close=lambda ident, econ: self.written.append(econ),
            log_=self.log,
            reconcile=lambda **kw: __import__(
                "execution.close_reconciler", fromlist=["x"]
            ).reconcile_close(sleep=lambda _: None, **kw),
        )


def identity():
    return {"symbol": SYMBOL, "direction": "SHORT", "hold_side": "short",
            "opened_at_ms": OPEN_MS, "confirmed_position_size": 0.0003,
            "exchange_avg_entry": 62900.0, "exchange_position_id": "P1"}


def run(tmp_path, readbacks):
    c = Client(readbacks)
    s = Service(c, tmp_path)
    s._fail_safe_close(symbol=SYMBOL, size=0.0003, close_side="sell",
                       direction="SHORT", reason="unprotected_position",
                       lifecycle_identity=identity())
    return c, s


# ── TEST 1: remaining size > 0 blocks everything ────────────────────────────

def test_nonzero_remaining_blocks_economics(tmp_path):
    """Order accepted, but the readback still shows the position."""
    c, s = run(tmp_path, [[position_row("0.0003")]])

    status = "CLOSE_FULL_POSITION_REMAINS"
    assert c.close_orders, "the close order should still have been attempted"
    assert s.recorder_calls == [] or all(
        str((r[0] or {}).get("status")).upper() != "CLOSED" for r in s.recorder_calls
    ), "economics must not open on a non-flat close"
    assert s.written == [], "no economic CLOSE may be written"
    assert c.cancelled_tpsl == [], "protection must survive an unproven close"
    del status


def test_partial_close_still_blocks_economics(tmp_path):
    """Size shrank but is not zero: still not closed."""
    c, s = run(tmp_path, [[position_row("0.0003")], [position_row("0.0001")]])
    assert s.written == []
    assert c.cancelled_tpsl == []


# ── TEST 2: remaining size exactly 0 opens economics ────────────────────────

def test_zero_remaining_opens_economics(tmp_path):
    """Second readback no longer holds the position."""
    c, s = run(tmp_path, [[position_row("0.0003")], []])

    assert len(s.recorder_calls) == 1, "exactly one hop into the shared recorder"
    close_result, ident = s.recorder_calls[0]
    assert str(close_result.get("status")).upper() == "CLOSED"
    assert ident == identity(), "identity must arrive unchanged"
    assert len(s.written) == 1, "exactly one economic CLOSE"
    assert s.written[0]["net_pnl"] == pytest.approx(-0.06631471)
    assert s.written[0]["fees"] == pytest.approx(0.01505149 + 0.01507321)


def test_zero_remaining_writes_no_duplicate(tmp_path):
    c, s = run(tmp_path, [[position_row("0.0003")], []])
    assert len(s.written) == 1
