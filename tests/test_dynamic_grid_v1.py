from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from execution.dynamic_grid_service import DynamicGridService
from execution.dynamic_grid_shadow import DynamicGridShadow
from execution.state_store import JsonStateStore
from strategies.dynamic_grid_v1 import GridRegime, build_grid_decision, reset_allowed


def _settings(**overrides):
    values = dict(
        dynamic_grid_min_atr_bps=5.0,
        dynamic_grid_max_atr_bps=150.0,
        dynamic_grid_max_spread_bps=5.0,
        dynamic_grid_max_trend_bps=45.0,
        dynamic_grid_min_depth_usdt=100_000.0,
        dynamic_grid_min_score=60.0,
        dynamic_grid_drag_bps=1.0,
        dynamic_grid_edge_margin_bps=2.0,
        dynamic_grid_max_notional_usdt=30.0,
        dynamic_grid_max_equity_pct=3.0,
        dynamic_grid_max_equity_risk_pct=0.25,
        dynamic_grid_min_level_notional_usdt=5.0,
        dynamic_grid_max_level_notional_usdt=10.0,
        dynamic_grid_hard_invalidation_atr=3.0,
        dynamic_grid_reset_atr=0.75,
        dynamic_grid_reset_cooldown_minutes=30,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _candles(*, count=80, start=100.0, step=0.0, swing=0.10):
    rows = []
    for index in range(count):
        close = start + (step * index) + (swing if index % 2 else -swing)
        rows.append({
            "timestamp": 1_700_000_000_000 + index * 300_000,
            "open": close - 0.02,
            "high": close + 0.10,
            "low": close - 0.10,
            "close": close,
            "volume": 1_000.0 + index,
        })
    return rows


def _decision(**overrides):
    params = dict(
        symbol="BTCUSDT",
        candles_5m=_candles(),
        candles_15m=_candles(),
        candles_1h=_candles(),
        orderbook={"spread_bps": 1.0, "total_depth_notional": 1_000_000.0},
        maker_fee_rate=0.0002,
        equity_usdt=1_000.0,
        settings=_settings(),
        stale=False,
    )
    params.update(overrides)
    return build_grid_decision(**params)


def test_grid_has_exactly_three_descending_equal_notional_levels_and_one_x_geometry():
    decision = _decision()
    assert decision.regime is GridRegime.ALLOWED
    assert len(decision.levels) == 3
    assert decision.center > decision.levels[0].entry_price > decision.levels[1].entry_price > decision.levels[2].entry_price
    assert {round(level.notional_usdt, 8) for level in decision.levels} == {10.0}
    assert all(level.take_profit_price > level.entry_price for level in decision.levels)
    assert decision.hard_invalidation < decision.levels[-1].entry_price


def test_fee_hurdle_uses_authenticated_maker_roundtrip_plus_drag_and_margin():
    decision = _decision(maker_fee_rate=0.0002)
    economics = decision.economics
    assert economics.maker_fee_bps == pytest.approx(2.0)
    assert economics.roundtrip_fee_bps == pytest.approx(4.0)
    assert economics.hurdle_bps == pytest.approx(7.0)
    assert economics.expected_net_capture_bps > 0
    assert economics.gross_capture_bps > economics.hurdle_bps


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"stale": True}, GridRegime.PAUSED_VOLATILITY),
        ({"orderbook": {"spread_bps": 8.0, "total_depth_notional": 1_000_000.0}}, GridRegime.PAUSED_SPREAD),
        ({"candles_1h": _candles(step=0.20, swing=0.0)}, GridRegime.PAUSED_TREND),
    ],
)
def test_regime_kill_switches(updates, expected):
    assert _decision(**updates).regime is expected


def test_caps_can_make_exchange_minimum_impractical_and_pause():
    decision = _decision(equity_usdt=100.0, settings=_settings(dynamic_grid_min_level_notional_usdt=5.0))
    assert decision.regime is GridRegime.PAUSED_VOLATILITY
    assert decision.reason == "minimum_practical_size_exceeds_cap"


def test_reset_requires_flat_resolved_cooldown_and_material_center_move():
    settings = _settings()
    base = dict(old_center=100.0, new_center=101.0, atr_value=1.0, minutes_since_reset=31, settings=settings)
    assert reset_allowed(**base, flat=True, working_orders=0)
    assert not reset_allowed(**base, flat=False, working_orders=0)
    assert not reset_allowed(**base, flat=True, working_orders=1)
    assert not reset_allowed(**{**base, "minutes_since_reset": 5}, flat=True, working_orders=0)


class _ShadowClient:
    def __init__(self):
        self.order_calls = 0

    def get_trade_fee_rate(self, symbol, business_type):
        assert business_type == "mix"
        return {"data": {"makerFeeRate": "0.0002", "takerFeeRate": "0.0006"}}

    def get_orderbook(self, symbol, limit):
        return {"spread_bps": 1.0, "total_depth_notional": 1_000_000.0}

    def __getattr__(self, name):
        if name.startswith(("place_", "cancel_", "close_", "set_futures_leverage")):
            self.order_calls += 1
            raise AssertionError(f"shadow attempted order mutation: {name}")
        raise AttributeError(name)


class _Cache:
    def get(self, symbol, timeframe):
        return _candles()

    def is_stale(self, symbol, timeframe):
        return False


def test_shadow_queries_account_fees_but_makes_no_order_calls_and_emits_dashboard_jsonl(tmp_path):
    client = _ShadowClient()
    settings = Settings(
        _env_file=None,
        DYNAMIC_GRID_ENABLED=True,
        DYNAMIC_GRID_MODE="SHADOW",
        DYNAMIC_GRID_MIN_SCORE=60,
        DYNAMIC_GRID_STATE_PATH=str(tmp_path / "state.json"),
        DYNAMIC_GRID_SHADOW_STATE_PATH=str(tmp_path / "shadow.json"),
        DYNAMIC_GRID_EVENTS_PATH=str(tmp_path / "events.jsonl"),
    )
    service = DynamicGridService(settings=settings, client=client, cache=_Cache())
    decisions = service.cycle(equity_usdt=1_000.0)
    assert {decision.symbol for decision in decisions} == {"BTCUSDT", "SOLUSDT"}
    assert client.order_calls == 0
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert {row["event_type"] for row in rows} >= {
        "FEE_RATE_AUTHENTICATED", "GRID_DECISION", "GRID_SELECTION"
    }
    decisions_rows = [row for row in rows if row["event_type"] == "GRID_DECISION"]
    assert all(row["strategy"] == "dynamic_grid_v1" for row in decisions_rows)
    assert all(len(row["levels"]) == 3 for row in decisions_rows)
    assert all("expected_net_capture_bps" in row["economics"] for row in decisions_rows)
    assert {row["event_type"] for row in rows} & {
        "SHADOW_GRID_OPENED", "SHADOW_LEVEL_FILLED"
    }


def test_shadow_maps_fill_to_tp_then_resolves_without_an_order_client(tmp_path):
    events = []
    settings = _settings(dynamic_grid_reset_cooldown_minutes=0)
    shadow = DynamicGridShadow(
        settings=settings,
        store=JsonStateStore(str(tmp_path / "shadow.json")),
        emit=lambda event_type, **details: events.append({"event_type": event_type, **details}),
    )
    decision = _decision()
    first = dict(_candles()[-1])
    first["low"] = decision.levels[0].entry_price - 0.01
    first["high"] = decision.levels[0].entry_price
    shadow.process(
        decisions=[decision], selected=decision,
        candles_by_symbol={"BTCUSDT": [first]},
    )
    assert any(row["event_type"] == "SHADOW_LEVEL_FILLED" for row in events)

    second = dict(first)
    second["timestamp"] += 300_000
    second["low"] = decision.levels[0].entry_price
    second["high"] = decision.levels[0].take_profit_price + 0.01
    shadow.process(
        decisions=[decision], selected=decision,
        candles_by_symbol={"BTCUSDT": [second]},
    )
    tp = next(row for row in events if row["event_type"] == "SHADOW_TP_HIT")
    assert tp["level"] == 1
    assert tp["entry_price"] == decision.levels[0].entry_price
    assert tp["exit_price"] == decision.levels[0].take_profit_price
    assert tp["inventory_after"] < tp["inventory_before"]


def test_shadow_emits_trend_pause_and_hard_kill_events(tmp_path):
    events = []
    settings = _settings(dynamic_grid_reset_cooldown_minutes=0)
    shadow = DynamicGridShadow(
        settings=settings,
        store=JsonStateStore(str(tmp_path / "shadow.json")),
        emit=lambda event_type, **details: events.append({"event_type": event_type, **details}),
    )
    allowed = _decision()
    fill_candle = dict(_candles()[-1])
    fill_candle["low"] = allowed.levels[0].entry_price - 0.01
    fill_candle["high"] = allowed.levels[0].entry_price
    shadow.process(
        decisions=[allowed], selected=allowed,
        candles_by_symbol={"BTCUSDT": [fill_candle]},
    )
    paused = _decision(candles_1h=_candles(step=0.20, swing=0.0))
    kill_candle = dict(fill_candle)
    kill_candle["timestamp"] += 300_000
    kill_candle["low"] = allowed.hard_invalidation - 0.01
    shadow.process(
        decisions=[paused], selected=None,
        candles_by_symbol={"BTCUSDT": [kill_candle]},
    )
    kinds = {row["event_type"] for row in events}
    assert "SHADOW_KILL_SWITCH" in kinds
    assert "SHADOW_HARD_KILL" in kinds


def test_live_config_is_frozen_and_fails_closed_on_unsafe_variants():
    with pytest.raises(ValueError, match="exactly 3 levels"):
        Settings(
            _env_file=None, DYNAMIC_GRID_ENABLED=True, DYNAMIC_GRID_MODE="SHADOW",
            DYNAMIC_GRID_LEVELS=4,
        )


@pytest.mark.parametrize(
    "state,reason",
    [
        ({"max_drawdown_usdt": 6.0}, "dynamic_grid_max_drawdown_reached"),
        ({"order_error_count": 3}, "dynamic_grid_repeated_order_errors"),
    ],
)
def test_strategy_drawdown_and_repeated_order_errors_stop_shadow_selection(
    tmp_path, state, reason
):
    client = _ShadowClient()
    settings = Settings(
        _env_file=None, DYNAMIC_GRID_ENABLED=True, DYNAMIC_GRID_MODE="SHADOW",
        DYNAMIC_GRID_MIN_SCORE=60,
        DYNAMIC_GRID_STATE_PATH=str(tmp_path / "state.json"),
        DYNAMIC_GRID_SHADOW_STATE_PATH=str(tmp_path / "shadow.json"),
        DYNAMIC_GRID_EVENTS_PATH=str(tmp_path / "events.jsonl"),
    )
    service = DynamicGridService(settings=settings, client=client, cache=_Cache())
    service.store.save(state)
    service.cycle(equity_usdt=1_000.0)
    rows = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert any(row.get("reason") == reason for row in rows)
    selection = next(row for row in rows if row["event_type"] == "GRID_SELECTION")
    assert selection["selected_symbol"] == ""
    assert client.order_calls == 0
    with pytest.raises(ValueError, match="global LIVE execution gate"):
        Settings(
            _env_file=None, DYNAMIC_GRID_ENABLED=True, DYNAMIC_GRID_MODE="LIVE",
        )


def test_duplicate_or_ambiguous_client_oid_is_never_resubmitted(tmp_path):
    settings = Settings(
        _env_file=None, DYNAMIC_GRID_ENABLED=True, DYNAMIC_GRID_MODE="SHADOW",
        DYNAMIC_GRID_STATE_PATH=str(tmp_path / "state.json"),
        DYNAMIC_GRID_EVENTS_PATH=str(tmp_path / "events.jsonl"),
    )
    client = _ShadowClient()
    blocked = SimpleNamespace(
        status="BLOCKED_UNKNOWN", classification="UNKNOWN", order_id="",
        client_oid="bgai-k-ambiguous",
    )
    submitter = SimpleNamespace(submit_entry=lambda **kwargs: blocked)
    service = DynamicGridService(
        settings=settings, client=client, cache=_Cache(), entry_submitter=submitter
    )
    decision = _decision()
    level = decision.levels[0]
    with pytest.raises(RuntimeError, match="BLOCKED_UNKNOWN"):
        service._submit_entry_once(decision, level)
    assert client.order_calls == 0


class _LiveClient(_ShadowClient):
    def __init__(self):
        super().__init__()
        self.entries = []
        self.leverages = []
        self.cancels = []
        self.closes = []
        self.positions = []

    def get_all_positions(self):
        return {"data": list(self.positions)}

    def set_futures_leverage(self, **kwargs):
        self.leverages.append(kwargs)
        return {"data": {}}

    def place_futures_limit_order(self, **kwargs):
        self.entries.append(kwargs)
        return {"data": {"orderId": f"order-{len(self.entries)}"}}

    def find_order_by_client_oid(self, symbol, client_oid):
        return {"status": "FOUND", "order": {
            "clientOid": client_oid, "orderId": client_oid, "state": "live",
        }}

    def cancel_futures_order(self, **kwargs):
        self.cancels.append(kwargs)
        return {"data": {}}

    def close_futures_position_full(self, **kwargs):
        self.closes.append(kwargs)
        return {"status": "CLOSED", "data": {"orderId": "emergency-close"}}

    def extract_fill_metrics(self, payload):
        row = payload.get("data") or {}
        return {
            "filled_qty": float(row.get("baseVolume") or 0.0),
            "avg_price": float(row.get("avgPrice") or 0.0),
            "fee": float(row.get("fee") or 0.0),
            "state": str(row.get("state") or ""),
            "raw": row,
        }


class _LiveSubmitter:
    def __init__(self):
        self.closed = []

    def client_oid_for(self, plan, leg):
        return f"safe-{plan.strategy}-{leg}"

    def submit_entry(self, *, plan, place, **kwargs):
        oid = self.client_oid_for(plan, "maker")
        payload = place(oid)
        return SimpleNamespace(
            status="ACCEPTED", classification="ACCEPTED",
            order_id=payload["data"]["orderId"], client_oid=oid,
        )

    def mark_closed_out(self, client_oid, *, reason):
        self.closed.append((client_oid, reason))


def test_live_opens_one_three_level_grid_once_with_persisted_lineage(tmp_path):
    client = _LiveClient()
    submitter = _LiveSubmitter()
    settings = Settings(
        _env_file=None, DYNAMIC_GRID_ENABLED=True, DYNAMIC_GRID_MODE="SHADOW",
        DYNAMIC_GRID_STATE_PATH=str(tmp_path / "state.json"),
        DYNAMIC_GRID_EVENTS_PATH=str(tmp_path / "events.jsonl"),
    )
    service = DynamicGridService(
        settings=settings, client=client, cache=_Cache(), entry_submitter=submitter
    )
    decision = _decision()
    service._live_cycle(decision)
    assert len(client.entries) == 3
    assert all(row["post_only"] is True for row in client.entries)
    assert client.leverages == [{
        "symbol": "BTCUSDT", "leverage": 1, "hold_side": "long", "margin_mode": "isolated"
    }]
    active = service.store.load({})["active_grid"]
    assert active["symbol"] == "BTCUSDT"
    assert len(active["levels"]) == 3
    assert all(level["entry_client_oid"].startswith("safe-dynamic_grid_v1_level_") for level in active["levels"])

    service._live_cycle(decision)
    assert len(client.entries) == 3, "a second cycle must not duplicate entry orders"


def test_hard_invalidation_cancels_known_orders_then_uses_emergency_market_close(tmp_path):
    client = _LiveClient()
    submitter = _LiveSubmitter()
    settings = Settings(
        _env_file=None, DYNAMIC_GRID_ENABLED=True, DYNAMIC_GRID_MODE="SHADOW",
        DYNAMIC_GRID_STATE_PATH=str(tmp_path / "state.json"),
        DYNAMIC_GRID_EVENTS_PATH=str(tmp_path / "events.jsonl"),
    )
    service = DynamicGridService(
        settings=settings, client=client, cache=_Cache(), entry_submitter=submitter
    )
    decision = _decision()
    service._live_cycle(decision)
    client.positions = [{
        "symbol": "BTCUSDT", "holdSide": "long", "total": "0.2",
        "markPrice": str(decision.hard_invalidation - 0.01),
    }]
    service._live_cycle(decision)
    assert len(client.cancels) == 6
    assert len(client.closes) == 1
    assert client.closes[0]["direction"] == "LONG"
    assert len(submitter.closed) == 3


def test_completed_live_cycle_emits_requested_per_level_and_rolling_economics(tmp_path):
    client = _LiveClient()
    submitter = _LiveSubmitter()
    settings = Settings(
        _env_file=None, DYNAMIC_GRID_ENABLED=True, DYNAMIC_GRID_MODE="SHADOW",
        DYNAMIC_GRID_STATE_PATH=str(tmp_path / "state.json"),
        DYNAMIC_GRID_SHADOW_STATE_PATH=str(tmp_path / "shadow.json"),
        DYNAMIC_GRID_EVENTS_PATH=str(tmp_path / "events.jsonl"),
    )
    service = DynamicGridService(
        settings=settings, client=client, cache=_Cache(), entry_submitter=submitter
    )
    decision = _decision()
    levels = []
    order_rows = {}
    for level in decision.levels:
        entry_oid = f"entry-{level.index}"
        tp_oid = f"tp-{level.index}"
        levels.append({
            "index": level.index, "entry_client_oid": entry_oid,
            "tp_client_oid": tp_oid, "entry_order_id": entry_oid,
            "entry_price": level.entry_price, "tp_price": level.take_profit_price,
            "quantity": level.quantity, "notional_usdt": level.notional_usdt,
            "entry_submitted": True, "entry_resolved": True, "tp_submitted": True,
            "filled_at_ms": decision.candle_timestamp_ms - 600_000,
            "mae_bps": -5.0, "mfe_bps": 20.0,
        })
        order_rows[entry_oid] = {
            "clientOid": entry_oid, "state": "filled", "baseVolume": level.quantity,
            "avgPrice": level.entry_price, "fee": "0.002", "uTime": decision.candle_timestamp_ms - 600_000,
        }
        order_rows[tp_oid] = {
            "clientOid": tp_oid, "state": "filled", "baseVolume": level.quantity,
            "avgPrice": level.take_profit_price, "fee": "0.002", "uTime": decision.candle_timestamp_ms,
        }
    client.find_order_by_client_oid = lambda symbol, client_oid: {
        "status": "FOUND", "order": order_rows[client_oid]
    }
    service.store.save({"active_grid": {
        "strategy": "dynamic_grid_v1", "symbol": "BTCUSDT",
        "center": decision.center, "hard_invalidation": decision.hard_invalidation,
        "created_at": "2026-08-10T00:00:00+00:00", "levels": levels,
        "reset_count": 0,
    }})
    service._live_cycle(decision)
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    closes = [row for row in events if row["event_type"] == "GRID_LEVEL_CLOSED"]
    assert len(closes) == 3
    required = {
        "symbol", "level", "entry", "exit", "gross_bps", "maker_fees_usdt",
        "taker_fees_usdt", "execution_drag_bps", "funding_usdt", "net_bps",
        "hold_time_minutes", "mae_bps", "mfe_bps", "inventory_before",
        "inventory_after",
    }
    assert all(required <= row.keys() for row in closes)
    cycle = next(row for row in events if row["event_type"] == "GRID_CYCLE_CLOSED")
    assert cycle["gross_capture_bps"] > cycle["net_capture_bps"]
    assert cycle["maker_fees_usdt"] > 0


def test_cycle_funding_uses_unambiguous_position_history_match(tmp_path, monkeypatch):
    client = _LiveClient()
    client.get_position_history = lambda **kwargs: {"data": {"list": [{"id": "p1"}]}}
    settings = Settings(
        _env_file=None, DYNAMIC_GRID_ENABLED=True, DYNAMIC_GRID_MODE="SHADOW",
        DYNAMIC_GRID_STATE_PATH=str(tmp_path / "state.json"),
        DYNAMIC_GRID_SHADOW_STATE_PATH=str(tmp_path / "shadow.json"),
        DYNAMIC_GRID_EVENTS_PATH=str(tmp_path / "events.jsonl"),
    )
    service = DynamicGridService(settings=settings, client=client, cache=_Cache())
    monkeypatch.setattr("execution.close_reconciler.match_lifecycle", lambda rows, **kwargs: rows[0])
    monkeypatch.setattr(
        "execution.close_reconciler.economics_from_history",
        lambda row: SimpleNamespace(
            funding=-0.0123, gross_pnl=0.1, fees=0.01, net_profit=0.0777
        ),
    )
    funding, source = service._cycle_funding_truth(
        active={"created_at": "2026-08-10T00:00:00+00:00"},
        symbol="BTCUSDT", confirmed_size=0.001,
    )
    assert funding == pytest.approx(-0.0123)
    assert source == "bitget_position_history"
