from __future__ import annotations

from clients.bitget_base_client import BitgetBaseClient
from clients.bitget_market_client import BitgetMarketClientMixin
from clients.bitget_precision import BitgetPrecisionMixin


class BitgetPublicClient(
    BitgetBaseClient,
    BitgetMarketClientMixin,
    BitgetPrecisionMixin,
):
    """Bitget client exposing only unauthenticated market-data surfaces."""


__all__ = ["BitgetPublicClient"]
