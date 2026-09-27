from clients.schemas import Candle
from data.market_fetcher import MarketFetcher


def test_ema_returns_float() -> None:
    values = [float(i) for i in range(1, 101)]
    ema = MarketFetcher._ema(values, 20)
    assert isinstance(ema, float)
    assert ema > 0


def test_alignment_labels() -> None:
    # _trend_label bestaat niet meer; alignment is de huidige trend-API.
    assert MarketFetcher._alignment("bullish", "bullish") == "aligned_bullish"
    assert MarketFetcher._alignment("bearish", "bearish") == "aligned_bearish"
    assert MarketFetcher._alignment("bullish", "bearish") == "conflicted"
    assert MarketFetcher._alignment("neutral", "bullish") == "mixed"
    assert MarketFetcher._alignment(None, None) == "mixed"


def test_score_hint_stays_bounded() -> None:
    class Dummy:
        def __init__(self, trend: str, volume_ratio_20: float, range_pct: float):
            self.trend = trend
            self.volume_ratio_20 = volume_ratio_20
            self.range_pct = range_pct

    class Contract:
        def __init__(self, volume_24h_usdt: float):
            self.volume_24h_usdt = volume_24h_usdt

    score = MarketFetcher._score_hint(
        primary=Dummy("bullish", 5.0, 2.0),
        confirmation=Dummy("bullish", 1.0, 1.0),
        contract=Contract(100_000_000),
    )
    assert 0.0 <= score <= 100.0
