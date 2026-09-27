"""Position ledger and cost-adjusted break-even (pure)."""
from __future__ import annotations

from dataclasses import dataclass

from lsrl.core.rounding import round_to_tick
from lsrl.core.types import Direction


@dataclass(slots=True)
class Ledger:
    direction: Direction
    entry_qty: float = 0.0
    entry_notional: float = 0.0
    entry_fees: float = 0.0
    exit_qty: float = 0.0
    exit_gross: float = 0.0
    exit_fees: float = 0.0

    @property
    def avg_entry(self) -> float:
        return self.entry_notional / self.entry_qty if self.entry_qty > 0 else 0.0

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.entry_qty - self.exit_qty)

    def on_entry(self, price: float, qty: float, fee: float) -> None:
        self.entry_qty += qty
        self.entry_notional += price * qty
        self.entry_fees += fee

    def on_exit(self, price: float, qty: float, fee: float) -> None:
        self.exit_gross += self.direction.sgn * (price - self.avg_entry) * qty
        self.exit_qty += qty
        self.exit_fees += fee

    def realized_net(self) -> float:
        """Realized PnL net of fees. Entry fees are allocated pro-rata to exited size."""
        if self.entry_qty <= 0:
            return 0.0
        exited_share = min(1.0, self.exit_qty / self.entry_qty)
        return self.exit_gross - self.exit_fees - self.entry_fees * exited_share

    def total_fees(self) -> float:
        return self.entry_fees + self.exit_fees


def cost_adjusted_break_even(ledger: Ledger, tick_size: float, *, taker_fee_rate: float,
                             stop_slippage_bps: float) -> float | None:
    """Weighted-average ACTUAL fill price plus estimated realized+unrealized trade costs.

    costs = entry fees paid + exit fees paid so far + estimated exit fee and
    slippage on the remaining size; spread over the remaining units. Rounded so
    the stop is never on the wrong side of break-even (LONG up, SHORT down)."""
    remaining = ledger.remaining_qty
    avg = ledger.avg_entry
    if remaining <= 0 or avg <= 0:
        return None
    est_exit_fee = remaining * avg * taker_fee_rate
    est_slippage = remaining * avg * stop_slippage_bps / 10_000.0
    per_unit = (ledger.entry_fees + ledger.exit_fees + est_exit_fee + est_slippage) / remaining
    raw = avg + ledger.direction.sgn * per_unit
    return round_to_tick(raw, tick_size, "up" if ledger.direction is Direction.LONG else "down")
