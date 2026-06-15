from __future__ import annotations

from typing import Any


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

        payload = self._request(
            "GET",
            "/api/v2/mix/position/all-position",
            params=params,
            private=True,
        )

        data = payload.get("data") or []

        if isinstance(data, list):
            active_positions = []

            for pos in data:
                if not isinstance(pos, dict):
                    continue

                size_candidates = [
                    pos.get("total"),
                    pos.get("available"),
                    pos.get("locked"),
                    pos.get("holdVol"),
                    pos.get("size"),
                    pos.get("positionSize"),
                ]

                is_active = False

                for raw_size in size_candidates:
                    try:
                        if abs(float(raw_size or 0)) > 0:
                            is_active = True
                            break
                    except (TypeError, ValueError):
                        continue

                if is_active:
                    active_positions.append(pos)

            payload["data"] = active_positions

        return payload

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
        """Return current live exchange position size."""

        try:
            payload = self.get_all_positions()

            positions = payload.get("data") or []
            symbol_upper = symbol.upper()
            wanted_hold_side = (hold_side or "").lower()

            for position in positions:
                if not isinstance(position, dict):
                    continue

                position_symbol = str(
                    position.get("symbol") or ""
                ).upper()

                if position_symbol != symbol_upper:
                    continue

                exchange_hold_side = str(
                    position.get("holdSide")
                    or position.get("posSide")
                    or ""
                ).lower()

                if (
                    wanted_hold_side
                    and exchange_hold_side
                    and exchange_hold_side != wanted_hold_side
                ):
                    continue

                size_candidates = [
                    position.get("total"),
                    position.get("available"),
                    position.get("locked"),
                    position.get("holdVol"),
                    position.get("size"),
                    position.get("positionSize"),
                ]

                for raw_size in size_candidates:
                    try:
                        size = abs(float(raw_size or 0))
                        if size > 0:
                            return size
                    except (TypeError, ValueError):
                        continue

        except Exception as exc:
            self.log.warning(
                "LIVE_POSITION_SIZE_FETCH_FAILED | %s | hold_side=%s | error=%s",
                symbol,
                hold_side,
                exc,
            )

        return 0.0