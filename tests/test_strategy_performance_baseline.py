from scripts.strategy_performance_baseline import sample_quality


def test_sample_quality_contract_boundaries():
    assert sample_quality(0) == "insufficient"
    assert sample_quality(29) == "insufficient"
    assert sample_quality(30) == "weak"
    assert sample_quality(99) == "weak"
    assert sample_quality(100) == "moderate"
    assert sample_quality(299) == "moderate"
    assert sample_quality(300) == "stronger evidence"
