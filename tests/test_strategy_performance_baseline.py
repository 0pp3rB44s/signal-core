from scripts.strategy_performance_baseline import STRATEGIES, build_funnel_rows, sample_quality


def test_sample_quality_contract_boundaries():
    assert sample_quality(0) == "insufficient"
    assert sample_quality(29) == "insufficient"
    assert sample_quality(30) == "weak"
    assert sample_quality(99) == "weak"
    assert sample_quality(100) == "moderate"
    assert sample_quality(299) == "moderate"
    assert sample_quality(300) == "stronger evidence"


def test_detector_funnel_counting_is_descriptive_only():
    summaries = [{"strategy": row["strategy"], "candidates_accepted": 1, "orders_filled": 1, "closed_trades": 1} for row in STRATEGIES]
    keys = {row["strategy"]: f"{row['strategy']}_true" for row in STRATEGIES}
    debug = {"snapshots_evaluated": 10, "momentum_breakout_true": 2}
    rows = build_funnel_rows(debug, summaries, keys)
    breakout = next(row for row in rows if row["strategy"] == "momentum_breakout")
    assert breakout["detector_invoked"] == 10
    assert breakout["raw_detector_true"] == 2
    assert breakout["unattributed_detector_false"] == 8
    assert breakout["selector_accepted"] == 0
    fallback = next(row for row in rows if row["strategy"] == "adaptive_momentum_continuation")
    assert fallback["detector_invoked"] == 0
    assert fallback["main_rejection_reason"] == "DISABLED_FALLBACK"
