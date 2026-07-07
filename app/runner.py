import logging
from pathlib import Path
import json
import fcntl
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from collections import Counter

from app.config import Settings
from app.equity import write_equity_snapshot
from clients.bitget_rest import BitgetRestClient
from clients.schemas import ExecutionReport, MarketSnapshot, PositionUpdate, StrategyCandidate, StrategyScore, TradePlan, SweepDetection
from data.market_fetcher import MarketFetcher
from data.watchlist import get_watchlist
from market_data.market_data_service import MarketDataService
from market_data.multi_timeframe_cache import MultiTimeframeCache
from execution.execution_service import ExecutionService
from execution.position_manager import PositionManager
from execution.state_store import JsonStateStore
from planning.trade_planner import TradePlanner
from risk.risk_manager import RiskManager
from risk.cooldown_manager import SymbolCooldownManager
from agents_v2.learning.coach_rules import run as run_coach_rules
from strategies.liquidity_sweep import LiquiditySweepStrategy
from strategies.momentum_breakout import MomentumBreakoutStrategy, MomentumBreakdownStrategy
from strategies.strategies.continuation import detect_continuation
from strategies.strategies.low_vol_reclaim import detect_low_vol_reclaim
from strategies.strategies.selector import select_best_candidate
from strategies.scoring import StrategyScorer
from telemetry.trade_logger import (
    ExecutionCsvLogger,
    MarketScanCsvLogger,
    PositionUpdateCsvLogger,
    StrategyCandidateCsvLogger,
    TradePlanCsvLogger,
    StrategyPerformanceLogger,
)

from telemetry.market_context_logger import MarketContextLogger


# --- Execution-Aware Scoring Helpers ---
def _extract_note_float(notes: list[str], marker: str, default: float = 0.0) -> float:
    note_text = " ".join(str(note).lower() for note in (notes or []))
    marker = marker.lower()

    if marker not in note_text:
        return default

    try:
        section = note_text.split(marker, 1)[1]
        raw = section.split()[0].strip(";|,")
        return float(raw)
    except Exception:
        return default


def _execution_aware_score(snapshot: MarketSnapshot) -> float:
    notes = snapshot.notes or []
    note_text = " ".join(str(note).lower() for note in notes)

    score = float(snapshot.score_hint or 0.0)
    close_pos = _extract_note_float(notes, "close_pos=", 0.5)

    long_score = _extract_note_float(notes, "entry_quality long=", 100.0)
    short_score = _extract_note_float(notes, "short=", 100.0)
    entry_quality = max(long_score, short_score)

    if entry_quality < 50:
        score -= 18
    elif entry_quality < 65:
        score -= 12
    elif entry_quality < 80:
        score -= 6

    if "late long entry near candle high" in note_text:
        score -= 18
        if close_pos >= 0.90:
            score -= 12
        elif close_pos >= 0.80:
            score -= 6

    if "late short entry near candle low" in note_text:
        score -= 18
        if close_pos <= 0.10:
            score -= 12
        elif close_pos <= 0.20:
            score -= 6

    if "wide spread" in note_text:
        score -= 8

    if "orderbook bias against long" in note_text or "orderbook bias against short" in note_text:
        score -= 8

    if "upper wick rejection risk" in note_text or "lower wick rejection risk" in note_text:
        score -= 4

    return score


def _build_fallback_candidate(snapshot: MarketSnapshot) -> StrategyCandidate | None:
    note_text = " ".join(str(note).lower() for note in (snapshot.notes or []))

    def _strategy_set_from_env(name: str) -> set[str]:
        values: list[str] = []
        raw_value = os.getenv(name, "")
        if raw_value:
            values.extend(raw_value.split(","))

        env_path = Path(".env")
        if env_path.exists():
            try:
                for line in env_path.read_text(errors="ignore").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, value = line.split("=", 1)
                    if key.strip() == name:
                        values.extend(value.split(","))
            except Exception:
                pass

        return {item.strip().lower() for item in values if item.strip()}

    fallback_strategy_name = "adaptive_momentum_continuation"
    enabled_strategies = _strategy_set_from_env("ENABLED_STRATEGIES")
    disabled_strategies = _strategy_set_from_env("DISABLED_STRATEGIES")
    explicitly_enabled = fallback_strategy_name in enabled_strategies
    explicitly_disabled = fallback_strategy_name in disabled_strategies

    if explicitly_disabled or not explicitly_enabled:
        logging.getLogger("fallback_candidate_bridge").info(
            "ADAPTIVE_CONTINUATION_DISABLED | %s | source=env_or_dotenv | guard=builder | explicitly_enabled=%s | explicitly_disabled=%s",
            snapshot.symbol,
            explicitly_enabled,
            explicitly_disabled,
        )
        return None

    execution_score = _execution_aware_score(snapshot)
    alignment = (snapshot.alignment or "").lower()
    primary_trend = (snapshot.primary.trend or "").lower()
    confirmation_trend = (snapshot.confirmation.trend or "").lower()

    if execution_score < 75.0:
        return None

    bullish = (
        alignment == "aligned_bullish"
        and primary_trend == "bullish"
        and confirmation_trend == "bullish"
    )

    bearish = (
        alignment == "aligned_bearish"
        and primary_trend == "bearish"
        and confirmation_trend == "bearish"
    )

    if not bullish and not bearish:
        return None

    if not (
        "volume expansion" in note_text
        or "range expansion" in note_text
        or "breakout_pressure=" in note_text
        or "higher_lows_building=true" in note_text
        or "higher_highs_building=true" in note_text
    ):
        return None

    direction = "LONG" if bullish else "SHORT"

    pressure_score = _extract_note_float(snapshot.notes or [], "pressure_score=", 0.0)
    expansion_prob = _extract_note_float(snapshot.notes or [], "expansion_prob=", 0.0)
    breakout_ready = "breakout_ready=true" in note_text

    close_pos = _extract_note_float(snapshot.notes or [], "close_pos=", 0.50)
    direction_entry_quality = _extract_note_float(
        snapshot.notes or [],
        "entry_quality long=" if direction == "LONG" else "entry_quality short=",
        100.0,
    )

    if direction_entry_quality < 75.0:
        logging.getLogger("fallback_candidate_bridge").info(
            "LATE_ENTRY_BLOCKED | %s | direction=%s | reason=entry_quality_below_75 | entry_quality=%.2f | close_pos=%.2f | execution_score=%.2f",
            snapshot.symbol,
            direction,
            direction_entry_quality,
            close_pos,
            execution_score,
        )
        return None

    if direction == "LONG" and close_pos >= 0.75:
        logging.getLogger("fallback_candidate_bridge").info(
            "LATE_ENTRY_BLOCKED | %s | direction=LONG | reason=long_too_high_in_candle | entry_quality=%.2f | close_pos=%.2f | execution_score=%.2f",
            snapshot.symbol,
            direction_entry_quality,
            close_pos,
            execution_score,
        )
        return None

    if direction == "SHORT" and close_pos <= 0.25:
        logging.getLogger("fallback_candidate_bridge").info(
            "LATE_ENTRY_BLOCKED | %s | direction=SHORT | reason=short_too_low_in_candle | entry_quality=%.2f | close_pos=%.2f | execution_score=%.2f",
            snapshot.symbol,
            direction_entry_quality,
            close_pos,
            execution_score,
        )
        return None

    latest_close = float(snapshot.primary.latest_close or 0.0)
    range_pct = max(float(snapshot.primary.range_pct or 0.0), 0.35)

    stop_distance_pct = max(range_pct * 0.45, 0.20)

    if direction == "LONG":
        invalidation = latest_close * (1.0 - stop_distance_pct / 100.0)
        reclaim_level = latest_close * 0.998
    else:
        invalidation = latest_close * (1.0 + stop_distance_pct / 100.0)
        reclaim_level = latest_close * 1.002

    detection = SweepDetection(
        side=direction,
        swept_level=latest_close,
        sweep_extreme=latest_close,
        reclaim_level=reclaim_level,
        entry_hint=latest_close,
        invalidation=invalidation,
        displacement_pct=range_pct,
        bars_since_sweep=0,
        volume_ratio_on_sweep=float(snapshot.primary.volume_ratio_20 or 1.0),
        local_range_size_pct=range_pct,
        reason_flags=["fallback_candidate_bridge"],
    )

    fallback_notes = list(snapshot.notes or [])
    fallback_notes.append("fallback_candidate_bridge=true")
    fallback_notes.append(f"fallback_execution_score={execution_score:.1f}")
    fallback_notes.append(f"fallback_entry_quality={direction_entry_quality:.2f}")
    fallback_notes.append(f"fallback_close_pos={close_pos:.2f}")
    fallback_notes.append(f"pressure_score={pressure_score:.2f}")
    fallback_notes.append(f"expansion_prob={expansion_prob:.2f}")
    fallback_notes.append(f"breakout_context_ready={breakout_ready}")

    primary_volume_ratio = float(getattr(snapshot.primary, "volume_ratio_20", 0.0) or 0.0)
    fallback_notes.append(f"participation_score={primary_volume_ratio:.2f}")
    fallback_notes.append(f"followthrough_volume_ratio={primary_volume_ratio:.2f}")
    fallback_notes.append(f"volume_ratio={primary_volume_ratio:.2f}")

    if pressure_score <= 0.0 or expansion_prob <= 0.0:
        logging.getLogger("fallback_candidate_bridge").info(
            "FALLBACK_BLOCKED_MISSING_PRESSURE | %s | pressure_score=%.2f | expansion_prob=%.2f",
            snapshot.symbol,
            pressure_score,
            expansion_prob,
        )
        return None

    return StrategyCandidate(
        symbol=snapshot.symbol,
        strategy="adaptive_momentum_continuation",
        direction=direction,
        primary_granularity=snapshot.primary.granularity,
        confirmation_granularity=snapshot.confirmation.granularity,
        market=snapshot,
        detection=detection,
        notes=fallback_notes,
        candidate_status="fallback_candidate",
    )


class StartupRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = logging.getLogger(self.__class__.__name__)
        self.client = BitgetRestClient(settings=settings)
        self.fetcher = MarketFetcher(client=self.client, settings=settings)
        self.multi_tf_cache = MultiTimeframeCache()
        self.market_data_service = MarketDataService(
            rest_client=self.client,
            cache=self.multi_tf_cache,
        )
        self.scan_logger = MarketScanCsvLogger()
        self.market_context_logger = MarketContextLogger()
        self.candidate_logger = StrategyCandidateCsvLogger()
        self.trade_plan_logger = TradePlanCsvLogger()
        self.strategy_performance_logger = StrategyPerformanceLogger()
        self.execution_logger = ExecutionCsvLogger()
        self.position_logger = PositionUpdateCsvLogger()
        self.strategy = LiquiditySweepStrategy(settings=settings)
        self.momentum_strategy = MomentumBreakoutStrategy(settings=settings)
        self.momentum_breakdown_strategy = MomentumBreakdownStrategy(settings=settings)
        self.scorer = StrategyScorer(settings=settings)
        self.risk_manager = RiskManager(settings=settings)
        self.trade_planner = TradePlanner(settings=settings)
        self.execution_service = ExecutionService(settings=settings)
        self.position_manager = PositionManager(settings=settings)
        self.cooldown_store = JsonStateStore("state/symbol_cooldowns.json")
        self.cooldown_manager = SymbolCooldownManager(self.cooldown_store)
        self.signal_cooldown_minutes = 30
        self.recent_close_cooldown_minutes = 15
        self._last_scan_log = {}
        self._last_top_symbols = []
        self._last_plan_summary = None
        self._last_position_snapshot = {}
        self._last_no_setup_log = {}
        self._last_no_candidate_intelligence_log = {}
        self._last_network_error_log = None
        self._last_reject_log = {}
        self._last_sweep_reject_log = {}
        self._last_continuation_reject_log = {}
        self._scan_in_progress = False
        self._scan_lock_path = "state/scan_cycle.lock"
        self._learning_refresh_proc: subprocess.Popen | None = None

    def _maybe_refresh_learning_reports(self) -> None:
        """Regenerate the daily learning/expectancy reports when they go stale.

        The launchd job that used to do this gets blocked by macOS TCC (launchd
        children may not write inside ~/Desktop), so the bot — started from a
        terminal that does have that access — refreshes its own learning input.
        Runs as a background subprocess so the scan loop never stalls on it.
        """
        if self._learning_refresh_proc is not None:
            if self._learning_refresh_proc.poll() is None:
                return
            exit_code = self._learning_refresh_proc.returncode
            self._learning_refresh_proc = None
            if exit_code == 0:
                self.log.info("LEARNING_REFRESH_OK | strategy_expectancy.json regenerated")
            else:
                self.log.warning("LEARNING_REFRESH_FAILED | exit_code=%s", exit_code)

        report_path = Path("reports/backtests/strategy_expectancy.json")
        try:
            age_hours = (time.time() - report_path.stat().st_mtime) / 3600.0
        except OSError:
            age_hours = float("inf")

        if age_hours < 24.0:
            return

        chain = (
            "import subprocess, sys;"
            "subprocess.run([sys.executable, '-m', 'telemetry.dataset_builder'], check=False);"
            "subprocess.run([sys.executable, 'scripts/run_backtest.py', '--validation-only'], check=True);"
            "subprocess.run([sys.executable, 'morning_audit.py'], check=False)"
        )
        try:
            with open("logs/daily_learning_report.log", "a", encoding="utf-8") as log_handle:
                self._learning_refresh_proc = subprocess.Popen(
                    [sys.executable, "-c", chain],
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                )
            self.log.info("LEARNING_REFRESH_STARTED | report_age_hours=%.1f", age_hours)
        except Exception as exc:
            self.log.warning("LEARNING_REFRESH_SPAWN_FAILED | error=%s", exc)

    def _is_network_resolution_error(self, exc: Exception) -> bool:
        error_text = str(exc).lower()
        return (
            "failed to resolve" in error_text
            or "nameresolutionerror" in error_text
            or "nodename nor servname provided" in error_text
            or "temporary failure in name resolution" in error_text
        )

    def run(self) -> None:
        self.log.info("Starting Bitget AI Agent Phase 7")
        self._startup_checks()

        if self.settings.scan_on_start:
            self._scan_cycle()

        if self.settings.scan_loop_enabled:
            self.log.info("Scan loop enabled | interval=%ss", self.settings.scan_interval_sec)
            while True:
                time.sleep(self.settings.scan_interval_sec)
                self._scan_cycle()
                if self.settings.position_loop_enabled:
                    time.sleep(max(1, self.settings.position_check_interval_sec))

    def _startup_checks(self) -> None:
        self.log.info("Running startup checks")
        contracts = self.fetcher.fetch_contracts(force_refresh=True)
        self.log.info(
            "Public API OK | product_type=%s | contracts=%s",
            self.settings.bitget_product_type,
            len(contracts),
        )

        if self.client.has_credentials:
            try:
                account_payload = self.client.ping_private_account()
                accounts = account_payload.get("data", [])
                self.log.info("Private API OK | accounts=%s", len(accounts))
            except Exception as exc:
                self.log.warning("Private API check failed: %s", exc)
        else:
            self.log.warning("Private API skipped | credentials missing")

    def _cooldown_file_path(self) -> Path:
        return Path("state/symbol_cooldowns.json")

    def _load_cooldown_data(self) -> dict:
        path = self._cooldown_file_path()
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text() or "{}")
        except Exception:
            return {}

    def _save_cooldown_data(self, data: dict) -> None:
        path = self._cooldown_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True))

    def _cooldown_is_on(self, key: str, minutes: int) -> bool:
        data = self._load_cooldown_data()
        record = data.get(key)

        if record is None and isinstance(data.get("cooldowns"), dict):
            record = data["cooldowns"].get(key)

        if record is None:
            return False

        now = datetime.now(timezone.utc)

        try:
            if isinstance(record, dict):
                until_raw = record.get("until") or record.get("expires_at")
                created_raw = record.get("created_at") or record.get("timestamp")

                if until_raw:
                    until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00"))
                    return until > now

                if created_raw:
                    created = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00"))
                    return created + timedelta(minutes=minutes) > now

            if isinstance(record, str):
                until = datetime.fromisoformat(record.replace("Z", "+00:00"))
                return until > now
        except Exception:
            return False

        return False

    def _cooldown_register(self, key: str, minutes: int, reason: str) -> None:
        data = self._load_cooldown_data()
        target = data["cooldowns"] if isinstance(data.get("cooldowns"), dict) else data

        now = datetime.now(timezone.utc)
        until = now + timedelta(minutes=minutes)

        target[key] = {
            "created_at": now.isoformat(),
            "until": until.isoformat(),
            "minutes": minutes,
            "reason": reason,
        }

        self._save_cooldown_data(data)

    def _scan_cycle(self) -> None:
        if self._scan_in_progress:
            self.log.warning("SCAN_SKIPPED | previous scan cycle still running")
            return

        self._scan_in_progress = True
        scan_lock_handle = None

        try:
            os.makedirs("state", exist_ok=True)
            scan_lock_handle = open(self._scan_lock_path, "w")
            try:
                fcntl.flock(scan_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.log.warning("SCAN_SKIPPED | another runner process is already scanning")
                return

            try:
                agent_report = run_coach_rules()
                self.log.info(
                    "AI_AGENT_DECISIONS_REFRESHED | decision_count=%s",
                    agent_report.get("decision_count", 0),
                )
            except Exception as exc:
                self.log.warning("AI_AGENT_DECISIONS_REFRESH_FAILED | error=%s", exc)

            self._maybe_refresh_learning_reports()

            if self.client.has_credentials:
                try:
                    accounts_payload = self.client.get_accounts()
                    for account in accounts_payload.get("data") or []:
                        if str(account.get("marginCoin", "")).upper() != "USDT":
                            continue
                        live_equity = float(
                            account.get("accountEquity")
                            or account.get("usdtEquity")
                            or account.get("equity")
                            or 0.0
                        )
                        if live_equity > 0:
                            write_equity_snapshot(live_equity)
                        break
                except Exception as exc:
                    self.log.warning("EQUITY_SNAPSHOT_FAILED | error=%s", exc)

            try:
                day_mode = self.risk_manager.day_mode()
                self.log.info(
                    "DAY_MODE | mode=%s | daily_pnl=%.2f | daily_loss_pct=%.2f | consecutive_losses=%s | weekly_pnl=%.2f | weekly_loss_pct=%.2f | equity=%.2f (%s)",
                    day_mode["mode"],
                    day_mode["daily_realized_pnl"],
                    day_mode["daily_loss_pct"],
                    day_mode["consecutive_losses"],
                    day_mode["weekly_realized_pnl"],
                    day_mode["weekly_loss_pct"],
                    day_mode["account_equity"],
                    day_mode["equity_source"],
                )
            except Exception as exc:
                self.log.warning("DAY_MODE_CHECK_FAILED | error=%s", exc)

            contracts = self.fetcher.fetch_contracts(force_refresh=False)
            symbols = get_watchlist(self.settings, contracts=contracts)

            try:
                self.market_data_service.refresh_many(
                    symbols=symbols,
                    limit=200,
                )

                self.log.info(
                    "MULTI_TF_REFRESH_OK | symbols=%s",
                    len(symbols),
                )

            except Exception as exc:
                self.log.warning(
                    "MULTI_TF_REFRESH_FAILED | error=%s",
                    exc,
                )
                if self._is_network_resolution_error(exc):
                    error_signature = ("multi_tf_refresh", str(exc)[:180])
                    if self._last_network_error_log != error_signature:
                        self.log.error(
                            "API_NETWORK_UNAVAILABLE | stage=multi_tf_refresh | action=skip_scan_cycle_preserve_local_state | error=%s",
                            exc,
                        )
                        self._last_network_error_log = error_signature
                    self.log.warning(
                        "NETWORK_RECOVERY_MODE | stage=multi_tf_refresh | sleep_seconds=60"
                    )
                    time.sleep(60)
                    return

            self.log.info(
                "Scanning watchlist | symbols=%s | primary=%s | confirm=%s",
                len(symbols),
                self.settings.bitget_default_granularity,
                self.settings.bitget_confirmation_granularity,
            )

            snapshots: list[MarketSnapshot] = []
            candidates: list[tuple[StrategyCandidate, StrategyScore]] = []
            plans: list[TradePlan] = []
            for symbol in symbols:
                try:
                    snapshot = self.fetcher.build_market_snapshot(symbol)
                    multi_tf_snapshot = self.market_data_service.get_symbol_snapshot(symbol)

                    self.log.info(
                        "MULTI_TF_STATE | %s | available=%s | stale=%s",
                        symbol,
                        self.multi_tf_cache.get_available_timeframes(symbol),
                        self.multi_tf_cache.is_symbol_stale(symbol),
                    )
                    snapshots.append(snapshot)
                    self.market_context_logger.append(
                        symbol=snapshot.symbol,
                        alignment=snapshot.alignment,
                        score_hint=snapshot.score_hint,
                        primary_trend=snapshot.primary.trend,
                        confirmation_trend=snapshot.confirmation.trend,
                        volatility_rank=getattr(snapshot, "volatility_rank", 0.0),
                        notes=snapshot.notes,
                    )
                    scan_signature = (
                        snapshot.alignment,
                        round(snapshot.score_hint, 1),
                        snapshot.primary.trend,
                        snapshot.confirmation.trend,
                        round(snapshot.primary.change_pct, 3),
                        round(snapshot.primary.volume_ratio_20, 2),
                    )

                    previous_signature = self._last_scan_log.get(snapshot.symbol)

                    if previous_signature != scan_signature:
                        self.log.info(
                            "SCAN | %s | align=%s | score_hint=%.1f | %s=%s %.3f%% vr=%.2f | %s=%s | notes=%s",
                            snapshot.symbol,
                            snapshot.alignment,
                            snapshot.score_hint,
                            snapshot.primary.granularity,
                            snapshot.primary.trend,
                            snapshot.primary.change_pct,
                            snapshot.primary.volume_ratio_20,
                            snapshot.confirmation.granularity,
                            snapshot.confirmation.trend,
                            "; ".join(snapshot.notes),
                        )
                        self._last_scan_log[snapshot.symbol] = scan_signature

                    sweep_candidate = self.strategy.detect(snapshot)
                    momentum_candidate = self.momentum_strategy.detect(snapshot)
                    momentum_breakdown_candidate = self.momentum_breakdown_strategy.detect(snapshot)
                    continuation_candidate = None
                    low_vol_reclaim_candidate = None

                    try:
                        continuation_candidate = detect_continuation(snapshot)
                    except Exception:
                        continuation_candidate = None

                    try:
                        low_vol_reclaim_candidate = detect_low_vol_reclaim(snapshot)
                    except Exception as exc:
                        self.log.warning(
                            "LOW_VOL_RECLAIM_ERROR | %s | error=%s",
                            snapshot.symbol,
                            exc,
                        )
                        low_vol_reclaim_candidate = None

                    raw_debug_symbols = os.getenv("STRATEGY_DEBUG_SYMBOLS", "NEARUSDT,WIFUSDT")
                    debug_symbols = {symbol.strip().upper() for symbol in raw_debug_symbols.split(",") if symbol.strip()}
                    if snapshot.symbol.upper() in debug_symbols:
                        self.log.info(
                            "STRATEGY_ROUTE | %s | sweep=%s | continuation=%s | low_vol_reclaim=%s | momentum_breakout=%s | momentum_breakdown=%s",
                            snapshot.symbol,
                            sweep_candidate.strategy if sweep_candidate else "none",
                            continuation_candidate.strategy if continuation_candidate else "none",
                            low_vol_reclaim_candidate.strategy if low_vol_reclaim_candidate else "none",
                            momentum_candidate.strategy if momentum_candidate else "none",
                            momentum_breakdown_candidate.strategy if momentum_breakdown_candidate else "none",
                        )

                    selector_result = select_best_candidate(
                        sweep_candidate,
                        continuation_candidate,
                        low_vol_reclaim_candidate,
                        momentum_candidate,
                        momentum_breakdown_candidate,
                    )

                    if selector_result is None:
                        self.log.error(
                            "SELECTOR_NONE_RETURN | %s | sweep=%s | continuation=%s | low_vol_reclaim=%s | momentum_breakout=%s | momentum_breakdown=%s | treating_as_no_candidate",
                            snapshot.symbol,
                            sweep_candidate.strategy if sweep_candidate else "none",
                            continuation_candidate.strategy if continuation_candidate else "none",
                            low_vol_reclaim_candidate.strategy if low_vol_reclaim_candidate else "none",
                            momentum_candidate.strategy if momentum_candidate else "none",
                            momentum_breakdown_candidate.strategy if momentum_breakdown_candidate else "none",
                        )
                        candidate = None
                        selector_reason = "selector_none_return_treated_as_no_candidate"
                    else:
                        try:
                            candidate, selector_reason = selector_result
                        except Exception as exc:
                            self.log.exception(
                                "SELECTOR_BAD_RETURN | %s | result=%r | error=%s | treating_as_no_candidate",
                                snapshot.symbol,
                                selector_result,
                                exc,
                            )
                            candidate = None
                            selector_reason = "selector_bad_return_treated_as_no_candidate"

                    if candidate is None:
                        enabled_strategies = {
                            item.strip().lower()
                            for item in os.getenv("ENABLED_STRATEGIES", "").split(",")
                            if item.strip()
                        }
                        disabled_strategies = {
                            item.strip().lower()
                            for item in os.getenv("DISABLED_STRATEGIES", "").split(",")
                            if item.strip()
                        }

                        fallback_strategy_name = "adaptive_momentum_continuation"
                        fallback_enabled = (
                            fallback_strategy_name in enabled_strategies
                            if enabled_strategies
                            else True
                        )
                        fallback_disabled = fallback_strategy_name in disabled_strategies

                        if fallback_disabled or not fallback_enabled:
                            self.log.info(
                                "ADAPTIVE_CONTINUATION_DISABLED | %s | source=env | enabled=%s | disabled=%s",
                                snapshot.symbol,
                                fallback_enabled,
                                fallback_disabled,
                            )
                        else:
                            fallback_candidate = _build_fallback_candidate(snapshot)

                            if fallback_candidate is not None:
                                candidate = fallback_candidate
                                selector_reason = (
                                    f"fallback adaptive continuation bridge | execution_score="
                                    f"{_execution_aware_score(snapshot):.1f}"
                                )

                                self.log.warning(
                                    "FALLBACK_CANDIDATE_BRIDGE | %s | direction=%s | execution_score=%.1f | alignment=%s | trend=%s/%s",
                                    snapshot.symbol,
                                    candidate.direction,
                                    _execution_aware_score(snapshot),
                                    snapshot.alignment,
                                    snapshot.primary.trend,
                                    snapshot.confirmation.trend,
                                )

                    if candidate is not None:
                        signal_cooldown_key = f"signal::{candidate.symbol}::{candidate.direction}::{candidate.strategy}"
                        recent_close_key = f"recent_close::{candidate.symbol}"

                        if self._cooldown_is_on(
                            signal_cooldown_key,
                            minutes=self.signal_cooldown_minutes,
                        ):
                            self.log.info(
                                "DUPLICATE_SIGNAL_BLOCKED | %s | direction=%s | strategy=%s",
                                candidate.symbol,
                                candidate.direction,
                                candidate.strategy,
                            )
                            candidate = None
                            selector_reason = "duplicate_signal_cooldown"
                        elif self._cooldown_is_on(
                            recent_close_key,
                            minutes=self.recent_close_cooldown_minutes,
                        ):
                            self.log.info(
                                "RECENT_CLOSE_COOLDOWN_BLOCKED | %s | direction=%s | strategy=%s",
                                candidate.symbol,
                                candidate.direction,
                                candidate.strategy,
                            )
                            candidate = None
                            selector_reason = "recent_close_cooldown"

                    if candidate is not None and selector_reason:
                        candidate.notes.append(f"selector: {selector_reason}")
                    if candidate is not None and candidate.direction == "SHORT":
                        candidate.notes.append("LIVE SHORT ENABLED")
                    if candidate is None:
                        no_candidate_intelligence_signature = (
                            selector_reason,
                            sweep_candidate.strategy if sweep_candidate else "none",
                            continuation_candidate.strategy if continuation_candidate else "none",
                            low_vol_reclaim_candidate.strategy if low_vol_reclaim_candidate else "none",
                            momentum_candidate.strategy if momentum_candidate else "none",
                            momentum_breakdown_candidate.strategy if momentum_breakdown_candidate else "none",
                            snapshot.alignment,
                            snapshot.primary.trend,
                            snapshot.confirmation.trend,
                            round(float(snapshot.score_hint or 0.0), 1),
                            round(float(snapshot.primary.volume_ratio_20 or 0.0), 2),
                            round(float(getattr(snapshot, "volatility_rank", 0.0) or 0.0), 1),
                        )

                        previous_no_candidate_intelligence_signature = self._last_no_candidate_intelligence_log.get(snapshot.symbol)

                        if previous_no_candidate_intelligence_signature != no_candidate_intelligence_signature:
                            self.log.info(
                                "NO_CANDIDATE_INTELLIGENCE | %s | selector_reason=%s | sweep=%s | continuation=%s | low_vol_reclaim=%s | momentum_breakout=%s | momentum_breakdown=%s | align=%s | score_hint=%.1f | volatility_rank=%.1f | vr=%.2f | trend=%s/%s | notes=%s",
                                snapshot.symbol,
                                selector_reason,
                                sweep_candidate.strategy if sweep_candidate else "none",
                                continuation_candidate.strategy if continuation_candidate else "none",
                                low_vol_reclaim_candidate.strategy if low_vol_reclaim_candidate else "none",
                                momentum_candidate.strategy if momentum_candidate else "none",
                                momentum_breakdown_candidate.strategy if momentum_breakdown_candidate else "none",
                                snapshot.alignment,
                                snapshot.score_hint,
                                getattr(snapshot, "volatility_rank", 0.0),
                                snapshot.primary.volume_ratio_20,
                                snapshot.primary.trend,
                                snapshot.confirmation.trend,
                                "; ".join(snapshot.notes or [])[:300],
                            )
                            self._last_no_candidate_intelligence_log[snapshot.symbol] = no_candidate_intelligence_signature

                        self.strategy_performance_logger.append_setup_event(
                            symbol=snapshot.symbol,
                            strategy="NO_SETUP",
                            direction="NONE",
                            verdict="NO_CANDIDATE",
                            score=round(float(snapshot.score_hint or 0.0), 2),
                            stage="SCAN_REJECT",
                            reasons=[
                                f"selector_reason={selector_reason}",
                                f"alignment={snapshot.alignment}",
                                f"primary_trend={snapshot.primary.trend}",
                                f"confirmation_trend={snapshot.confirmation.trend}",
                                f"volume_ratio={round(snapshot.primary.volume_ratio_20, 2)}",
                                f"volatility_rank={round(getattr(snapshot, 'volatility_rank', 0.0), 2)}",
                            ],
                            notes=list(snapshot.notes or []),
                        )
                    if candidate is None:
                        no_setup_signature = (
                            selector_reason,
                            snapshot.alignment,
                            round(snapshot.score_hint, 1),
                            round(snapshot.primary.volume_ratio_20, 2),
                            snapshot.primary.trend,
                            snapshot.confirmation.trend,
                        )

                        previous_signature = self._last_no_setup_log.get(snapshot.symbol)

                        if previous_signature != no_setup_signature:
                            self.log.info(
                                "NO_SETUP | %s | reason=%s | align=%s | score_hint=%.1f | volatility_rank=%.1f | vr=%.2f | trend=%s/%s",
                                snapshot.symbol,
                                selector_reason,
                                snapshot.alignment,
                                snapshot.score_hint,
                                getattr(snapshot, "volatility_rank", 0.0),
                                snapshot.primary.volume_ratio_20,
                                snapshot.primary.trend,
                                snapshot.confirmation.trend,
                            )
                            self._last_no_setup_log[snapshot.symbol] = no_setup_signature


                    if candidate is not None:
                        cooldown_payload = self.cooldown_manager.as_log_payload(candidate.symbol)
                        if cooldown_payload is not None:
                            remaining_minutes = cooldown_payload.get("remaining_minutes", 0)
                            reason = cooldown_payload.get("reason", "cooldown")
                            until = cooldown_payload.get("until", "")

                            self.log.warning(
                                "SYMBOL_COOLDOWN_ACTIVE | %s | remaining_minutes=%s | reason=%s | until=%s",
                                candidate.symbol,
                                remaining_minutes,
                                reason,
                                until,
                            )

                            candidate.notes.append(f"symbol cooldown active ({remaining_minutes}m)")
                            candidate = None


                    if candidate is not None:
                        duplicate_block = self._duplicate_continuation_block(candidate)
                        if duplicate_block is not None:
                            self.log.warning(
                                "DUPLICATE_CONTINUATION_BLOCKED | %s | reason=%s",
                                candidate.symbol,
                                duplicate_block,
                            )
                            candidate.notes.append(f"duplicate continuation blocked ({duplicate_block})")
                            candidate = None

                    if candidate is not None:
                        score = self.scorer.score(candidate)
                        candidates.append((candidate, score))
                        risk = self.risk_manager.evaluate(candidate, score)
                        plan = self.trade_planner.build(candidate, score, risk)
                        self.strategy_performance_logger.append_setup_event(
                            symbol=plan.symbol,
                            strategy=plan.strategy,
                            direction=plan.direction,
                            verdict=plan.verdict,
                            score=round(float(score.total), 2),
                            stage="PLAN",
                            reasons=list(getattr(risk, "reasons", []) or []) + list(getattr(score, "reasons", []) or []),
                            notes=list(candidate.notes or []) + list(plan.notes or []),
                        )
                        plans.append(plan)
                        if (
                            plan.verdict == "BLOCKED"
                            and score.total >= 64.0
                            and risk.allowed
                            and any(
                                "planner_soft_bridge_candidate=true" in str(note)
                                for note in (plan.notes or [])
                            )
                        ):
                            self.log.warning(
                                "PLANNER_SOFT_BRIDGE | %s | strategy=%s | score=%.1f | risk_status=%s",
                                plan.symbol,
                                plan.strategy,
                                score.total,
                                risk.status,
                            )
                        if plan.verdict != "EXECUTABLE":
                            self.strategy_performance_logger.append_setup_event(
                                symbol=plan.symbol,
                                strategy=plan.strategy,
                                direction=plan.direction,
                                verdict=plan.verdict,
                                score=round(float(score.total), 2),
                                stage="PLAN_REJECT",
                                reasons=list(getattr(risk, "reasons", []) or []),
                                notes=list(candidate.notes or []) + list(plan.notes or []),
                            )
                            reject_signature = (
                                plan.verdict,
                                risk.status,
                                score.verdict,
                                round(score.total, 1),
                                round(plan.risk_reward_ratio, 2),
                                "; ".join(getattr(risk, "reasons", []) or []),
                            )

                            previous_signature = self._last_reject_log.get(plan.symbol)

                            if previous_signature != reject_signature:
                                self.log.info(
                                    "REJECTED_SETUP | %s | %s | %s | plan_verdict=%s | risk_verdict=%s | score_verdict=%s | score=%.1f | rr=%.2f | reasons=%s | notes=%s",
                                    plan.symbol,
                                    plan.strategy,
                                    plan.direction,
                                    plan.verdict,
                                    risk.status,
                                    score.verdict,
                                    score.total,
                                    plan.risk_reward_ratio,
                                    "; ".join(getattr(risk, "reasons", []) or []),
                                    "; ".join(candidate.notes or []),
                                )
                                self._last_reject_log[plan.symbol] = reject_signature
                        else:
                            self.log.info(
                                "ACCEPTED_SETUP | %s | %s | %s | score=%.1f | rr=%.2f | volatility_rank=%.1f | reasons=%s",
                                plan.symbol,
                                plan.strategy,
                                plan.direction,
                                score.total,
                                plan.risk_reward_ratio,
                                getattr(candidate.market, "volatility_rank", 0.0),
                                "; ".join(candidate.notes or []),
                            )
                        self.log.info(
                            "SETUP | %s | %s | %s | score=%.1f | verdict=%s | entry=%.6f | invalidation=%.6f | notes=%s",
                            candidate.symbol,
                            candidate.strategy,
                            candidate.direction,
                            score.total,
                            score.verdict,
                            candidate.detection.entry_hint,
                            candidate.detection.invalidation,
                            "; ".join(candidate.notes),
                        )
                        self.log.info(
                            "PLAN | %s | %s | %s | score=%.1f | verdict=%s | entries=%s | sl=%.6f | tp=%s | rr=%.2f | risk=%.2f%% | lev=%.1fx | notional=%.2f",
                            plan.symbol,
                            plan.strategy,
                            plan.direction,
                            plan.score,
                            plan.verdict,
                            ", ".join(f"{x:.6f}" for x in plan.entry_prices),
                            plan.stop_loss,
                            ", ".join(f"{x:.6f}" for x in plan.take_profits),
                            plan.risk_reward_ratio,
                            plan.account_risk_pct,
                            plan.leverage,
                            plan.position_notional_usdt,
                        )
                except Exception as exc:
                    if self._is_network_resolution_error(exc):
                        error_signature = ("symbol_scan", str(exc)[:180])
                        if self._last_network_error_log != error_signature:
                            self.log.error(
                                "API_NETWORK_UNAVAILABLE | stage=symbol_scan | symbol=%s | action=break_scan_cycle_preserve_local_state | error=%s",
                                symbol,
                                exc,
                            )
                            self._last_network_error_log = error_signature
                        break
                    self.log.exception("Scan failed for %s: %s", symbol, exc)

            if snapshots:
                self._emit_summary(snapshots)
                self.scan_logger.append_rows(snapshots)
            if candidates:
                self._emit_candidate_summary(candidates)
                self.candidate_logger.append_rows(candidates)
            if plans:
                self._emit_plan_summary(plans)
                self.trade_plan_logger.append_rows(plans)

            exec_reports = self.execution_service.execute(plans)
            if exec_reports:
                self._emit_execution_summary(exec_reports)
                self.execution_logger.append_rows(exec_reports)

            if snapshots and self.settings.position_manager_enabled:
                position_updates = self.position_manager.sync(snapshots)
                if position_updates:
                    self._emit_position_summary(position_updates)
                    self.position_logger.append_rows(position_updates)


        finally:
            if scan_lock_handle is not None:
                try:
                    fcntl.flock(scan_lock_handle, fcntl.LOCK_UN)
                    scan_lock_handle.close()
                except Exception:
                    pass
            self._scan_in_progress = False

    def _active_symbol_cooldown(self, symbol: str) -> dict | None:
        return self.cooldown_manager.as_log_payload(symbol)


    def _duplicate_continuation_block(self, candidate: StrategyCandidate) -> str | None:
        cooldowns = self.cooldown_store.load(default={})
        if not isinstance(cooldowns, dict):
            return None

        payload = cooldowns.get(candidate.symbol)
        if not isinstance(payload, dict):
            return None

        reason = str(payload.get("reason") or "")
        pnl_pct = float(payload.get("pnl_pct") or 0.0)
        cooldown_minutes = int(payload.get("cooldown_minutes") or 0)

        explosive_move = pnl_pct >= 0.5
        recent_close = cooldown_minutes > 0

        if explosive_move and recent_close:
            return f"recent explosive move after {reason}"

        return None

    def _emit_summary(self, snapshots: list[MarketSnapshot]) -> None:
        ranked = sorted(snapshots, key=_execution_aware_score, reverse=True)
        top = ranked[:3]
        alignments = Counter(s.alignment for s in snapshots)

        current_top_symbols = [snap.symbol for snap in top]

        if current_top_symbols != self._last_top_symbols:
            self.log.info(
                "SUMMARY | total=%s | aligned_bullish=%s | aligned_bearish=%s | conflicted=%s | mixed=%s",
                len(snapshots),
                alignments.get("aligned_bullish", 0),
                alignments.get("aligned_bearish", 0),
                alignments.get("conflicted", 0),
                alignments.get("mixed", 0),
            )

            for idx, snap in enumerate(top, start=1):
                execution_score = _execution_aware_score(snap)
                self.log.info(
                    "TOP%d | %s | score_hint=%.1f | execution_score=%.1f | align=%s | close=%.6f | notes=%s",
                    idx,
                    snap.symbol,
                    snap.score_hint,
                    execution_score,
                    snap.alignment,
                    snap.primary.latest_close,
                    "; ".join(snap.notes),
                )

            self._last_top_symbols = current_top_symbols

    def _emit_candidate_summary(self, candidates: list[tuple[StrategyCandidate, StrategyScore]]) -> None:
        ranked = sorted(candidates, key=lambda row: row[1].total, reverse=True)[: self.settings.strategy_candidate_limit]
        verdicts = Counter(score.verdict for _, score in candidates)
        self.log.info(
            "SETUP_SUMMARY | total=%s | go=%s | watch=%s | no_go=%s",
            len(candidates),
            verdicts.get("GO", 0),
            verdicts.get("WATCH", 0),
            verdicts.get("NO_GO", 0),
        )
        for idx, (candidate, score) in enumerate(ranked, start=1):
            self.log.info(
                "CANDIDATE%d | %s | %s | score=%.1f | verdict=%s | alignment=%s | entry=%.6f | reclaim=%.6f | invalidation=%.6f",
                idx,
                candidate.symbol,
                candidate.direction,
                score.total,
                score.verdict,
                candidate.market.alignment,
                candidate.detection.entry_hint,
                candidate.detection.reclaim_level,
                candidate.detection.invalidation,
            )

    def _emit_plan_summary(self, plans: list[TradePlan]) -> None:
        ranked = sorted(plans, key=lambda plan: (plan.verdict == "EXECUTABLE", plan.score, plan.risk_reward_ratio), reverse=True)
        executable = sum(1 for plan in plans if plan.verdict == "EXECUTABLE")
        blocked = len(plans) - executable
        plan_signature = (len(plans), executable, blocked)

        if self._last_plan_summary != plan_signature:
            self.log.info("PLAN_SUMMARY | total=%s | executable=%s | blocked=%s", len(plans), executable, blocked)
            self._last_plan_summary = plan_signature
        for idx, plan in enumerate(ranked[: self.settings.strategy_candidate_limit], start=1):
            self.log.info(
                "TRADEPLAN%d | %s | %s | verdict=%s | score=%.1f | rr=%.2f | entries=%s | sl=%.6f | tp1=%.6f",
                idx,
                plan.symbol,
                plan.direction,
                plan.verdict,
                plan.score,
                plan.risk_reward_ratio,
                ", ".join(f"{x:.6f}" for x in plan.entry_prices),
                plan.stop_loss,
                plan.take_profits[0] if plan.take_profits else 0.0,
            )

    def _emit_execution_summary(self, reports: list[ExecutionReport]) -> None:
        counts = Counter(report.status for report in reports)
        self.log.info(
            "EXEC_SUMMARY | total=%s | simulated=%s | executed=%s | skipped=%s",
            len(reports),
            counts.get("SIMULATED", 0),
            counts.get("EXECUTED", 0),
            counts.get("SKIPPED", 0),
        )
        for idx, report in enumerate(reports[: self.settings.execution_plan_limit], start=1):
            self.log.info(
                "EXECUTION%d | %s | %s | mode=%s | status=%s | avg_entry=%.6f | sl=%.6f | tp1=%.6f | msg=%s",
                idx,
                report.symbol,
                report.direction,
                report.mode,
                report.status,
                report.avg_entry,
                report.stop_loss,
                report.take_profits[0] if report.take_profits else 0.0,
                report.message,
            )

    def _emit_position_summary(self, updates: list[PositionUpdate]) -> None:
        counts = Counter(update.status for update in updates)
        self.log.info(
            "POSITION_SUMMARY | total=%s | open=%s | closed=%s",
            len(updates),
            counts.get("OPEN", 0),
            counts.get("CLOSED", 0),
        )
        for idx, update in enumerate(updates[: self.settings.execution_plan_limit], start=1):
            position_signature = (
                update.status,
                round(update.current_price, 4),
                round(update.unrealized_pnl_pct, 2),
                round(update.stop_loss, 6),
                update.break_even_active,
                update.tp1_hit,
                update.tp2_hit,
                update.tp3_hit,
            )

            previous_signature = self._last_position_snapshot.get(update.symbol)

            if previous_signature != position_signature:
                self.log.info(
                    "POSITION%d | %s | status=%s | px=%.6f | upnl=%.3f%% | sl=%.6f | be=%s | tp1=%s | tp2=%s | tp3=%s | note=%s",
                    idx,
                    update.symbol,
                    update.status,
                    update.current_price,
                    update.unrealized_pnl_pct,
                    update.stop_loss,
                    update.break_even_active,
                    update.tp1_hit,
                    update.tp2_hit,
                    update.tp3_hit,
                    update.note,
                )

                self._last_position_snapshot[update.symbol] = position_signature


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    settings = Settings()
    StartupRunner(settings).run()


if __name__ == "__main__":
    main()

