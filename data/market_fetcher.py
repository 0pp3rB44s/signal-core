import json
import logging
import time
from typing import Callable, TypeVar
from pathlib import Path
from statistics import mean
from math import fabs

BASE_PATH = Path(__file__).resolve().parents[1]
REPORTS_PATH = BASE_PATH / "reports" / "backtests"

from app.config import Settings
from clients.bitget_rest import BitgetRestClient
from clients.schemas import ContractSpec, MarketSnapshot, SymbolSnapshot, TimeframeSnapshot
from data.cache import TTLCache
from data.normalizer import normalize_candles, normalize_contracts
from market_data.htf_regime import classify_htf_regime
from market_data.liquidity_heatmap import build_liquidity_heatmap
from market_data.orderbook_analyzer import OrderbookAnalyzer
from market_data.entry_quality import EntryQualityAnalyzer
from market_data.volatility_engine import VolatilityEngine
from market_data.breakout_engine import BreakoutEngine


T = TypeVar("T")


class MarketFetcher:
    @staticmethod
    def _candle_open(candle) -> float:
        if isinstance(candle, dict):
            return float(candle.get("open") or candle.get("o") or candle.get("open_price") or 0.0)
        if hasattr(candle, "open"):
            return float(candle.open)
        return 0.0

    @staticmethod
    def _candle_high(candle) -> float:
        if isinstance(candle, dict):
            return float(candle.get("high") or candle.get("h") or 0.0)
        if hasattr(candle, "high"):
            return float(candle.high)
        return 0.0

    @staticmethod
    def _candle_low(candle) -> float:
        if isinstance(candle, dict):
            return float(candle.get("low") or candle.get("l") or 0.0)
        if hasattr(candle, "low"):
            return float(candle.low)
        return 0.0

    @staticmethod
    def _candle_close(candle) -> float:
        if isinstance(candle, dict):
            return float(candle.get("close") or candle.get("c") or candle.get("close_price") or 0.0)
        if hasattr(candle, "close"):
            return float(candle.close)
        return 0.0

    @classmethod
    def _candle_structure_metrics(cls, candle) -> dict[str, float]:
        open_price = cls._candle_open(candle)
        high_price = cls._candle_high(candle)
        low_price = cls._candle_low(candle)
        close_price = cls._candle_close(candle)

        candle_range = high_price - low_price
        if open_price <= 0 or high_price <= 0 or low_price <= 0 or close_price <= 0 or candle_range <= 0:
            return {
                "candle_body_pct": 0.0,
                "upper_wick_pct": 0.0,
                "lower_wick_pct": 0.0,
                "close_strength": 0.5,
                "candle_direction": 0.0,
            }

        body = abs(close_price - open_price)
        upper_wick = high_price - max(open_price, close_price)
        lower_wick = min(open_price, close_price) - low_price
        close_strength = (close_price - low_price) / candle_range
        candle_direction = 1.0 if close_price > open_price else (-1.0 if close_price < open_price else 0.0)

        return {
            "candle_body_pct": max(0.0, min(100.0, (body / candle_range) * 100.0)),
            "upper_wick_pct": max(0.0, min(100.0, (upper_wick / candle_range) * 100.0)),
            "lower_wick_pct": max(0.0, min(100.0, (lower_wick / candle_range) * 100.0)),
            "close_strength": max(0.0, min(1.0, close_strength)),
            "candle_direction": candle_direction,
        }
    @staticmethod
    def _alignment(primary_trend: str | None, confirmation_trend: str | None) -> str:
        primary = str(primary_trend or "").lower()
        confirmation = str(confirmation_trend or "").lower()

        if primary == "bullish" and confirmation == "bullish":
            return "aligned_bullish"
        if primary == "bearish" and confirmation == "bearish":
            return "aligned_bearish"
        if primary in {"bullish", "bearish"} and confirmation in {"bullish", "bearish"}:
            return "conflicted"
        if primary in {"mixed", "neutral", ""} or confirmation in {"mixed", "neutral", ""}:
            return "mixed"
        return "mixed"



    @staticmethod
    def _volatility_rank(
        atr_percent: float | int | None = None,
        primary: object | None = None,
        contract: object | None = None,
        **_: object,
    ) -> float:
        try:
            if atr_percent is None and primary is not None:
                atr_percent = getattr(primary, "atr_percent", None)
            atr = float(atr_percent or 0.0)
        except (TypeError, ValueError):
            return 0.0

        if atr <= 0:
            return 0.0

        # VolatilityEngine returns ATR as percent points, e.g. 0.42 means 0.42%.
        # Map 0.20% ATR ~= 20, 1.00% ATR ~= 100.
        if atr <= 5.0:
            rank = atr * 100.0
        else:
            # Defensive fallback if a caller accidentally passes basis-points-like values.
            rank = atr

        return max(0.0, min(100.0, rank))

    @staticmethod
    def _score_hint(
        primary: object | None = None,
        confirmation: object | None = None,
        contract: object | None = None,
        alignment: str | None = None,
        volatility_rank: float | int | None = None,
        volume_ratio: float | int | None = None,
        spread_bps: float | int | None = None,
        **_: object,
    ) -> float:
        score = 50.0
        alignment_value = str(alignment or "").lower()
        primary_trend = str(getattr(primary, "trend", "") or "").lower()
        confirmation_trend = str(getattr(confirmation, "trend", "") or "").lower()

        if not alignment_value and primary_trend and confirmation_trend:
            alignment_value = MarketFetcher._alignment(primary_trend, confirmation_trend)

        if alignment_value in {"aligned_bullish", "aligned_bearish"}:
            score += 18.0
        elif alignment_value == "conflicted":
            score -= 12.0

        if primary_trend in {"bullish", "bearish"}:
            score += 6.0
        if confirmation_trend in {"bullish", "bearish"}:
            score += 4.0

        try:
            vol = float(volatility_rank or 0.0)
            if 15.0 <= vol <= 80.0:
                score += 8.0
            elif vol > 90.0:
                score -= 6.0
        except (TypeError, ValueError):
            pass

        try:
            vol_ratio = float(volume_ratio or getattr(primary, "volume_ratio_20", 0.0) or 0.0)
            if vol_ratio >= 1.20:
                score += 6.0
            elif 0 < vol_ratio < 0.70:
                score -= 6.0
        except (TypeError, ValueError):
            pass

        try:
            spread = float(spread_bps or 0.0)
            if spread > 18.0:
                score -= 10.0
            elif 0 < spread <= 8.0:
                score += 4.0
        except (TypeError, ValueError):
            pass

        try:
            volume_24h = float(getattr(contract, "volume_24h_usdt", 0.0) or 0.0)
            if volume_24h >= 100_000_000:
                score += 6.0
            elif 0 < volume_24h < 10_000_000:
                score -= 8.0
        except (TypeError, ValueError):
            pass

        return round(max(0.0, min(100.0, score)), 1)


    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        if not values:
            return 0.0
        if period <= 1:
            return float(values[-1])

        clean_values = [float(value) for value in values if value is not None]
        if not clean_values:
            return 0.0

        alpha = 2.0 / (float(period) + 1.0)
        ema_value = float(clean_values[0])
        for value in clean_values[1:]:
            ema_value = (float(value) * alpha) + (ema_value * (1.0 - alpha))
        return float(ema_value)


    def __init__(self, client: BitgetRestClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.log = logging.getLogger(self.__class__.__name__)
        self.contract_cache = TTLCache[list[ContractSpec]](ttl_seconds=settings.bitget_contract_cache_ttl_sec)
        self.orderbook_analyzer = OrderbookAnalyzer()
        self.entry_quality_analyzer = EntryQualityAnalyzer()
        self.volatility_engine = VolatilityEngine()
        self.breakout_engine = BreakoutEngine()

    _htf_regime_cache: dict[str, tuple[float, dict]] = {}
    HTF_REGIME_TTL_SECONDS = 1800.0  # 4H/1D veranderen traag; 30 min cache

    def _htf_regime_for(self, symbol: str) -> dict:
        """1D+4H regime met lange cache; fail-open naar neutral bij API-falen."""
        cached = self._htf_regime_cache.get(symbol)
        now = time.monotonic()
        if cached and (now - cached[0]) < self.HTF_REGIME_TTL_SECONDS:
            return cached[1]

        try:
            product = self.settings.bitget_product_type
            candles_4h = (self.client.get_candles(symbol=symbol, product_type=product, granularity="4H", limit=60).get("data") or [])
            candles_1d = (self.client.get_candles(symbol=symbol, product_type=product, granularity="1D", limit=40).get("data") or [])
            regime = classify_htf_regime(candles_4h, candles_1d)
        except Exception as exc:
            self.log.warning("HTF_REGIME_FETCH_FAILED | %s | error=%s", symbol, exc)
            regime = {"regime_1d": "neutral", "regime_4h": "neutral", "htf_regime": "neutral"}

        self._htf_regime_cache[symbol] = (now, regime)
        return regime

    _liquidity_heatmap_state: dict[str, dict] = {}

    def _persist_liquidity_heatmap(self, symbol: str, heatmap: dict) -> None:
        """Read-only snapshot per symbool voor het dashboard; nooit fataal."""
        try:
            self._liquidity_heatmap_state[str(symbol).upper()] = heatmap
            path = Path("state/liquidity_heatmap.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._liquidity_heatmap_state, indent=1))
        except Exception:
            pass

    def _fetch_with_retry(self, label: str, fetch_fn: Callable[[], T], *, attempts: int = 2) -> T:
        last_exception: Exception | None = None
        attempts = max(1, int(attempts or 1))

        for attempt in range(1, attempts + 1):
            try:
                return fetch_fn()
            except Exception as exc:
                last_exception = exc
                retryable = attempt < attempts
                self.log.warning(
                    "MARKET_FETCH_RETRY | label=%s | attempt=%s/%s | retryable=%s | error=%s",
                    label,
                    attempt,
                    attempts,
                    retryable,
                    exc,
                )
                if retryable:
                    time.sleep(0.35 * attempt)
                    continue

        raise RuntimeError(f"Market fetch failed after retries: {label}: {last_exception}") from last_exception

    def fetch_contracts(self, force_refresh: bool = False) -> list[ContractSpec]:
        cache_key = self.settings.bitget_product_type.upper()
        if not force_refresh:
            cached = self.contract_cache.get(cache_key)
            if cached is not None:
                return cached
        payload = self.client.get_contracts(product_type=self.settings.bitget_product_type)
        contracts = normalize_contracts(payload.get("data", []), product_type=self.settings.bitget_product_type)
        contracts = self._rank_contracts_for_rotation(contracts)
        self.contract_cache.set(cache_key, contracts)
        return contracts

    def _rank_contracts_for_rotation(self, contracts: list[ContractSpec]) -> list[ContractSpec]:
        """Rank contracts for adaptive watchlist rotation.

        The goal is not to blindly add the biggest coins. We prefer symbols with:
        - enough liquidity
        - active 24h movement
        - positive recent backtest expectancy
        - no strongly negative recent backtest behavior
        """
        expectancy = self._symbol_expectancy_map()
        configured_watchlist = set(self.settings.watchlist_symbols)

        def score(contract: ContractSpec) -> float:
            value = 0.0

            if contract.symbol in configured_watchlist:
                value += 35.0

            if contract.volume_24h_usdt:
                if contract.volume_24h_usdt >= 100_000_000:
                    value += 25.0
                elif contract.volume_24h_usdt >= 25_000_000:
                    value += 15.0
                elif contract.volume_24h_usdt >= float(self.settings.min_usdt_volume_24h):
                    value += 8.0

            if contract.change_pct_24h is not None:
                value += min(fabs(contract.change_pct_24h) * 4.0, 25.0)

            symbol_stats = expectancy.get(contract.symbol, {})
            exp = float(symbol_stats.get("expectancy", 0.0) or 0.0)
            trades = int(symbol_stats.get("trades", 0) or 0)
            tp1_hit_rate = float(symbol_stats.get("tp1_hit_rate", 0.0) or 0.0)

            if trades >= 3:
                value += max(-25.0, min(exp * 40.0, 25.0))
                value += min(tp1_hit_rate * 10.0, 10.0)
                if exp < 0:
                    value -= 20.0

            # Keep obvious low quality low-volume symbols at the back.
            if contract.volume_24h_usdt and contract.volume_24h_usdt < float(self.settings.min_usdt_volume_24h):
                value -= 30.0

            return value

        ranked = sorted(contracts, key=score, reverse=True)
        top_symbols = [c.symbol for c in ranked[: min(10, len(ranked))]]
        self.log.info("ADAPTIVE_ROTATION | top=%s", ",".join(top_symbols))
        return ranked

    @staticmethod
    def _symbol_expectancy_map() -> dict[str, dict]:
        path = REPORTS_PATH / "latest_summary.json"
        if not path.exists():
            return {}

        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return {}

        by_symbol = payload.get("by_symbol") or {}
        if not isinstance(by_symbol, dict):
            return {}

        return {str(symbol).upper(): stats for symbol, stats in by_symbol.items() if isinstance(stats, dict)}

    def fetch_contract_meta(self, symbol: str) -> ContractSpec:
        contracts = self.fetch_contracts(force_refresh=False)
        match = next((c for c in contracts if c.symbol == symbol.upper()), None)
        if match:
            return match
        payload = self.client.get_contracts(
            product_type=self.settings.bitget_product_type,
            symbol=symbol,
        )
        rows = normalize_contracts(payload.get("data", []), product_type=self.settings.bitget_product_type)
        if not rows:
            raise ValueError(f"No contract meta found for {symbol}")
        return rows[0]

    def fetch_snapshot(self, symbol: str, granularity: str | None = None) -> SymbolSnapshot:
        used_granularity = granularity or self.settings.bitget_default_granularity
        candle_payload = self._fetch_with_retry(
            label=f"candles:{symbol.upper()}:{used_granularity}",
            fetch_fn=lambda: self.client.get_candles(
                symbol=symbol,
                product_type=self.settings.bitget_product_type,
                granularity=used_granularity,
                limit=self.settings.bitget_candle_limit,
            ),
            attempts=2,
        )
        raw_candles = candle_payload.get("data", [])
        candles = normalize_candles(raw_candles)
        self._validate_candle_quality(
            symbol=symbol,
            granularity=used_granularity,
            raw_count=len(raw_candles),
            candles=candles,
        )
        meta = self.fetch_contract_meta(symbol)
        return SymbolSnapshot(
            symbol=symbol,
            granularity=used_granularity,
            candles=candles,
            contract_meta=meta.raw,
        )

    def _validate_candle_quality(
        self,
        symbol: str,
        granularity: str,
        raw_count: int,
        candles: list,
    ) -> None:
        normalized_count = len(candles)
        dropped_count = max(0, int(raw_count or 0) - normalized_count)

        if dropped_count:
            self.log.warning(
                "CANDLE_NORMALIZATION_DROPPED | %s | tf=%s | raw=%s | normalized=%s | dropped=%s",
                symbol.upper(),
                granularity,
                raw_count,
                normalized_count,
                dropped_count,
            )

        if normalized_count < 55:
            self.log.warning(
                "CANDLE_QUALITY_INSUFFICIENT | %s | tf=%s | candles=%s | required=55",
                symbol.upper(),
                granularity,
                normalized_count,
            )
            return

        timestamps = [int(c.timestamp_ms) for c in candles]
        duplicate_count = len(timestamps) - len(set(timestamps))
        if duplicate_count:
            self.log.error(
                "CANDLE_DUPLICATE_TIMESTAMP_AFTER_NORMALIZE | %s | tf=%s | duplicates=%s",
                symbol.upper(),
                granularity,
                duplicate_count,
            )

        intervals = [
            timestamps[index] - timestamps[index - 1]
            for index in range(1, len(timestamps))
            if timestamps[index] > timestamps[index - 1]
        ]
        interval_ms = max(1, int(sorted(intervals)[len(intervals) // 2])) if intervals else 1
        gap_threshold_ms = interval_ms * 1.5
        missing_gaps = [gap for gap in intervals if gap > gap_threshold_ms]
        missing_candle_estimate = sum(max(0, round(gap / interval_ms) - 1) for gap in missing_gaps)

        if missing_candle_estimate:
            self.log.error(
                "CANDLE_MISSING_GAPS_DETECTED | %s | tf=%s | estimated_missing=%s | gaps=%s | interval_ms=%s",
                symbol.upper(),
                granularity,
                missing_candle_estimate,
                len(missing_gaps),
                interval_ms,
            )

        range_pcts = []
        for candle in candles[-80:]:
            close_price = float(getattr(candle, "close", 0.0) or 0.0)
            high_price = float(getattr(candle, "high", 0.0) or 0.0)
            low_price = float(getattr(candle, "low", 0.0) or 0.0)
            if close_price <= 0 or high_price <= 0 or low_price <= 0:
                continue
            range_pcts.append(((high_price - low_price) / close_price) * 100.0)

        if len(range_pcts) >= 20:
            sorted_ranges = sorted(range_pcts)
            median_range_pct = sorted_ranges[len(sorted_ranges) // 2]
            latest_range_pct = range_pcts[-1]
            outlier_threshold_pct = max(median_range_pct * 6.0, 4.0)

            if latest_range_pct >= outlier_threshold_pct:
                self.log.error(
                    "CANDLE_OUTLIER_RANGE_DETECTED | %s | tf=%s | latest_range_pct=%.4f | median_range_pct=%.4f | threshold_pct=%.4f",
                    symbol.upper(),
                    granularity,
                    latest_range_pct,
                    median_range_pct,
                    outlier_threshold_pct,
                )

        volumes = [
            float(getattr(candle, "volume_base", 0.0) or 0.0)
            for candle in candles[-80:]
            if float(getattr(candle, "volume_base", 0.0) or 0.0) >= 0
        ]
        closes = []
        for candle in candles[-80:]:
            try:
                if isinstance(candle, dict):
                    close_value = (
                        candle.get("close")
                        or candle.get("c")
                        or candle.get("close_price")
                    )
                elif hasattr(candle, "close"):
                    close_value = candle.close
                else:
                    continue

                closes.append(float(close_value))
            except (TypeError, ValueError, IndexError, KeyError, AttributeError):
                continue

        if len(closes) < 20:
            self.log.warning(
                "CANDLE_CLOSE_SERIES_TOO_SHORT | %s | tf=%s | closes=%s | candles=%s",
                symbol.upper(),
                granularity,
                len(closes),
                len(candles),
            )
            return

        if len(volumes) >= 20:
            active_candle_volume = volumes[-1]
            latest_closed_volume = volumes[-2] if len(volumes) >= 2 else volumes[-1]
            historical_volumes = volumes[:-2] if len(volumes) >= 21 else volumes[:-1]

            if historical_volumes:
                avg_volume_20 = mean(historical_volumes[-20:])

                if active_candle_volume == 0.0 and latest_closed_volume > 0.0:
                    self.log.info(
                        "VOLUME_RATIO_SOURCE | %s | tf=%s | source=last_closed_candle | active_candle_volume=%.6f | closed_candle_volume=%.6f | avg_volume_20=%.6f",
                        symbol.upper(),
                        granularity,
                        float(active_candle_volume or 0.0),
                        float(latest_closed_volume or 0.0),
                        float(avg_volume_20 or 0.0),
                    )

                if latest_closed_volume == 0.0:
                    self.log.warning(
                        "VOLUME_RATIO_ZERO_CURRENT | %s | tf=%s | latest_closed_volume=%.6f | active_candle_volume=%.6f",
                        symbol.upper(),
                        granularity,
                        float(latest_closed_volume or 0.0),
                        float(active_candle_volume or 0.0),
                    )

                    if avg_volume_20 > 0:
                        self.log.warning(
                            "VOLUME_RATIO_ZERO_RESULT | %s | tf=%s | latest_closed_volume=%.6f | active_candle_volume=%.6f | avg_volume_20=%.6f | source=last_closed_candle",
                            symbol.upper(),
                            granularity,
                            float(latest_closed_volume or 0.0),
                            float(active_candle_volume or 0.0),
                            float(avg_volume_20 or 0.0),
                        )



    def build_timeframe_snapshot(self, snapshot):
        """Build a TimeframeSnapshot from raw SymbolSnapshot, or pass through if already built."""
        if hasattr(snapshot, "trend") and hasattr(snapshot, "latest_close"):
            return snapshot

        candles = list(getattr(snapshot, "candles", []) or [])
        if len(candles) < 20:
            raise ValueError(f"Not enough values for EMA20: {len(candles)}")

        def candle_close(candle) -> float:
            if isinstance(candle, dict):
                return float(candle.get("close") or candle.get("c") or candle.get("close_price") or 0.0)
            if hasattr(candle, "close"):
                return float(candle.close)
            raise TypeError(f"Unsupported candle type for close: {type(candle)}")

        def candle_high(candle) -> float:
            if isinstance(candle, dict):
                return float(candle.get("high") or candle.get("h") or 0.0)
            if hasattr(candle, "high"):
                return float(candle.high)
            raise TypeError(f"Unsupported candle type for high: {type(candle)}")

        def candle_low(candle) -> float:
            if isinstance(candle, dict):
                return float(candle.get("low") or candle.get("l") or 0.0)
            if hasattr(candle, "low"):
                return float(candle.low)
            raise TypeError(f"Unsupported candle type for low: {type(candle)}")

        def candle_volume(candle) -> float:
            if isinstance(candle, dict):
                return float(
                    candle.get("volume")
                    or candle.get("vol")
                    or candle.get("base_volume")
                    or candle.get("volume_base")
                    or 0.0
                )
            if hasattr(candle, "volume_base"):
                return float(candle.volume_base)
            if hasattr(candle, "volume"):
                return float(candle.volume)
            raise TypeError(f"Unsupported candle type for volume: {type(candle)}")

        closes = [candle_close(c) for c in candles if candle_close(c) > 0]
        if len(closes) < 20:
            raise ValueError(f"Not enough values for EMA20: {len(closes)}")

        latest_close = closes[-1]
        previous_close = closes[-2] if len(closes) >= 2 else latest_close
        latest_change_pct = ((latest_close - previous_close) / previous_close * 100.0) if previous_close else 0.0

        recent = candles[-20:]
        highs = [candle_high(c) for c in recent]
        lows = [candle_low(c) for c in recent]
        range_pct = ((max(highs) - min(lows)) / latest_close * 100.0) if latest_close and highs and lows else 0.0

        volumes = [candle_volume(c) for c in candles[-21:]]
        latest_closed_volume = volumes[-2] if len(volumes) >= 2 else (volumes[-1] if volumes else 0.0)
        historical = volumes[:-2] if len(volumes) >= 21 else volumes[:-1]
        avg_volume = mean(historical[-20:]) if historical else 0.0
        volume_ratio = (latest_closed_volume / avg_volume) if avg_volume > 0 else 0.0

        ema20 = self._ema(closes, 20)
        ema50 = self._ema(closes, 50) if len(closes) >= 50 else ema20

        if latest_close > ema20 > ema50:
            trend = "bullish"
        elif latest_close < ema20 < ema50:
            trend = "bearish"
        else:
            trend = "mixed"

        return TimeframeSnapshot(
            symbol=snapshot.symbol,
            granularity=snapshot.granularity,
            latest_close=latest_close,
            change_pct=latest_change_pct,
            range_pct=range_pct,
            volume_ratio_20=volume_ratio,
            ema20=ema20,
            ema50=ema50,
            trend=trend,
            candles=candles,
        )


    def _notes(
        self,
        primary: TimeframeSnapshot,
        confirmation: TimeframeSnapshot,
        contract: ContractSpec,
        volatility_rank: float,
    ) -> list[str]:
        alignment = self._alignment(primary.trend, confirmation.trend)
        notes: list[str] = []

        notes.append(f"primary_trend={primary.trend}")
        notes.append(f"confirmation_trend={confirmation.trend}")
        notes.append(f"alignment={alignment}")
        notes.append(f"volatility_rank={float(volatility_rank or 0.0):.2f}")
        notes.append(f"primary_tf={primary.granularity}")
        notes.append(f"confirmation_tf={confirmation.granularity}")
        notes.append(f"latest_close={float(primary.latest_close or 0.0):.8f}")
        notes.append(f"primary_ema20={float(primary.ema20 or 0.0):.8f}")
        notes.append(f"primary_ema50={float(primary.ema50 or 0.0):.8f}")
        notes.append(f"volume_ratio_20={float(primary.volume_ratio_20 or 0.0):.4f}")
        notes.append(f"range_pct={float(primary.range_pct or 0.0):.4f}")

        try:
            latest_candle = (primary.candles or [])[-1]
            candle_metrics = self._candle_structure_metrics(latest_candle)
            notes.append(f"candle_body_pct={candle_metrics['candle_body_pct']:.2f}")
            notes.append(f"upper_wick_pct={candle_metrics['upper_wick_pct']:.2f}")
            notes.append(f"lower_wick_pct={candle_metrics['lower_wick_pct']:.2f}")
            notes.append(f"close_strength={candle_metrics['close_strength']:.4f}")
            notes.append(f"candle_direction={candle_metrics['candle_direction']:.0f}")
        except Exception as exc:
            notes.append("candle_structure_available=false")
            self.log.warning(
                "CANDLE_STRUCTURE_METRICS_FAILED | %s | error=%s",
                getattr(primary, "symbol", "UNKNOWN"),
                exc,
            )

        try:
            notes.append(f"contract_volume_24h_usdt={float(contract.volume_24h_usdt or 0.0):.2f}")
        except Exception:
            notes.append("contract_volume_24h_usdt=0.00")

        try:
            if contract.change_pct_24h is not None:
                notes.append(f"contract_change_pct_24h={float(contract.change_pct_24h):.4f}")
        except Exception:
            pass

        return notes

    def build_market_snapshot(self, symbol: str) -> MarketSnapshot:
        contract = self.fetch_contract_meta(symbol)
        primary_raw = self.fetch_snapshot(symbol, self.settings.bitget_default_granularity)
        confirmation_raw = self.fetch_snapshot(symbol, self.settings.bitget_confirmation_granularity)

        primary = self.build_timeframe_snapshot(primary_raw)
        confirmation = self.build_timeframe_snapshot(confirmation_raw)
        alignment = self._alignment(primary.trend, confirmation.trend)
        volatility_rank = self._volatility_rank(primary=primary, contract=contract)
        volatility_context: dict = {}
        breakout_context: dict = {}
        origin_distance_score = 0.0
        impulse_freshness_score = 100.0
        expansion_exhaustion_score = 0.0
        score_hint = self._score_hint(primary=primary, confirmation=confirmation, contract=contract)
        score_hint += volatility_rank * 0.15
        score_hint = max(0.0, min(100.0, score_hint))
        notes = self._notes(
            primary=primary,
            confirmation=confirmation,
            contract=contract,
            volatility_rank=volatility_rank,
        )

        try:
            latest_candle = (primary.candles or [])[-1]
            candle_metrics = self._candle_structure_metrics(latest_candle)
            self.log.info(
                "CANDLE_STRUCTURE | %s | body_pct=%.2f | upper_wick_pct=%.2f | lower_wick_pct=%.2f | close_strength=%.4f | direction=%.0f",
                symbol.upper(),
                candle_metrics["candle_body_pct"],
                candle_metrics["upper_wick_pct"],
                candle_metrics["lower_wick_pct"],
                candle_metrics["close_strength"],
                candle_metrics["candle_direction"],
            )
        except Exception:
            pass

        orderbook_analysis: dict | None = None

        try:
            orderbook = self._fetch_with_retry(
                label=f"orderbook:{symbol.upper()}",
                fetch_fn=lambda: self.client.get_orderbook(symbol, limit=50),
                attempts=2,
            )
            orderbook_analysis = self.orderbook_analyzer.analyze(orderbook)

            spread_bps = float(orderbook_analysis.get("spread_bps", 0.0) or 0.0)
            bid_depth = float(orderbook_analysis.get("bid_depth_notional") or orderbook.get("bid_depth_notional") or 0.0)
            ask_depth = float(orderbook_analysis.get("ask_depth_notional") or orderbook.get("ask_depth_notional") or 0.0)
            total_depth = float(orderbook_analysis.get("total_depth_notional") or orderbook.get("total_depth_notional") or 0.0)
            if total_depth <= 0:
                total_depth = bid_depth + ask_depth
            if total_depth <= 0:
                total_depth = sum(
                    float(row.get("price") or 0.0) * float(row.get("size") or 0.0)
                    for row in (orderbook.get("bids") or []) + (orderbook.get("asks") or [])
                    if isinstance(row, dict)
                )
            max_spread_bps = 8.0
            min_orderbook_depth_usdt = 25_000.0
            orderbook_liquidity_ok = bool(spread_bps <= max_spread_bps and total_depth >= min_orderbook_depth_usdt)

            notes.append("orderbook_available=true")
            notes.append(f"orderbook_risk_off={str(not orderbook_liquidity_ok).lower()}")
            notes.append(f"orderbook_liquidity_ok={str(orderbook_liquidity_ok).lower()}")
            notes.append(f"spread_bps={spread_bps:.3f}")
            notes.append(f"orderbook_total_depth_usdt={total_depth:.2f}")
            notes.append(f"orderbook_imbalance={float(orderbook_analysis.get('imbalance', 0.0)):+.3f}")
            notes.append(f"orderbook_bias={orderbook_analysis.get('continuation_bias', 'neutral')}")

            # Read-only liquidity heatmap (eigenaar 2026-07-07): alleen
            # notes/snapshot voor dashboard en latere backtest-analyse —
            # geen gate- of score-invloed.
            try:
                heatmap = build_liquidity_heatmap(orderbook)
                if heatmap.get("data_ok"):
                    notes.append(f"liq_above_score={heatmap['liquidity_above_score']:.1f}")
                    notes.append(f"liq_below_score={heatmap['liquidity_below_score']:.1f}")
                    notes.append(f"liq_magnet={heatmap['liquidity_magnet_direction']}")
                    notes.append(f"liq_risk_zone={str(heatmap['liquidity_risk_zone']).lower()}")
                    if heatmap["nearest_bid_wall_price"] > 0:
                        notes.append(f"liq_bid_wall={heatmap['nearest_bid_wall_price']:.8f}x{heatmap['bid_wall_strength']:.1f}")
                    if heatmap["nearest_ask_wall_price"] > 0:
                        notes.append(f"liq_ask_wall={heatmap['nearest_ask_wall_price']:.8f}x{heatmap['ask_wall_strength']:.1f}")
                self._persist_liquidity_heatmap(symbol, heatmap)
            except Exception as heatmap_exc:
                self.log.debug("LIQUIDITY_HEATMAP_SKIPPED | %s | error=%s", symbol, heatmap_exc)

            # HTF-regime (1D+4H, 30-min cache): risk gate leest deze notes
            # om counter-trend entries te blokkeren/degraderen.
            try:
                htf = self._htf_regime_for(symbol)
                notes.append(f"htf_regime_1d={htf['regime_1d']}")
                notes.append(f"htf_regime_4h={htf['regime_4h']}")
                notes.append(f"htf_regime={htf['htf_regime']}")
            except Exception as htf_exc:
                self.log.debug("HTF_REGIME_SKIPPED | %s | error=%s", symbol, htf_exc)
            if not orderbook_liquidity_ok:
                notes.append("risk_off_reason=orderbook_spread_or_depth")
                self.log.warning(
                    "ORDERBOOK_RISK_OFF | %s | spread_bps=%.3f | total_depth=%.2f | max_spread_bps=%.3f | min_depth=%.2f",
                    symbol.upper(),
                    spread_bps,
                    total_depth,
                    max_spread_bps,
                    min_orderbook_depth_usdt,
                )

            largest_bid_wall = orderbook_analysis.get("largest_bid_wall") or {}
            largest_ask_wall = orderbook_analysis.get("largest_ask_wall") or {}

            if largest_bid_wall.get("is_significant"):
                notes.append(
                    f"significant bid wall {largest_bid_wall.get('price')} ratio {float(largest_bid_wall.get('wall_ratio', 0.0)):.2f}"
                )

            if largest_ask_wall.get("is_significant"):
                notes.append(
                    f"significant ask wall {largest_ask_wall.get('price')} ratio {float(largest_ask_wall.get('wall_ratio', 0.0)):.2f}"
                )

            self.log.info(
                "ORDERBOOK_CONTEXT | %s | spread_bps=%.3f | imbalance=%.3f | depth=%.2f | bias=%s",
                symbol.upper(),
                float(orderbook_analysis.get("spread_bps", 0.0)),
                float(orderbook_analysis.get("imbalance", 0.0)),
                total_depth,
                orderbook_analysis.get("continuation_bias", "neutral"),
            )

        except Exception as exc:
            notes.append("orderbook_available=false")
            notes.append("orderbook_risk_off=true")
            notes.append("risk_off_reason=orderbook_context_unavailable")
            self.log.warning(
                "ORDERBOOK_CONTEXT_FAILED | %s | error=%s",
                symbol.upper(),
                exc,
            )

        try:
            latest = primary.candles[-1]
            latest_candle = {
                "open": latest.open,
                "high": latest.high,
                "low": latest.low,
                "close": latest.close,
            }

            long_quality = self.entry_quality_analyzer.analyze(
                direction="LONG",
                latest_candle=latest_candle,
                orderbook_context=orderbook_analysis,
            )
            short_quality = self.entry_quality_analyzer.analyze(
                direction="SHORT",
                latest_candle=latest_candle,
                orderbook_context=orderbook_analysis,
            )

            notes.append(
                f"entry_quality long={long_quality.get('entry_quality_score')} short={short_quality.get('entry_quality_score')} close_pos={long_quality.get('close_position')}"
            )
            # Explicit parser-friendly entry quality context notes
            notes.append(f"entry_quality_long={long_quality.get('entry_quality_score')}")
            notes.append(f"entry_quality_short={short_quality.get('entry_quality_score')}")
            notes.append(f"close_position={long_quality.get('close_position')}")

            if long_quality.get("notes"):
                notes.append("long_entry_warning=" + "; ".join(long_quality.get("notes") or []))
            if short_quality.get("notes"):
                notes.append("short_entry_warning=" + "; ".join(short_quality.get("notes") or []))

            self.log.info(
                "ENTRY_QUALITY_CONTEXT | %s | long_score=%s | short_score=%s | close_pos=%s",
                symbol.upper(),
                long_quality.get("entry_quality_score"),
                short_quality.get("entry_quality_score"),
                long_quality.get("close_position"),
            )

        except Exception as exc:
            notes.append("entry quality unavailable")
            self.log.warning(
                "ENTRY_QUALITY_CONTEXT_FAILED | %s | error=%s",
                symbol.upper(),
                exc,
            )

        try:
            volatility_context = self.volatility_engine.analyze(primary.candles)
            volatility_rank = self._volatility_rank(
                atr_percent=volatility_context.get("atr_percent"),
                primary=primary,
                contract=contract,
            )
            score_hint = self._score_hint(
                primary=primary,
                confirmation=confirmation,
                contract=contract,
                alignment=alignment,
                volatility_rank=volatility_rank,
                volume_ratio=getattr(primary, "volume_ratio_20", 0.0),
            )
            score_hint += volatility_rank * 0.15
            score_hint = max(0.0, min(100.0, score_hint))
            notes.append(f"volatility_rank={float(volatility_rank or 0.0):.2f}")

            notes.append(
                f"volatility_context compression={volatility_context.get('compression')} expansion_prob={volatility_context.get('expansion_probability')} pressure={volatility_context.get('breakout_pressure')}"
            )

            for note in volatility_context.get("notes", []):
                notes.append(f"volatility_note={note}")

            self.log.info(
                "VOLATILITY_CONTEXT | %s | compression=%s | ratio=%s | expansion_prob=%s | pressure=%s",
                symbol.upper(),
                volatility_context.get("compression"),
                volatility_context.get("compression_ratio"),
                volatility_context.get("expansion_probability"),
                volatility_context.get("breakout_pressure"),
            )

        except Exception as exc:
            notes.append("volatility context unavailable")
            self.log.warning(
                "VOLATILITY_CONTEXT_FAILED | %s | error=%s",
                symbol.upper(),
                exc,
            )

        try:
            breakout_context = self.breakout_engine.analyze(primary.candles)
            origin_distance_score = float(breakout_context.get("origin_distance_score", 0.0) or 0.0)
            impulse_freshness_score = float(breakout_context.get("impulse_freshness_score", 100.0) or 100.0)
            expansion_exhaustion_score = float(breakout_context.get("expansion_exhaustion_score", 0.0) or 0.0)
            notes.append(f"origin_distance_score={origin_distance_score:.2f}")
            notes.append(f"impulse_freshness_score={impulse_freshness_score:.2f}")
            notes.append(f"expansion_exhaustion_score={expansion_exhaustion_score:.2f}")

            notes.append(
                f"breakout_context ready={breakout_context.get('breakout_ready')} pressure_score={breakout_context.get('pressure_score')} direction={breakout_context.get('direction')}"
            )
            # Explicit parser-friendly breakout context notes
            breakout_ready = bool(breakout_context.get("breakout_ready"))
            breakout_direction = str(breakout_context.get("direction") or "unknown").lower()
            notes.append(f"breakout_ready={str(breakout_ready).lower()}")
            notes.append(f"breakout_direction={breakout_direction}")
            if breakout_direction in {"short", "bearish", "down", "breakdown"}:
                notes.append(f"breakdown_ready={str(breakout_ready).lower()}")
            elif breakout_direction in {"long", "bullish", "up", "breakout"}:
                notes.append(f"breakout_ready_directional={str(breakout_ready).lower()}")
            # Add possible bullish/bearish reversal context for parser
            if breakout_ready and breakout_direction in {"long", "bullish", "up", "breakout"}:
                notes.append("possible_bullish_reversal_context=true")
            if breakout_ready and breakout_direction in {"short", "bearish", "down", "breakdown"}:
                notes.append("possible_bearish_reversal_context=true")

            # P5.3 Structure Engine handoff: expose raw breakout structure flags
            # to momentum_breakout.py. Without these parser-friendly notes the
            # prearmed context sees structure_source=missing|score=0 and blocks
            # otherwise valid high-probability breakout/breakdown setups.
            for raw_note in breakout_context.get("notes", []) or []:
                text = str(raw_note).strip()
                if not text:
                    continue
                notes.append(text)
                notes.append(f"breakout_note={text}")

            notes.append(f"range_tightening={str(bool(breakout_context.get('tightening', False))).lower()}")
            notes.append(f"higher_lows_building={str(bool(breakout_context.get('higher_lows', False))).lower()}")
            notes.append(f"lower_highs_building={str(bool(breakout_context.get('lower_highs', False))).lower()}")
            notes.append(f"closes_pressing_highs={str(bool(breakout_context.get('close_near_high', False))).lower()}")
            notes.append(f"closes_pressing_lows={str(bool(breakout_context.get('close_near_low', False))).lower()}")
            notes.append(f"breakout_structure_detected={str(bool(breakout_context.get('diag_structure_detected', False))).lower()}")
        except Exception as exc:
            self.log.warning("BREAKOUT_CONTEXT_FAILED | %s | error=%s", primary.symbol, exc)

        if primary is None or confirmation is None:
            raise ValueError(
                f"Market snapshot build failed for {symbol}: primary={primary} confirmation={confirmation}"
            )

        return MarketSnapshot(
            symbol=symbol.upper(),
            contract=contract,
            primary=primary,
            confirmation=confirmation,
            alignment=alignment,
            score_hint=round(float(score_hint or 0.0), 2),
            notes=notes,
            volatility_rank=round(float(volatility_rank or 0.0), 2),
            context={
                "volatility": volatility_context,
                "breakout": breakout_context,
            },
            origin_distance_score=round(float(origin_distance_score or 0.0), 2),
            impulse_freshness_score=round(float(impulse_freshness_score or 100.0), 2),
            expansion_exhaustion_score=round(float(expansion_exhaustion_score or 0.0), 2),
        )
