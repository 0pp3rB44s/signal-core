"""Restart-safe ATR trailing-stop sync for OPEN adaptive_trend_tsmom_v1 positions.

Mirrors the isolation contract established for the rest of this rollout:
AdaptiveTrend positions never touch any MicroFlow-specific mechanism
(selector, expectancy kill-switch, TP1 guard, wick penalty, follow-through
logic) and MicroFlow positions never touch this path. PositionManager.sync()
dispatches here for `strategy == STRATEGY_VERSION` positions and skips the
MicroFlow branch entirely for them.

Restart safety, by construction (see strategies/adaptive_trend_trail.py):
the ratchet always continues from the EXCHANGE's own reported stop, read
fresh from the live position payload every call via
`_extract_live_protection_payload`, never from a locally-cached value. The
only local state that must survive a restart is `last_processed_close_ms`,
a plain integer written back to the position record only AFTER the
exchange confirms the new stop -- never before, so a crash between the
exchange call and the local write simply re-evaluates the same candle next
cycle (idempotent no-op, proven by NO_NEW_CANDLE/UNCHANGED outcomes).
"""

from __future__ import annotations

from datetime import datetime, timezone

from clients.schemas import PositionUpdate
from strategies.adaptive_trend_runtime import fetch_6h_candles
from strategies.adaptive_trend_trail import TrailOutcome, evaluate_trail
from strategies.adaptive_trend_tsmom import STRATEGY_VERSION, Side


class AdaptiveTrendSyncMixin:
    def _sync_adaptive_trend_position(
        self,
        position: dict,
        *,
        bitget_sync_ok: bool,
        bitget_open_symbols: set,
        positions_live: list[dict],
        price_map: dict,
        updates: list,
        events: list,
    ) -> None:
        symbol = position["symbol"]
        current_price = float(price_map.get(symbol, position.get("last_price") or 0.0))
        position["last_price"] = current_price

        if not bitget_sync_ok or symbol not in bitget_open_symbols:
            # No trustworthy live snapshot this cycle -- do not touch the
            # exchange stop on stale/unavailable data. Preserve local state
            # and emit an update reflecting last-known values only.
            note = "adaptive_trend: sync skipped, no trustworthy exchange snapshot this cycle"
            self._append_adaptive_trend_update(
                position, current_price=current_price, note=note,
                updates=updates, events=events,
            )
            return

        live_position = self._find_live_position(symbol, positions_live)
        if live_position is None:
            note = "adaptive_trend: symbol reported open but no matching live position payload"
            self._append_adaptive_trend_update(
                position, current_price=current_price, note=note,
                updates=updates, events=events,
            )
            return

        protection = self._extract_live_protection_payload(live_position)
        exchange_stop = float(protection.get("stop_loss") or 0.0)
        if exchange_stop <= 0:
            # Unprotected position -- the existing _heal_missing_protection_from_fallback
            # call (already run earlier in sync() for every OPEN, live-tracked
            # symbol) is responsible for restoring protection. Trailing must
            # never invent a stop from nothing.
            note = "adaptive_trend: no exchange-reported stop yet, deferring to protection repair"
            self._append_adaptive_trend_update(
                position, current_price=current_price, note=note,
                updates=updates, events=events,
            )
            return

        direction = str(position.get("direction") or "").upper()
        side = Side.LONG if direction == "LONG" else Side.SHORT

        candles = fetch_6h_candles(self.client, symbol, self.settings.bitget_product_type)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        last_processed_close_ms = position.get("adaptive_trend_last_processed_close_ms")

        decision = evaluate_trail(
            symbol=symbol,
            side=side,
            current_stop=exchange_stop,
            candles=candles,
            now_ms=now_ms,
            last_processed_close_ms=last_processed_close_ms,
        )

        note = f"adaptive_trend: trail={decision.outcome.value}"

        if decision.outcome == TrailOutcome.UPDATED:
            try:
                self.client.place_futures_protection_orders(
                    symbol=symbol,
                    direction=direction,
                    stop_loss=float(decision.new_stop),
                    take_profits=[],
                    size=self._live_position_size(live_position),
                    product_type=self.settings.bitget_product_type,
                )
            except Exception as exc:
                self.log.warning(
                    "ADAPTIVE_TREND_TRAIL_UPDATE_FAILED | symbol=%s | candidate_stop=%.8f | error=%s",
                    symbol, decision.new_stop, exc,
                )
                note = f"adaptive_trend: trail update FAILED, stop unchanged on exchange | {exc}"
                self._append_adaptive_trend_update(
                    position, current_price=current_price, note=note,
                    updates=updates, events=events,
                )
                return
            # Only now, after exchange confirmation, advance local state.
            position["stop_loss"] = float(decision.new_stop)
            position["adaptive_trend_last_processed_close_ms"] = decision.last_processed_close_ms
            self.log.info(
                "ADAPTIVE_TREND_TRAIL_UPDATED | symbol=%s | new_stop=%.8f | candle_close=%s",
                symbol, decision.new_stop, decision.candle_close,
            )
        elif decision.outcome in (TrailOutcome.UNCHANGED, TrailOutcome.NO_NEW_CANDLE):
            if decision.last_processed_close_ms is not None:
                position["adaptive_trend_last_processed_close_ms"] = decision.last_processed_close_ms
            position["stop_loss"] = exchange_stop
        else:  # DATA_UNHEALTHY
            position["stop_loss"] = exchange_stop
            note = f"adaptive_trend: trail={decision.outcome.value} ({decision.reason}), stop untouched"

        self._append_adaptive_trend_update(
            position, current_price=current_price, note=note,
            updates=updates, events=events,
        )

    def _append_adaptive_trend_update(
        self, position: dict, *, current_price: float, note: str, updates: list, events: list,
    ) -> None:
        symbol = position["symbol"]
        timestamp = datetime.now(timezone.utc).isoformat()
        updates.append(
            PositionUpdate(
                symbol=symbol,
                status=position["status"],
                current_price=current_price,
                unrealized_pnl_pct=0.0,
                stop_loss=float(position.get("stop_loss") or 0.0),
                break_even_active=False,
                tp1_hit=False,
                tp2_hit=False,
                tp3_hit=False,
                note=note,
                protection_state=str(position.get("protection_state") or ""),
                confirmed_stop=float(position.get("stop_loss") or 0.0),
            )
        )
        events.append(
            {
                "timestamp": timestamp,
                "symbol": symbol,
                "status": position["status"],
                "current_price": current_price,
                "stop_loss": float(position.get("stop_loss") or 0.0),
                "break_even_active": False,
                "tp1_locked_stop_active": False,
                "tp1_hit": False,
                "tp2_hit": False,
                "tp3_hit": False,
                "note": note,
            }
        )
