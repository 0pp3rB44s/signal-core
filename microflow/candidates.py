from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class FrozenResearchSpec:
    ofi_window: str = "5s"
    ofi_threshold: float = 0.25
    book_threshold: float = 0.20
    microprice_edge_bps: float = 0.0
    persistence_ms: int = 2_000
    neutralize_ms: int = 1_000
    cooldown_ms: int = 60_000
    max_spread_bps: float = 5.0
    freshness_ms: int = 1_000
    minimum_60s_range_bps: float = 10.0
    tp_bps: float = 40.0
    sl_bps: float = 20.0
    max_hold_ms: int = 600_000
    spec_id: str = "microflow-v1-baseline-20260812"

    def digest(self) -> str:
        body = "|".join(f"{name}={value}" for name, value in sorted(self.__dict__.items()))
        return hashlib.sha256(body.encode()).hexdigest()


class CandidateEpisodeSampler:
    """Emit at most one research candidate per persistent directional episode."""

    def __init__(self, spec: FrozenResearchSpec | None = None) -> None:
        self.spec = spec or FrozenResearchSpec()
        self.forming_direction: str | None = None
        self.forming_since_ms: int | None = None
        self.active_direction: str | None = None
        self.neutral_since_ms: int | None = None
        self.cooldown_until_ms = 0

    def _direction(self, snapshot: dict) -> str | None:
        flow = snapshot["trade_flow"][self.spec.ofi_window]
        ofi = flow.get("ofi")
        book = snapshot["book"].get("book_imbalance_top5")
        edge = snapshot["microprice"].get("microprice_vs_mid_bps")
        freshness = snapshot["freshness"]
        movement = snapshot["trade_flow"]["60s"].get("realized_range_bps")
        fresh = (
            freshness.get("sequence_valid") is True
            and freshness.get("trade_stream_age_ms") is not None
            and freshness.get("book_stream_age_ms") is not None
            and freshness["trade_stream_age_ms"] <= self.spec.freshness_ms
            and freshness["book_stream_age_ms"] <= self.spec.freshness_ms
        )
        common = (
            fresh
            and snapshot["book"]["spread_bps"] <= self.spec.max_spread_bps
            and movement is not None
            and movement >= self.spec.minimum_60s_range_bps
            and ofi is not None and book is not None and edge is not None
        )
        if not common:
            return None
        if ofi >= self.spec.ofi_threshold and book >= self.spec.book_threshold and edge > self.spec.microprice_edge_bps:
            return "LONG"
        if ofi <= -self.spec.ofi_threshold and book <= -self.spec.book_threshold and edge < -self.spec.microprice_edge_bps:
            return "SHORT"
        return None

    def observe(self, snapshot: dict) -> dict | None:
        now_ms = int(snapshot["timestamp_local"])
        direction = self._direction(snapshot)
        if self.active_direction:
            if direction == self.active_direction:
                self.neutral_since_ms = None
            else:
                self.neutral_since_ms = self.neutral_since_ms or now_ms
                if now_ms - self.neutral_since_ms >= self.spec.neutralize_ms:
                    self.active_direction = None
                    self.cooldown_until_ms = now_ms + self.spec.cooldown_ms
                    self.neutral_since_ms = None
            return None
        if now_ms < self.cooldown_until_ms or direction is None:
            self.forming_direction = None
            self.forming_since_ms = None
            return None
        if direction != self.forming_direction:
            self.forming_direction = direction
            self.forming_since_ms = now_ms
            return None
        persistence_ms = now_ms - int(self.forming_since_ms or now_ms)
        if persistence_ms < self.spec.persistence_ms:
            return None
        self.active_direction = direction
        self.forming_direction = None
        self.forming_since_ms = None
        reference = float(snapshot["microprice"]["mid_price"])
        sign = 1.0 if direction == "LONG" else -1.0
        raw_id = f"{snapshot['symbol']}|{direction}|{now_ms}|{reference:.12f}|{self.spec.digest()}"
        candidate_id = hashlib.sha256(raw_id.encode()).hexdigest()
        return {
            "schema_version": "microflow_candidate_v1",
            "candidate_id": candidate_id,
            "strategy_version": "microflow_scalper_v1_research",
            "spec_id": self.spec.spec_id,
            "spec_hash": self.spec.digest(),
            "symbol": snapshot["symbol"],
            "side": direction,
            "signal_ts": now_ms,
            "persistence_ms": persistence_ms,
            "entry_reference": reference,
            "take_profit": reference * (1.0 + sign * self.spec.tp_bps / 10_000.0),
            "stop_loss": reference * (1.0 - sign * self.spec.sl_bps / 10_000.0),
            "tp_bps": self.spec.tp_bps,
            "sl_bps": self.spec.sl_bps,
            "max_hold_ms": self.spec.max_hold_ms,
            "features": snapshot,
            "research_only": True,
            "orders_allowed": False,
        }
