"""The single active LSRL setup and its observability record (pure)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lsrl.core.lifecycle import Ledger
from lsrl.core.types import Direction

STATE_AWAIT_RECLAIM = "AWAIT_RECLAIM"
STATE_LADDER_PENDING = "LADDER_PENDING"
STATE_IN_POSITION = "IN_POSITION"
STATE_CLOSED = "CLOSED"

RUNG_PLANNED = "PLANNED"
RUNG_OPEN = "OPEN"
RUNG_FILLED = "FILLED"
RUNG_CANCELLED = "CANCELLED"
RUNG_REJECTED = "REJECTED"
RUNG_SKIPPED = "SKIPPED"


@dataclass(slots=True)
class RungState:
    rung: int
    tag: str
    price: float
    qty: float
    status: str = RUNG_PLANNED
    filled_qty: float = 0.0
    reject_reason: str = ""

    def live(self) -> bool:
        return self.status in (RUNG_PLANNED, RUNG_OPEN)

    def to_state(self) -> dict[str, Any]:
        return {"rung": self.rung, "tag": self.tag, "price": self.price, "qty": self.qty,
                "status": self.status, "filled_qty": self.filled_qty,
                "reject_reason": self.reject_reason}

    @classmethod
    def from_state(cls, d: dict[str, Any]) -> "RungState":
        return cls(**d)


@dataclass(slots=True)
class Setup:
    setup_id: str
    direction: Direction
    symbol: str
    state: str
    candidate_t_ms: int
    pool: dict[str, Any]
    pool_id: str
    pool_first_seen_ms: int
    pool_frozen_t_ms: int
    pool_frozen_mid: float
    pool_age_s_at_freeze: float
    pool_in_range_since_ms: int | None
    boundary: float
    interaction_start_ms: int
    interaction_source: str
    sweep_bar_open_ts: int
    sweep_t_ms: int
    extreme: float
    consumption: dict[str, Any]
    depth_series: list[list[float]]
    tape_trades: int
    tape_consumed: float
    bars_after_sweep: int = 0
    reclaim_bar_open_ts: int | None = None
    reclaim_t_ms: int | None = None
    atr15: float | None = None
    atr1m: float | None = None
    spread_bps: float | None = None
    geometry: dict[str, Any] | None = None
    sizing: dict[str, Any] | None = None
    targets: list[dict[str, Any]] | None = None
    expected_rr_tp2: float | None = None
    risk_gate: dict[str, Any] | None = None
    absorption: dict[str, Any] | None = None
    rungs: dict[int, RungState] = field(default_factory=dict)
    entry_expires_ms: int | None = None
    entries_cancelled: bool = False
    ledger: Ledger | None = None
    stop_price: float | None = None
    planned_loss_usdt: float = 0.0
    tp_orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    tp_done: list[str] = field(default_factory=list)
    entry_slippage_usdt: float = 0.0
    stop_slippage_usdt: float = 0.0
    events: list[dict[str, Any]] = field(default_factory=list)
    close_reason: str = ""
    realized_net_usdt: float | None = None
    realized_r: float | None = None

    # ------------------------------------------------------------- helpers
    def event(self, t_ms: int, kind: str, **data: Any) -> dict[str, Any]:
        row = {"t_ms": t_ms, "kind": kind, **data}
        self.events.append(row)
        return row

    def live_rungs(self) -> list[RungState]:
        return [r for r in self.rungs.values() if r.live()]

    # ------------------------------------------------------------- persistence
    def to_state(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in self.__dataclass_fields__:                      # type: ignore[attr-defined]
            value = getattr(self, name)
            if name == "direction":
                value = value.value
            elif name == "rungs":
                value = {str(k): v.to_state() for k, v in value.items()}
            elif name == "ledger":
                value = None if value is None else {
                    "direction": value.direction.value, "entry_qty": value.entry_qty,
                    "entry_notional": value.entry_notional, "entry_fees": value.entry_fees,
                    "exit_qty": value.exit_qty, "exit_gross": value.exit_gross,
                    "exit_fees": value.exit_fees}
            out[name] = value
        return out

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "Setup":
        body = dict(state)
        body["direction"] = Direction(body["direction"])
        body["rungs"] = {int(k): RungState.from_state(v) for k, v in body["rungs"].items()}
        led = body["ledger"]
        body["ledger"] = None if led is None else Ledger(
            direction=Direction(led["direction"]), entry_qty=led["entry_qty"],
            entry_notional=led["entry_notional"], entry_fees=led["entry_fees"],
            exit_qty=led["exit_qty"], exit_gross=led["exit_gross"], exit_fees=led["exit_fees"])
        return cls(**body)

    # ------------------------------------------------------------- observability
    def to_record(self, *, stage: str, reason: str, t_ms: int) -> dict[str, Any]:
        """Full per-setup observability row (accepted OR rejected)."""
        led = self.ledger
        return {
            "stage": stage,
            "reason": reason,
            "t_ms": t_ms,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "direction": self.direction.value,
            "pool_id": self.pool_id,
            "pool_frozen": self.pool,
            "pool_first_seen_ms": self.pool_first_seen_ms,
            "pool_age_s_at_freeze": self.pool_age_s_at_freeze,
            "pool_in_range_since_ms": self.pool_in_range_since_ms,
            "pool_strength": self.pool.get("strength"),
            "pool_depth_series": self.depth_series,
            "interaction_start_ms": self.interaction_start_ms,
            "interaction_source": self.interaction_source,
            "sweep_t_ms": self.sweep_t_ms,
            "sweep_bar_open_ts": self.sweep_bar_open_ts,
            "sweep_extreme": self.extreme,
            "consumption": self.consumption,
            "reclaim_t_ms": self.reclaim_t_ms,
            "reclaim_bar_open_ts": self.reclaim_bar_open_ts,
            "bars_after_sweep": self.bars_after_sweep,
            "atr15": self.atr15,
            "atr1m": self.atr1m,
            "spread_bps": self.spread_bps,
            "geometry": self.geometry,
            "sizing": self.sizing,
            "targets": self.targets,
            "expected_rr_tp2": self.expected_rr_tp2,
            "risk_gate": self.risk_gate,
            "absorption": self.absorption,
            "rungs": {str(k): v.to_state() for k, v in self.rungs.items()},
            "actual": None if led is None else {
                "entry_qty": led.entry_qty, "avg_entry": led.avg_entry,
                "entry_fees_usdt": led.entry_fees, "exit_qty": led.exit_qty,
                "exit_fees_usdt": led.exit_fees, "remaining_qty": led.remaining_qty,
                "entry_slippage_usdt": self.entry_slippage_usdt,
                "stop_slippage_usdt": self.stop_slippage_usdt,
            },
            "lifecycle_events": self.events,
            "close_reason": self.close_reason,
            "realized_net_usdt": self.realized_net_usdt,
            "realized_r": self.realized_r,
        }
