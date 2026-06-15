from app.config import Settings
from clients.schemas import ContractSpec
from data.watchlist import filter_contracts


def test_filter_contracts_keeps_liquid_usdt_pairs() -> None:
    settings = Settings(
        MIN_USDT_VOLUME_24H=1_000_000,
        MAX_SYMBOLS=5,
        BITGET_MARGIN_COIN="USDT",
    )
    contracts = [
        ContractSpec(
            symbol="BTCUSDT",
            product_type="USDT-FUTURES",
            quote_coin="USDT",
            base_coin="BTC",
            status="normal",
            min_trade_num=None,
            size_multiplier=None,
            price_place=None,
            volume_24h_usdt=100_000_000,
            change_pct_24h=2.5,
            raw={},
        ),
        ContractSpec(
            symbol="LOWVOLUSDT",
            product_type="USDT-FUTURES",
            quote_coin="USDT",
            base_coin="LOW",
            status="normal",
            min_trade_num=None,
            size_multiplier=None,
            price_place=None,
            volume_24h_usdt=100_000,
            change_pct_24h=2.5,
            raw={},
        ),
    ]

    filtered = filter_contracts(settings, contracts)
    assert [c.symbol for c in filtered] == ["BTCUSDT"]
