import json
from pathlib import Path

import pytest

from historical_data.bitget_archive import (
    DatasetQuality, acquire_dataset, canonicalize_rows, common_window,
    content_hash, paginate, validate_candles,
)


def row(ts, o=100, h=101, l=99, c=100, v=10):
    return [str(ts), str(o), str(h), str(l), str(c), str(v), str(v * c)]


def test_pagination_is_deterministic_and_moves_backward():
    calls = []
    pages = {
        4_500_000: [row(2_700_000), row(3_600_000)],
        2_700_000: [row(900_000), row(1_800_000)],
        900_000: [],
    }
    def fetch(symbol, start, end):
        calls.append((symbol, start, end))
        return pages[end]
    assert [int(value[0]) for value in paginate(fetch, "BTCUSDT", 0, 4_500_000)] == [2_700_000, 3_600_000, 900_000, 1_800_000]
    assert calls == [("BTCUSDT", 0, 4_500_000), ("BTCUSDT", 0, 2_700_000), ("BTCUSDT", 0, 900_000)]


def test_duplicate_removal_keeps_last_and_sorts():
    rows, duplicates, out_of_order = canonicalize_rows([row(1_800_000, c=1), row(900_000), row(1_800_000, c=2)], 0, 3_000_000)
    assert [item["timestamp"] for item in rows] == [900_000, 1_800_000]
    assert rows[-1]["close"] == 2
    assert duplicates == 1
    assert out_of_order == 1


def test_gap_detection_and_longest_gap():
    rows, duplicates, ordering = canonicalize_rows([row(0), row(900_000), row(3_600_000)], 0, 4_500_000)
    quality, gaps = validate_candles("BTCUSDT", rows, 0, 4_500_000, duplicates, ordering)
    assert quality.missing_count == 2
    assert quality.longest_gap_candles == 2
    assert gaps[0]["classification"] == "UNKNOWN"


def test_invalid_ohlc_is_counted_not_repaired():
    rows, duplicates, ordering = canonicalize_rows([row(0, o=100, h=99, l=98, c=100)], 0, 900_000)
    quality, _ = validate_candles("BTCUSDT", rows, 0, 900_000, duplicates, ordering)
    assert quality.invalid_ohlc_count == 1
    assert rows[0]["high"] == 99


def test_timestamp_alignment_failure():
    rows, duplicates, ordering = canonicalize_rows([row(1)], 0, 900_000)
    quality, _ = validate_candles("BTCUSDT", rows, 0, 900_000, duplicates, ordering)
    assert quality.timestamp_alignment_failures == 1


def test_canonical_schema_conversion_preserves_quote_volume():
    rows, _, _ = canonicalize_rows([row(0)], 0, 900_000)
    assert set(rows[0]) == {"timestamp", "open", "high", "low", "close", "volume_base", "volume_quote"}
    assert rows[0]["volume_quote"] == 1000


def test_common_window_uses_latest_first_and_earliest_last():
    def q(symbol, first, last):
        return DatasetQuality(symbol, 0, 10, first, last, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, "x", "NONE")
    assert common_window([q("A", 1, 9), q("B", 2, 8)]) == (2, 8)


def test_dataset_hash_is_key_order_independent():
    assert content_hash({"a": 1, "b": 2}) == content_hash({"b": 2, "a": 1})


def test_acquisition_is_isolated_and_writes_raw_canonical_atomically(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    class Source:
        @staticmethod
        def fetch_page(symbol, start, end):
            return [row(0), row(900_000)] if end > 900_000 else [row(0)]
    output = tmp_path / "dataset"
    manifest = acquire_dataset(["BTCUSDT"], 0, 1_800_000, output, Source())
    assert manifest["quality"][0]["candle_count"] == 2
    assert json.loads((output / "raw/BTCUSDT.json").read_text())
    assert json.loads((output / "canonical/BTCUSDT.json").read_text())
    assert not list(output.rglob("*.tmp"))
    assert not (tmp_path / ".env").exists()


def test_invalid_acquisition_fails_closed(tmp_path):
    class Source:
        @staticmethod
        def fetch_page(symbol, start, end):
            return [row(0, h=90)]
    with pytest.raises(ValueError, match="invalid historical data"):
        acquire_dataset(["BTCUSDT"], 0, 900_000, tmp_path / "dataset", Source())


def test_repeated_canonicalization_is_identical():
    source = [row(1_800_000), row(0), row(900_000), row(900_000)]
    assert canonicalize_rows(source, 0, 3_000_000) == canonicalize_rows(source, 0, 3_000_000)
