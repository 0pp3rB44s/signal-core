"""Liquidity heatmap — read-only analyselaag (geen behavior change)."""

from market_data.liquidity_heatmap import MAX_SPREAD_BPS, build_liquidity_heatmap


def _book(bids, asks, mid=100.0, spread_bps=1.0, symbol="TESTUSDT"):
    bid_depth = sum(p * s for p, s in bids)
    ask_depth = sum(p * s for p, s in asks)
    return {
        "symbol": symbol,
        "bids": [{"price": p, "size": s} for p, s in bids],
        "asks": [{"price": p, "size": s} for p, s in asks],
        "mid_price": mid,
        "spread_bps": spread_bps,
        "bid_depth_notional": bid_depth,
        "ask_depth_notional": ask_depth,
    }


def _flat_levels(start, step, n=10, size=10.0):
    return [(start + i * step, size) for i in range(n)]


def test_wall_detection_finds_oversized_level():
    bids = _flat_levels(99.9, -0.1)
    bids[3] = (99.6, 100.0)  # 10x normaal -> duidelijke wall
    asks = _flat_levels(100.1, 0.1)
    result = build_liquidity_heatmap(_book(bids, asks))
    assert result["data_ok"]
    assert result["nearest_bid_wall_price"] == 99.6
    assert result["bid_wall_strength"] >= 3.0
    assert result["nearest_ask_wall_price"] == 0.0  # geen ask wall


def test_no_wall_on_uniform_book():
    result = build_liquidity_heatmap(_book(_flat_levels(99.9, -0.1), _flat_levels(100.1, 0.1)))
    assert result["bid_wall_strength"] == 0.0
    assert result["ask_wall_strength"] == 0.0
    assert not result["liquidity_risk_zone"]


def test_spread_risk_off():
    ok = build_liquidity_heatmap(_book(_flat_levels(99.9, -0.1), _flat_levels(100.1, 0.1), spread_bps=MAX_SPREAD_BPS - 1))
    wide = build_liquidity_heatmap(_book(_flat_levels(99.9, -0.1), _flat_levels(100.1, 0.1), spread_bps=MAX_SPREAD_BPS + 5))
    assert not ok["risk_off"]
    assert wide["risk_off"]


def test_no_data_is_neutral_not_crash():
    for payload in (None, {}, {"bids": [], "asks": []}, {"bids": [{"price": 1, "size": 1}], "asks": [], "mid_price": 0}):
        result = build_liquidity_heatmap(payload)
        assert result["data_ok"] is False
        assert result["liquidity_magnet_direction"] == "NEUTRAL"
        assert result["liquidity_above_score"] == 50.0
        assert not result["liquidity_risk_zone"]
        assert not result["risk_off"]


def test_long_short_symmetry():
    """Gespiegeld book moet gespiegelde output geven."""
    heavy_below = build_liquidity_heatmap(_book(
        [(99.9 - i * 0.1, 50.0) for i in range(10)],   # zware bids
        _flat_levels(100.1, 0.1, size=10.0),
    ))
    heavy_above = build_liquidity_heatmap(_book(
        _flat_levels(99.9, -0.1, size=10.0),
        [(100.1 + i * 0.1, 50.0) for i in range(10)],  # zware asks
    ))
    assert heavy_below["liquidity_magnet_direction"] == "DOWN"
    assert heavy_above["liquidity_magnet_direction"] == "UP"
    assert abs(heavy_below["liquidity_below_score"] - heavy_above["liquidity_above_score"]) < 3.0
    assert abs(heavy_below["imbalance"] + heavy_above["imbalance"]) < 0.05  # tegengesteld teken


def test_risk_zone_when_mid_sits_against_big_wall():
    asks = _flat_levels(100.05, 0.1)
    asks[0] = (100.05, 200.0)  # enorme wall op 5bps van mid
    result = build_liquidity_heatmap(_book(_flat_levels(99.9, -0.1), asks))
    assert result["liquidity_risk_zone"]
