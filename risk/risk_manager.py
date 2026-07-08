from __future__ import annotations

import csv
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.equity import resolve_account_equity
from clients.schemas import RiskVerdict, StrategyCandidate, StrategyScore

BASE_PATH = Path(__file__).resolve().parents[1]
REPORTS_PATH = BASE_PATH / "reports" / "backtests"
AGENT_DECISIONS_PATH = BASE_PATH / "agents_v2" / "reports" / "coach_decisions.json"
logger = logging.getLogger("risk_manager")

BETA_CLUSTERS = {
    "L1_BETA": {"SOLUSDT", "AVAXUSDT", "SUIUSDT", "INJUSDT", "SEIUSDT"},
    "MAJORS": {"BTCUSDT", "ETHUSDT"},
    "AI_BETA": {"FETUSDT", "RNDRUSDT", "TAOUSDT"},
}

class RiskManager:
    SAFE_ALPHA_MAX_LEVERAGE = 8
    SAFE_ALPHA_MAX_RISK_PCT = 0.75
    # Probe mode: a strategy flagged by the expectancy report or the coach
    # keeps trading at this fraction of normal risk so it can re-qualify.
    PROBE_RISK_MULTIPLIER = 0.5

    # Last known allocation state per strategy, to log promotion/demotion
    # transitions (roadmap N3). Class-level so all gate paths share it.
    _allocation_states: dict[str, str] = {}

    def _log_allocation_transition(self, strategy: str, state: str) -> None:
        strategy_key = str(strategy or "unknown").lower()
        previous = self._allocation_states.get(strategy_key)
        if previous == state:
            return
        self._allocation_states[strategy_key] = state
        if previous is not None:
            logger.warning(
                "ALLOCATION_CHANGED | strategy=%s | %s -> %s",
                strategy_key,
                previous,
                state,
            )
    SAFE_ALPHA_MIN_SCORE = 60
    SAFE_MOMENTUM_MIN_SCORE = 72
    SAFE_CONTINUATION_MIN_SCORE = 78
    SAFE_ALPHA_MAX_BARS_SINCE_SWEEP = 1

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def _latest_agent_decisions() -> dict:
        """Read Learning/Coach decisions. Safe fallback: no decisions."""
        if not AGENT_DECISIONS_PATH.exists():
            return {}

        try:
            with open(AGENT_DECISIONS_PATH, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            logger.warning("AI_AGENT_DECISIONS_UNAVAILABLE | error=%s", exc)
            return {}

        return payload if isinstance(payload, dict) else {}

    def _ai_agent_gate(self, candidate: StrategyCandidate) -> tuple[bool, list[str], bool]:
        """Apply Learning Agent decisions as a live risk gate.

        Returns (allowed, reasons, probe). A coach "reduce_strategy_exposure"
        now does what it says — reduce size — instead of hard-blocking; only
        symbol-level avoidance stays a hard block.
        """
        reasons: list[str] = []
        payload = self._latest_agent_decisions()
        decisions = payload.get("decisions") or []

        if not decisions:
            reasons.append("ai-agent: no active decisions")
            return True, reasons, False

        strategy_name = str(candidate.strategy or "").strip().lower()
        symbol = str(candidate.symbol or "").strip().upper()

        for decision in decisions:
            if not isinstance(decision, dict):
                continue

            action = str(decision.get("action") or "").strip()
            target = str(decision.get("target") or "").strip()
            target_lower = target.lower()
            target_upper = target.upper()
            reason = str(decision.get("reason") or "")

            if action == "reduce_strategy_exposure" and target_lower and target_lower in strategy_name:
                reasons.append(f"ai-agent PROBE: strategy exposure reduced by coach ({target}) | {reason}")
                logger.info(
                    "AI_AGENT_STRATEGY_PROBE | symbol=%s | strategy=%s | target=%s | reason=%s",
                    symbol,
                    candidate.strategy,
                    target,
                    reason,
                )
                return True, reasons, True

            if action == "avoid_symbol_until_improved" and target_upper and target_upper == symbol:
                reasons.append(f"ai-agent HARD_BLOCK: symbol avoided by coach ({target}) | {reason}")
                logger.warning(
                    "AI_AGENT_SYMBOL_BLOCK | symbol=%s | strategy=%s | target=%s | reason=%s",
                    symbol,
                    candidate.strategy,
                    target,
                    reason,
                )
                return False, reasons, False

        reasons.append(f"ai-agent: passed ({len(decisions)} decisions checked)")
        logger.info(
            "AI_AGENT_GATE_PASSED | symbol=%s | strategy=%s | decisions=%s",
            symbol,
            candidate.strategy,
            len(decisions),
        )
        return True, reasons, False

    def _kill_switch_gate(self, candidate: StrategyCandidate) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        summary = self._latest_backtest_summary()
        if not summary:
            return True, reasons

        strategy_name = (candidate.strategy or "").lower()
        symbol = (candidate.symbol or "").upper()

        by_strategy = summary.get("by_strategy") or {}
        by_symbol = summary.get("by_symbol") or {}

        clean_expectancy = self._latest_strategy_expectancy()
        clean_by_strategy = clean_expectancy.get("strategies") or {}
        if clean_by_strategy:
            by_strategy = clean_by_strategy

        strategy_stats = by_strategy.get(strategy_name) or {}
        symbol_stats = by_symbol.get(symbol) or {}

        # --- Defensive daily status checks ---
        daily_status = self._daily_defensive_status()
        if daily_status.get("daily_status_unreadable"):
            reasons.append(
                "kill-switch: daily learning report unreadable; failing closed until it is restored"
            )
        daily_realized_pnl = float(daily_status.get("daily_total_net_pnl", 0.0) or 0.0)
        consecutive_losses = int(daily_status.get("consecutive_losses", 0) or 0)

        # Daily loss kill-switch as a % of account equity, matching the
        # HARD_DAILY_STOP_PCT the rest of the system (dashboard_v2) already
        # surfaces -- previously this was a flat -10.0 USD figure that didn't
        # scale with account size.
        account_equity, _equity_source = resolve_account_equity(self.settings)
        hard_daily_stop_pct = float(getattr(self.settings, "hard_daily_stop_pct", 0.0) or 0.0)
        daily_loss_pct = (
            abs(daily_realized_pnl) / account_equity * 100.0
            if account_equity > 0 and daily_realized_pnl < 0
            else 0.0
        )

        if hard_daily_stop_pct and daily_loss_pct >= hard_daily_stop_pct:
            reasons.append(
                f"kill-switch: daily defensive mode active "
                f"(daily_pnl={daily_realized_pnl:.2f}, daily_loss_pct={daily_loss_pct:.2f}%, "
                f"hard_daily_stop_pct={hard_daily_stop_pct:.2f}%)"
            )

        if consecutive_losses >= 3:
            reasons.append(
                f"kill-switch: consecutive loss limit reached ({consecutive_losses})"
            )

        weekly_freeze_pct = float(getattr(self.settings, "weekly_freeze_loss_pct", 0.0) or 0.0)
        if weekly_freeze_pct and account_equity > 0:
            weekly_pnl = self._weekly_realized_pnl()
            weekly_loss_pct = abs(weekly_pnl) / account_equity * 100.0 if weekly_pnl < 0 else 0.0
            if weekly_loss_pct >= weekly_freeze_pct:
                reasons.append(
                    f"kill-switch: weekly freeze active "
                    f"(weekly_pnl={weekly_pnl:.2f}, weekly_loss_pct={weekly_loss_pct:.2f}%, "
                    f"weekly_freeze_loss_pct={weekly_freeze_pct:.2f}%)"
                )

        if self._stats_should_pause(strategy_stats, min_trades=5):
            reasons.append(f"expectancy-watch: strategy weak but not hard-paused ({strategy_name})")

        if self._stats_should_pause(symbol_stats, min_trades=3):
            reasons.append(f"kill-switch: symbol paused by expectancy ({symbol})")

        if self._too_many_failed_tp1(symbol_stats):
            reasons.append(f"kill-switch: symbol failed TP1 too often ({symbol})")

        hard_reasons = [
            reason for reason in reasons
            if not str(reason).startswith("expectancy-watch: strategy weak but not hard-paused")
        ]
        return not hard_reasons, reasons

    def _strategy_weighting_gate(self, candidate: StrategyCandidate) -> tuple[bool, list[str], bool]:
        """Returns (allowed, reasons, probe).

        probe=True means: keep trading, but at reduced size (hedge-fund style
        dynamic allocation). A strategy with negative recent expectancy earns
        its full allocation back through fresh results instead of being frozen
        out entirely — a hard freeze can never re-qualify because it generates
        no new data.
        """
        reasons: list[str] = []
        strategy_expectancy = self._latest_strategy_expectancy()
        if not strategy_expectancy:
            return True, reasons, False

        strategy_name = (candidate.strategy or "").lower()
        note_text = self._note_text(candidate)

        if (
            "low_vol_reclaim" in strategy_name
            or "low vol reclaim" in strategy_name
            or "fallback_candidate_bridge=true" in note_text
            or "reclaim_unlock_v" in note_text
            or "entry_model=retest_zone_first" in note_text
        ):
            strategy_name = "low_vol_reclaim"

        clean_strategies = strategy_expectancy.get("strategies") or {}
        stats = clean_strategies.get(strategy_name) or {}
        if not isinstance(stats, dict):
            reasons.append(f"strategy weighting WATCH: no clean expectancy data ({strategy_name})")
            return True, reasons, False

        trades = int(stats.get("trades", 0) or 0)
        expectancy = float(stats.get("expectancy", 0.0) or 0.0)
        raw_tp1_hit_rate = stats.get("tp1_hit_rate")
        # The dataset explicitly distinguishes "missing" (tp1_hit_rate_status
        # == "missing_not_zero") from a genuine 0% hit-rate. Coercing a missing
        # value to 0.0 here previously hard-blocked strategies with excellent
        # real performance (e.g. low_vol_reclaim: 82% winrate, +expectancy)
        # purely because TP1 tracking data hadn't been backfilled yet.
        tp1_hit_rate_missing = raw_tp1_hit_rate is None
        tp1_hit_rate = float(raw_tp1_hit_rate) if raw_tp1_hit_rate is not None else 0.0
        reasons.append(
            f"strategy weighting source=clean_strategy_expectancy ({strategy_name}, trades={trades}, exp={expectancy:.3f})"
        )

        if trades < 5:
            reasons.append(f"strategy weighting WATCH: insufficient data ({strategy_name}, trades={trades})")
            return True, reasons, False

        if expectancy < 0:
            reasons.append(
                f"strategy weighting PROBE: negative expectancy, trading at reduced size ({strategy_name}, trades={trades}, exp={expectancy:.3f})"
            )
            return True, reasons, True

        if tp1_hit_rate_missing:
            reasons.append(f"strategy weighting WATCH: tp1_hit_rate data missing, not treated as zero ({strategy_name}, trades={trades}, exp={expectancy:.3f})")
        elif tp1_hit_rate < 0.25:
            reasons.append(
                f"strategy weighting PROBE: weak TP1 hit-rate, trading at reduced size ({strategy_name}, trades={trades}, tp1={tp1_hit_rate:.3f})"
            )
            return True, reasons, True

        if expectancy >= 0.15 and tp1_hit_rate >= 0.45:
            reasons.append(f"strategy weighting BOOST: strong expectancy ({strategy_name}, exp={expectancy:.3f})")
        else:
            reasons.append(f"strategy weighting WATCH: neutral expectancy ({strategy_name}, exp={expectancy:.3f})")

        return True, reasons, False

    _weekly_pnl_cache: tuple[float, float] | None = None  # (monotonic_ts, value)
    WEEKLY_PNL_CACHE_SECONDS = 60.0

    def _weekly_realized_pnl(self) -> float:
        """Rolling 7-day realized net PnL from the v2 close dataset.

        Backs the WEEKLY_FREEZE_LOSS_PCT kill-switch, which was configured in
        .env since the start but never enforced anywhere. Cached 60s; a read
        failure returns 0.0 (the daily stop and consecutive-loss switches
        remain the primary intraday brakes).
        """
        now = time.monotonic()
        if self._weekly_pnl_cache and (now - self._weekly_pnl_cache[0]) < self.WEEKLY_PNL_CACHE_SECONDS:
            return self._weekly_pnl_cache[1]

        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        total = 0.0
        try:
            dataset_path = BASE_PATH / "logs" / "trade_dataset_v2.csv"
            for path in (dataset_path.with_name(dataset_path.name + ".1"), dataset_path):
                if not path.exists():
                    continue
                with path.open("r", newline="", encoding="utf-8") as handle:
                    for row in csv.DictReader(handle):
                        if str(row.get("event_type") or "").upper() not in ("CLOSE", "POSITION_CLOSED"):
                            continue
                        closed_at = str(row.get("closed_at") or row.get("timestamp") or "")
                        if closed_at < cutoff:
                            continue
                        raw = row.get("net_pnl") or row.get("pnl") or 0
                        try:
                            total += float(raw)
                        except (TypeError, ValueError):
                            continue
        except Exception as exc:
            logger.warning("WEEKLY_PNL_READ_FAILED | error=%s", exc)
            total = 0.0

        self._weekly_pnl_cache = (now, total)
        return total

    def day_mode(self) -> dict:
        """RED/GREEN day verdict for the daily defensive layer (roadmap P0.8).

        RED = capital-protection mode: the hard daily stop distance is more
        than half consumed, or 3+ consecutive losses. Logged once per scan
        cycle by the runner so every day has an auditable mode trail.
        """
        daily_status = self._daily_defensive_status()
        daily_realized_pnl = float(daily_status.get("daily_total_net_pnl", 0.0) or 0.0)
        consecutive_losses = int(daily_status.get("consecutive_losses", 0) or 0)
        account_equity, equity_source = resolve_account_equity(self.settings)
        hard_daily_stop_pct = float(getattr(self.settings, "hard_daily_stop_pct", 0.0) or 0.0)
        daily_loss_pct = (
            abs(daily_realized_pnl) / account_equity * 100.0
            if account_equity > 0 and daily_realized_pnl < 0
            else 0.0
        )
        weekly_pnl = self._weekly_realized_pnl()
        weekly_freeze_pct = float(getattr(self.settings, "weekly_freeze_loss_pct", 0.0) or 0.0)
        weekly_loss_pct = abs(weekly_pnl) / account_equity * 100.0 if account_equity > 0 and weekly_pnl < 0 else 0.0
        red = (
            (hard_daily_stop_pct > 0 and daily_loss_pct >= hard_daily_stop_pct * 0.5)
            or consecutive_losses >= 3
            or (weekly_freeze_pct > 0 and weekly_loss_pct >= weekly_freeze_pct)
        )
        return {
            "mode": "RED" if red else "GREEN",
            "daily_realized_pnl": round(daily_realized_pnl, 4),
            "daily_loss_pct": round(daily_loss_pct, 4),
            "consecutive_losses": consecutive_losses,
            "weekly_realized_pnl": round(weekly_pnl, 4),
            "weekly_loss_pct": round(weekly_loss_pct, 4),
            "account_equity": round(account_equity, 2),
            "equity_source": equity_source,
        }

    def _session_risk_multiplier(self, now_hour_utc: int | None = None) -> tuple[float, str]:
        """Size down (never up) inside UTC hour windows with negative live history.

        Live data (2026-06/07): 08-11 UTC (EU morning chop) and 23-00 UTC
        (post-US dead zone) were consistently red; US session hours were green.
        """
        raw_windows = str(getattr(self.settings, "session_risk_reduction_windows_utc", "") or "")
        multiplier = float(getattr(self.settings, "session_risk_multiplier", 0.5) or 0.5)
        multiplier = min(max(multiplier, 0.1), 1.0)
        if not raw_windows or multiplier >= 1.0:
            return 1.0, ""

        if now_hour_utc is None:
            now_hour_utc = datetime.now(timezone.utc).hour

        for window in raw_windows.split(","):
            window = window.strip()
            if "-" not in window:
                continue
            try:
                start, end = (int(part) % 24 for part in window.split("-", 1))
            except ValueError:
                continue
            in_window = start <= now_hour_utc < end if start < end else (now_hour_utc >= start or now_hour_utc < end)
            if in_window:
                return multiplier, f"session risk window {window} UTC active: risk x{multiplier:.2f}"

        return 1.0, ""

    @staticmethod
    def _stats_should_pause(stats: dict, min_trades: int) -> bool:
        if not isinstance(stats, dict):
            return False

        trades = int(stats.get("trades", 0) or 0)
        expectancy = float(stats.get("expectancy", 0.0) or 0.0)
        lossrate = float(stats.get("lossrate", 0.0) or 0.0)

        if trades < min_trades:
            return False

        if expectancy < 0:
            return True

        if trades >= 5 and lossrate >= 0.75:
            return True

        return False

    @staticmethod
    def _too_many_failed_tp1(stats: dict) -> bool:
        if not isinstance(stats, dict):
            return False

        trades = int(stats.get("trades", 0) or 0)
        tp1_hit_rate = float(stats.get("tp1_hit_rate", 0.0) or 0.0)

        return trades >= 5 and tp1_hit_rate < 0.25

    @staticmethod
    def _latest_backtest_summary() -> dict:
        path = REPORTS_PATH / "latest_summary.json"
        if not path.exists():
            return {}

        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return {}

        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _latest_strategy_expectancy() -> dict:
        path = REPORTS_PATH / "strategy_expectancy.json"
        if not path.exists():
            return {}

        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return {}

        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _daily_defensive_status() -> dict:
        """Fail-closed nuance: a MISSING report is a normal fresh state (no
        defensive data -> no block), but an UNREADABLE/corrupt report means the
        daily kill-switch would be silently disabled — that must block instead.
        """
        path = BASE_PATH / "data_store" / "trades" / "daily_learning_report.json"
        if not path.exists():
            return {}

        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception as exc:
            logger.error("DAILY_DEFENSIVE_STATUS_UNREADABLE | fail-closed | error=%s", exc)
            return {"daily_status_unreadable": True}

        if not isinstance(payload, dict):
            logger.error("DAILY_DEFENSIVE_STATUS_MALFORMED | fail-closed | type=%s", type(payload).__name__)
            return {"daily_status_unreadable": True}

        return payload

    def optimization_advice(self) -> dict:
        """Read-only advisory layer. Never mutates settings or trading state."""
        summary = self._latest_backtest_summary()
        if not summary:
            return {
                "mode": "ADVISORY_ONLY",
                "status": "NO_DATA",
                "symbols_to_pause": [],
                "strategies_to_boost": [],
                "strategies_to_watch": [],
                "regime_notes": [],
                "risk_suggestions": ["no backtest summary available; keep current risk settings"],
            }

        by_symbol = summary.get("by_symbol") or {}
        by_strategy = summary.get("by_strategy") or {}
        by_regime = summary.get("by_regime") or {}
        clean_expectancy = self._latest_strategy_expectancy()
        clean_by_strategy = clean_expectancy.get("strategies") or {}
        if clean_by_strategy:
            by_strategy = clean_by_strategy

        symbols_to_pause: list[dict] = []
        strategies_to_boost: list[dict] = []
        strategies_to_watch: list[dict] = []
        regime_notes: list[dict] = []
        risk_suggestions: list[str] = []

        for symbol, stats in by_symbol.items():
            if not isinstance(stats, dict):
                continue
            trades = int(stats.get("trades", 0) or 0)
            expectancy = float(stats.get("expectancy", 0.0) or 0.0)
            tp1_hit_rate = float(stats.get("tp1_hit_rate", 0.0) or 0.0)
            lossrate = float(stats.get("lossrate", 0.0) or 0.0)

            if trades >= 3 and (expectancy < 0 or lossrate >= 0.75 or tp1_hit_rate < 0.25):
                symbols_to_pause.append(
                    {
                        "symbol": str(symbol).upper(),
                        "trades": trades,
                        "expectancy": round(expectancy, 4),
                        "tp1_hit_rate": round(tp1_hit_rate, 4),
                        "lossrate": round(lossrate, 4),
                        "reason": "negative expectancy / weak TP1 / high lossrate",
                    }
                )

        for strategy, stats in by_strategy.items():
            if not isinstance(stats, dict):
                continue
            trades = int(stats.get("trades", 0) or 0)
            expectancy = float(stats.get("expectancy", 0.0) or 0.0)
            tp1_hit_rate = float(stats.get("tp1_hit_rate", 0.0) or 0.0)
            lossrate = float(stats.get("lossrate", 0.0) or 0.0)

            payload = {
                "strategy": str(strategy),
                "trades": trades,
                "expectancy": round(expectancy, 4),
                "tp1_hit_rate": round(tp1_hit_rate, 4),
                "lossrate": round(lossrate, 4),
            }

            if trades >= 5 and expectancy >= 0.15 and tp1_hit_rate >= 0.45:
                strategies_to_boost.append({**payload, "reason": "positive expectancy with acceptable TP1 hit-rate"})
            elif trades >= 5 and (expectancy < 0 or tp1_hit_rate < 0.25 or lossrate >= 0.75):
                strategies_to_watch.append({**payload, "reason": "weak expectancy / TP1 / lossrate; consider pause"})
            elif trades > 0:
                strategies_to_watch.append({**payload, "reason": "insufficient or neutral data; keep watching"})

        for regime, stats in by_regime.items():
            if not isinstance(stats, dict):
                continue
            trades = int(stats.get("trades", 0) or 0)
            expectancy = float(stats.get("expectancy", 0.0) or 0.0)
            winrate = float(stats.get("winrate", 0.0) or 0.0)

            if trades >= 3:
                verdict = "FAVORABLE" if expectancy > 0 else "HOSTILE"
                regime_notes.append(
                    {
                        "regime": str(regime),
                        "verdict": verdict,
                        "trades": trades,
                        "expectancy": round(expectancy, 4),
                        "winrate": round(winrate, 4),
                    }
                )

        if symbols_to_pause:
            risk_suggestions.append("pause or reduce size on weak symbols until expectancy improves")
        if not strategies_to_boost:
            risk_suggestions.append("no strategy has earned a size boost yet; keep risk conservative")
        if len(symbols_to_pause) >= 3:
            risk_suggestions.append("broad symbol weakness detected; consider lowering risk per trade temporarily")
        if strategies_to_boost:
            risk_suggestions.append("boost candidates are advisory only; require manual approval before changing weights")

        return {
            "mode": "ADVISORY_ONLY",
            "status": "OK",
            "symbols_to_pause": symbols_to_pause[:10],
            "strategies_to_boost": strategies_to_boost[:10],
            "strategies_to_watch": strategies_to_watch[:10],
            "regime_notes": regime_notes[:10],
            "risk_suggestions": risk_suggestions,
        }

    @staticmethod
    def _cluster_for_symbol(symbol: str) -> str | None:
        upper = symbol.upper()
        for cluster_name, symbols in BETA_CLUSTERS.items():
            if upper in symbols:
                return cluster_name
        return None

    @staticmethod
    def _load_open_positions() -> list[dict]:
        path = BASE_PATH / "state" / "executed_trades.json"
        if not path.exists():
            return []

        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return []

        if not isinstance(payload, list):
            return []

        return [p for p in payload if isinstance(p, dict) and str(p.get("status") or "") == "OPEN"]

    def _cluster_risk_gate(self, candidate: StrategyCandidate) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        symbol = (candidate.symbol or "").upper()
        cluster = self._cluster_for_symbol(symbol)

        if not cluster:
            return True, reasons

        open_positions = self._load_open_positions()
        same_cluster = []

        for position in open_positions:
            open_symbol = str(position.get("symbol") or "").upper()
            open_cluster = self._cluster_for_symbol(open_symbol)
            if open_cluster == cluster:
                same_cluster.append(position)

        if len(same_cluster) >= self.settings.max_correlated_positions:
            reasons.append(
                f"cluster limit reached ({cluster}): {len(same_cluster)} open correlated positions"
            )
            return False, reasons

        total_cluster_exposure = 0.0
        for position in same_cluster:
            total_cluster_exposure += float(position.get("position_notional_usdt") or 0.0)

        wallet_reference = 100.0
        cluster_exposure_pct = (total_cluster_exposure / wallet_reference) * 100 if wallet_reference else 0.0

        if cluster_exposure_pct >= self.settings.max_cluster_exposure_pct:
            reasons.append(
                f"cluster exposure too high ({cluster}): {cluster_exposure_pct:.1f}%"
            )
            return False, reasons

        return True, reasons

    def _directional_exposure_gate(self, candidate: StrategyCandidate) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        direction = str(candidate.direction or "").upper()
        symbol = str(candidate.symbol or "").upper()

        if direction not in {"LONG", "SHORT"}:
            return True, reasons

        open_positions = self._load_open_positions()
        open_other_positions = [
            position for position in open_positions
            if str(position.get("symbol") or "").upper() != symbol
        ]

        max_total_positions = int(getattr(self.settings, "max_open_positions", 4) or 4)
        max_same_direction_positions = int(getattr(self.settings, "max_same_direction_positions", 3) or 3)

        same_direction_positions = [
            position for position in open_other_positions
            if str(position.get("direction") or "").upper() == direction
        ]

        if len(open_other_positions) >= max_total_positions:
            reasons.append(
                f"portfolio exposure blocked: max total open positions reached ({len(open_other_positions)}/{max_total_positions})"
            )
            logger.warning(
                "PORTFOLIO_EXPOSURE_BLOCKED | symbol=%s | reason=max_total_positions | total=%s/%s",
                symbol,
                len(open_other_positions),
                max_total_positions,
            )
            return False, reasons

        if len(same_direction_positions) >= max_same_direction_positions:
            open_symbols = ",".join(
                str(position.get("symbol") or "").upper()
                for position in same_direction_positions
            )
            reasons.append(
                f"portfolio exposure blocked: max {direction.lower()} positions reached ({len(same_direction_positions)}/{max_same_direction_positions}) open={open_symbols}"
            )
            logger.warning(
                "PORTFOLIO_EXPOSURE_BLOCKED | symbol=%s | direction=%s | reason=max_same_direction_positions | same_direction=%s/%s",
                symbol,
                direction,
                len(same_direction_positions),
                max_same_direction_positions,
            )
            return False, reasons

        reasons.append(
            f"portfolio exposure ok: direction={direction} same_direction={len(same_direction_positions)}/{max_same_direction_positions} total={len(open_other_positions)}/{max_total_positions}"
        )
        logger.info(
            "PORTFOLIO_EXPOSURE_OK | symbol=%s | direction=%s | same_direction=%s/%s | total=%s/%s",
            symbol,
            direction,
            len(same_direction_positions),
            max_same_direction_positions,
            len(open_other_positions),
            max_total_positions,
        )
        return True, reasons

    def _alignment_gate(self, candidate: StrategyCandidate, score: StrategyScore) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        alignment = (candidate.market.alignment or "").lower()
        direction = candidate.direction.upper()
        primary_trend = (candidate.market.primary.trend or "").lower()
        confirmation_trend = (candidate.market.confirmation.trend or "").lower()
        strategy_name = (candidate.strategy or "").lower()
        mtf_quality = self._mtf_quality(candidate)

        if alignment == "conflicted":
            if "low_vol_reclaim" in strategy_name and RiskManager._has_mtf_override(candidate):
                reasons.append("watch: conflicted alignment allowed for reclaim MTF override")
            else:
                reasons.append("blocked: market alignment conflicted")
                return False, reasons

        if alignment == "mixed" and not mtf_quality:
            if "low_vol_reclaim" in strategy_name and RiskManager._has_mtf_override(candidate):
                reasons.append("watch: mixed alignment allowed for reclaim MTF override")
            else:
                reasons.append("blocked: market alignment mixed without MTF confirmation")
                return False, reasons

        if direction == "LONG":
            if primary_trend != "bullish":
                if "low_vol_reclaim" in strategy_name and primary_trend in {"mixed", "neutral"} and RiskManager._has_mtf_override(candidate):
                    reasons.append("watch: long reclaim allowed with mixed/neutral primary via MTF override")
                else:
                    reasons.append("blocked: long without bullish primary trend")
                    return False, reasons
            if confirmation_trend not in {"bullish", "neutral"}:
                if "low_vol_reclaim" in strategy_name and confirmation_trend in {"mixed", "neutral"} and RiskManager._has_mtf_override(candidate):
                    reasons.append("watch: long reclaim allowed with mixed/neutral confirmation via MTF override")
                else:
                    reasons.append("blocked: long without bullish/neutral confirmation trend")
                    return False, reasons
        elif direction == "SHORT":
            if primary_trend != "bearish":
                if "low_vol_reclaim" in strategy_name and primary_trend in {"mixed", "neutral"} and RiskManager._has_mtf_override(candidate):
                    reasons.append("watch: short reclaim allowed with mixed/neutral primary via MTF override")
                else:
                    reasons.append("blocked: short without bearish primary trend")
                    return False, reasons
            if confirmation_trend not in {"bearish", "neutral"}:
                if "low_vol_reclaim" in strategy_name and confirmation_trend in {"mixed", "neutral"} and RiskManager._has_mtf_override(candidate):
                    reasons.append("watch: short reclaim allowed with mixed/neutral confirmation via MTF override")
                else:
                    reasons.append("blocked: short without bearish/neutral confirmation trend")
                    return False, reasons

        if "sweep" in strategy_name and alignment not in {"aligned_bullish", "aligned_bearish"} and not mtf_quality:
            reasons.append("blocked: sweep requires fully aligned market or MTF confirmation")
            return False, reasons

        if "momentum_breakout" in strategy_name:
            if direction != "LONG":
                reasons.append("blocked: momentum breakout must be LONG")
                return False, reasons
            if alignment != "aligned_bullish" and not mtf_quality:
                reasons.append("blocked: momentum breakout requires aligned bullish market or MTF confirmation")
                return False, reasons

        if "momentum_breakdown" in strategy_name:
            if direction != "SHORT":
                reasons.append("blocked: momentum breakdown must be SHORT")
                return False, reasons
            if alignment != "aligned_bearish" and not mtf_quality:
                reasons.append("blocked: momentum breakdown requires aligned bearish market or MTF confirmation")
                return False, reasons

        if score.verdict != "GO":
            reasons.append(f"blocked: score verdict {score.verdict.lower()}")
            return False, reasons

        return True, reasons

    @staticmethod
    def _note_text(candidate: StrategyCandidate) -> str:
        candidate_notes = [str(note).lower() for note in (candidate.notes or [])]
        market_notes = [str(note).lower() for note in (getattr(candidate.market, "notes", []) or [])]
        return " ".join(candidate_notes + market_notes)

    @staticmethod
    def _is_backtest_candidate(candidate: StrategyCandidate) -> bool:
        return "backtest synthetic snapshot" in RiskManager._note_text(candidate)

    @staticmethod
    def _extract_note_float(candidate: StrategyCandidate, marker: str, default: float = 0.0) -> float:
        note_text = RiskManager._note_text(candidate)
        marker = marker.lower()

        if marker not in note_text:
            return default

        try:
            section = note_text.split(marker, 1)[1]
            raw = section.split()[0].strip(";|,").replace("bps", "")
            return float(raw)
        except Exception:
            return default

    @staticmethod
    def _has_mtf_override(candidate: StrategyCandidate) -> bool:
        note_text = RiskManager._note_text(candidate)
        return (
            "mtf_prearmed_override" in note_text
            or "prearmed_breakout" in note_text
            or "prearmed_breakdown" in note_text
            or "mtf_sweep_mode=mtf_override" in note_text
            or "mtf_sweep_mode mtf_override" in note_text
            or "mtf_continuation_mode mtf_override" in note_text
            or "mtf_reclaim_mode mtf_override" in note_text
        )

    @staticmethod
    def _mtf_pressure_score(candidate: StrategyCandidate) -> float:
        for marker in (
            "mtf_pressure_score=",
            "mtf_pressure_score ",
            "prearmed_pressure_score=",
            "prearmed_pressure_score ",
            "pressure_score=",
            "pressure_score ",
        ):
            value = RiskManager._extract_note_float(candidate, marker, 0.0)
            if value:
                return value
        return 0.0

    @staticmethod
    def _mtf_expansion_prob(candidate: StrategyCandidate) -> float:
        for marker in (
            "mtf_expansion_prob=",
            "mtf_expansion_prob ",
            "prearmed_expansion_prob=",
            "prearmed_expansion_prob ",
            "expansion_prob=",
            "expansion_prob ",
        ):
            value = RiskManager._extract_note_float(candidate, marker, 0.0)
            if value:
                return value
        return 0.0

    @staticmethod
    def _mtf_quality(candidate: StrategyCandidate) -> bool:
        if not RiskManager._has_mtf_override(candidate):
            return False

        pressure_score = RiskManager._mtf_pressure_score(candidate)
        expansion_prob = RiskManager._mtf_expansion_prob(candidate)
        participation_score = RiskManager._extract_note_float(candidate, "participation_score=", 0.0)
        if participation_score == 0.0:
            participation_score = RiskManager._extract_note_float(candidate, "participation_score ", 0.0)

        return (
            pressure_score >= 45.0
            and expansion_prob >= 65.0
            and participation_score >= 1.0
        )

    @staticmethod
    def _emit_near_risk_blocked(candidate: StrategyCandidate, score: StrategyScore, reasons: list[str]) -> None:
        if score.total < 68.0:
            return

        reason_text = " | ".join(str(reason) for reason in reasons[-8:]) if reasons else "no_reasons"
        logger.info(
            "NEAR_RISK_BLOCKED | %s | strategy=%s | direction=%s | score=%.1f | mtf_override=%s | mtf_quality=%s | mtf_pressure_score=%.2f | mtf_expansion_prob=%.1f | reasons=%s",
            candidate.symbol,
            candidate.strategy,
            candidate.direction,
            float(score.total),
            RiskManager._has_mtf_override(candidate),
            RiskManager._mtf_quality(candidate),
            RiskManager._mtf_pressure_score(candidate),
            RiskManager._mtf_expansion_prob(candidate),
            reason_text,
        )

    def _execution_cost_gate(self, candidate: StrategyCandidate) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        note_text = self._note_text(candidate)
        symbol = (candidate.symbol or "").upper()

        spread_bps = self._extract_note_float(candidate, "spread ", 0.0)
        entry_quality_long = self._extract_note_float(candidate, "entry_quality long=", 100.0)
        entry_quality_short = self._extract_note_float(candidate, "short=", 100.0)
        close_pos = self._extract_note_float(candidate, "close_pos=", 0.5)
        mtf_quality = self._mtf_quality(candidate)

        direction = (candidate.direction or "").upper()
        entry_quality = entry_quality_long if direction == "LONG" else entry_quality_short

        major_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        hard_spread_limit = 7.5 if symbol in major_symbols else 5.0
        soft_spread_limit = 5.0 if symbol in major_symbols else 3.5

        if spread_bps >= hard_spread_limit:
            reasons.append(
                f"execution-cost blocked: spread too wide ({spread_bps:.2f}bps >= {hard_spread_limit:.2f}bps)"
            )

        if spread_bps >= soft_spread_limit and entry_quality < (72 if mtf_quality else 80):
            reasons.append(
                f"execution-cost blocked: spread {spread_bps:.2f}bps with weak entry quality {entry_quality:.1f}"
            )

        if direction == "LONG" and close_pos >= (0.94 if mtf_quality else 0.90):
            reasons.append(f"execution-cost blocked: long entry too high in candle (close_pos={close_pos:.3f})")

        if direction == "SHORT" and close_pos <= (0.06 if mtf_quality else 0.10):
            reasons.append(f"execution-cost blocked: short entry too low in candle (close_pos={close_pos:.3f})")

        if "vertical extension risk" in note_text:
            reasons.append("execution-cost blocked: vertical extension risk")

        if "reclaim timing extended" in note_text and entry_quality < 80:
            reasons.append(
                f"execution-cost blocked: extended reclaim with weak entry quality {entry_quality:.1f}"
            )

        return not reasons, reasons

    def _momentum_quality_gate(self, candidate: StrategyCandidate) -> tuple[bool, list[str], bool]:
        """Returns (allowed, reasons, probe).

        Volume slightly below the requirement (>= 75% of it) trades at probe
        size instead of being hard-blocked: 74 push-candidates in one day died
        solely on this gate while the market kept running. Extension/lateness
        blocks stay hard — chasing a vertical move is not a size question.
        """
        reasons: list[str] = []
        strategy_name = (candidate.strategy or "").lower()
        if "momentum" not in strategy_name and "breakout" not in strategy_name and "breakdown" not in strategy_name:
            return True, reasons, False

        if strategy_name == "adaptive_momentum_continuation":
            reasons.append("momentum-quality watch: adaptive fallback skips legacy breakout/breakdown age gate")
            return True, reasons, False

        note_text = self._note_text(candidate)
        symbol = (candidate.symbol or "").upper()
        major_symbols = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
        mtf_quality = self._mtf_quality(candidate)

        direction = (candidate.direction or "").upper()
        if direction == "SHORT" or "breakdown" in strategy_name:
            move_pct = self._extract_note_float(candidate, "breakdown_pct=", 0.0)
            bars_since_move = int(self._extract_note_float(candidate, "bars_since_breakdown=", 999.0))
            move_label = "breakdown"
        else:
            move_pct = self._extract_note_float(candidate, "breakout_pct=", 0.0)
            bars_since_move = int(self._extract_note_float(candidate, "bars_since_breakout=", 999.0))
            move_label = "breakout"

        volume_ratio = self._extract_note_float(candidate, "volume_ratio=", 0.0)

        is_prearmed = "prearmed_breakout" in note_text or "prearmed_breakdown" in note_text
        min_volume_ratio = 1.20 if mtf_quality else (1.60 if is_prearmed else (2.50 if symbol in major_symbols else 3.00))
        max_clean_breakout_pct = 0.70 if mtf_quality else (0.55 if symbol in major_symbols else 0.45)
        hard_extension_pct = 1.05 if mtf_quality else (0.85 if symbol in major_symbols else 0.70)

        momentum_probe = False
        if volume_ratio and volume_ratio < min_volume_ratio:
            if volume_ratio >= min_volume_ratio * 0.75:
                momentum_probe = True
                reasons.append(
                    f"momentum-quality PROBE: volume below requirement, reduced size ({volume_ratio:.2f} < {min_volume_ratio:.2f})"
                )
            else:
                reasons.append(
                    f"momentum-quality blocked: volume ratio too weak ({volume_ratio:.2f} < {min_volume_ratio:.2f})"
                )

        if move_pct >= hard_extension_pct:
            reasons.append(
                f"momentum-quality blocked: {move_label} too extended ({move_pct:.2f}% >= {hard_extension_pct:.2f}%)"
            )

        if move_pct >= max_clean_breakout_pct and volume_ratio < 4.50:
            reasons.append(
                f"momentum-quality blocked: extended {move_label} without exceptional volume ({move_label}={move_pct:.2f}%, volume={volume_ratio:.2f})"
            )

        if bars_since_move > 1 and volume_ratio < 4.00:
            reasons.append(
                f"momentum-quality blocked: late {move_label} entry without strong follow-through (bars={bars_since_move}, volume={volume_ratio:.2f})"
            )

        if "weak_continuation_candle" in note_text:
            reasons.append("momentum-quality blocked: weak continuation candle")

        expansion_exhaustion_score = self._extract_note_float(
            candidate,
            "expansion_exhaustion_score=",
            0.0,
        )

        if "move already expanded" in note_text or expansion_exhaustion_score >= 85.0:
            is_coil = "entry_model=pre_breakout_coil" in note_text
            if is_coil:
                # Forward-return studie 2026-07-07 (12 symbolen, 331 entries):
                # een verse coil NA een geexpandeerde move was de enige
                # netto-positieve bucket (+0.198R, 61.5% TP1, n=26) — dat is
                # het "push meeliften"-setup. Chases na expansie blijven hard
                # geblokkeerd (25.5% TP1, 48.9% timeout). n is klein, dus
                # coils draaien op probe-size tot de leerloop ze bewijst.
                momentum_probe = True
                reasons.append(
                    f"momentum-quality PROBE: coil after expansion, reduced size (exhaustion={expansion_exhaustion_score:.2f})"
                )
            else:
                reasons.append(
                    f"momentum-quality blocked: exhaustion/expanded move warning (exhaustion={expansion_exhaustion_score:.2f})"
                )

        hard_blocks = [r for r in reasons if "momentum-quality blocked" in r]
        return not hard_blocks, reasons, momentum_probe and not hard_blocks

    def evaluate(self, candidate: StrategyCandidate, score: StrategyScore) -> RiskVerdict:
        reasons: list[str] = []

        note_text = self._note_text(candidate)
        leverage = min(self.settings.default_leverage, self.settings.max_leverage, self.SAFE_ALPHA_MAX_LEVERAGE)
        account_risk_pct = min(self.settings.account_risk_per_trade_pct, self.SAFE_ALPHA_MAX_RISK_PCT)

        if "orderbook_risk_off=true" in note_text or "orderbook_available=false" in note_text:
            reasons.append("blocked: orderbook risk-off")
            return RiskVerdict(
                allowed=False,
                status="BLOCKED",
                reasons=reasons,
                account_risk_pct=account_risk_pct,
                leverage=leverage,
                max_open_positions=self.settings.max_open_positions,
            )
        allowed = True

        # Autonomous optimization is advisory-only. Execution gates below remain deterministic.
        if score.verdict != "GO":
            allowed = False
            reasons.append(f"score verdict {score.verdict} blocks execution")

        if self.settings.account_risk_per_trade_pct <= 0:
            allowed = False
            reasons.append("account risk per trade must be > 0")

        if self.settings.account_risk_per_trade_pct > self.SAFE_ALPHA_MAX_RISK_PCT:
            allowed = False
            reasons.append(
                f"risk per trade too high for Safe Alpha: {self.settings.account_risk_per_trade_pct}% > {self.SAFE_ALPHA_MAX_RISK_PCT}%"
            )

        if leverage <= 0:
            allowed = False
            reasons.append("leverage must be > 0")

        if self.settings.max_open_positions < 1:
            allowed = False
            reasons.append("max_open_positions must be at least 1")

        strategy_name = (candidate.strategy or "").lower()
        is_sweep = "sweep" in strategy_name
        is_momentum = "momentum" in strategy_name or "breakout" in strategy_name or "breakdown" in strategy_name
        is_continuation = "continuation" in strategy_name
        is_low_vol_reclaim = "low_vol_reclaim" in strategy_name
        mtf_quality = self._mtf_quality(candidate)
        is_adaptive_fallback = strategy_name == "adaptive_momentum_continuation"

        if not is_sweep and not is_momentum and not is_continuation and not is_low_vol_reclaim:
            allowed = False
            reasons.append(f"Safe Mode blocks unsupported strategy: {candidate.strategy}")

        # Same explicit allow-list rule as execution_service's hybrid gate,
        # so risk and execution can never disagree about supported strategies.
        enabled_set = self.settings.enabled_strategy_set
        if enabled_set and not any(name in strategy_name for name in enabled_set):
            allowed = False
            reasons.append(f"strategy not in ENABLED_STRATEGIES allow-list: {candidate.strategy}")

        probe_mode = False
        if self._is_backtest_candidate(candidate):
            reasons.append("backtest mode: adaptive kill-switch/strategy-weighting disabled")
        else:
            kill_allowed, kill_reasons = self._kill_switch_gate(candidate)
            reasons.extend(kill_reasons)
            if not kill_allowed:
                allowed = False

            strategy_weight_allowed, strategy_weight_reasons, strategy_probe = self._strategy_weighting_gate(candidate)
            reasons.extend(strategy_weight_reasons)
            if not strategy_weight_allowed:
                allowed = False
            probe_mode = probe_mode or strategy_probe

            ai_agent_allowed, ai_agent_reasons, ai_agent_probe = self._ai_agent_gate(candidate)
            reasons.extend(ai_agent_reasons)
            if not ai_agent_allowed:
                allowed = False
            probe_mode = probe_mode or ai_agent_probe

            self._log_allocation_transition(
                candidate.strategy,
                "PROBE" if probe_mode else ("FULL" if (kill_allowed and strategy_weight_allowed and ai_agent_allowed) else "BLOCKED"),
            )

        cluster_allowed, cluster_reasons = self._cluster_risk_gate(candidate)
        reasons.extend(cluster_reasons)
        if not cluster_allowed:
            allowed = False

        execution_cost_allowed, execution_cost_reasons = self._execution_cost_gate(candidate)
        reasons.extend(execution_cost_reasons)
        if not execution_cost_allowed:
            allowed = False

        momentum_quality_allowed, momentum_quality_reasons, momentum_quality_probe = self._momentum_quality_gate(candidate)
        reasons.extend(momentum_quality_reasons)
        if not momentum_quality_allowed:
            allowed = False
        probe_mode = probe_mode or momentum_quality_probe

        if is_adaptive_fallback:
            required_score = 74
        elif is_momentum:
            required_score = 74 if mtf_quality else self.SAFE_MOMENTUM_MIN_SCORE
        elif is_continuation:
            required_score = 74 if mtf_quality else self.SAFE_CONTINUATION_MIN_SCORE
        elif is_low_vol_reclaim:
            required_score = 68 if mtf_quality else max(self.SAFE_ALPHA_MIN_SCORE, 72)
        else:
            required_score = self.SAFE_ALPHA_MIN_SCORE
        if score.total < required_score:
            allowed = False
            reasons.append(f"score below Safe Mode minimum: {score.total:.1f} < {required_score}")

        if is_continuation:
            volume_ratio = float(candidate.market.primary.volume_ratio_20 or 0.0)
            required_continuation_volume = 0.60 if mtf_quality else 0.80
            if volume_ratio < required_continuation_volume:
                allowed = False
                reasons.append(
                    f"continuation blocked: weak volume confirmation ({volume_ratio:.2f} < {required_continuation_volume:.2f})"
                )

        if candidate.direction == "LONG" and candidate.market.alignment == "aligned_bearish":
            allowed = False
            reasons.append("HTF alignment opposes long setup")
        if candidate.direction == "SHORT" and candidate.market.alignment == "aligned_bullish":
            allowed = False
            reasons.append("HTF alignment opposes short setup")

        # 1D/4H regime-laag (timeframe-uitbreiding 2026-07-07): beide HTF's
        # tegen de richting → hard block ("nooit tegen de dagtrend in");
        # één van beide tegen → probe-size. Geen data → neutraal, geen block.
        opposition_count = 0
        opposition_hits: list[str] = []
        opposing_regime = "bearish" if candidate.direction == "LONG" else "bullish"
        if f"htf_regime_1d={opposing_regime}" in note_text:
            opposition_count += 1
            opposition_hits.append(f"1D={opposing_regime}")
        if f"htf_regime_4h={opposing_regime}" in note_text:
            opposition_count += 1
            opposition_hits.append(f"4H={opposing_regime}")
        if opposition_count >= 2:
            allowed = False
            reasons.append(
                f"HTF regime blocks {candidate.direction}: {', '.join(opposition_hits)} (1D+4H beide tegen)"
            )
        elif opposition_count == 1:
            probe_mode = True
            reasons.append(
                f"HTF regime PROBE: {', '.join(opposition_hits)} tegen {candidate.direction}, halve size"
            )

        # Reclaim = mean-reversion; verdient alleen edge MET trend-consensus.
        # 90d-validatie (10.814 reclaim-setups): met 1D+4H consensus +0,071R,
        # geen consensus -0,15R (56% van het volume), tegen -0,35R. De
        # ochtend-audit 2026-07-08 bevestigde dit live: 20 chop-reclaims
        # verdunden de winst van 32 in-regime trades naar break-even. Zonder
        # volledige consensus in de richting: probe-size.
        if is_low_vol_reclaim and opposition_count == 0:
            full_consensus = (
                (candidate.direction == "LONG"
                 and "htf_regime_1d=bullish" in note_text
                 and "htf_regime_4h=bullish" in note_text)
                or (candidate.direction == "SHORT"
                    and "htf_regime_1d=bearish" in note_text
                    and "htf_regime_4h=bearish" in note_text)
            )
            if not full_consensus:
                probe_mode = True
                reasons.append(
                    "reclaim PROBE: geen volledige HTF-consensus (mean-reversion zonder trendrug), halve size"
                )

        if is_sweep:
            bars_since_sweep = getattr(candidate.detection, "bars_since_sweep", 999)
            if bars_since_sweep > self.SAFE_ALPHA_MAX_BARS_SINCE_SWEEP:
                allowed = False
                reasons.append(
                    f"sweep is too old for Safe Alpha: {bars_since_sweep} bars"
                )

        alignment_allowed, alignment_reasons = self._alignment_gate(candidate, score)
        reasons.extend(alignment_reasons)
        if not alignment_allowed:
            allowed = False

        if not allowed:
            self._emit_near_risk_blocked(candidate, score, reasons)

        status = "EXECUTABLE" if allowed else "BLOCKED"
        if allowed:
            reasons.append("risk gate passed")
        elif not reasons:
            reasons.append("risk checks failed")
        account_risk_pct = min(self.settings.account_risk_per_trade_pct, self.SAFE_ALPHA_MAX_RISK_PCT)
        if probe_mode and allowed:
            account_risk_pct = round(account_risk_pct * self.PROBE_RISK_MULTIPLIER, 4)
            reasons.append(
                f"probe mode: risk reduced to {account_risk_pct:.2f}% until strategy re-qualifies on fresh data"
            )
        if allowed and not self._is_backtest_candidate(candidate):
            session_multiplier, session_reason = self._session_risk_multiplier()
            if session_multiplier < 1.0:
                account_risk_pct = round(account_risk_pct * session_multiplier, 4)
                reasons.append(session_reason)
        return RiskVerdict(
            allowed=allowed,
            status=status,
            reasons=reasons,
            account_risk_pct=account_risk_pct,
            leverage=leverage,
            max_open_positions=self.settings.max_open_positions,
        )
