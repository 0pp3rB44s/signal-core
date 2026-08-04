from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Any


class PositionReadbackState(str, Enum):
    FLAT = "FLAT"
    REMAINS = "REMAINS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PositionReadback:
    state: PositionReadbackState
    size: float | None = None
    detail: str = ""


class PositionReadbackUnknown(RuntimeError):
    """A fresh exchange read could not prove whether a position remains."""


class BitgetAccountClientMixin:
    """Account and position endpoints only."""

    def get_accounts(self, product_type: str | None = None) -> dict[str, Any]:
        params = {
            "productType": (product_type or self.settings.bitget_product_type).upper(),
        }

        return self._request(
            "GET",
            "/api/v2/mix/account/accounts",
            params=params,
            private=True,
        )

    def get_symbol_account(
        self,
        symbol: str,
        product_type: str | None = None,
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        """Read one symbol's current margin mode and leverage configuration."""
        return self._request(
            "GET",
            "/api/v2/mix/account/account",
            params={
                "symbol": symbol.upper(),
                "productType": (
                    product_type or self.settings.bitget_product_type
                ).upper(),
                "marginCoin": margin_coin.upper(),
            },
            private=True,
        )

    def get_all_positions(
        self,
        product_type: str | None = None,
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        """Return only active non-zero futures positions from Bitget."""

        params = {
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "marginCoin": margin_coin.upper(),
        }

        payload = self._get_all_positions_unfiltered(
            product_type=product_type,
            margin_coin=margin_coin,
        )

        if not isinstance(payload, dict) or "data" not in payload or not isinstance(payload["data"], list):
            raise PositionReadbackUnknown("all-position response missing a valid data list")
        data = payload["data"]

        if isinstance(data, list):
            active_positions = []

            for pos in data:
                if not isinstance(pos, dict):
                    raise PositionReadbackUnknown("all-position response contains a non-object row")

                size_candidates = [
                    pos.get("total"),
                    pos.get("available"),
                    pos.get("locked"),
                    pos.get("holdVol"),
                    pos.get("size"),
                    pos.get("positionSize"),
                ]

                is_active = False
                saw_valid_size = False

                for raw_size in size_candidates:
                    try:
                        if raw_size not in (None, ""):
                            parsed = float(raw_size)
                            if not math.isfinite(parsed) or parsed < 0:
                                raise PositionReadbackUnknown("all-position row has invalid size")
                            saw_valid_size = True
                            if parsed > 0:
                                is_active = True
                                break
                    except (TypeError, ValueError):
                        raise PositionReadbackUnknown("all-position row has unparseable size") from None

                if not saw_valid_size:
                    raise PositionReadbackUnknown("all-position row has no valid size")

                if is_active:
                    active_positions.append(pos)

            payload["data"] = active_positions

        return payload

    def _get_all_positions_unfiltered(
        self,
        product_type: str | None = None,
        margin_coin: str = "USDT",
    ) -> dict[str, Any]:
        """Fetch the raw position list used for safety-critical readback."""
        params = {
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "marginCoin": margin_coin.upper(),
        }
        return self._request(
            "GET",
            "/api/v2/mix/position/all-position",
            params=params,
            private=True,
        )

    def get_position_history(
        self,
        product_type: str | None = None,
        symbol: str | None = None,
        start_time_ms: int | str | None = None,
        end_time_ms: int | str | None = None,
        limit: int = 100,
        id_less_than: str | None = None,
    ) -> dict[str, Any]:
        """Return closed futures position history from Bitget."""

        params: dict[str, Any] = {
            "productType": (product_type or self.settings.bitget_product_type).upper(),
            "limit": str(max(1, min(int(limit), 100))),
        }

        if symbol:
            params["symbol"] = symbol.upper()
        if start_time_ms is not None:
            params["startTime"] = str(start_time_ms)
        if end_time_ms is not None:
            params["endTime"] = str(end_time_ms)
        if id_less_than:
            params["idLessThan"] = str(id_less_than)

        return self._request(
            "GET",
            "/api/v2/mix/position/history-position",
            params=params,
            private=True,
        )

    def get_open_orders(
        self,
        product_type: str | None = None,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Return current open futures orders from Bitget."""

        params: dict[str, Any] = {
            "productType": (product_type or self.settings.bitget_product_type).upper(),
        }

        if symbol:
            params["symbol"] = symbol.upper()

        return self._request(
            "GET",
            "/api/v2/mix/order/orders-pending",
            params=params,
            private=True,
        )

    def ping_private_account(self) -> dict[str, Any]:
        """Simple authenticated endpoint check."""
        return self.get_accounts(
            product_type=self.settings.bitget_product_type,
        )

    def _live_position_size_for_symbol(
        self,
        symbol: str,
        hold_side: str | None = None,
    ) -> float:
        """Compatibility helper that never maps UNKNOWN to zero."""
        readback = self.read_position_state(symbol, hold_side=hold_side)
        if readback.state is PositionReadbackState.UNKNOWN:
            raise PositionReadbackUnknown(readback.detail)
        return float(readback.size or 0.0)

    def read_position_state(
        self,
        symbol: str,
        hold_side: str | None = None,
    ) -> PositionReadback:
        """Return FLAT/REMAINS/UNKNOWN from one fresh raw exchange response."""
        wanted_symbol = str(symbol or "").upper()
        wanted_side = str(hold_side or "").lower()
        if not wanted_symbol or wanted_side not in {"long", "short"}:
            return PositionReadback(PositionReadbackState.UNKNOWN, detail="invalid symbol or hold side")
        try:
            if type(self).get_all_positions is not BitgetAccountClientMixin.get_all_positions:
                # A concrete adapter/test double may provide its own raw GET.
                payload = self.get_all_positions()
            else:
                payload = self._get_all_positions_unfiltered()
        except Exception as exc:
            self.log.warning(
                "POSITION_READBACK_UNKNOWN | %s | hold_side=%s | transport_error=%s",
                wanted_symbol,
                wanted_side,
                exc,
            )
            return PositionReadback(PositionReadbackState.UNKNOWN, detail=f"exchange read failed: {exc}")
        if not isinstance(payload, dict) or "data" not in payload or not isinstance(payload["data"], list):
            return PositionReadback(PositionReadbackState.UNKNOWN, detail="missing or malformed data list")

        matching: list[float] = []
        for position in payload["data"]:
            if not isinstance(position, dict):
                return PositionReadback(PositionReadbackState.UNKNOWN, detail="non-object position row")
            row_symbol = str(position.get("symbol") or "").upper()
            if not row_symbol:
                return PositionReadback(PositionReadbackState.UNKNOWN, detail="position row missing symbol")
            if row_symbol != wanted_symbol:
                continue
            row_side = str(position.get("holdSide") or position.get("posSide") or "").lower()
            if row_side not in {"long", "short"}:
                return PositionReadback(PositionReadbackState.UNKNOWN, detail="matching row has invalid hold side")
            if row_side != wanted_side:
                continue
            raw_size = next(
                (position.get(field) for field in ("total", "size", "positionSize", "holdVol")
                 if position.get(field) not in (None, "")),
                None,
            )
            try:
                parsed = float(raw_size)
            except (TypeError, ValueError):
                return PositionReadback(PositionReadbackState.UNKNOWN, detail="matching row has invalid size")
            if not math.isfinite(parsed) or parsed < 0:
                return PositionReadback(PositionReadbackState.UNKNOWN, detail="matching row has non-finite/negative size")
            matching.append(parsed)

        if len(matching) > 1:
            return PositionReadback(PositionReadbackState.UNKNOWN, detail="ambiguous duplicate position rows")
        if not matching or matching[0] == 0.0:
            return PositionReadback(PositionReadbackState.FLAT, size=0.0)
        return PositionReadback(PositionReadbackState.REMAINS, size=matching[0])


__all__ = [
    "BitgetAccountClientMixin",
    "PositionReadback",
    "PositionReadbackState",
    "PositionReadbackUnknown",
]
