from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import Settings
from app.equity import resolve_account_equity
from clients.schemas import RiskVerdict, StrategyCandidate, StrategyScore, TradePlan
from execution.adaptive_tp_engine import AdaptiveTPContext, AdaptiveTPEngine


logger = logging.getLogger("trade_planner")

STRATEGY_EXPECTANCY_PATH = Path(__file__).resolve().parents[1] / "reports" / "backtests" / "strategy_expectancy.json"


class TradePlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.min_live_notional_usdt = float(getattr(settings, "planner_min_live_notional_usdt", 10.0))
        self.adaptive_tp_engine = AdaptiveTPEngine()
        self._expectancy_cache: dict = {}
        self._expectancy_mtime: float = -1.0

    def _strategy_learning_stats(self, strategy: str) -> dict:
        """Per-strategy stats from the daily expectancy report, cached by mtime.

        Feeds real TP1/TP3 hit rates into the adaptive TP engine so it can adjust
        targets from live results instead of the hardcoded zeros it used to get.
        """
        try:
            mtime = STRATEGY_EXPECTANCY_PATH.stat().st_mtime
        except OSError:
            return {}

        if mtime != self._expectancy_mtime:
            try:
                with STRATEGY_EXPECTANCY_PATH.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                self._expectancy_cache = payload.get("strategies") or {} if isinstance(payload, dict) else {}
            except Exception:
                self._expectancy_cache = {}
            self._expectancy_mtime = mtime

        stats = self._expectancy_cache.get(str(strategy or "").lower())
        return stats if isinstance(stats, dict) else {}

    @staticmethod
    def _note_text(candidate: StrategyCandidate) -> str:
        candidate_notes = [str(note).lower() for note in (candidate.notes or [])]
        market_notes = [str(note).lower() for note in (getattr(candidate.market, "notes", []) or [])]
        detection_flags = [str(flag).lower() for flag in (getattr(candidate.detection, "reason_flags", []) or [])]
        return " ".join(candidate_notes + market_notes + detection_flags)

    @staticmethod
    def _extract_note_float(candidate: StrategyCandidate, marker: str, default: float = 0.0) -> float:
        import re

        note_text = TradePlanner._note_text(candidate)
        marker = marker.lower()

        if marker not in note_text:
            return default

        try:
            pattern = re.escape(marker) + r"\s*([+-]?\d+(?:\.\d+)?)"
            match = re.search(pattern, note_text)
            if match:
                return float(match.group(1))
        except Exception:
            pass

        try:
            section = note_text.split(marker, 1)[1]
            raw = section.split()[0].strip(";|,").replace("bps", "")
            return float(raw)
        except Exception:
            return default

    @staticmethod
    def _extract_note_bool(candidate: StrategyCandidate, marker: str, default: bool = False) -> bool:
        note_text = TradePlanner._note_text(candidate)
        marker = marker.lower()
        if marker not in note_text:
            return default
        try:
            raw = note_text.split(marker, 1)[1].split()[0].strip(";|,").lower()
        except Exception:
            return default
        return raw in {"true", "1", "yes", "y"}

    @staticmethod
    def _extract_note_float_any(candidate: StrategyCandidate, markers: list[str], default: float = 0.0) -> float:
        for marker in markers:
            value = TradePlanner._extract_note_float(candidate, marker, default)
            if value != default:
                return value
        return default

    @staticmethod
    def _extract_entry_quality(candidate: StrategyCandidate) -> float:
        note_text = TradePlanner._note_text(candidate)
        direction = str(candidate.direction or "").upper()

        if "entry_quality" not in note_text:
            return 0.0

        # Gate: allow direct entry_quality_long/short= if present and > 0
        direct_long = TradePlanner._extract_note_float(candidate, "entry_quality_long=", 0.0)
        direct_short = TradePlanner._extract_note_float(candidate, "entry_quality_short=", 0.0)
        direction = str(candidate.direction or "").upper()
        if direction == "LONG" and direct_long > 0.0:
            return direct_long
        if direction == "SHORT" and direct_short > 0.0:
            return direct_short

        try:
            section = note_text.split("entry_quality", 1)[1]
            parts = section.replace("=", " = ").split()
            values: dict[str, float] = {}

            for idx, part in enumerate(parts):
                key = part.strip().lower().strip(";|,")
                if key not in {"long", "short"}:
                    continue

                raw_value = None
                if idx + 2 < len(parts) and parts[idx + 1] == "=":
                    raw_value = parts[idx + 2]
                elif idx + 1 < len(parts):
                    raw_value = parts[idx + 1]

                if raw_value is None:
                    continue

                try:
                    values[key] = float(str(raw_value).strip(";|,"))
                except ValueError:
                    continue

            if direction == "LONG":
                return float(values.get("long", 0.0))
            if direction == "SHORT":
                return float(values.get("short", 0.0))
        except Exception:
            return 0.0

        return 0.0

    @staticmethod
    def _normalize_plan_strategy(candidate: StrategyCandidate) -> str:
        raw_strategy = str(candidate.strategy or "").strip()
        if raw_strategy and raw_strategy.lower() not in {"unknown", "none", "null", "na", "n/a"}:
            return raw_strategy

        note_text = TradePlanner._note_text(candidate)
        if (
            "low_vol_reclaim" in note_text
            or "low vol reclaim" in note_text
            or "fallback_candidate_bridge=true" in note_text
            or "reclaim_unlock_v" in note_text
        ):
            return "low_vol_reclaim"
        if "prearmed_breakdown" in note_text or "momentum_breakdown" in note_text:
            return "momentum_breakdown"
        if "prearmed_breakout" in note_text or "momentum_breakout" in note_text:
            return "momentum_breakout"
        if "sweep" in note_text:
            return "liquidity_sweep_reversal"
        if "continuation" in note_text:
            return "trend_continuation"

        return "planner_recovered_strategy"

    @staticmethod
    def _master_entry_quality_gate(
        candidate: StrategyCandidate,
        score_total: float,
        entry_quality: float,
        pressure_score: float,
        expansion_prob: float,
        participation_score: float,
        followthrough_volume_ratio: float,
        volume_ratio: float,
    ) -> tuple[bool, list[str]]:
        """Final planner-level quality gate before a candidate can become executable.

        This centralizes the context checks that were previously spread across selector,
        scorer, and strategy-specific notes. It is intentionally conservative: weak
        candidates may still be logged/observed, but they must not reach execution.
        """
        strategy = str(candidate.strategy or "").lower()
        reasons: list[str] = []

        if entry_quality > 0.0 and entry_quality < 75.0:
            reasons.append(f"entry_quality={entry_quality:.2f}<75")

        if pressure_score < 40.0 and expansion_prob < 65.0:
            reasons.append(f"weak_pressure_expansion={pressure_score:.2f}/{expansion_prob:.2f}")

        if participation_score > 0.0 and participation_score < 0.70:
            reasons.append(f"participation={participation_score:.2f}<0.70")

        if followthrough_volume_ratio > 0.0 and followthrough_volume_ratio < 0.10:
            reasons.append(f"followthrough={followthrough_volume_ratio:.2f}<0.10")

        if volume_ratio > 0.0 and volume_ratio < 0.60:
            reasons.append(f"volume_ratio={volume_ratio:.2f}<0.60")

        if "continuation" in strategy:
            if not TradePlanner._extract_note_bool(candidate, "continuation_quality=", False):
                reasons.append("continuation_quality=false")
            if not TradePlanner._extract_note_bool(candidate, "structure_ok=", False):
                reasons.append("continuation_structure_ok=false")
            if not TradePlanner._extract_note_bool(candidate, "pressure_ok=", False):
                reasons.append("continuation_pressure_ok=false")
            if not TradePlanner._extract_note_bool(candidate, "participation_ok=", False):
                reasons.append("continuation_participation_ok=false")
            if pressure_score < 50.0:
                reasons.append(f"continuation_pressure={pressure_score:.2f}<50")
            if participation_score > 0.0 and participation_score < 1.00:
                reasons.append(f"continuation_participation={participation_score:.2f}<1.00")

        if "momentum" in strategy or "breakout" in strategy or "breakdown" in strategy:
            if expansion_prob < 70.0:
                reasons.append(f"momentum_expansion={expansion_prob:.2f}<70")
            if pressure_score < 45.0:
                reasons.append(f"momentum_pressure={pressure_score:.2f}<45")

        if "low_vol_reclaim" in strategy:
            if score_total < 72.0:
                reasons.append(f"low_vol_score={score_total:.2f}<72")
            if participation_score > 0.0 and participation_score < 0.75:
                reasons.append(f"low_vol_participation={participation_score:.2f}<0.75")
            if followthrough_volume_ratio > 0.0 and followthrough_volume_ratio < 0.10:
                reasons.append(f"low_vol_followthrough={followthrough_volume_ratio:.2f}<0.10")

        return len(reasons) == 0, reasons

    @staticmethod
    def _target_move_bps(entry: float, target: float) -> float:
        if entry <= 0:
            return 0.0
        return abs(target - entry) / entry * 10_000

    @staticmethod
    def _emit_near_executable(
        candidate: StrategyCandidate,
        score: StrategyScore,
        risk: RiskVerdict,
        verdict: str,
        reasons: list[str],
        rr: float,
        rr_to_tp1: float,
        tp1_move_bps: float,
        minimum_tp1_move_bps: float,
        position_notional: float,
        min_live_notional: float,
        strong_continuation_quality: bool,
    ) -> None:
        if verdict != "BLOCKED":
            return

        near_score = score.total >= 68.0
        near_risk = bool(getattr(risk, "allowed", False)) or str(getattr(risk, "status", "")).upper() == "GO"
        near_rr = rr >= 0.85 or rr_to_tp1 >= 0.70
        near_edge = tp1_move_bps >= (minimum_tp1_move_bps * 0.75) if minimum_tp1_move_bps > 0 else False
        near_size = position_notional >= (min_live_notional * 0.75)

        if not (near_score and (near_risk or near_rr or near_edge or strong_continuation_quality)):
            return

        reason_text = " | ".join(str(reason) for reason in reasons[-6:]) if reasons else "no_reasons"
        missing: list[str] = []

        if not near_risk:
            missing.append("risk_gate")
        if rr < 1.3:
            missing.append("rr_to_tp2")
        if rr_to_tp1 < 1.30:
            missing.append("rr_to_tp1")
        if not near_edge:
            missing.append("tp1_net_edge")
        if not near_size:
            missing.append("position_notional")

        logger.info(
            "NEAR_EXECUTABLE | %s | strategy=%s | direction=%s | score=%.1f | risk_status=%s | risk_allowed=%s | rr=%.2f | rr_to_tp1=%.2f | tp1_move_bps=%.2f | minimum_tp1_move_bps=%.2f | notional=%.2f | min_notional=%.2f | strong_continuation=%s | missing=%s | reasons=%s",
            candidate.symbol,
            candidate.strategy,
            candidate.direction,
            float(score.total),
            getattr(risk, "status", "UNKNOWN"),
            getattr(risk, "allowed", False),
            rr,
            rr_to_tp1,
            tp1_move_bps,
            minimum_tp1_move_bps,
            position_notional,
            min_live_notional,
            strong_continuation_quality,
            ",".join(missing) if missing else "unknown",
            reason_text,
        )

    def build(self, candidate: StrategyCandidate, score: StrategyScore, risk: RiskVerdict) -> TradePlan:
        entry = candidate.detection.entry_hint
        stop = self._build_stop(candidate)
        entries = self._build_entries(candidate, entry)
        risk_per_unit = abs(entry - stop)
        strategy_name_preview = str(self._normalize_plan_strategy(candidate) or "").lower()
        if strategy_name_preview == "adaptive_momentum_continuation":
            logger.warning(
                "ADAPTIVE_CONTINUATION_OBSERVE_ONLY | %s | strategy=%s",
                candidate.symbol,
                strategy_name_preview,
            )

        adaptive_tp = self._build_adaptive_tp(candidate, entry, stop)

        # Low-vol reclaim runs in single-TP mode at 1.30R so the built TP1 actually
        # satisfies the rr_to_tp1 >= 1.30 execution gate. At 1.00R the roundtrip fees
        # (~12bps) turned every win into ~0.7R net and every loss into ~1.3R net,
        # which is negative expectancy at any win rate below ~62%.
        if "low_vol_reclaim" in strategy_name_preview:
            adaptive_tp.tp1_rr = 1.30
            adaptive_tp.tp2_rr = min(adaptive_tp.tp2_rr, 1.30)
            adaptive_tp.tp3_rr = min(adaptive_tp.tp3_rr, 1.50)
        tp1 = self._tp_from_r(candidate.direction, entry, risk_per_unit, adaptive_tp.tp1_rr)
        tp2 = self._tp_from_r(candidate.direction, entry, risk_per_unit, adaptive_tp.tp2_rr)
        tp3 = self._tp_from_r(candidate.direction, entry, risk_per_unit, adaptive_tp.tp3_rr)
        position_notional, sizing_notes = self._position_notional(entry, stop, risk.account_risk_pct, risk.leverage)
        rr = self._risk_reward(entry, stop, tp2, candidate.direction)
        rr_to_tp1 = self._risk_reward(entry, stop, tp1, candidate.direction)
        tp1_move_bps = self._target_move_bps(entry, tp1)
        stop_move_bps = self._target_move_bps(entry, stop)
        atr_pct = float(getattr(candidate.market.primary, "atr_percent", 0.0) or 0.0)
        # Market fetcher writes "spread_bps=X"; older note formats used "spread X bps".
        # Parsing only the old format made the planner price the edge with spread=0.
        spread_bps = self._extract_note_float_any(candidate, ["spread_bps=", "spread "], 0.0)
        estimated_roundtrip_fee_bps = float(getattr(self.settings, "planner_estimated_roundtrip_fee_bps", 12.0))
        minimum_net_edge_buffer_bps = float(getattr(self.settings, "planner_minimum_net_edge_buffer_bps", 4.0))
        minimum_tp1_move_bps = spread_bps + estimated_roundtrip_fee_bps + minimum_net_edge_buffer_bps
        note_text = self._note_text(candidate)
        plan_strategy = self._normalize_plan_strategy(candidate)
        strategy_name = str(plan_strategy or "").lower()
        # Adaptive continuation fallback candidates are now routed to observation mode (not remapped).
        if strategy_name == "adaptive_momentum_continuation":
            logger.warning(
                "ADAPTIVE_CONTINUATION_OBSERVE_ONLY_POST_GUARD | %s | strategy=%s",
                candidate.symbol,
                strategy_name,
            )

        if strategy_name == "adaptive_momentum_continuation":
            verdict = "BLOCKED"
            reasons = ["adaptive continuation observe-only mode"]
            notes = list(candidate.notes)
            notes.append("adaptive_continuation_observe_only=true")

            return TradePlan(
                symbol=candidate.symbol,
                strategy=plan_strategy,
                direction=candidate.direction,
                verdict=verdict,
                score=score.total,
                entry_prices=entries,
                stop_loss=stop,
                take_profits=[tp1],
                tp_size_pcts=[100.0],
                risk_reward_ratio=rr_to_tp1,
                account_risk_pct=risk.account_risk_pct,
                leverage=risk.leverage,
                position_notional_usdt=0.0,
                notes=notes,
                reasons=reasons,
            )
        is_prearmed_momentum = "prearmed_breakout" in note_text or "prearmed_breakdown" in note_text
        is_short_continuation = "trend_continuation" in strategy_name and str(candidate.direction or "").upper() == "SHORT"
        is_low_vol_reclaim = (
            "low_vol_reclaim" in strategy_name
            or "low vol reclaim" in strategy_name
            or "fallback_candidate_bridge=true" in note_text
            or "reclaim_unlock_v" in note_text
        )
        participation_score = self._extract_note_float_any(
            candidate,
            ["participation_score=", "participation_score ", "planner_participation_score="],
            0.0,
        )
        followthrough_volume_ratio = self._extract_note_float_any(
            candidate,
            ["followthrough_volume_ratio=", "followthrough_volume_ratio ", "planner_followthrough_volume_ratio="],
            0.0,
        )
        volume_ratio = self._extract_note_float_any(
            candidate,
            ["volume_ratio=", "volume_ratio ", "planner_volume_ratio="],
            0.0,
        )
        if volume_ratio <= 0.0:
            volume_ratio = float(getattr(candidate.market.primary, "volume_ratio_20", 0.0) or 0.0)

        entry_quality = self._extract_entry_quality(candidate)
        pressure_score = self._extract_note_float_any(
            candidate,
            ["pressure_score=", "pressure_score ", "mtf_pressure_score=", "prearmed_pressure_score="],
            0.0,
        )
        expansion_prob = self._extract_note_float_any(
            candidate,
            ["expansion_prob=", "expansion_prob ", "mtf_expansion_prob=", "prearmed_expansion_prob="],
            0.0,
        )
        master_gate_passed, master_gate_reasons = self._master_entry_quality_gate(
            candidate=candidate,
            score_total=float(score.total),
            entry_quality=entry_quality,
            pressure_score=pressure_score,
            expansion_prob=expansion_prob,
            participation_score=participation_score,
            followthrough_volume_ratio=followthrough_volume_ratio,
            volume_ratio=volume_ratio,
        )
        strong_continuation_quality = (
            "trend_continuation" in str(candidate.strategy or "").lower()
            and score.total >= 82
            and participation_score >= 1.5
            and followthrough_volume_ratio >= 1.0
            and volume_ratio >= 1.0
            and "extended" not in note_text
            and "vertical extension" not in note_text
        )
        if strong_continuation_quality:
            minimum_tp1_move_bps *= 0.75

        if is_prearmed_momentum:
            minimum_tp1_move_bps *= 0.90
            notes_profile_hint = "prearmed_momentum"
        elif is_short_continuation:
            minimum_tp1_move_bps *= 0.85
            notes_profile_hint = "short_continuation"
        elif is_low_vol_reclaim:
            # Reclaim scalps live or die on net edge: the gross TP1 move must be a
            # comfortable multiple of total roundtrip costs, otherwise fees eat the
            # win side while inflating the loss side. Require >= 2.5x costs.
            minimum_tp1_move_bps = max(
                minimum_tp1_move_bps,
                (spread_bps + estimated_roundtrip_fee_bps) * 2.5,
            )

            # Cap relative to the actual stop so a 1.30R target stays reachable:
            # with stops capped at 30-85bps this allows 45-130bps targets while
            # still blocking multi-percent swings.
            reclaim_tp1_cap_bps = max(45.0, min(130.0, stop_move_bps * 1.45))

            notes_profile_hint = "low_vol_reclaim_controlled_unlock"
        else:
            notes_profile_hint = "default"

        notes = list(candidate.notes)

        if not is_low_vol_reclaim:
            reclaim_tp1_cap_bps = 999999.0

        notes.append(f"reclaim_tp1_cap_bps={reclaim_tp1_cap_bps:.2f}")
        notes.append(
            f"adaptive_tp_rr=tp1:{adaptive_tp.tp1_rr:.2f}|tp2:{adaptive_tp.tp2_rr:.2f}|tp3:{adaptive_tp.tp3_rr:.2f}"
        )
        notes.append(
            f"adaptive_tp_size=tp1:{adaptive_tp.tp1_size_pct:.0f}|tp2:{adaptive_tp.tp2_size_pct:.0f}|tp3:{adaptive_tp.tp3_size_pct:.0f}"
        )
        notes.append(f"adaptive_tp_profile={notes_profile_hint}")
        notes.append(
            f"adaptive_tp_size_sum={adaptive_tp.tp1_size_pct + adaptive_tp.tp2_size_pct + adaptive_tp.tp3_size_pct:.2f}"
        )
        if adaptive_tp.reasoning:
            notes.append("adaptive_tp_reason=" + "; ".join(adaptive_tp.reasoning))
        notes.extend(sizing_notes)
        notes.append(f"position_notional_usdt={position_notional:.2f}")
        notes.append(f"max_loss_budget_usdt={resolve_account_equity(self.settings)[0] * (risk.account_risk_pct / 100):.2f}")
        notes.append(f"rr_to_tp2={rr:.2f}")
        notes.append(f"rr_to_tp1={rr_to_tp1:.2f}")
        notes.append(f"tp1_move_bps={tp1_move_bps:.2f}")
        notes.append(f"stop_move_bps={stop_move_bps:.2f}")
        if is_low_vol_reclaim:
            notes.append("low_vol_reclaim_tight_sl_tp_profile=true")
        notes.append(f"spread_bps_for_edge={spread_bps:.2f}")
        notes.append(f"estimated_roundtrip_fee_bps={estimated_roundtrip_fee_bps:.2f}")
        notes.append(f"minimum_tp1_move_bps={minimum_tp1_move_bps:.2f}")
        notes.append(f"strong_continuation_quality={strong_continuation_quality}")
        notes.append(f"planner_participation_score={participation_score:.2f}")
        notes.append(f"planner_followthrough_volume_ratio={followthrough_volume_ratio:.2f}")
        notes.append(f"planner_volume_ratio={volume_ratio:.2f}")
        notes.append(f"planner_entry_quality={entry_quality:.2f}")
        notes.append(f"planner_pressure_score={pressure_score:.2f}")
        notes.append(f"planner_expansion_prob={expansion_prob:.2f}")
        notes.append(f"master_entry_quality_passed={master_gate_passed}")
        if master_gate_reasons:
            notes.append("master_entry_quality_reasons=" + "|".join(master_gate_reasons))
        notes.append(f"planner_strategy_score={float(score.total):.2f}")
        notes.append(f"planner_alignment={candidate.market.alignment}")
        notes.append(f"planner_risk_status={getattr(risk, 'status', 'UNKNOWN')}")
        min_live_notional = self.min_live_notional_usdt
        a_plus_low_vol_reclaim = (
            is_low_vol_reclaim
            and score.total >= 85.0
            and entry_quality >= 80.0
            and rr_to_tp1 >= 1.00
            and spread_bps <= 12.0
            and risk.allowed
        )
        if a_plus_low_vol_reclaim:
            original_reclaim_tp1_cap_bps = reclaim_tp1_cap_bps
            reclaim_tp1_cap_bps = max(reclaim_tp1_cap_bps, 180.0)
            notes.append("a_plus_low_vol_reclaim_rr_override=true")
            notes.append(
                f"a_plus_low_vol_reclaim_cap_override={original_reclaim_tp1_cap_bps:.2f}->{reclaim_tp1_cap_bps:.2f}"
            )

        breakout_ready = (
            "breakout_ready=true" in note_text
            or "breakout_context_ready=true" in note_text
            or "breakout_context ready=true" in note_text
            or "breakdown_ready=true" in note_text
            or "entry_model=retest_zone_first" in note_text
        )
        low_vol_reclaim_day_defensive_block = False
        low_vol_reclaim_day_defensive_reasons: list[str] = []
        if is_low_vol_reclaim and not a_plus_low_vol_reclaim:
            if entry_quality < 75.0:
                low_vol_reclaim_day_defensive_block = True
                low_vol_reclaim_day_defensive_reasons.append(f"entry_quality={entry_quality:.2f}<75")
            if pressure_score < 45.0 and expansion_prob < 70.0:
                low_vol_reclaim_day_defensive_block = True
                low_vol_reclaim_day_defensive_reasons.append(
                    f"pressure_expansion_weak={pressure_score:.2f}/{expansion_prob:.2f}"
                )
            if not breakout_ready and score.total < 80.0:
                low_vol_reclaim_day_defensive_block = True
                low_vol_reclaim_day_defensive_reasons.append(f"no_breakout_context_score={score.total:.2f}<80")

            if low_vol_reclaim_day_defensive_block:
                notes.append("day_defensive_low_vol_reclaim_gate=true")
                notes.append("day_defensive_reasons=" + "|".join(low_vol_reclaim_day_defensive_reasons))

        if is_low_vol_reclaim:
            # Reclaim scalps are fast mean-reversion trades. Use TP1 RR as the execution gate,
            # not the global TP2-style planner_min_rr gate.
            verdict = "EXECUTABLE" if ((rr_to_tp1 >= 1.30 and score.total >= 64.0 and risk.allowed) or a_plus_low_vol_reclaim) else "BLOCKED"
            notes.append("planner_gate=low_vol_reclaim_fast_scalp_rr_to_tp1")
            if score.total >= 64.0 and risk.allowed:
                notes.append("low_vol_reclaim_score_gate=64")
        else:
            verdict = "EXECUTABLE" if rr >= self.settings.planner_min_rr and risk.allowed else "BLOCKED"
        if (
            verdict == "BLOCKED"
            and is_low_vol_reclaim
            and not low_vol_reclaim_day_defensive_block
            and score.total >= 72.0
            and risk.allowed
            and rr_to_tp1 >= 1.30
            and tp1_move_bps <= reclaim_tp1_cap_bps
            and str(getattr(risk, "status", "")).upper() in {"GO", "WATCH"}
        ):
            verdict = "EXECUTABLE"
            notes.append("planner_soft_bridge_activated=low_vol_reclaim_rr_guarded")

        if low_vol_reclaim_day_defensive_block:
            verdict = "BLOCKED"
            notes.append("blocked_reason=day_defensive_low_vol_reclaim_quality_gate")

        if verdict == "EXECUTABLE" and not master_gate_passed:
            verdict = "BLOCKED"
            notes.append("blocked_reason=master_entry_quality_gate")
            notes.append("master_entry_quality_gate_blocked=true")
            logger.warning(
                "MASTER_ENTRY_QUALITY_BLOCKED | %s | strategy=%s | direction=%s | reasons=%s",
                candidate.symbol,
                candidate.strategy,
                candidate.direction,
                "|".join(master_gate_reasons) if master_gate_reasons else "unknown",
            )

        if (
            verdict == "BLOCKED"
            and not low_vol_reclaim_day_defensive_block
            and score.total >= 64.0
            and risk.allowed
            and str(getattr(risk, "status", "")).upper() in {"GO", "WATCH"}
        ):
            notes.append("planner_soft_bridge_candidate=true")
        reasons = list(score.reasons) + list(risk.reasons)
        if low_vol_reclaim_day_defensive_block:
            reasons.append("DAY_DEFENSIVE_LOW_VOL_RECLAIM_BLOCK " + " | ".join(low_vol_reclaim_day_defensive_reasons))
        if strategy_name == "adaptive_momentum_continuation":
            if entry_quality < 75.0:
                verdict = "BLOCKED"
                reasons.append(f"ADAPTIVE_ENTRY_QUALITY {entry_quality:.2f} below hard minimum 75.00")
                notes.append("blocked_reason=adaptive_entry_quality_below_75")

            if pressure_score < 35.0 and expansion_prob < 65.0:
                verdict = "BLOCKED"
                reasons.append(
                    f"ADAPTIVE_PRESSURE_EXPANSION_WEAK pressure={pressure_score:.2f} expansion={expansion_prob:.2f}"
                )
                notes.append("blocked_reason=adaptive_weak_pressure_and_expansion")

            if participation_score < 1.0 and followthrough_volume_ratio < 0.50:
                verdict = "BLOCKED"
                reasons.append(
                    f"ADAPTIVE_PARTICIPATION_WEAK participation={participation_score:.2f} followthrough={followthrough_volume_ratio:.2f}"
                )
                notes.append("blocked_reason=adaptive_participation_weak")
        if not is_low_vol_reclaim and rr < self.settings.planner_min_rr:
            reasons.append(f"RR {rr:.2f} below planner minimum {self.settings.planner_min_rr:.2f}")
        elif is_low_vol_reclaim and rr_to_tp1 < 1.30 and not a_plus_low_vol_reclaim:
            reasons.append(f"LOW_VOL_RECLAIM_RR_TO_TP1 {rr_to_tp1:.2f} below fast scalp minimum 1.00")
        min_rr_to_tp1 = float(getattr(self.settings, "planner_min_rr_to_tp1", 1.00))

        if strategy_name == "adaptive_momentum_continuation":
            min_rr_to_tp1 = max(
                float(getattr(self.settings, "planner_adaptive_fallback_min_rr_to_tp1", 0.70)),
                0.70,
            )
            notes.append("adaptive_rr_to_tp1=adaptive_fallback_profile")

        elif strong_continuation_quality:
            min_rr_to_tp1 = max(float(getattr(self.settings, "planner_strong_continuation_min_rr_to_tp1", 0.75)), 0.75)
            notes.append("adaptive_rr_to_tp1=strong_continuation_quality")

        elif "low_vol_reclaim" in strategy_name:
            # Single-TP reclaim trades must clear fees/slippage and still keep positive expectancy.
            min_rr_to_tp1 = 1.30
            notes.append("controlled_unlock=low_vol_reclaim_min_rr_to_tp1_1_30")

        else:
            min_rr_to_tp1 = max(min_rr_to_tp1, 1.00)
        if rr_to_tp1 < min_rr_to_tp1:
            if strong_continuation_quality and rr_to_tp1 >= 0.70:
                notes.append("adaptive_rr_override=strong_continuation")
            elif a_plus_low_vol_reclaim:
                notes.append("adaptive_rr_override=a_plus_low_vol_reclaim_1_00")
            else:
                verdict = "BLOCKED"
                reasons.append(
                    f"RR_TO_TP1 {rr_to_tp1:.2f} below minimum {min_rr_to_tp1:.2f}"
                )
                notes.append("blocked_reason=weak_rr_to_tp1")

        # Largest Loss Guard
        # Prevent trades whose stop distance is abnormally large relative to
        # the expected target and recent planner profile.
        largest_loss_guard_bps = float(
            getattr(self.settings, "planner_largest_loss_guard_bps", 250.0)
        )

        if stop_move_bps > largest_loss_guard_bps:
            verdict = "BLOCKED"
            reasons.append(
                f"LARGEST_LOSS_GUARD stop_bps={stop_move_bps:.2f} limit={largest_loss_guard_bps:.2f}"
            )
            notes.append("blocked_reason=largest_loss_guard")

        # Institutional risk-shape guard.
        # In single-TP mode we allow a small practical buffer, but still block trades
        # where one SL can wipe out multiple TP wins.
        max_stop_to_tp_ratio = 1.20

        if stop_move_bps > (tp1_move_bps * max_stop_to_tp_ratio):
            verdict = "BLOCKED"
            reasons.append(
                f"POOR_RISK_SHAPE stop_bps={stop_move_bps:.2f} tp1_bps={tp1_move_bps:.2f} max_ratio={max_stop_to_tp_ratio:.2f}"
            )
            notes.append("blocked_reason=poor_risk_shape_stop_larger_than_target")
        if is_low_vol_reclaim and tp1_move_bps > reclaim_tp1_cap_bps:
            verdict = "BLOCKED"
            reasons.append(
                f"LOW_VOL_RECLAIM_TARGET_TOO_FAR {tp1_move_bps:.2f}bps > cap {reclaim_tp1_cap_bps:.2f}bps"
            )
            notes.append("blocked_reason=reclaim_target_too_far")

        # Reality Guard: block trades with unrealistic stop distance for low-vol reclaim
        if is_low_vol_reclaim and stop_move_bps > max(reclaim_tp1_cap_bps * 1.50, 220.0):
            verdict = "BLOCKED"
            reasons.append(
                f"LOW_VOL_RECLAIM_STOP_TOO_FAR {stop_move_bps:.2f}bps"
            )
            notes.append("blocked_reason=reclaim_stop_too_far")

        # Reality Guard: block poor risk shape for low-vol reclaim
        if (
            is_low_vol_reclaim
            and rr_to_tp1 < 0.90
            and stop_move_bps > (tp1_move_bps * 1.80)
            and not a_plus_low_vol_reclaim
        ):
            verdict = "BLOCKED"
            reasons.append(
                f"LOW_VOL_RECLAIM_POOR_RISK_SHAPE stop_bps={stop_move_bps:.2f} tp1_bps={tp1_move_bps:.2f}"
            )
            notes.append("blocked_reason=reclaim_poor_risk_shape")

        if a_plus_low_vol_reclaim and stop_move_bps > (tp1_move_bps * 2.10):
            verdict = "BLOCKED"
            reasons.append(
                f"A_PLUS_LOW_VOL_RECLAIM_RISK_SHAPE_TOO_WIDE stop_bps={stop_move_bps:.2f} tp1_bps={tp1_move_bps:.2f}"
            )
            notes.append("blocked_reason=a_plus_reclaim_risk_shape_too_wide")

        if tp1_move_bps < minimum_tp1_move_bps:
            if strong_continuation_quality and tp1_move_bps >= (minimum_tp1_move_bps * 0.85):
                notes.append("adaptive_tp1_edge_override=strong_continuation")
            else:
                verdict = "BLOCKED"
                reasons.append(
                    f"TP1_NET_EDGE {tp1_move_bps:.2f}bps below minimum {minimum_tp1_move_bps:.2f}bps after spread/fees buffer"
                )
                notes.append("blocked_reason=tp1_net_edge_too_small")
        if position_notional < min_live_notional:
            verdict = "BLOCKED"
            reasons.append(f"position notional {position_notional:.2f} below live minimum {min_live_notional:.2f}")
            notes.append(f"live_min_notional_usdt={min_live_notional:.2f}")

        self._emit_near_executable(
            candidate=candidate,
            score=score,
            risk=risk,
            verdict=verdict,
            reasons=reasons,
            rr=rr,
            rr_to_tp1=rr_to_tp1,
            tp1_move_bps=tp1_move_bps,
            minimum_tp1_move_bps=minimum_tp1_move_bps,
            position_notional=position_notional,
            min_live_notional=min_live_notional,
            strong_continuation_quality=strong_continuation_quality,
        )
        if is_low_vol_reclaim:
            notes.append(f"low_vol_reclaim_planner_final_rr_to_tp1={rr_to_tp1:.2f}")
            notes.append(f"low_vol_reclaim_planner_final_rr={rr:.2f}")
            notes.append(f"low_vol_reclaim_planner_final_verdict={verdict}")
        planner_reason_text = " | ".join(str(reason) for reason in reasons[-8:]) if reasons else "no_reasons"
        if "blocked_reason=largest_loss_guard" in notes:
            logger.warning(
                "LARGEST_LOSS_GUARD_BLOCKED | %s | strategy=%s | direction=%s | stop_bps=%.2f | tp1_bps=%.2f",
                candidate.symbol,
                plan_strategy,
                candidate.direction,
                stop_move_bps,
                tp1_move_bps,
            )
        if verdict == "BLOCKED":
            logger.warning(
                "PLAN_REJECT | %s | strategy=%s | direction=%s | score=%.1f | rr=%.2f | rr_to_tp1=%.2f | tp1_move_bps=%.2f | min_tp1_move_bps=%.2f | notional=%.2f | reasons=%s",
                candidate.symbol,
                plan_strategy,
                candidate.direction,
                float(score.total),
                rr,
                rr_to_tp1,
                tp1_move_bps,
                minimum_tp1_move_bps,
                position_notional,
                planner_reason_text,
            )
        else:
            logger.info(
                "PLAN_ACCEPTED | %s | strategy=%s | direction=%s | score=%.1f | rr=%.2f | rr_to_tp1=%.2f | tp1_move_bps=%.2f | notional=%.2f",
                candidate.symbol,
                plan_strategy,
                candidate.direction,
                float(score.total),
                rr,
                rr_to_tp1,
                tp1_move_bps,
                position_notional,
            )
        # Single-TP mode must use the REACHABLE tp1, not tp2. Live excursion
        # data (2026-07-07): median favorable move = 0.5-1.0R while tp2 sits at
        # 1.5-1.7R -> the target almost never filled and every reversal closed
        # red. tp1 (>=1.05R) is the fee-viable target the TP engine designs and
        # the profit-lock (60% of tp1) is calibrated against.
        if is_low_vol_reclaim:
            single_tp = tp1
            notes.append("single_tp_source=tp1_reclaim_profile")
        else:
            single_tp = tp1
            notes.append("single_tp_source=tp1_reachable_profile")

        notes.append("execution_profile=single_tp_full_close")
        notes.append(f"single_tp_target={single_tp:.8f}")

        return TradePlan(
            symbol=candidate.symbol,
            strategy=plan_strategy,
            direction=candidate.direction,
            verdict=verdict,
            score=score.total,
            entry_prices=entries,
            stop_loss=stop,
            take_profits=[single_tp],
            tp_size_pcts=[100.0],
            risk_reward_ratio=rr,
            account_risk_pct=risk.account_risk_pct,
            leverage=risk.leverage,
            position_notional_usdt=position_notional,
            notes=notes,
            reasons=reasons,
        )

    def _build_adaptive_tp(self, candidate: StrategyCandidate, entry: float, stop: float):
        market = getattr(candidate, "market", None)
        primary = getattr(market, "primary", None)

        raw_strategy = str(candidate.strategy or "trend_continuation").lower().strip()
        note_text = self._note_text(candidate)
        if "low_vol_reclaim" in raw_strategy or "low vol reclaim" in raw_strategy:
            strategy = "low_vol_reclaim"
        elif "momentum_breakdown" in raw_strategy or "prearmed_breakdown" in note_text:
            strategy = "momentum_breakdown"
        elif "momentum_breakout" in raw_strategy or "prearmed_breakout" in note_text:
            strategy = "momentum_breakout"
        elif "sweep" in raw_strategy:
            strategy = "liquidity_sweep_reversal"
        elif "continuation" in raw_strategy:
            strategy = "trend_continuation"
        else:
            strategy = "trend_continuation"

        volatility_rank = float(getattr(market, "volatility_rank", 0.0) or 0.0)
        volume_ratio = float(getattr(primary, "volume_ratio_20", 0.0) or 0.0)
        atr_pct = float(getattr(primary, "atr_percent", 0.0) or 0.0)

        alignment = str(getattr(market, "alignment", "") or "").lower()
        trend = str(getattr(primary, "trend", "") or "").lower()
        compression_quality = "compression_quality true" in note_text or "compression_quality=true" in note_text
        prearmed_momentum = "prearmed_breakout" in note_text or "prearmed_breakdown" in note_text

        if compression_quality:
            market_regime = "compression"
        elif prearmed_momentum:
            market_regime = "pre_expansion"
        elif "conflicted" in alignment or "mixed" in alignment:
            market_regime = "chop"
        elif volatility_rank >= 18:
            market_regime = "volatile"
        elif strategy in {"momentum_breakout", "momentum_breakdown"}:
            market_regime = "breakout"
        elif strategy == "trend_continuation":
            market_regime = "grind"
        elif trend in {"bullish", "bearish"}:
            market_regime = "trend"
        else:
            market_regime = "chop"

        learning_stats = self._strategy_learning_stats(strategy)
        # tp1_hit_rate can be null in the report ("missing_not_zero"); treat
        # missing as 0.0 here — the engine only *boosts* on high rates, so a
        # missing value simply means no adjustment, never a false block.
        tp1_hit_rate = float(learning_stats.get("tp1_hit_rate") or 0.0)
        tp3_hit_rate = float(learning_stats.get("tp3_hit_rate") or 0.0)
        missed_tp1_to_sl_rate = float(learning_stats.get("missed_tp1_to_sl_rate") or 0.0)

        ctx = AdaptiveTPContext(
            symbol=candidate.symbol,
            strategy=strategy,  # type: ignore[arg-type]
            direction=candidate.direction,
            entry=entry,
            stop_loss=stop,
            atr_pct=atr_pct,
            volatility_rank=volatility_rank,
            volume_ratio=volume_ratio,
            tp1_hit_rate=tp1_hit_rate,
            tp3_hit_rate=tp3_hit_rate,
            missed_tp1_to_sl_rate=missed_tp1_to_sl_rate,
            market_regime=market_regime,  # type: ignore[arg-type]
        )
        return self.adaptive_tp_engine.build(ctx)

    def _build_stop(self, candidate: StrategyCandidate) -> float:
        raw_stop = candidate.detection.invalidation
        entry = float(candidate.detection.entry_hint or 0.0)
        buffer = raw_stop * (self.settings.planner_stop_buffer_bps / 10_000)

        strategy_name = str(candidate.strategy or "").lower()
        note_text = self._note_text(candidate)

        is_low_vol_reclaim = (
            "low_vol_reclaim" in strategy_name
            or "low vol reclaim" in note_text
            or "fallback_candidate_bridge=true" in note_text
            or "reclaim_unlock_v" in note_text
        )

        if candidate.direction == "LONG":
            stop = raw_stop - buffer
        else:
            stop = raw_stop + buffer

        if not is_low_vol_reclaim or entry <= 0:
            return stop

        atr_pct = float(getattr(candidate.market.primary, "atr_percent", 0.0) or 0.0)

        if atr_pct <= 0:
            return stop

        # atr_pct is stored as percent points, e.g. 0.42 means 0.42%.
        # Convert to bps with *100, not *10000. This keeps SL and therefore 1R TP closer.
        max_stop_bps = min(max(atr_pct * 100.0 * 0.90, 30.0), 85.0)

        if candidate.direction == "LONG":
            max_stop_price = entry * (1.0 - (max_stop_bps / 10000.0))
            stop = max(stop, max_stop_price)
        else:
            max_stop_price = entry * (1.0 + (max_stop_bps / 10000.0))
            stop = min(stop, max_stop_price)

        return round(stop, 8)

    def _build_entries(self, candidate: StrategyCandidate, anchor: float) -> list[float]:
        reclaim = candidate.detection.reclaim_level
        extreme = candidate.detection.sweep_extreme
        steps = max(1, self.settings.planner_ladder_steps)
        if steps == 1:
            return [round(anchor, 8)]

        if candidate.direction == "LONG":
            top = max(anchor, reclaim)
            bottom = min(anchor, reclaim, extreme)
            span = max(top - bottom, anchor * 0.0008)
            return [round(top - (span * i / max(1, steps - 1)), 8) for i in range(steps)]

        top = max(anchor, reclaim, extreme)
        bottom = min(anchor, reclaim)
        span = max(top - bottom, anchor * 0.0008)
        return [round(bottom + (span * i / max(1, steps - 1)), 8) for i in range(steps)]

    @staticmethod
    def _tp_from_r(direction: str, entry: float, risk_per_unit: float, r_multiple: float) -> float:
        if direction == "LONG":
            return round(entry + (risk_per_unit * r_multiple), 8)
        return round(entry - (risk_per_unit * r_multiple), 8)

    def _position_notional(self, entry: float, stop: float, account_risk_pct: float, leverage: float) -> tuple[float, list[str]]:
        notes: list[str] = []
        account_equity, equity_source = resolve_account_equity(self.settings)
        risk_budget = account_equity * (account_risk_pct / 100)
        notes.append("dynamic_compounding_enabled=true")
        notes.append(f"equity_source={equity_source}")
        risk_per_unit = abs(entry - stop)

        notes.append(f"account_equity_usdt={account_equity:.2f}")
        notes.append(f"risk_pct={account_risk_pct:.2f}")
        notes.append(f"risk_budget_usdt={risk_budget:.2f}")

        if entry <= 0 or risk_per_unit <= 0:
            notes.append("position sizing blocked: invalid entry/stop")
            return 0.0, notes

        raw_units = risk_budget / risk_per_unit
        raw_notional = raw_units * entry

        leverage_cap_notional = account_equity * min(float(leverage or 1.0), float(self.settings.max_leverage))
        planner_max_notional_pct = float(
            getattr(self.settings, "planner_max_notional_pct_of_equity", 50.0) or 50.0
        )
        equity_cap_notional = account_equity * (planner_max_notional_pct / 100.0)
        configured_cap_notional = float(
            getattr(self.settings, "planner_max_notional_per_trade_usdt", 0.0) or 0.0
        )

        hard_cap_candidates = [leverage_cap_notional, equity_cap_notional]
        if configured_cap_notional > 0:
            hard_cap_candidates.append(configured_cap_notional)

        hard_cap_notional = max(0.0, min(hard_cap_candidates))
        final_notional = min(raw_notional, hard_cap_notional)

        notes.append(f"raw_position_notional_usdt={raw_notional:.2f}")
        notes.append(f"leverage_cap_notional_usdt={leverage_cap_notional:.2f}")
        notes.append(f"equity_cap_notional_usdt={equity_cap_notional:.2f}")
        notes.append(f"configured_cap_notional_usdt={configured_cap_notional:.2f}")
        notes.append(f"planner_hard_cap_notional_usdt={hard_cap_notional:.2f}")
        if final_notional < raw_notional:
            notes.append("position capped by planner notional cap")
            logger.warning(
                "PLANNER_NOTIONAL_CAPPED | raw=%.2f | final=%.2f | equity=%.2f | risk_pct=%.2f | leverage=%s | cap_pct=%.2f | configured_cap=%.2f",
                raw_notional,
                final_notional,
                account_equity,
                account_risk_pct,
                leverage,
                planner_max_notional_pct,
                configured_cap_notional,
            )

        return round(final_notional, 2), notes

    @staticmethod
    def _risk_reward(entry: float, stop: float, target: float, direction: str) -> float:
        risk = abs(entry - stop)
        if risk <= 0:
            return 0.0
        reward = (target - entry) if direction == "LONG" else (entry - target)
        return round(max(0.0, reward / risk), 2)
