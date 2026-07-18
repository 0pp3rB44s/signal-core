from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telemetry.csv_rotation import rotate_if_needed
from telemetry.safe_io import locked_open


class MarketContextLogger:
    """Structured market context logger for backtests and self-learning."""

    def __init__(self, path: str | Path = "logs/market_context.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        symbol: str,
        alignment: str,
        score_hint: float,
        primary_trend: str,
        confirmation_trend: str,
        volatility_rank: float,
        notes: list[str],
        orderbook_context: dict[str, Any] | None = None,
        selected_candidate: bool = False,
        trade_opened: bool = False,
    ) -> None:
        parsed = self._parse_notes(notes)
        parsed.update(self._orderbook_fields(orderbook_context))

        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": symbol.upper(),
            "alignment": alignment,
            "score_hint": round(float(score_hint or 0.0), 4),
            "primary_trend": primary_trend,
            "confirmation_trend": confirmation_trend,
            "volatility_rank": round(float(volatility_rank or 0.0), 4),
            "entry_quality_long": parsed.get("entry_quality_long", ""),
            "entry_quality_short": parsed.get("entry_quality_short", ""),
            "close_position": parsed.get("close_position", ""),
            "spread_bps": parsed.get("spread_bps", ""),
            "orderbook_imbalance": parsed.get("orderbook_imbalance", ""),
            "orderbook_bias": parsed.get("orderbook_bias", ""),
            "largest_bid_wall_ratio": parsed.get("largest_bid_wall_ratio", ""),
            "largest_ask_wall_ratio": parsed.get("largest_ask_wall_ratio", ""),
            "volatility_compression": parsed.get("volatility_compression", ""),
            "expansion_probability": parsed.get("expansion_probability", ""),
            "breakout_pressure": parsed.get("breakout_pressure", ""),
            "breakout_ready": parsed.get("breakout_ready", ""),
            "breakout_pressure_score": parsed.get("breakout_pressure_score", ""),
            "pullback_depth_pct": parsed.get("pullback_depth_pct", ""),
            "reclaim_proximity_pct": parsed.get("reclaim_proximity_pct", ""),
            "reclaim_timing": parsed.get("reclaim_timing", ""),
            "vertical_extension_risk": parsed.get("vertical_extension_risk", ""),
            "continuation_regime": parsed.get("continuation_regime", ""),
            "directional_pressure_ok": parsed.get("directional_pressure_ok", ""),
            "participation_score": parsed.get("participation_score", ""),
            "followthrough_volume_ratio": parsed.get("followthrough_volume_ratio", ""),
            "long_entry_warning": parsed.get("long_entry_warning", ""),
            "short_entry_warning": parsed.get("short_entry_warning", ""),
            "volatility_notes": parsed.get("volatility_notes", ""),
            "selected_candidate": selected_candidate,
            "trade_opened": trade_opened,
            "raw_notes": " | ".join(notes or []),
        }

        self._append_row(row)

    def _append_row(self, row: dict[str, Any]) -> None:
        fieldnames = [
            "timestamp",
            "symbol",
            "alignment",
            "score_hint",
            "primary_trend",
            "confirmation_trend",
            "volatility_rank",
            "entry_quality_long",
            "entry_quality_short",
            "close_position",
            "spread_bps",
            "orderbook_imbalance",
            "orderbook_bias",
            "largest_bid_wall_ratio",
            "largest_ask_wall_ratio",
            "volatility_compression",
            "expansion_probability",
            "breakout_pressure",
            "breakout_ready",
            "breakout_pressure_score",
            "pullback_depth_pct",
            "reclaim_proximity_pct",
            "reclaim_timing",
            "vertical_extension_risk",
            "continuation_regime",
            "directional_pressure_ok",
            "participation_score",
            "followthrough_volume_ratio",
            "long_entry_warning",
            "short_entry_warning",
            "volatility_notes",
            "selected_candidate",
            "trade_opened",
            "raw_notes",
        ]

        rotate_if_needed(self.path)
        with locked_open(self.path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if handle.tell() == 0:
                writer.writeheader()
            writer.writerow(row)

    @staticmethod
    def _orderbook_fields(orderbook_context: dict[str, Any] | None) -> dict[str, Any]:
        """Structured OrderbookAnalyzer output; overrides values parsed from notes."""
        if not isinstance(orderbook_context, dict):
            return {}

        fields: dict[str, Any] = {}

        spread = MarketContextLogger._safe_float(orderbook_context.get("spread_bps"))
        if spread != "":
            fields["spread_bps"] = spread

        imbalance_raw = orderbook_context.get("imbalance")
        if imbalance_raw is None:
            imbalance_raw = orderbook_context.get("depth_imbalance")
        imbalance = MarketContextLogger._safe_float(imbalance_raw)
        if imbalance != "":
            fields["orderbook_imbalance"] = imbalance

        bias = orderbook_context.get("continuation_bias")
        if bias:
            fields["orderbook_bias"] = str(bias)

        for wall_key, column in (
            ("largest_bid_wall", "largest_bid_wall_ratio"),
            ("largest_ask_wall", "largest_ask_wall_ratio"),
        ):
            wall = orderbook_context.get(wall_key)
            if isinstance(wall, dict):
                ratio = MarketContextLogger._safe_float(wall.get("wall_ratio"))
                if ratio != "":
                    fields[column] = ratio

        return fields

    def _parse_notes(self, notes: list[str]) -> dict[str, Any]:
        parsed: dict[str, Any] = {}
        volatility_notes: list[str] = []

        for note in notes or []:
            text = str(note)
            lower = text.lower()

            if lower.startswith("spread ") and "bps" in lower:
                parsed["spread_bps"] = self._extract_first_float(text)

            elif lower.startswith("spread_bps="):
                parsed["spread_bps"] = self._safe_float(text.split("=", 1)[1])

            elif lower.startswith("orderbook imbalance"):
                parsed["orderbook_imbalance"] = self._extract_first_float(text)

            elif lower.startswith("orderbook_imbalance="):
                parsed["orderbook_imbalance"] = self._safe_float(text.split("=", 1)[1])

            elif lower.startswith("orderbook bias"):
                parsed["orderbook_bias"] = text.split("orderbook bias", 1)[1].strip()

            elif lower.startswith("orderbook_bias="):
                parsed["orderbook_bias"] = text.split("=", 1)[1].strip()

            elif lower.startswith("significant bid wall"):
                parsed["largest_bid_wall_ratio"] = self._extract_after_token(text, "ratio")

            elif lower.startswith("significant ask wall"):
                parsed["largest_ask_wall_ratio"] = self._extract_after_token(text, "ratio")

            elif lower.startswith("entry_quality"):
                parts = text.split()
                for part in parts:
                    if "=" not in part:
                        continue
                    key, value = part.split("=", 1)
                    value = value.strip(";|,")
                    if key == "long":
                        parsed["entry_quality_long"] = self._safe_float(value)
                    elif key == "short":
                        parsed["entry_quality_short"] = self._safe_float(value)
                    elif key == "close_pos":
                        parsed["close_position"] = self._safe_float(value)

            elif lower.startswith("long_entry_warning="):
                parsed["long_entry_warning"] = text.split("=", 1)[1].strip()

            elif lower.startswith("short_entry_warning="):
                parsed["short_entry_warning"] = text.split("=", 1)[1].strip()

            elif lower.startswith("volatility_context"):
                parts = text.split()
                for part in parts:
                    if "=" not in part:
                        continue
                    key, value = part.split("=", 1)
                    value = value.strip(";|,")
                    if key == "compression":
                        parsed["volatility_compression"] = value
                    elif key == "expansion_prob":
                        parsed["expansion_probability"] = self._safe_float(value)
                    elif key == "pressure":
                        parsed["breakout_pressure"] = value

            elif lower.startswith("volatility_note="):
                volatility_notes.append(text.split("=", 1)[1].strip())

            elif lower.startswith("breakout_context"):
                parts = text.split()
                for part in parts:
                    if "=" not in part:
                        continue
                    key, value = part.split("=", 1)
                    value = value.strip(";|,")
                    if key == "ready":
                        parsed["breakout_ready"] = value
                    elif key == "pressure_score":
                        parsed["breakout_pressure_score"] = self._safe_float(value)
                    elif key == "direction":
                        parsed["breakout_pressure"] = value

            elif lower.startswith("pullback_depth_pct"):
                parsed["pullback_depth_pct"] = self._extract_first_float(text)

            elif lower.startswith("reclaim_proximity_pct"):
                parsed["reclaim_proximity_pct"] = self._extract_first_float(text)

            elif lower.startswith("reclaim timing efficient"):
                parsed["reclaim_timing"] = "efficient"

            elif lower.startswith("reclaim timing extended"):
                parsed["reclaim_timing"] = "extended"

            elif lower.startswith("continuation_regime"):
                parsed["continuation_regime"] = text.split("continuation_regime", 1)[1].strip()

            elif lower.startswith("directional_pressure_ok"):
                parsed["directional_pressure_ok"] = text.split("directional_pressure_ok", 1)[1].strip()

            elif lower.startswith("participation_score"):
                parsed["participation_score"] = self._extract_first_float(text)

            elif lower.startswith("followthrough_volume_ratio"):
                parsed["followthrough_volume_ratio"] = self._extract_first_float(text)

            elif lower.startswith("vertical extension risk"):
                parsed["vertical_extension_risk"] = True

        if volatility_notes:
            parsed["volatility_notes"] = " | ".join(volatility_notes)

        return parsed

    @staticmethod
    def _extract_first_float(text: str) -> float | str:
        for token in text.replace("bps", "").replace(",", " ").split():
            value = MarketContextLogger._safe_float(token)
            if value != "":
                return value
        return ""

    @staticmethod
    def _extract_after_token(text: str, token: str) -> float | str:
        parts = text.split()
        for idx, part in enumerate(parts):
            if part.lower() == token.lower() and len(parts) > idx + 1:
                return MarketContextLogger._safe_float(parts[idx + 1])
        return ""

    @staticmethod
    def _safe_float(value: Any) -> float | str:
        try:
            if value in (None, ""):
                return ""
            return round(float(value), 6)
        except (TypeError, ValueError):
            return ""
