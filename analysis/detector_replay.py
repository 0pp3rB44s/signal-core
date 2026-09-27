from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from app.config import Settings
from clients.schemas import Candle
from market_features.engine import FeatureInputs, aggregate_candles, build_market_snapshot
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.momentum_breakout import MomentumBreakdownStrategy, MomentumBreakoutStrategy
from strategies.strategies.continuation import ContinuationStrategy
from strategies.strategies.low_vol_reclaim import LowVolReclaimStrategy


def replay_snapshot(symbol: str, candles: list[Candle], *, as_of_timestamp_ms: int, inputs: FeatureInputs | None = None):
    hourly = aggregate_candles(candles, "15m", "1h", as_of_timestamp_ms)
    return build_market_snapshot(symbol, candles, hourly, as_of_timestamp_ms=as_of_timestamp_ms, inputs=inputs or FeatureInputs())


def contiguous_suffix(candles: list[Candle], step: int = 900_000) -> list[Candle]:
    start = len(candles) - 1
    while start > 0 and candles[start].timestamp_ms - candles[start - 1].timestamp_ms == step:
        start -= 1
    return candles[start:]


def run(root: Path, limit_per_symbol: int = 1200) -> dict:
    settings = Settings(_env_file=None)
    detectors = (LiquiditySweepStrategy(settings), MomentumBreakoutStrategy(settings), MomentumBreakdownStrategy(settings), ContinuationStrategy(), LowVolReclaimStrategy())
    signals: Counter[str] = Counter(); attempts: Counter[str] = Counter(); gates: dict[str, Counter[str]] = defaultdict(Counter); examples: dict[str, list[dict]] = defaultdict(list); evaluated = 0
    for path in sorted((root / "data" / "history").glob("*_15m.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        candles = [Candle(int(c[0] if isinstance(c, list) else c.get("timestamp_ms", c.get("timestamp"))), *map(float, (c[1:6] if isinstance(c, list) else (c["open"], c["high"], c["low"], c["close"], c.get("volume_base", 0))))) for c in raw]
        start = max(200, len(candles) - limit_per_symbol)
        for index in range(start, len(candles)):
            series = contiguous_suffix(candles[max(0, index - 319):index + 1])
            as_of_timestamp_ms = candles[index].timestamp_ms + 900_000
            try: snapshot = replay_snapshot(path.stem.removesuffix("_15m"), series, as_of_timestamp_ms=as_of_timestamp_ms)
            except ValueError: continue
            evaluated += 1
            for detector in detectors:
                attempts[detector.name] += 1
                reasons: list[str] = []
                attribute = "_log_rejection" if hasattr(detector, "_log_rejection") else "_reject" if hasattr(detector, "_reject") else None
                original = getattr(detector, attribute) if attribute else None
                if attribute:
                    def capture(*args, **kwargs):
                        if "reason" in kwargs: reasons.append(str(kwargs["reason"]))
                        elif attribute == "_log_rejection" and len(args) >= 2: reasons.append(str(args[1]))
                        elif len(args) >= 3: reasons.append(str(args[2]))
                        elif len(args) >= 2: reasons.append(str(args[1]))
                    setattr(detector, attribute, capture)
                try: candidate = detector.detect(snapshot)
                finally:
                    if attribute: setattr(detector, attribute, original)
                gate = "signal" if candidate else reasons[0] if reasons else "no_explicit_reject"
                gates[detector.name][gate] += 1
                if candidate: signals[candidate.strategy] += 1
                if len(examples[detector.name]) < 5 and not candidate:
                    context = snapshot.context.get("breakout", {})
                    examples[detector.name].append({"gate": gate, "timestamp": snapshot.primary.closed_candle_timestamp_ms, "volume_ratio": round(snapshot.primary.volume_ratio_20, 4), "volatility_rank": snapshot.volatility_rank, "pressure_score": context.get("pressure_score"), "expansion_probability": snapshot.context.get("volatility", {}).get("expansion_probability"), "spread_bps": snapshot.context.get("spread_bps")})
    payload = {"evaluated_snapshots": evaluated, "attempts": dict(sorted(attempts.items())), "first_gate_counts": {key: dict(value.most_common()) for key, value in sorted(gates.items())}, "boundary_examples": dict(examples), "signals": dict(sorted(signals.items()))}
    payload["replay_hash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return payload


if __name__ == "__main__":
    print(json.dumps(run(Path(__file__).resolve().parents[1]), indent=2, sort_keys=True))
