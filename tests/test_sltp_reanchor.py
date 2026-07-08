"""SL/TP-herankering op de echte fill (bug 2026-07-08).

De planner ankert geometrie op latest_close, maar de market-order vult op de
live prijs. Zonder herankering verschrompelt de stopafstand. Deze tests pinnen
de her-ankering-wiskunde: de ontworpen prijs-RATIO's blijven behouden t.o.v.
de echte fill, ongeacht de drift.
"""


def _reanchor(plan_entry, plan_stop, plan_tp, fill):
    scale = fill / plan_entry
    return round(plan_stop * scale, 8), round(plan_tp * scale, 8)


def _dist_bps(a, b):
    return abs(a - b) / b * 10000


def test_reanchor_preserves_stop_and_tp_distance_short():
    # SHORT: plan entry 100, stop 100.6 (60bps), tp 98.7 (130bps). Fill drift
    # +0,3% naar 100.3 (richting stop). Zonder herankering zou de stop nog maar
    # 30bps weg zijn; met herankering weer 60bps.
    plan_entry, plan_stop, plan_tp, fill = 100.0, 100.6, 98.7, 100.3
    naive_stop_bps = _dist_bps(plan_stop, fill)          # verschrompeld
    stop, tp = _reanchor(plan_entry, plan_stop, plan_tp, fill)
    assert naive_stop_bps < 40, "zonder herankering is de stop inderdaad te krap"
    assert abs(_dist_bps(stop, fill) - 60) < 0.5         # weer 60bps
    assert abs(_dist_bps(tp, fill) - 130) < 0.5          # tp weer 130bps
    # RR behouden op ~1.30/0.60
    assert abs((_dist_bps(tp, fill) / _dist_bps(stop, fill)) - (130 / 60)) < 0.05


def test_reanchor_preserves_distance_long():
    plan_entry, plan_stop, plan_tp, fill = 50.0, 49.7, 50.65, 49.85
    stop, tp = _reanchor(plan_entry, plan_stop, plan_tp, fill)
    assert stop < fill < tp  # long: stop onder, tp boven
    assert abs(_dist_bps(stop, fill) - _dist_bps(plan_stop, plan_entry)) < 0.5
    assert abs(_dist_bps(tp, fill) - _dist_bps(plan_tp, plan_entry)) < 0.5


def test_no_drift_is_identity():
    stop, tp = _reanchor(100.0, 100.6, 98.7, 100.0)
    assert stop == 100.6 and tp == 98.7
