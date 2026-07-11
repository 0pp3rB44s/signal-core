"""Safety tests for the TP/SL lifecycle in PositionManager (Fase 2).

These pin down the behavior that protects capital:
- TP1 -> SL to fee-adjusted break-even, TP2 -> TP1 lock, TP3 -> close-all
- exchange truth over local truth (closed-on-exchange recovery, ghost reconcile)
- idempotency across monitor cycles (no double closes, no double orders)
- fail-safe on Bitget API errors (freeze, no risky actions)
- long/short symmetry

They run against the real PositionManager with a mocked Bitget client and the
real JsonStateStore/loggers writing into the per-test tmp cwd (see conftest).
"""

from unittest.mock import MagicMock

import csv
from pathlib import Path

from execution.position_manager import PositionManager


TP1_CLOSE_PCT = 40.0
TP2_CLOSE_PCT = 30.0
BE_FEE_BUFFER_PCT = 0.10
ROUNDTRIP_FEE_BPS = 12.0
BE_EXTRA_MARGIN_PCT = 0.04
# Effective BE buffer must clear the full roundtrip fees + margin (see
# _fee_adjusted_break_even), so a BE stop-out is flat-to-green, not a fee loss.
EFFECTIVE_BE_BUFFER_PCT = max(BE_FEE_BUFFER_PCT, ROUNDTRIP_FEE_BPS / 100.0 + BE_EXTRA_MARGIN_PCT)


def _settings() -> MagicMock:
    settings = MagicMock()
    settings.tp1_close_pct = TP1_CLOSE_PCT
    settings.tp2_close_pct = TP2_CLOSE_PCT
    settings.tp3_close_pct = 30.0
    settings.move_stop_to_be_after_tp1 = True
    settings.tp3_close_all_remainder = True
    settings.break_even_fee_buffer_pct = BE_FEE_BUFFER_PCT
    settings.planner_estimated_roundtrip_fee_bps = ROUNDTRIP_FEE_BPS
    settings.break_even_extra_margin_pct = BE_EXTRA_MARGIN_PCT
    settings.symbol_cooldown_minutes = 30
    settings.account_equity_usdt = 100.0
    settings.profit_lock_tp1_fraction = 0.60
    settings.dead_trade_timeout_reclaim_minutes = 90.0
    settings.dead_trade_timeout_default_minutes = 240.0
    settings.dead_trade_max_abs_pnl_pct = 0.20
    return settings


def _client(open_positions: list[dict] | None = None) -> MagicMock:
    client = MagicMock()
    client.get_all_positions.return_value = {"data": open_positions or []}
    client.get_position_history.return_value = {"data": []}
    client.get_order_history.return_value = {"data": []}
    client.cancel_all_futures_tpsl_orders.return_value = {"status": "ok"}
    client.close_futures_position_full.return_value = {"status": "CLOSED"}
    client.move_futures_stop_loss.return_value = {"data": {"orderId": "sl-1"}}
    client.verify_active_stop_loss.return_value = {"verified": True}
    client.extract_fill_metrics.return_value = {}
    return client


def _manager(open_positions: list[dict] | None = None) -> PositionManager:
    manager = PositionManager(settings=_settings())
    manager.client = _client(open_positions)
    return manager


def _position(
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    entry: float = 100.0,
    stop: float = 99.0,
    tps: tuple = (101.0, 102.0, 103.0),
    size: float = 1.0,
    **extra,
) -> dict:
    position = {
        "symbol": symbol,
        "direction": direction,
        "status": "OPEN",
        "avg_entry": entry,
        "stop_loss": stop,
        "take_profits": list(tps),
        "size": size,
        "order_size": size,
        "remaining_size_pct": 100.0,
        "strategy": "trend_continuation",
        "opened_at": "2026-07-06T08:00:00+00:00",
        # Real executions carry a protection payload from entry; without it the
        # (correct) unprotected-position emergency close kicks in and masks the
        # lifecycle behavior under test.
        "protection_payload": {
            "stop_loss": {"orderId": "sl-0"},
            "take_profits": [{"orderId": f"tp-{i}"} for i, _ in enumerate(tps)],
        },
        "protection_verified": True,
    }
    position.update(extra)
    return position


def _live_payload(symbol: str = "BTCUSDT", size: float = 1.0, hold_side: str = "long", with_tpsl: bool = False) -> dict:
    payload = {
        "symbol": symbol,
        "total": size,
        "holdSide": hold_side,
        "averageOpenPrice": 100.0,
        "markPrice": 100.0,
    }
    if with_tpsl:
        payload["takeProfit"] = "101"
        payload["stopLoss"] = "99"
    return payload


def _snapshot(symbol: str = "BTCUSDT", price: float = 100.0, high: float | None = None, low: float | None = None) -> MagicMock:
    snapshot = MagicMock()
    snapshot.symbol = symbol
    snapshot.primary.latest_close = price
    candle = MagicMock()
    candle.high = high if high is not None else price
    candle.low = low if low is not None else price
    snapshot.primary.candles = [candle]
    snapshot.primary.trend = "bullish"
    snapshot.market = MagicMock()
    return snapshot


def _v2_close_rows() -> list[dict]:
    path = Path("logs/trade_dataset_v2.csv")
    if not path.exists():
        return []
    with path.open() as handle:
        return [r for r in csv.DictReader(handle) if (r.get("event_type") or "").upper() in ("CLOSE", "POSITION_CLOSED")]


# --- 1. TP1 hit -> SL exact naar fee-adjusted break-even ---

def test_tp1_hit_moves_sl_to_exact_fee_adjusted_break_even():
    manager = _manager([_live_payload(size=1.0)])
    manager.store.save([_position()])

    manager.sync([_snapshot(price=101.2, high=101.2, low=100.5)])

    saved = manager.store.load(default=[])[0]
    assert saved["tp1_hit"] is True
    expected_be = 100.0 * (1.0 + EFFECTIVE_BE_BUFFER_PCT / 100.0)
    assert saved["stop_loss"] == expected_be
    assert saved["break_even_active"] is True
    assert saved["remaining_size_pct"] == 100.0 - TP1_CLOSE_PCT
    # De near-TP-protectie mag een tussentijdse SL-move doen; de LAATSTE move
    # moet de exacte fee-adjusted BE zijn.
    sl_calls = manager.client.move_futures_stop_loss.call_args_list
    assert sl_calls, "expected at least one SL move"
    assert sl_calls[-1].kwargs["trigger_price"] == expected_be
    assert sl_calls[-1].kwargs["reason"] == "TP1_FEE_BE"


# --- 2. TP2 hit -> remaining size correct + SL naar TP1-lock ---

def test_tp2_hit_reduces_remaining_and_locks_tp1():
    manager = _manager([_live_payload(size=1.0)])
    manager.store.save([_position(tp1_hit=True, remaining_size_pct=60.0)])

    manager.sync([_snapshot(price=102.1, high=102.1, low=101.0)])

    saved = manager.store.load(default=[])[0]
    assert saved["tp2_hit"] is True
    assert saved["remaining_size_pct"] == 100.0 - TP1_CLOSE_PCT - TP2_CLOSE_PCT
    assert saved["stop_loss"] == 101.0  # TP1 lock
    assert saved["status"] == "OPEN"


# --- 3. TP3 hit -> close-all + local state CLOSED ---

def test_tp3_hit_closes_all_and_marks_closed():
    manager = _manager([_live_payload(size=0.3)])
    manager.store.save([
        _position(tp1_hit=True, tp2_hit=True, remaining_size_pct=30.0, break_even_active=True)
    ])

    manager.sync([_snapshot(price=103.5, high=103.5, low=102.5)])

    saved = manager.store.load(default=[])[0]
    assert saved["tp3_hit"] is True
    assert saved["status"] == "CLOSED"
    assert saved["closed_reason"] == "tp3"
    assert saved["remaining_size_pct"] == 0.0
    manager.client.close_futures_position_full.assert_called_once()
    assert len(_v2_close_rows()) == 1


# --- 4/5. Exchange zegt closed, local open -> recovery + geen ghost ---

def test_exchange_closed_local_open_reconciles_and_cleans_tpsl():
    manager = _manager(open_positions=[])  # exchange heeft NIETS open
    manager.store.save([_position()])

    manager.sync([_snapshot(price=100.0)])

    saved = manager.store.load(default=[])[0]
    assert saved["status"] == "CLOSED_SYNCED"
    manager.client.cancel_all_futures_tpsl_orders.assert_called_once()
    assert len(_v2_close_rows()) == 1
    # geen nieuwe orders/closes richting exchange
    manager.client.close_futures_position_full.assert_not_called()
    manager.client.move_futures_stop_loss.assert_not_called()


# --- 6. Exchange open, local onbekend -> safe recovery, nooit unprotected OPEN ---

def test_exchange_open_local_missing_recovers_and_never_leaves_unprotected_open():
    manager = _manager([_live_payload(symbol="ETHUSDT", size=2.0)])
    manager.store.save([])

    manager.sync([_snapshot(symbol="ETHUSDT", price=100.0)])

    saved = manager.store.load(default=[])
    assert len(saved) == 1
    assert saved[0]["symbol"] == "ETHUSDT"
    # Safety-contract: de recovered positie is OF beschermd OPEN, OF
    # veiligheidshalve gesloten — maar nooit onbeschermd OPEN blijven hangen.
    if saved[0]["status"] == "OPEN":
        assert saved[0].get("protection_verified") is True
    else:
        assert saved[0]["status"] in ("CLOSED", "CLOSED_SYNCED")
        assert saved[0].get("closed_reason") == "protection_repair_failed"
    events = manager.event_store.load(default=[])
    assert any(e.get("status") == "STATE_RECOVERED" for e in events)


# --- 7. Dubbele monitor-cycle: geen dubbele close/order/dataset-row ---

def test_double_cycle_is_idempotent_for_tp3_close():
    manager = _manager([_live_payload(size=0.3)])
    manager.store.save([
        _position(tp1_hit=True, tp2_hit=True, remaining_size_pct=30.0, break_even_active=True)
    ])

    snapshot = _snapshot(price=103.5, high=103.5, low=102.5)
    manager.sync([snapshot])
    manager.client.get_all_positions.return_value = {"data": []}  # exchange nu leeg
    manager.sync([snapshot])

    assert manager.client.close_futures_position_full.call_count == 1
    assert len(_v2_close_rows()) == 1


def test_double_cycle_is_idempotent_for_exchange_closed_sync():
    manager = _manager(open_positions=[])
    manager.store.save([_position()])

    manager.sync([_snapshot(price=100.0)])
    manager.sync([_snapshot(price=100.0)])

    assert manager.client.cancel_all_futures_tpsl_orders.call_count == 1
    assert len(_v2_close_rows()) == 1


# --- 8. Stale TPSL alleen cancellen bij betrouwbare exchange-state ---

def test_no_tpsl_cleanup_when_exchange_sync_failed():
    manager = _manager()
    manager.client.get_all_positions.side_effect = RuntimeError("Bitget 5xx")
    manager.store.save([_position()])

    updates = manager.sync([_snapshot(price=95.0, high=95.0, low=95.0)])  # onder de stop!

    saved = manager.store.load(default=[])[0]
    assert saved["status"] == "OPEN"  # freeze: niet lokaal sluiten
    manager.client.cancel_all_futures_tpsl_orders.assert_not_called()
    manager.client.close_futures_position_full.assert_not_called()
    manager.client.move_futures_stop_loss.assert_not_called()
    assert any("exchange sync failed" in (u.note or "") for u in updates)


# --- 9. Short en long symmetrisch ---

def test_short_tp1_hit_moves_sl_to_fee_adjusted_break_even_below_entry():
    manager = _manager([_live_payload(size=1.0, hold_side="short")])
    manager.store.save([
        _position(direction="SHORT", entry=100.0, stop=101.0, tps=(99.0, 98.0, 97.0))
    ])

    manager.sync([_snapshot(price=98.8, high=99.4, low=98.8)])

    saved = manager.store.load(default=[])[0]
    assert saved["tp1_hit"] is True
    expected_be = 100.0 * (1.0 - EFFECTIVE_BE_BUFFER_PCT / 100.0)
    assert saved["stop_loss"] == expected_be


def test_stop_predicates_are_symmetric():
    assert PositionManager._stop_hit_range("LONG", candle_high=100.0, candle_low=98.9, stop=99.0)
    assert PositionManager._stop_hit_range("SHORT", candle_high=101.1, candle_low=100.0, stop=101.0)
    assert not PositionManager._stop_hit_range("LONG", candle_high=100.0, candle_low=99.1, stop=99.0)
    assert not PositionManager._stop_hit_range("SHORT", candle_high=100.9, candle_low=100.0, stop=101.0)
    assert PositionManager._target_hit_range("LONG", candle_high=101.0, candle_low=100.0, target=101.0)
    assert PositionManager._target_hit_range("SHORT", candle_high=100.0, candle_low=99.0, target=99.0)


# --- 10. Lokale stop touch terwijl exchange nog open is -> GEEN autoclose ---

def test_local_stop_touch_with_exchange_still_open_does_not_close():
    manager = _manager([_live_payload(size=1.0, with_tpsl=True)])
    manager.store.save([_position()])

    manager.sync([_snapshot(price=98.5, high=99.5, low=98.5)])  # low onder stop 99

    saved = manager.store.load(default=[])[0]
    assert saved["status"] == "OPEN"  # exchange truth boven local truth
    manager.client.close_futures_position_full.assert_not_called()
    assert len(_v2_close_rows()) == 0


# --- 11. Profit-lock (P1.1A): 60% van TP1 bereikt -> SL naar fee-adjusted BE ---

def test_profit_lock_arms_at_60pct_of_tp1_long():
    manager = _manager([_live_payload(size=1.0)])
    manager.store.save([_position()])  # entry 100, tp1 101 -> 60% = 100.60

    manager.sync([_snapshot(price=100.65, high=100.65, low=100.2)])

    saved = manager.store.load(default=[])[0]
    assert saved["profit_lock_active"] is True
    assert not saved.get("tp1_hit")  # TP1 zelf niet geraakt
    expected_be = 100.0 * (1.0 + EFFECTIVE_BE_BUFFER_PCT / 100.0)
    assert saved["stop_loss"] == expected_be
    assert saved["break_even_active"] is True


def test_profit_lock_arms_symmetrically_for_short():
    manager = _manager([_live_payload(size=1.0, hold_side="short")])
    manager.store.save([
        _position(direction="SHORT", entry=100.0, stop=101.0, tps=(99.0, 98.0, 97.0))
    ])  # tp1-afstand 1.0 -> 60% = 99.40

    manager.sync([_snapshot(price=99.35, high=99.8, low=99.35)])

    saved = manager.store.load(default=[])[0]
    assert saved["profit_lock_active"] is True
    expected_be = 100.0 * (1.0 - EFFECTIVE_BE_BUFFER_PCT / 100.0)
    assert saved["stop_loss"] == expected_be


def test_profit_lock_does_not_arm_below_threshold():
    manager = _manager([_live_payload(size=1.0)])
    manager.store.save([_position()])

    manager.sync([_snapshot(price=100.40, high=100.45, low=100.1)])  # 40-45% van TP1

    saved = manager.store.load(default=[])[0]
    assert not saved.get("profit_lock_active")
    assert saved["stop_loss"] == 99.0


def test_profit_lock_never_loosens_a_tighter_stop():
    manager = _manager([_live_payload(size=1.0)])
    # stop staat al strakker dan BE (bv. door failed-continuation tighten)
    manager.store.save([_position(stop=100.5, break_even_active=False)])

    manager.sync([_snapshot(price=100.65, high=100.65, low=100.55)])

    saved = manager.store.load(default=[])[0]
    assert saved["stop_loss"] == 100.5  # niet terug naar 100.1 gezet
    assert not saved.get("profit_lock_active")


def test_profit_lock_arms_only_once():
    manager = _manager([_live_payload(size=1.0)])
    manager.store.save([_position()])

    manager.sync([_snapshot(price=100.65, high=100.65, low=100.2)])
    calls_after_first = manager.client.move_futures_stop_loss.call_count
    manager.sync([_snapshot(price=100.70, high=100.70, low=100.5)])

    assert manager.client.move_futures_stop_loss.call_count == calls_after_first


# --- 10b. SL-verplaatsing faalt -> lokale SL blijft op oude waarde (fail closed) ---

def test_failed_sl_move_keeps_previous_stop():
    manager = _manager([_live_payload(size=1.0)])
    manager.client.move_futures_stop_loss.side_effect = RuntimeError("Bitget 400")
    manager.store.save([_position()])

    manager.sync([_snapshot(price=101.2, high=101.2, low=100.5)])

    saved = manager.store.load(default=[])[0]
    assert saved["tp1_hit"] is True
    assert saved["stop_loss"] == 99.0  # niet stilletjes op BE gezet zonder exchange-bevestiging
    assert saved["protection_integrity"] == "FAILED"


# --- 11. Mislukte failed-continuation tighten wordt elke cyclus opnieuw geprobeerd ---


def test_failed_tighten_sets_pending_and_retries_next_cycle_without_detection():
    """Live 2026-07-07 (FILUSDT): eerste tighten faalde, retry kwam pas na 28
    minuten omdat de detectie-condities (pressure/near-TP) moesten her-alignen.
    De pending-vlag maakt de retry-intentie persistent: cyclus 2 moet de stop
    verplaatsen ZONDER dat de detectie opnieuw vuurt."""
    live = _live_payload(size=1.0)
    manager = _manager([live])

    position = _position(
        failed_continuation_tighten_pending=True,
        failed_continuation_protection_failed=True,
    )
    manager.store.save([position])

    # Detectie mag niet opnieuw vuren (pressure hersteld): patch should-check uit.
    manager._should_tighten_failed_continuation = lambda **kwargs: (False, 0.0, {"reason": "pressure_not_failed"})

    manager.sync([_snapshot(price=100.6)])  # in winst; target stop komt boven oude 99.0

    saved = manager.store.save.call_args[0][0] if hasattr(manager.store.save, "call_args") else manager.store.load(default=[])
    row = saved[0] if isinstance(saved, list) else position
    assert row["failed_continuation_protection_active"] is True
    assert row["failed_continuation_tighten_pending"] is False
    assert row["stop_loss"] > 99.0  # aangescherpt, tighter-only
    assert manager.client.move_futures_stop_loss.called or manager.client.verify_active_stop_loss.called


def test_pending_retry_never_loosens_stop():
    live = _live_payload(size=1.0)
    manager = _manager([live])

    # Stop staat al strak boven de retry-target: pending mag NIET verruimen.
    position = _position(
        stop=100.5,
        failed_continuation_tighten_pending=True,
    )
    manager.store.save([position])
    manager._should_tighten_failed_continuation = lambda **kwargs: (False, 0.0, {"reason": "pressure_not_failed"})

    manager.sync([_snapshot(price=100.2)])

    rows = manager.store.load(default=[])
    row = rows[0] if isinstance(rows, list) and rows else position
    assert float(row["stop_loss"]) >= 100.5


# --- 12. Dead-trade timeout ---


def _iso_minutes_ago(minutes: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def test_dead_flat_reclaim_trade_closes_after_timeout():
    live = _live_payload(size=1.0)
    manager = _manager([live])
    position = _position(
        strategy="low_vol_reclaim",
        opened_at=_iso_minutes_ago(120),  # > 90 min reclaim timeout
    )
    manager.store.save([position])

    manager.sync([_snapshot(price=100.05)])  # vlak: +0.05%

    rows = manager.store.load(default=[])
    row = rows[0]
    assert row["status"] == "CLOSED"
    assert row["closed_reason"] == "dead_trade_timeout"
    manager.client.close_futures_position_full.assert_called()


def test_young_flat_trade_is_left_alone():
    live = _live_payload(size=1.0)
    manager = _manager([live])
    position = _position(strategy="low_vol_reclaim", opened_at=_iso_minutes_ago(30))
    manager.store.save([position])

    manager.sync([_snapshot(price=100.05)])

    rows = manager.store.load(default=[])
    assert rows[0]["status"] == "OPEN"


def test_old_trade_in_profit_is_not_dead():
    live = _live_payload(size=1.0)
    manager = _manager([live])
    position = _position(strategy="low_vol_reclaim", opened_at=_iso_minutes_ago(120))
    manager.store.save([position])

    manager.sync([_snapshot(price=100.5)])  # +0.5% > 0.20 band -> protecties beheren dit

    rows = manager.store.load(default=[])
    assert rows[0]["status"] == "OPEN"
    assert rows[0].get("closed_reason") != "dead_trade_timeout"


def test_dead_timeout_requires_exchange_sync():
    manager = _manager([])
    manager.client.get_all_positions.side_effect = Exception("bitget down")
    position = _position(strategy="low_vol_reclaim", opened_at=_iso_minutes_ago(300))
    manager.store.save([position])

    manager.sync([_snapshot(price=100.05)])

    rows = manager.store.load(default=[])
    assert rows[0]["status"] == "OPEN"  # fail-safe: geen close zonder exchange truth
    manager.client.close_futures_position_full.assert_not_called()


# --- BE-floor: elke BE-actieve stop staat op >= fee-adjusted break-even ---

def test_near_tp_protection_locks_at_fee_be_not_smaller_buffer():
    # Reach ~90% of TP1 without hitting it; the near-TP lock must sit at the
    # fee-adjusted BE (>=0.16%), not the old standalone 0.08% (net-loss) buffer.
    manager = _manager([_live_payload(size=1.0)])
    pos = _position(stop=99.0)
    pos["max_favorable_excursion_pct"] = 0.9  # reached 90% of the 1% TP1 distance
    manager.store.save([pos])
    manager.settings.profit_lock_tp1_fraction = 0.99  # isolate: profit-lock won't arm yet

    manager.sync([_snapshot(price=100.50, high=100.90, low=100.40)])

    saved = manager.store.load(default=[])[0]
    expected_min_be = 100.0 * (1.0 + EFFECTIVE_BE_BUFFER_PCT / 100.0)  # 100.16
    assert saved["stop_loss"] >= expected_min_be - 1e-9, (
        f"near-TP lock op {saved['stop_loss']}, moet >= fee-BE {expected_min_be}"
    )


def test_profit_lock_raises_below_be_stop_even_when_be_flag_set():
    # The ATOM situation: break_even_active already True but the stop is parked
    # below entry. The profit-lock must still raise it to the fee-adjusted BE.
    manager = _manager([_live_payload(size=1.0)])
    pos = _position(stop=99.98, break_even_active=True)  # below entry 100
    pos["max_favorable_excursion_pct"] = 0.9  # reached 90% of the 1% TP1 distance
    manager.store.save([pos])

    manager.sync([_snapshot(price=100.50, high=100.60, low=100.45)])

    saved = manager.store.load(default=[])[0]
    expected_be = 100.0 * (1.0 + EFFECTIVE_BE_BUFFER_PCT / 100.0)
    assert saved["stop_loss"] >= expected_be - 1e-9, (
        f"stop {saved['stop_loss']} niet opgetild naar fee-BE {expected_be} ondanks be_active"
    )


# --- avg_entry reconciliation: BE must use the REAL fill, not the planned price ---

def test_avg_entry_reconciled_to_real_fill_and_be_raised_above_true_breakeven():
    # Recorded avg_entry is the planned 99.8, but the real fill (exchange
    # averageOpenPrice) is 100.0. A BE computed from 99.8 would sit BELOW the
    # true break-even. After reconcile, BE is computed from 100.0 and the stop
    # is raised to >= 100.0 * (1 + 0.16%).
    manager = _manager([_live_payload(size=1.0)])  # averageOpenPrice = 100.0
    pos = _position(entry=99.8, stop=99.5)
    pos["actual_entry"] = 100.0
    pos["max_favorable_excursion_pct"] = 0.9
    pos["break_even_active"] = True
    pos["profit_lock_active"] = True  # already armed with the WRONG entry
    manager.store.save([pos])

    manager.sync([_snapshot(price=100.50, high=100.60, low=100.45)])

    saved = manager.store.load(default=[])[0]
    assert abs(saved["avg_entry"] - 100.0) < 1e-6, "avg_entry niet gereconcilieerd naar echte fill"
    expected_be = 100.0 * (1.0 + EFFECTIVE_BE_BUFFER_PCT / 100.0)
    assert saved["stop_loss"] >= expected_be - 1e-9, (
        f"stop {saved['stop_loss']} onder de echte break-even {expected_be}"
    )


def test_correct_avg_entry_not_reconciled():
    # avg_entry already matches the real fill -> no reconcile, no spurious change.
    manager = _manager([_live_payload(size=1.0)])  # averageOpenPrice = 100.0
    pos = _position(entry=100.0, stop=99.5)
    pos["actual_entry"] = 100.0
    manager.store.save([pos])

    manager.sync([_snapshot(price=100.10, high=100.15, low=100.05)])

    saved = manager.store.load(default=[])[0]
    assert abs(saved["avg_entry"] - 100.0) < 1e-6
