from __future__ import annotations

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from clients.schemas import MarketSnapshot, TradePlan
from forward_paper.store import ForwardPaperEventStore, ForwardPaperReconstructor, content_hash


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _iso_from_snapshot(snapshot: MarketSnapshot) -> str:
    candles = getattr(snapshot.primary, "candles", []) or []
    timestamp_ms = int(getattr(candles[-1], "timestamp_ms", 0) or 0) if candles else 0
    if timestamp_ms > 0:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def _session(timestamp: str) -> str:
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    hour = parsed.hour
    return "ASIA_00_08_UTC" if hour < 8 else "EUROPE_08_16_UTC" if hour < 16 else "US_16_24_UTC"


def _spread_bps(notes: list[str]) -> float | None:
    text = " | ".join(str(note) for note in notes)
    match = re.search(r"(?i)spread(?:_bps)?\s*[=: ]\s*(-?\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


class ForwardPaperService:
    """Paper-only plan observer and protection lifecycle simulator."""

    def __init__(
        self,
        settings: Settings,
        *,
        events_path: str | Path = "data_store/forward_paper_events.jsonl",
        outcomes_path: str | Path = "data_store/forward_paper_outcomes.csv",
        quality_path: str | Path = "reports/forward_paper_data_quality.json",
        git_commit: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = ForwardPaperEventStore(events_path)
        self.reconstructor = ForwardPaperReconstructor(self.store, outcomes_path, quality_path)
        self.git_commit = git_commit or self._git_commit()
        self.config_hash = self._config_hash()

    def process(self, plans: list[TradePlan], snapshots: list[MarketSnapshot]) -> None:
        if not self.settings.forward_paper_enabled:
            return
        snapshot_map = {snapshot.symbol: snapshot for snapshot in snapshots}
        self.update_market(snapshots)
        open_symbols = {state["symbol"] for state in self._open_states().values()}
        capacity = max(0, int(self.settings.max_open_positions) - len(open_symbols))
        for plan in plans:
            if capacity <= 0:
                break
            if plan.verdict != "EXECUTABLE" or plan.symbol in open_symbols:
                continue
            snapshot = snapshot_map.get(plan.symbol)
            if snapshot is None:
                self._reject(plan, "missing_market_snapshot")
                continue
            if self.open_trade(plan, snapshot):
                open_symbols.add(plan.symbol)
                capacity -= 1
        self.reconstructor.reconstruct()

    def open_trade(self, plan: TradePlan, snapshot: MarketSnapshot) -> str | None:
        timestamp = _iso_from_snapshot(snapshot)
        planned_entry = sum(plan.entry_prices) / len(plan.entry_prices) if plan.entry_prices else 0.0
        simulated_fill = _float(snapshot.primary.latest_close)
        stop = _float(plan.stop_loss)
        targets = [_float(target) for target in plan.take_profits if _float(target) > 0]
        direction = str(plan.direction or "").upper()
        risk_price = abs(simulated_fill - stop)
        position_size = _float(plan.position_notional_usdt) / simulated_fill if simulated_fill > 0 else 0.0
        critical = {
            "strategy": plan.strategy,
            "symbol": plan.symbol,
            "direction": direction if direction in {"LONG", "SHORT"} else "",
            "timeframe": getattr(snapshot.primary, "granularity", ""),
            "planned_entry": planned_entry,
            "simulated_fill": simulated_fill,
            "initial_stop": stop,
            "initial_targets": targets,
            "initial_risk_price": risk_price,
            "position_size": position_size,
        }
        missing = [key for key, value in critical.items() if value in (None, "", [], 0, 0.0)]
        if missing:
            self._reject(plan, f"critical_fields_missing:{','.join(missing)}", timestamp=timestamp)
            return None

        plan_material = {
            "signal_timestamp": timestamp,
            "symbol": plan.symbol,
            "strategy": plan.strategy,
            "direction": direction,
            "entries": plan.entry_prices,
            "stop": stop,
            "targets": targets,
            "score": plan.score,
        }
        plan_id = f"plan_{content_hash(plan_material)[:20]}"
        trade_id = f"paper_{content_hash({'plan_id': plan_id, 'fill': simulated_fill})[:20]}"
        expected_move_bps = abs(targets[0] - simulated_fill) / simulated_fill * 10_000
        entry_slippage = (simulated_fill - planned_entry) * position_size
        entry_slippage_pct = ((simulated_fill - planned_entry) / planned_entry * 100) if planned_entry else 0.0
        fee_rate = float(self.settings.forward_paper_roundtrip_fee_bps) / 10_000
        entry_fee = float(plan.position_notional_usdt) * fee_rate / 2
        regime = self._regime(snapshot)
        payload = {
            **critical,
            "plan_id": plan_id,
            "trade_id": trade_id,
            "signal_timestamp": timestamp,
            "regime": regime,
            "session": _session(timestamp),
            "config_version_hash": self.config_hash,
            "git_commit": self.git_commit,
            "initial_risk_currency": risk_price * position_size,
            "initial_risk_r": 1.0,
            "expected_reward_to_risk": _float(plan.risk_reward_ratio),
            "expected_move_bps": expected_move_bps,
            "spread_bps": _spread_bps(list(snapshot.notes or [])),
            "liquidity_assumption": self.settings.forward_paper_liquidity_assumption,
            "expected_fees": float(plan.position_notional_usdt) * fee_rate,
            "entry_fee": entry_fee,
            "entry_slippage": entry_slippage,
            "entry_slippage_pct": entry_slippage_pct,
            "volatility_rank": _float(getattr(snapshot, "volatility_rank", 0.0)),
            "strategy_score": _float(plan.score),
            "strategy_features": {
                "alignment": snapshot.alignment,
                "primary_trend": snapshot.primary.trend,
                "confirmation_trend": snapshot.confirmation.trend,
                "volume_ratio": snapshot.primary.volume_ratio_20,
                "score_hint": snapshot.score_hint,
                "notes": list(plan.notes),
                "reasons": list(plan.reasons),
                "market_context": dict(getattr(snapshot, "context", {}) or {}),
            },
        }
        self._append(trade_id, plan_id, "TRADE_OPENED", timestamp, payload)
        return trade_id

    def update_market(self, snapshots: list[MarketSnapshot]) -> None:
        states = self._open_states()
        snapshot_map = {snapshot.symbol: snapshot for snapshot in snapshots}
        for trade_id, state in states.items():
            snapshot = snapshot_map.get(state["symbol"])
            if snapshot is None:
                continue
            self._update_trade(trade_id, state, snapshot)
        self.reconstructor.reconstruct()

    def _update_trade(self, trade_id: str, state: dict[str, Any], snapshot: MarketSnapshot) -> None:
        timestamp = _iso_from_snapshot(snapshot)
        candles = getattr(snapshot.primary, "candles", []) or []
        candle = candles[-1] if candles else None
        mark = _float(snapshot.primary.latest_close)
        high = _float(getattr(candle, "high", mark), mark)
        low = _float(getattr(candle, "low", mark), mark)
        direction = state["direction"]
        fill = state["fill"]
        favorable_price = high if direction == "LONG" else low
        adverse_price = low if direction == "LONG" else high
        favorable_pct = ((favorable_price - fill) / fill * 100) if direction == "LONG" else ((fill - favorable_price) / fill * 100)
        adverse_pct = ((adverse_price - fill) / fill * 100) if direction == "LONG" else ((fill - adverse_price) / fill * 100)
        self._append(trade_id, state["plan_id"], "MARK_DECISION", timestamp, {
            "mark_price": mark, "candle_high": high, "candle_low": low,
            "current_stop": state["stop"], "remaining_size": state["remaining_size"],
        })
        if favorable_pct > state["mfe_pct"]:
            self._append(trade_id, state["plan_id"], "MFE_UPDATE", timestamp, {"price": favorable_price, "excursion_pct": favorable_pct})
            state["mfe_pct"] = favorable_pct
        if adverse_pct < state["mae_pct"]:
            self._append(trade_id, state["plan_id"], "MAE_UPDATE", timestamp, {"price": adverse_price, "excursion_pct": adverse_pct})
            state["mae_pct"] = adverse_pct

        stop_touched = low <= state["stop"] if direction == "LONG" else high >= state["stop"]
        if stop_touched:
            self._append(trade_id, state["plan_id"], "SL_TOUCH", timestamp, {"price": state["stop"], "mark_price": mark})
            self._close(trade_id, state, timestamp, state["stop"], "STOP_LOSS")
            return

        targets = state["targets"]
        for index, target in enumerate(targets):
            if index in state["touched_targets"]:
                continue
            touched = high >= target if direction == "LONG" else low <= target
            if not touched:
                continue
            self._append(trade_id, state["plan_id"], "TP_TOUCH", timestamp, {"target_index": index + 1, "price": target, "mark_price": mark})
            state["touched_targets"].add(index)
            if index == 0:
                close_fraction = float(self.settings.tp1_close_pct) / 100
            elif index == 1:
                close_fraction = float(self.settings.tp2_close_pct) / 100
            else:
                close_fraction = 1.0
            close_size = min(state["remaining_size"], state["initial_size"] * close_fraction)
            if index == len(targets) - 1:
                close_size = state["remaining_size"]
            self._partial(trade_id, state, timestamp, target, close_size, f"TP{index + 1}")
            state["remaining_size"] -= close_size
            if index == 0 and state["remaining_size"] > 0:
                be_stop = self._fee_break_even(fill, direction)
                self._append(trade_id, state["plan_id"], "BREAK_EVEN_ACTIVATED", timestamp, {"stop": be_stop, "mark_price": mark})
                self._append(trade_id, state["plan_id"], "STOP_UPDATED", timestamp, {"old_stop": state["stop"], "new_stop": be_stop, "reason": "TP1_FEE_BE"})
                state["stop"] = be_stop
            if state["remaining_size"] <= 1e-12:
                self._close(trade_id, state, timestamp, target, f"TP{index + 1}", remaining_size=0.0)
                return

        if not state["touched_targets"] and not state["profit_lock"] and targets:
            tp1_distance = abs(targets[0] - fill)
            if tp1_distance > 0 and state["mfe_pct"] >= float(self.settings.profit_lock_tp1_fraction) * (tp1_distance / fill * 100):
                be_stop = self._fee_break_even(fill, direction)
                self._append(trade_id, state["plan_id"], "PROFIT_LOCK_ACTIVATED", timestamp, {"stop": be_stop, "mark_price": mark})
                self._append(trade_id, state["plan_id"], "STOP_UPDATED", timestamp, {"old_stop": state["stop"], "new_stop": be_stop, "reason": "PROFIT_LOCK_FEE_BE"})

    def _partial(self, trade_id: str, state: dict[str, Any], timestamp: str, price: float, size: float, reason: str) -> None:
        if size <= 0:
            return
        gross = (price - state["fill"]) * size if state["direction"] == "LONG" else (state["fill"] - price) * size
        fee = price * size * (float(self.settings.forward_paper_roundtrip_fee_bps) / 10_000) / 2
        self._append(trade_id, state["plan_id"], "PARTIAL_EXIT", timestamp, {
            "exit_price": price, "exit_size": size, "gross_pnl": gross, "fee": fee,
            "reason": reason, "slippage": 0.0,
        })

    def _close(
        self, trade_id: str, state: dict[str, Any], timestamp: str, price: float,
        reason: str, *, remaining_size: float | None = None,
    ) -> None:
        size = state["remaining_size"] if remaining_size is None else remaining_size
        gross = (price - state["fill"]) * size if state["direction"] == "LONG" else (state["fill"] - price) * size
        fee = price * size * (float(self.settings.forward_paper_roundtrip_fee_bps) / 10_000) / 2
        self._append(trade_id, state["plan_id"], "EXIT_REASON_TRANSITION", timestamp, {"from": "OPEN", "to": reason, "mark_price": price})
        self._append(trade_id, state["plan_id"], "TRADE_CLOSED", timestamp, {
            "exit_price": price, "exit_size": size, "gross_pnl": gross, "fee": fee,
            "funding": 0.0, "slippage": 0.0, "slippage_pct": 0.0, "exit_reason": reason,
        })

    def _open_states(self) -> dict[str, dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in self.store.read_events():
            grouped.setdefault(event["trade_id"], []).append(event)
        states: dict[str, dict[str, Any]] = {}
        for trade_id, events in grouped.items():
            opened = next((event for event in events if event["event_type"] == "TRADE_OPENED"), None)
            if opened is None or any(event["event_type"] == "TRADE_CLOSED" for event in events):
                continue
            payload = opened["payload"]
            state = {
                "plan_id": opened["plan_id"], "symbol": payload["symbol"], "direction": payload["direction"],
                "fill": float(payload["simulated_fill"]), "stop": float(payload["initial_stop"]),
                "targets": [float(value) for value in payload["initial_targets"]],
                "initial_size": float(payload["position_size"]), "remaining_size": float(payload["position_size"]),
                "touched_targets": set(), "mfe_pct": 0.0, "mae_pct": 0.0, "profit_lock": False,
            }
            for event in events:
                event_type = event["event_type"]
                data = event["payload"]
                if event_type == "PARTIAL_EXIT":
                    state["remaining_size"] -= float(data["exit_size"])
                elif event_type == "TP_TOUCH":
                    state["touched_targets"].add(int(data["target_index"]) - 1)
                elif event_type == "STOP_UPDATED":
                    state["stop"] = float(data["new_stop"])
                elif event_type == "MFE_UPDATE":
                    state["mfe_pct"] = max(state["mfe_pct"], float(data["excursion_pct"]))
                elif event_type == "MAE_UPDATE":
                    state["mae_pct"] = min(state["mae_pct"], float(data["excursion_pct"]))
                elif event_type == "PROFIT_LOCK_ACTIVATED":
                    state["profit_lock"] = True
            states[trade_id] = state
        return states

    def _append(self, trade_id: str, plan_id: str, event_type: str, timestamp: str, payload: dict[str, Any]) -> bool:
        event_id = f"evt_{content_hash({'trade': trade_id, 'type': event_type, 'timestamp': timestamp, 'payload': payload})[:24]}"
        return self.store.append({
            "event_id": event_id, "trade_id": trade_id, "plan_id": plan_id,
            "event_type": event_type, "timestamp": timestamp, "payload": payload,
        })

    def _reject(self, plan: TradePlan, reason: str, *, timestamp: str | None = None) -> None:
        timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        material = {"symbol": plan.symbol, "strategy": plan.strategy, "timestamp": timestamp, "reason": reason}
        plan_id = f"rejected_plan_{content_hash(material)[:16]}"
        trade_id = f"rejected_trade_{content_hash({'plan': plan_id})[:16]}"
        self._append(trade_id, plan_id, "PAPER_REJECTED", timestamp, {"reason": reason, "symbol": plan.symbol, "strategy": plan.strategy})

    def _fee_break_even(self, fill: float, direction: str) -> float:
        buffer_pct = float(self.settings.break_even_fee_buffer_pct) / 100
        return fill * (1 + buffer_pct) if direction == "LONG" else fill * (1 - buffer_pct)

    @staticmethod
    def _regime(snapshot: MarketSnapshot) -> str:
        alignment = str(snapshot.alignment or "").lower()
        if "bull" in alignment:
            return "BULLISH"
        if "bear" in alignment:
            return "BEARISH"
        return str(getattr(snapshot, "context", {}).get("regime") or "UNKNOWN").upper()

    def _config_hash(self) -> str:
        sensitive_markers = ("secret", "password", "passphrase", "api_key", "token")
        safe_config = {
            key: value
            for key, value in self.settings.model_dump(mode="json").items()
            if not any(marker in key.lower() for marker in sensitive_markers)
        }
        safe_config["forward_paper_schema"] = 1
        return content_hash(safe_config)

    @staticmethod
    def _git_commit() -> str:
        try:
            return subprocess.run(
                ["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return "UNKNOWN"
