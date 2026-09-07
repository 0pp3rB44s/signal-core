"""Research-only cross-venue canonical market-state layer.

Implements the normalization contract frozen in
``reports/CROSS_VENUE_NORMALIZATION_CONTRACT_2026-09-07.md``. This package
reads already-collected clean-epoch segment files and produces a canonical,
venue-neutral representation plus a small set of frozen state-transition
primitives. It performs no alpha research, no PnL computation, no order
placement, and holds no authenticated client of any kind.

This package must never import ``app.main``, ``execution.*``, ``risk.*``, or
any authenticated client module. See ``tests/test_cross_venue_no_order_authority.py``
for the enforced boundary.
"""
