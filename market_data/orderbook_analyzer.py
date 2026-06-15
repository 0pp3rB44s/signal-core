from __future__ import annotations

from typing import Any


class OrderbookAnalyzer:

    def __init__(
        self,
        wall_threshold_ratio: float = 2.5,
    ) -> None:
        self.wall_threshold_ratio = wall_threshold_ratio

    def analyze(self, orderbook: dict[str, Any]) -> dict[str, Any]:
        bids = orderbook.get("bids") or []
        asks = orderbook.get("asks") or []
        mid_price = float(orderbook.get("mid_price") or 0.0)
        imbalance = float(orderbook.get("depth_imbalance") or 0.0)
        bid_depth_notional = float(orderbook.get("bid_depth_notional") or 0.0)
        ask_depth_notional = float(orderbook.get("ask_depth_notional") or 0.0)

        if bid_depth_notional <= 0 and bids:
            bid_depth_notional = sum(
                float(row.get("price") or 0.0) * float(row.get("size") or 0.0)
                for row in bids
                if isinstance(row, dict)
            )

        if ask_depth_notional <= 0 and asks:
            ask_depth_notional = sum(
                float(row.get("price") or 0.0) * float(row.get("size") or 0.0)
                for row in asks
                if isinstance(row, dict)
            )

        total_depth_notional = float(orderbook.get("total_depth_notional") or 0.0)
        if total_depth_notional <= 0:
            total_depth_notional = bid_depth_notional + ask_depth_notional

        if not bids or not asks or mid_price <= 0:
            return {
                "symbol": orderbook.get("symbol"),
                "mid_price": mid_price,
                "spread_bps": float(orderbook.get("spread_bps") or 0.0),
                "spread_regime": "unavailable",
                "imbalance": imbalance,
                "bid_depth_notional": bid_depth_notional,
                "ask_depth_notional": ask_depth_notional,
                "total_depth_notional": total_depth_notional,
                "largest_bid_wall": None,
                "largest_ask_wall": None,
                "bid_pressure": 0.0,
                "ask_pressure": 0.0,
                "continuation_bias": "neutral",
                "spoofing_risk": True,
                "empty_orderbook_risk_off": True,
                "confidence_score": 0.0,
            }

        largest_bid_wall = self._find_largest_wall(bids)
        largest_ask_wall = self._find_largest_wall(asks)

        bid_wall_distance_bps = self._distance_to_wall_bps(
            wall=largest_bid_wall,
            mid_price=mid_price,
        )
        ask_wall_distance_bps = self._distance_to_wall_bps(
            wall=largest_ask_wall,
            mid_price=mid_price,
        )

        bid_pressure = self._calculate_pressure(bids)
        ask_pressure = self._calculate_pressure(asks)

        spread_bps = float(orderbook.get("spread_bps") or 0.0)
        spread_regime = self._classify_spread_regime(spread_bps)

        spoofing_risk = self._detect_spoofing_risk(
            largest_bid_wall=largest_bid_wall,
            largest_ask_wall=largest_ask_wall,
            bid_pressure=bid_pressure,
            ask_pressure=ask_pressure,
            imbalance=imbalance,
        )

        continuation_bias = self._detect_continuation_bias(
            imbalance=imbalance,
            bid_pressure=bid_pressure,
            ask_pressure=ask_pressure,
        )

        wall_proximity_risk = bool(
            (ask_wall_distance_bps is not None and ask_wall_distance_bps <= 8)
            or (bid_wall_distance_bps is not None and bid_wall_distance_bps <= 8)
        )

        return {
            "symbol": orderbook.get("symbol"),
            "mid_price": mid_price,
            "spread_bps": spread_bps,
            "spread_regime": spread_regime,
            "imbalance": imbalance,
            "bid_depth_notional": bid_depth_notional,
            "ask_depth_notional": ask_depth_notional,
            "total_depth_notional": total_depth_notional,
            "largest_bid_wall": largest_bid_wall,
            "largest_ask_wall": largest_ask_wall,
            "bid_wall_distance_bps": bid_wall_distance_bps,
            "ask_wall_distance_bps": ask_wall_distance_bps,
            "wall_proximity_risk": wall_proximity_risk,
            "bid_pressure": bid_pressure,
            "ask_pressure": ask_pressure,
            "continuation_bias": continuation_bias,
            "spoofing_risk": spoofing_risk,
            "empty_orderbook_risk_off": False,
            "confidence_score": self._confidence_score(
                imbalance=imbalance,
                bid_pressure=bid_pressure,
                ask_pressure=ask_pressure,
                spread_regime=spread_regime,
                spoofing_risk=spoofing_risk,
            ),
        }

    def _classify_spread_regime(self, spread_bps: float) -> str:

        if spread_bps <= 1.5:
            return "tight"

        if spread_bps <= 4.0:
            return "normal"

        if spread_bps <= 8.0:
            return "wide"

        return "extreme"

    def _detect_spoofing_risk(
        self,
        *,
        largest_bid_wall: dict[str, float] | None,
        largest_ask_wall: dict[str, float] | None,
        bid_pressure: float,
        ask_pressure: float,
        imbalance: float,
    ) -> bool:

        walls = [wall for wall in [largest_bid_wall, largest_ask_wall] if wall]

        if not walls:
            return False

        significant_wall_count = sum(
            1
            for wall in walls
            if bool(wall.get("is_significant")) and float(wall.get("wall_ratio") or 0.0) >= 4.0
        )

        pressure_delta = abs(bid_pressure - ask_pressure)

        return bool(
            significant_wall_count >= 1
            and abs(imbalance) < 0.08
            and pressure_delta < 0.08
        )

    def _confidence_score(
        self,
        *,
        imbalance: float,
        bid_pressure: float,
        ask_pressure: float,
        spread_regime: str,
        spoofing_risk: bool,
    ) -> float:

        score = 50.0

        score += min(abs(imbalance) * 120.0, 25.0)
        score += min(abs(bid_pressure - ask_pressure) * 60.0, 15.0)

        if spread_regime == "tight":
            score += 10.0
        elif spread_regime == "wide":
            score -= 10.0
        elif spread_regime == "extreme":
            score -= 20.0

        if spoofing_risk:
            score -= 25.0

        if spread_regime == "unavailable":
            score -= 35.0

        return round(max(0.0, min(score, 100.0)), 2)

    @staticmethod
    def _distance_to_wall_bps(
        *,
        wall: dict[str, float] | None,
        mid_price: float,
    ) -> float | None:

        if not wall or mid_price <= 0:
            return None

        wall_price = float(wall.get("price") or 0.0)

        if wall_price <= 0:
            return None

        return round(abs((wall_price - mid_price) / mid_price) * 10000.0, 2)

    def _find_largest_wall(
        self,
        side: list[dict[str, float]],
    ) -> dict[str, float] | None:

        if not side:
            return None

        largest = max(side, key=lambda row: row["size"])

        avg_size = (
            sum(row["size"] for row in side) / len(side)
            if side else 0.0
        )

        wall_ratio = (
            largest["size"] / avg_size
            if avg_size else 0.0
        )

        return {
            "price": largest["price"],
            "size": largest["size"],
            "wall_ratio": wall_ratio,
            "is_significant": wall_ratio >= self.wall_threshold_ratio,
        }

    def _calculate_pressure(
        self,
        side: list[dict[str, float]],
    ) -> float:

        if not side:
            return 0.0

        total_notional = sum(
            row["price"] * row["size"]
            for row in side
        )

        top_5_notional = sum(
            row["price"] * row["size"]
            for row in side[:5]
        )

        return (
            top_5_notional / total_notional
            if total_notional else 0.0
        )

    def _detect_continuation_bias(
        self,
        imbalance: float,
        bid_pressure: float,
        ask_pressure: float,
    ) -> str:

        if imbalance > 0.15 and bid_pressure > ask_pressure:
            return "bullish"

        if imbalance < -0.15 and ask_pressure > bid_pressure:
            return "bearish"

        return "neutral"