import json
import logging
import time
from typing import Callable, TypeVar
from pathlib import Path
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
from market_features.engine import LiveMarketContext, alignment as unified_alignment, build_market_snapshot as build_unified_market_snapshot, build_timeframe_snapshot as build_unified_timeframe_snapshot, ema as unified_ema, score_hint as unified_score_hint, volatility_rank as unified_volatility_rank


T = TypeVar("T")


class MarketFetcher:
    @staticmethod
    def build_snapshot_from_inputs(symbol, primary_candles, confirmation_candles, *, as_of_timestamp_ms, primary_granularity="15m", confirmation_granularity="1h", inputs=None):
        return build_unified_market_snapshot(
            symbol, primary_candles, confirmation_candles,
            as_of_timestamp_ms=as_of_timestamp_ms,
            primary_granularity=primary_granularity,
            confirmation_granularity=confirmation_granularity,
            inputs=inputs or LiveMarketContext(),
        )
    @staticmethod
    def _alignment(primary_trend: str | None, confirmation_trend: str | None) -> str:
        primary = type("Trend", (), {"trend": str(primary_trend or "").lower()})()
        confirmation = type("Trend", (), {"trend": str(confirmation_trend or "").lower()})()
        return unified_alignment(primary, confirmation)



    @staticmethod
    def _volatility_rank(
        atr_percent: float | int | None = None,
        primary: object | None = None,
        contract: object | None = None,
        **_: object,
    ) -> float:
        if atr_percent is None and primary is not None:
            atr_percent = getattr(primary, "atr_percent", None)
        return unified_volatility_rank(atr_percent)

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
        if primary is None or confirmation is None:
            raise ValueError("score_hint requires unified primary and confirmation snapshots")
        return unified_score_hint(primary, confirmation, alignment or MarketFetcher._alignment(primary.trend, confirmation.trend), float(volatility_rank or 0.0), contract, float(spread_bps) if spread_bps is not None else None)


    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        clean_values = [float(value) for value in values if value is not None]
        return unified_ema(clean_values, period)


    def __init__(self, client: BitgetRestClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self.log = logging.getLogger(self.__class__.__name__)
        self.contract_cache = TTLCache[list[ContractSpec]](ttl_seconds=settings.bitget_contract_cache_ttl_sec)

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

    def fetch_snapshot(self, symbol: str, granularity: str | None = None, *, as_of_timestamp_ms: int) -> SymbolSnapshot:
        if not isinstance(as_of_timestamp_ms, int) or as_of_timestamp_ms <= 0:
            raise ValueError("as_of_timestamp_ms is required")
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
            as_of_timestamp_ms=as_of_timestamp_ms,
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

    def build_timeframe_snapshot(self, snapshot):
        """Compatibility adapter; only raw SymbolSnapshot input is accepted."""
        if hasattr(snapshot, "trend") and hasattr(snapshot, "latest_close"):
            raise ValueError("prebuilt timeframe snapshots are not accepted by the unified adapter")

        return build_unified_timeframe_snapshot(snapshot.symbol, snapshot.granularity, list(snapshot.candles), snapshot.as_of_timestamp_ms)


    # Canonical production entrypoint: raw public data in, one shared builder out.
    def build_market_snapshot(
        self,
        symbol: str,
        *,
        as_of_timestamp_ms: int,
        primary_granularity: str | None = None,
        confirmation_granularity: str | None = None,
    ) -> MarketSnapshot:
        if not isinstance(as_of_timestamp_ms, int) or as_of_timestamp_ms <= 0:
            raise ValueError("as_of_timestamp_ms is required")
        contract = self.fetch_contract_meta(symbol)
        primary_raw = self.fetch_snapshot(symbol, primary_granularity or self.settings.bitget_default_granularity, as_of_timestamp_ms=as_of_timestamp_ms)
        confirmation_raw = self.fetch_snapshot(symbol, confirmation_granularity or self.settings.bitget_confirmation_granularity, as_of_timestamp_ms=as_of_timestamp_ms)
        orderbook = None
        try:
            orderbook = self._fetch_with_retry(label=f"orderbook:{symbol.upper()}", fetch_fn=lambda: self.client.get_orderbook(symbol, limit=50), attempts=2)
        except Exception as exc:
            self.log.warning("ORDERBOOK_CONTEXT_FAILED | %s | error=%s", symbol.upper(), exc)
        htf_context = None
        try:
            htf_context = self._htf_regime_for(symbol)
        except Exception as exc:
            self.log.debug("HTF_REGIME_SKIPPED | %s | error=%s", symbol, exc)
        snapshot = self.build_snapshot_from_inputs(
            symbol, primary_raw.candles, confirmation_raw.candles,
            as_of_timestamp_ms=as_of_timestamp_ms,
            primary_granularity=primary_raw.granularity,
            confirmation_granularity=confirmation_raw.granularity,
            inputs=LiveMarketContext(orderbook=orderbook, htf_context=htf_context, contract=contract),
        )
        liquidity = snapshot.context.get("liquidity")
        if isinstance(liquidity, dict):
            self._persist_liquidity_heatmap(symbol, liquidity)
        return snapshot
