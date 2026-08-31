import pandas as pd

from research.speculative_lab.run_hv_pilot import event_rows, nonoverlapping, slippage_bps


def test_event_horizons_use_the_requested_forward_bar():
    rows = 120
    close = [100.0] * rows
    close[80] = 110.0
    close[81] = 111.0
    close[84] = 120.0
    close[92] = 99.0
    close[104] = 130.0
    quote = [100.0] * rows
    quote[80] = 1000.0
    data = pd.DataFrame({
        "ts": [i * 3_600_000 for i in range(rows)],
        "open": close, "high": [x * 1.01 for x in close],
        "low": [x * .99 for x in close], "close": close,
        "base_volume": quote, "quote_volume": quote,
    })

    events = pd.DataFrame(event_rows("TESTUSDT", data))
    event = events[(events.architecture == "attention_continuation") & (events.event_ts == 80 * 3_600_000)]
    values = event.set_index("horizon_h").signed_return_bps

    assert values.loc[1] == (111 / 110 - 1) * 10_000
    assert values.loc[4] == (120 / 110 - 1) * 10_000
    assert values.loc[12] == (99 / 110 - 1) * 10_000
    assert values.loc[24] == (130 / 110 - 1) * 10_000
    assert values.nunique() == 4


def test_slippage_is_size_aware_and_returns_unknown_beyond_depth():
    asks = [[100, 0.5], [101, 1.0]]
    assert slippage_bps(asks, "buy", 25) == 0
    assert slippage_bps(asks, "buy", 100) > 0
    assert slippage_bps(asks, "buy", 1_000) is None


def test_nonoverlapping_events_respect_holding_horizon_per_symbol():
    data = pd.DataFrame({
        "symbol": ["A", "A", "A", "B"],
        "event_ts": [0, 3_600_000, 4 * 3_600_000, 3_600_000],
    })
    kept = nonoverlapping(data, 4)
    assert list(kept.index) == [0, 2, 3]
