from planning.trade_planner import low_vol_reclaim_v2_2_selection_gate


def test_known_good_historical_feature_row_passes_v2_2_selection():
    selected, participation_passed, pressure_passed = low_vol_reclaim_v2_2_selection_gate(
        participation_score=15.0,
        pressure_score=15.45,
    )

    assert selected is True
    assert participation_passed is True
    assert pressure_passed is True


def test_known_bad_inverse_selection_row_fails_on_pressure():
    selected, participation_passed, pressure_passed = low_vol_reclaim_v2_2_selection_gate(
        participation_score=15.0,
        pressure_score=25.42,
    )

    assert selected is False
    assert participation_passed is True
    assert pressure_passed is False


def test_low_participation_row_fails_without_relaxing_pressure_gate():
    selected, participation_passed, pressure_passed = low_vol_reclaim_v2_2_selection_gate(
        participation_score=2.75,
        pressure_score=15.0,
    )

    assert selected is False
    assert participation_passed is False
    assert pressure_passed is True


def test_v2_2_threshold_boundaries_are_frozen_and_strict():
    assert low_vol_reclaim_v2_2_selection_gate(10.0, 24.999)[0] is True
    assert low_vol_reclaim_v2_2_selection_gate(10.0, 25.0)[0] is False
