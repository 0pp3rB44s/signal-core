"""Entry routing observability: what the market did between plan and fill.

Nothing here places, prices, sizes, cancels or times an order. It records.

WHY THIS EXISTS
---------------
Every one of the first 16 post-hotfix entries was labelled
``maker_then_market_fallback``, 15 of 16 filled adverse to plan, and the median
adverse move between plan and fill (17.3 bps) was the same size as the median
favourable move that remained after the fill (17.0 bps). That is consistent with
adverse selection in the maker leg, and equally consistent with three other
stories. None of them can be told apart, because the label names the route that
was *attempted* and no price is recorded between plan and fill.

SIGN CONVENTION — ONE RULE, NO EXCEPTIONS
-----------------------------------------
**Every ``*_drag_bps`` and ``*_bps`` execution field is positive when adverse
and negative when favourable, for longs and for shorts alike.** A long filled
above its plan and a short filled below its plan both report positive drag,
because both paid.

This matches the pre-existing ``slippage_pct`` in execution_service, so the
codebase carries one execution sign convention rather than two.
``assert_matches_legacy_slippage_convention`` pins that agreement, since a
future edit to either is exactly the kind of silent inversion this module is
supposed to make visible.

Two primitives, deliberately not interchangeable:

``execution_drag_bps``
    What the move between two prices *cost the entry*. Buying higher hurts,
    selling lower hurts, so the arithmetic flips with direction. Everything
    from plan through fill is built on this. Positive = adverse.

``position_return_bps``
    What an already-open position has *gained*. Price up helps a long, price
    down helps a short. Everything after the fill uses this, and it keeps the
    opposite sense on purpose: a gain is positive. Conflating the two is how a
    spread ends up counted twice or not at all, so they are named apart and a
    test asserts they disagree where they should.

``entry_advantage_bps`` remains available as the negation of
``execution_drag_bps`` for callers that want a favourable-positive reading. It
is not used in the emitted metrics.

PRICE VOCABULARY
----------------
Five prices are kept apart on purpose; conflating them is how a spread gets
counted twice or not at all.

``bid``   best bid from ``/api/v2/mix/market/merge-depth``
``ask``   best ask from the same book snapshot
``mid``   ``(bid + ask) / 2`` — only when both sides exist, else NULL
``mark``  ``markPrice`` from ``/api/v2/mix/market/symbol-price``; what the
          exchange liquidates and triggers TP/SL against
``last``  last traded price from the same endpoint

Reference price per metric is stated on the metric itself. Where a metric needs
a price that was not captured, the result is ``None`` — never zero. Zero is a
real price on no instrument here, so it must never stand in for absence.

TIMESTAMPS
----------
UTC, millisecond resolution, taken at the moment the stage is recorded rather
than derived from any exchange field. Exchange timestamps are recorded
alongside where the payload carries them, under their own keys, so local and
remote clocks are never silently mixed.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

# --- route stages -----------------------------------------------------------

STAGE_PLAN = "plan"
STAGE_MAKER_SUBMIT = "maker_submit"
STAGE_MAKER_ACK = "maker_ack"
STAGE_MAKER_FILL = "maker_fill"
STAGE_MAKER_END = "maker_end"
STAGE_FALLBACK_SUBMIT = "fallback_submit"
STAGE_FALLBACK_ACK = "fallback_ack"
STAGE_FALLBACK_FILL = "fallback_fill"
STAGE_POSITION_CONFIRMED = "position_confirmed"
STAGE_PROTECTION_CONFIRMED = "protection_confirmed"

#: How the position was actually acquired, as opposed to how it was attempted.
ROUTE_MAKER_FULL = "MAKER_FULL"
ROUTE_MAKER_PARTIAL_THEN_FALLBACK = "MAKER_PARTIAL_THEN_FALLBACK"
ROUTE_FALLBACK_FULL = "FALLBACK_FULL"
ROUTE_MAKER_FILLED_DURING_CANCEL = "MAKER_FILLED_DURING_CANCEL"
ROUTE_ADOPTED_AFTER_RECONCILIATION = "ADOPTED_AFTER_RECONCILIATION"
ROUTE_UNKNOWN = "UNKNOWN"

_LONG = "LONG"
_SHORT = "SHORT"


def utc_now_iso_ms() -> str:
    """UTC timestamp with millisecond resolution."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _positive(value: Any) -> float | None:
    """A usable price, or None. Zero and junk are absence, not data."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number != number:  # NaN != NaN
        return None
    return number


def execution_drag_bps(
    direction: str,
    from_price: Any,
    to_price: Any,
) -> float | None:
    """Basis points the move from ``from_price`` to ``to_price`` cost the entry.

    Positive is adverse: a long that ends up buying higher, a short that ends up
    selling lower. Negative is a gift. Returns None when either price is
    missing, because an absent price is not a zero move.
    """
    reference = _positive(from_price)
    candidate = _positive(to_price)
    if reference is None or candidate is None:
        return None
    if str(direction).upper() == _LONG:
        delta = candidate - reference
    else:
        delta = reference - candidate
    return round(delta / reference * 10_000, 4)


def entry_advantage_bps(
    direction: str,
    from_price: Any,
    to_price: Any,
) -> float | None:
    """Favourable-positive reading of :func:`execution_drag_bps`.

    Kept for callers that think in advantage rather than cost. Not used by the
    emitted metrics: one convention reaches the dataset, and it is drag.
    """
    drag = execution_drag_bps(direction, from_price, to_price)
    return None if drag is None else round(-drag, 4)


def position_return_bps(
    direction: str,
    entry_price: Any,
    later_price: Any,
) -> float | None:
    """Basis points an open position has gained by ``later_price``.

    Positive is profit. This is not ``entry_advantage_bps``: an entry is better
    when it is cheaper, a long position is better when the price has risen.
    """
    entry = _positive(entry_price)
    later = _positive(later_price)
    if entry is None or later is None:
        return None
    if str(direction).upper() == _LONG:
        delta = later - entry
    else:
        delta = entry - later
    return round(delta / entry * 10_000, 4)


def assert_matches_legacy_slippage_convention(
    direction: str,
    expected_entry: float,
    actual_entry: float,
    legacy_slippage_pct: float,
) -> bool:
    """True when this module agrees in sign and size with ``slippage_pct``.

    execution_service records ``slippage_pct`` positive-when-adverse, in
    percent. This module records drag positive-when-adverse, in basis points.
    They must be the same number times 100. Pinning it means a future edit to
    either one fails a test instead of quietly producing a dataset where half
    the rows mean the opposite of the other half.
    """
    drag = execution_drag_bps(direction, expected_entry, actual_entry)
    if drag is None:
        return False
    return abs(drag - legacy_slippage_pct * 100) < 0.01


@dataclass
class Quote:
    """One market snapshot. Every field is optional; absence is None."""

    captured_at: str = field(default_factory=utc_now_iso_ms)
    bid: float | None = None
    ask: float | None = None
    mid: float | None = None
    mark: float | None = None
    last: float | None = None
    spread_bps: float | None = None
    source: str = "unavailable"

    @classmethod
    def unavailable(cls, reason: str) -> Quote:
        return cls(source=f"unavailable:{reason}")


def capture_quote(client: Any, symbol: str, log: Any = None) -> Quote:
    """Best-effort market snapshot. Never raises, never blocks an order.

    Two independent GETs: the book gives bid/ask/mid, the price endpoint gives
    mark/last. Either may fail on its own and the other still counts, so they
    are caught separately rather than as one block.
    """
    quote = Quote()
    got_any = False

    try:
        book = client.get_orderbook(symbol=symbol, limit=1) or {}
        quote.bid = _positive(book.get("best_bid"))
        quote.ask = _positive(book.get("best_ask"))
        if quote.bid is not None and quote.ask is not None:
            quote.mid = round((quote.bid + quote.ask) / 2, 10)
            quote.spread_bps = round((quote.ask - quote.bid) / quote.mid * 10_000, 4)
        got_any = True
    except Exception as exc:  # noqa: BLE001 - observability must not raise
        if log is not None:
            log.info("ENTRY_QUOTE_BOOK_UNAVAILABLE | %s | error=%s", symbol, exc)

    try:
        payload = client.get_symbol_price(symbol=symbol) or {}
        rows = payload.get("data") or []
        row = rows[0] if isinstance(rows, list) and rows else (rows if isinstance(rows, dict) else {})
        quote.mark = _positive(row.get("markPrice"))
        quote.last = _positive(row.get("lastPr") or row.get("last") or row.get("price"))
        got_any = True
    except Exception as exc:  # noqa: BLE001
        if log is not None:
            log.info("ENTRY_QUOTE_PRICE_UNAVAILABLE | %s | error=%s", symbol, exc)

    quote.source = "bitget" if got_any else "unavailable:all"
    return quote


@dataclass
class RouteStage:
    """One observed point in the entry lifecycle."""

    stage: str
    at: str = field(default_factory=utc_now_iso_ms)
    quote: Quote | None = None
    order_id: str | None = None
    client_oid: str | None = None
    order_price: float | None = None
    fill_price: float | None = None
    size_requested: float | None = None
    size_filled: float | None = None
    remaining_size: float | None = None
    exchange_order_status: str | None = None
    api_latency_ms: float | None = None
    maker_wait_elapsed_ms: float | None = None
    reason: str | None = None
    exchange_timestamp_ms: int | None = None


class EntryRoutingRecorder:
    """Accumulates one entry lifecycle and writes exactly one row.

    Deliberately holds no outcome fields. Post-fill returns live in a separate
    file written by a separate component, so a pre-entry consumer reading this
    file cannot reach them even by mistake. See FORBIDDEN_OUTCOME_KEYS.
    """

    #: Anything that could only be known after the fill. Never in this record.
    FORBIDDEN_OUTCOME_KEYS = frozenset({
        "post_fill_return_10s_bps",
        "post_fill_return_30s_bps",
        "post_fill_return_60s_bps",
        "post_fill_mfe_60s_bps",
        "post_fill_mae_60s_bps",
        "realized_pnl",
        "exchange_truth_pnl",
        "net_pnl",
        "closed_reason",
        "closed_at",
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
    })

    _write_lock = threading.Lock()

    def __init__(
        self,
        *,
        lifecycle_id: str,
        plan_id: str,
        candidate_id: str,
        symbol: str,
        direction: str,
        planned_entry: float | None,
        intended_route: str,
        size_requested: float | None,
        log: Any = None,
        path: str = "logs/entry_routing.jsonl",
    ) -> None:
        self.lifecycle_id = lifecycle_id
        self.plan_id = plan_id
        self.candidate_id = candidate_id
        self.symbol = symbol
        self.direction = str(direction).upper()
        self.planned_entry = _positive(planned_entry)
        self.intended_route = intended_route
        self.size_requested = size_requested
        self.log = log
        self.path = path
        self.stages: list[RouteStage] = []
        self.pre_entry_features: dict[str, Any] = {}

    # --- recording ---------------------------------------------------------

    def record(self, stage: str, **kwargs: Any) -> RouteStage:
        """Append one stage. Unknown values stay absent rather than becoming 0."""
        entry = RouteStage(stage=stage, **kwargs)
        self.stages.append(entry)
        return entry

    def set_pre_entry_features(self, features: dict[str, Any]) -> None:
        """Attach the snapshot the bot actually had before entering.

        Outcome keys are rejected loudly: this record must stay usable as
        training or gating input without leaking the future into it.
        """
        leaked = sorted(self.FORBIDDEN_OUTCOME_KEYS.intersection(features))
        if leaked:
            raise ValueError(
                f"post-fill fields may not enter the pre-entry snapshot: {leaked}"
            )
        self.pre_entry_features = dict(features)

    def safe_set_pre_entry_features(self, features: dict[str, Any]) -> bool:
        """Attach features from inside the order path, never raising.

        ``set_pre_entry_features`` raises on a leak, which is right for a
        library invariant and wrong for the live entry path: a plan note that
        happened to be named like an outcome field would abort a trade the bot
        had already decided to take. Here the offending keys are dropped, the
        event is logged loudly, and the entry proceeds untouched.
        """
        try:
            self.set_pre_entry_features(features)
            return True
        except ValueError:
            cleaned = {
                key: value for key, value in features.items()
                if key not in self.FORBIDDEN_OUTCOME_KEYS
            }
            dropped = sorted(self.FORBIDDEN_OUTCOME_KEYS.intersection(features))
            self.pre_entry_features = cleaned
            if self.log is not None:
                self.log.warning(
                    "ENTRY_SNAPSHOT_OUTCOME_KEYS_DROPPED | %s | lifecycle=%s | dropped=%s",
                    self.symbol, self.lifecycle_id, dropped,
                )
            return False
        except Exception as exc:  # noqa: BLE001 - observability must not raise
            if self.log is not None:
                self.log.warning(
                    "ENTRY_SNAPSHOT_ATTACH_FAILED | %s | lifecycle=%s | error=%s",
                    self.symbol, self.lifecycle_id, exc,
                )
            return False

    def stage(self, name: str) -> RouteStage | None:
        for entry in self.stages:
            if entry.stage == name:
                return entry
        return None

    def _quote_at(self, stage_name: str) -> Quote | None:
        found = self.stage(stage_name)
        return found.quote if found else None

    def _mid_at(self, stage_name: str) -> float | None:
        quote = self._quote_at(stage_name)
        return quote.mid if quote else None

    # --- classification ----------------------------------------------------

    def actual_fill_route(self) -> str:
        """How the position was really acquired.

        The attempted route is not the achieved route. A maker leg that filled
        half and a maker leg that filled nothing both used to be recorded as
        ``maker_then_market_fallback``, which makes the two indistinguishable
        in exactly the comparison this data is for.
        """
        maker_fill = self.stage(STAGE_MAKER_FILL)
        fallback_fill = self.stage(STAGE_FALLBACK_FILL)
        maker_qty = (maker_fill.size_filled or 0.0) if maker_fill else 0.0
        fallback_qty = (fallback_fill.size_filled or 0.0) if fallback_fill else 0.0

        if maker_fill is not None and maker_fill.reason == "filled_during_cancel":
            return ROUTE_MAKER_FILLED_DURING_CANCEL
        if maker_qty > 0 and fallback_qty > 0:
            return ROUTE_MAKER_PARTIAL_THEN_FALLBACK
        if maker_qty > 0:
            return ROUTE_MAKER_FULL
        if fallback_qty > 0:
            return ROUTE_FALLBACK_FULL
        return ROUTE_UNKNOWN

    def final_fill_price(self) -> float | None:
        for stage_name in (STAGE_FALLBACK_FILL, STAGE_MAKER_FILL, STAGE_POSITION_CONFIRMED):
            found = self.stage(stage_name)
            if found and _positive(found.fill_price):
                return _positive(found.fill_price)
        return None

    # --- the twelve metrics ------------------------------------------------

    def metrics(self) -> dict[str, float | None]:
        """Execution decomposition. Positive is adverse, longs and shorts alike.

        ``total_execution_drag_bps`` is measured end to end rather than summed
        from the components, because the components are individually allowed to
        be None. A sum over partial data would silently under-report the total,
        which is the one number that must stay honest.
        """
        direction = self.direction
        fill = self.final_fill_price()
        submit_mid = self._mid_at(STAGE_MAKER_SUBMIT) or self._mid_at(STAGE_FALLBACK_SUBMIT)
        fill_quote = self._quote_at(STAGE_FALLBACK_FILL) or self._quote_at(STAGE_MAKER_FILL)
        fallback_fill = self.stage(STAGE_FALLBACK_FILL)

        return {
            # how stale the plan was by the time anything was submitted
            "plan_to_submit_bps": execution_drag_bps(direction, self.planned_entry, submit_mid),
            # what the market did between submitting and being filled
            "submit_to_fill_bps": execution_drag_bps(direction, submit_mid, fill),
            # the end-to-end number, measured, never summed
            "plan_to_fill_bps": execution_drag_bps(direction, self.planned_entry, fill),
            "total_execution_drag_bps": execution_drag_bps(direction, self.planned_entry, fill),
            # book width at each end, unsigned by nature
            "spread_at_submit_bps": (
                self._quote_at(STAGE_MAKER_SUBMIT).spread_bps
                if self._quote_at(STAGE_MAKER_SUBMIT)
                else (self._quote_at(STAGE_FALLBACK_SUBMIT).spread_bps
                      if self._quote_at(STAGE_FALLBACK_SUBMIT) else None)
            ),
            "spread_at_fill_bps": fill_quote.spread_bps if fill_quote else None,
            # drift across the maker wait window: the adverse-selection term
            "maker_wait_drift_bps": execution_drag_bps(
                direction, self._mid_at(STAGE_MAKER_SUBMIT), self._mid_at(STAGE_MAKER_END)
            ),
            # what crossing cost relative to the mid the fallback aimed at
            "fallback_cross_bps": execution_drag_bps(
                direction,
                self._mid_at(STAGE_FALLBACK_SUBMIT),
                (fallback_fill.fill_price if fallback_fill else None),
            ),
        }

    def metric_provenance(self) -> dict[str, str]:
        """MEASURED / DERIVED / UNKNOWN per decomposition component.

        A None value is not self-explanatory: it can mean the market data was
        never captured or that the stage never happened. The dataset needs both
        answers, so the reason travels with the number.
        """
        values = self.metrics()
        measured_from_prices = {"plan_to_fill_bps", "total_execution_drag_bps"}
        provenance: dict[str, str] = {}
        for name, value in values.items():
            if value is None:
                provenance[name] = "UNKNOWN"
            elif name in measured_from_prices:
                provenance[name] = "MEASURED"
            else:
                provenance[name] = "DERIVED"
        return provenance

    # --- output ------------------------------------------------------------

    def to_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {
            "schema_version": 1,
            "record_type": "entry_routing",
            "written_at": utc_now_iso_ms(),
            "lifecycle_id": self.lifecycle_id,
            "plan_id": self.plan_id,
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "direction": self.direction,
            "planned_entry": self.planned_entry,
            "size_requested": self.size_requested,
            "intended_route": self.intended_route,
            "actual_fill_route": self.actual_fill_route(),
            "fill_price": self.final_fill_price(),
            "stages": [asdict(s) for s in self.stages],
            "metrics": self.metrics(),
            "metric_provenance": self.metric_provenance(),
            "pre_entry_features": self.pre_entry_features,
        }
        leaked = sorted(self.FORBIDDEN_OUTCOME_KEYS.intersection(row))
        if leaked:
            raise ValueError(f"outcome field leaked into entry routing row: {leaked}")
        return row

    def write(self) -> bool:
        """Append the row. Returns False on failure; never raises into the caller."""
        try:
            row = self.to_row()
        except Exception as exc:  # noqa: BLE001
            if self.log is not None:
                self.log.warning(
                    "ENTRY_ROUTING_ROW_BUILD_FAILED | %s | lifecycle=%s | error=%s",
                    self.symbol, self.lifecycle_id, exc,
                )
            return False

        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            line = json.dumps(row, default=str, sort_keys=True)
            with self._write_lock:
                # Append-only and line-atomic: a crash mid-write must not
                # corrupt an earlier lifecycle's row.
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except Exception as exc:  # noqa: BLE001
            if self.log is not None:
                self.log.warning(
                    "ENTRY_ROUTING_WRITE_FAILED | %s | lifecycle=%s | error=%s",
                    self.symbol, self.lifecycle_id, exc,
                )
            return False

        if self.log is not None:
            metrics = row["metrics"]
            self.log.warning(
                "ENTRY_ROUTING | %s | lifecycle=%s | intended=%s | actual=%s | "
                "plan_to_fill_bps=%s | mid_to_fill_bps=%s | stages=%s",
                self.symbol, self.lifecycle_id, self.intended_route,
                row["actual_fill_route"], metrics.get("plan_to_fill_bps"),
                metrics.get("mid_to_fill_slippage_bps"), len(self.stages),
            )
        return True


def _atomic_replace(path: str, payload: str) -> None:
    """Unused by the append path; kept for callers that rewrite whole files."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False
    )
    try:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.replace(handle.name, path)
    except Exception:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
