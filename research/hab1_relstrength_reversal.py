"""H-AB1-A: SOLUSDT cross-sectional relative-strength reversal.

Pre-registered 2026-09-24 in docs/RESEARCH_JOURNAL.md (commit 156c2f5),
BEFORE this script was written or run. Implements exactly the frozen
protocol: signal, thresholds, entry/exit/stop, costs, TRAIN/VAL/TEST
splits, and pass/fail gates. No parameter search, no post-hoc tuning.
Reproducible from data/history/*.json alone (stdlib only, no numpy).

Usage: python3 research/hab1_relstrength_reversal.py
"""
from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HISTORY = ROOT / "data" / "history"
OUT_DIR = ROOT / "reports" / "analysis"

SYMBOLS = ["ADAUSDT", "AVAXUSDT", "BNBUSDT", "BTCUSDT", "DOGEUSDT", "ETHUSDT",
           "FILUSDT", "LINKUSDT", "SOLUSDT", "TRXUSDT", "UNIUSDT", "XLMUSDT"]
TRADED = "SOLUSDT"
BASKET = [s for s in SYMBOLS if s != TRADED]

W = 16                     # lookback bars for relative strength (4h @ 15m)
ATR_N = 14
HORIZON_BARS = 16          # time-stop (4h) -- matches project convention (validation_engine.py)
STOP_ATR_MULT = 1.5
TARGET_R = 2.0
FEE_ROUNDTRIP_PCT = 0.14   # 12bps taker (codebase default) + 2bps slippage buffer (H-4D-1 convention)
FEE_STRESS_PCT = 0.25      # pre-registered cost-stress test

TRAIN_END = 3520           # exclusive; 40% of 8800
VAL_END = 6160             # exclusive; next 30%
# TEST = [VAL_END, n)      # final 30%

PREREG_COMMIT = "156c2f5e34345e053a19d16c6542904cfc6cafe9"


def load(symbol: str) -> list[dict]:
    with open(HISTORY / f"{symbol}_15m.json") as f:
        return json.load(f)


def percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def day_key(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).date().isoformat()


def main() -> None:
    data = {s: load(s) for s in SYMBOLS}
    n = len(data[TRADED])
    ts = [row["timestamp"] for row in data[TRADED]]
    for s in SYMBOLS:
        assert len(data[s]) == n, f"{s}: length mismatch ({len(data[s])} vs {n})"
        assert [r["timestamp"] for r in data[s]] == ts, f"{s}: timestamp misalignment"

    closes = {s: [row["close"] for row in data[s]] for s in SYMBOLS}
    highs = {s: [row["high"] for row in data[s]] for s in SYMBOLS}
    lows = {s: [row["low"] for row in data[s]] for s in SYMBOLS}
    opens = {s: [row["open"] for row in data[s]] for s in SYMBOLS}

    def ret_w(s: str, t: int) -> float:
        return math.log(closes[s][t] / closes[s][t - W])

    # ATR(14) simple rolling mean of true range, TRADED symbol only, no look-ahead.
    tr: list[float | None] = [None] * n
    for t in range(1, n):
        h, l, pc = highs[TRADED][t], lows[TRADED][t], closes[TRADED][t - 1]
        tr[t] = max(h - l, abs(h - pc), abs(l - pc))
    atr: list[float | None] = [None] * n
    for t in range(ATR_N, n):
        window = tr[t - ATR_N + 1: t + 1]
        if any(v is None for v in window):
            continue
        atr[t] = sum(window) / ATR_N

    warmup = max(W, ATR_N)

    # Cross-sectional z-score for SOL vs the 11-symbol basket, at each valid t.
    z: list[float | None] = [None] * n
    for t in range(warmup, n):
        r_basket = [ret_w(s, t) for s in BASKET]
        r_sol = ret_w(TRADED, t)
        mu = statistics.mean(r_basket)
        sd = statistics.pstdev(r_basket)
        z[t] = (r_sol - mu) / sd if sd > 0 else None

    # ---- TRAIN: threshold calibration ONLY. No forward/outcome data touched. ----
    train_z = [z[t] for t in range(warmup, TRAIN_END) if z[t] is not None]
    train_sorted = sorted(train_z)
    p20 = percentile(train_sorted, 0.20)
    p80 = percentile(train_sorted, 0.80)
    print(f"TRAIN z-distribution: n={len(train_z)}  P20={p20:.4f}  P80={p80:.4f}  "
          f"mean={statistics.mean(train_z):.4f}  pstdev={statistics.pstdev(train_z):.4f}")

    def simulate_split(start: int, end: int, fee_pct: float) -> list[dict]:
        """Signal bars t in [start, end); full trade lifecycle must resolve before `end`."""
        trades = []
        t = max(start, warmup)
        in_position_until = -1
        while t < end:
            if t <= in_position_until:
                t += 1
                continue
            zt = z[t]
            if zt is None:
                t += 1
                continue
            if zt <= p20:
                direction = "LONG"
            elif zt >= p80:
                direction = "SHORT"
            else:
                t += 1
                continue

            entry_idx = t + 1
            if entry_idx + HORIZON_BARS >= end or entry_idx >= n:
                t += 1
                continue
            atr_t = atr[t]
            if atr_t is None or atr_t <= 0:
                t += 1
                continue

            entry_price = opens[TRADED][entry_idx]
            stop_dist = STOP_ATR_MULT * atr_t
            sign = 1 if direction == "LONG" else -1
            stop_price = entry_price - sign * stop_dist
            target_price = entry_price + sign * TARGET_R * stop_dist
            stop_pct = stop_dist / entry_price * 100
            fee_r = fee_pct / stop_pct

            outcome, r_gross, exit_idx = None, None, None
            for bar_i in range(entry_idx + 1, entry_idx + 1 + HORIZON_BARS):
                h, l = highs[TRADED][bar_i], lows[TRADED][bar_i]
                hit_target = (h >= target_price) if direction == "LONG" else (l <= target_price)
                hit_stop = (l <= stop_price) if direction == "LONG" else (h >= stop_price)
                if hit_target and hit_stop:
                    outcome, r_gross, exit_idx = "STOP(ambiguous)", -1.0, bar_i
                    break
                if hit_stop:
                    outcome, r_gross, exit_idx = "STOP", -1.0, bar_i
                    break
                if hit_target:
                    outcome, r_gross, exit_idx = "TARGET", TARGET_R, bar_i
                    break
            if outcome is None:
                exit_idx = entry_idx + HORIZON_BARS
                exit_price = closes[TRADED][exit_idx]
                r_gross = sign * (exit_price - entry_price) / stop_dist
                outcome = "TIME"

            trades.append({
                "signal_idx": t, "entry_idx": entry_idx, "exit_idx": exit_idx,
                "timestamp": data[TRADED][t]["timestamp"],
                "direction": direction, "z": zt, "outcome": outcome,
                "r_gross": r_gross, "r_net": r_gross - fee_r, "fee_r": fee_r,
                "stop_pct": stop_pct,
            })
            in_position_until = exit_idx
            t += 1
        return trades

    def metrics(trades: list[dict], label: str) -> dict:
        n_t = len(trades)
        if n_t == 0:
            return {"label": label, "n": 0}
        r_list = [tr["r_net"] for tr in trades]
        wins = [r for r in r_list if r > 0]
        losses = [r for r in r_list if r <= 0]
        gross_win, gross_loss = sum(wins), -sum(losses) if losses else 0.0
        pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0.0)

        by_day = defaultdict(list)
        for tr in trades:
            by_day[day_key(tr["timestamp"])].append(tr["r_net"])
        cluster_means = [statistics.mean(v) for v in by_day.values()]
        n_clusters = len(cluster_means)
        if n_clusters >= 2:
            cm_mean, cm_sd = statistics.mean(cluster_means), statistics.stdev(cluster_means)
            t_stat = (cm_mean / (cm_sd / math.sqrt(n_clusters))) if cm_sd > 0 else (
                float("inf") if cm_mean != 0 else 0.0)
        else:
            t_stat = None

        eq = peak = max_dd = 0.0
        for r in r_list:
            eq += r
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)

        by_month: dict[str, float] = defaultdict(float)
        for tr in trades:
            m = datetime.fromtimestamp(tr["timestamp"] / 1000, tz=timezone.utc).strftime("%Y-%m")
            by_month[m] += tr["r_net"]
        total_r = sum(r_list)
        month_share = {m: (v / total_r if total_r != 0 else None) for m, v in by_month.items()}

        long_r = [tr["r_net"] for tr in trades if tr["direction"] == "LONG"]
        short_r = [tr["r_net"] for tr in trades if tr["direction"] == "SHORT"]

        return {
            "label": label, "n": n_t, "n_clusters_days": n_clusters,
            "avg_r": statistics.mean(r_list), "median_r": statistics.median(r_list),
            "win_rate": len(wins) / n_t, "profit_factor": pf,
            "t_stat_day_clustered": t_stat, "total_r": total_r, "max_drawdown_r": max_dd,
            "avg_stop_pct": statistics.mean(tr["stop_pct"] for tr in trades),
            "long_n": len(long_r), "long_avg_r": (statistics.mean(long_r) if long_r else None),
            "short_n": len(short_r), "short_avg_r": (statistics.mean(short_r) if short_r else None),
            "month_r": dict(by_month), "month_share": month_share,
        }

    results: dict = {"pregistration_commit": PREREG_COMMIT, "p20": p20, "p80": p80,
                      "train_n": len(train_z)}

    val_trades = simulate_split(TRAIN_END, VAL_END, FEE_ROUNDTRIP_PCT)
    val_m = metrics(val_trades, "VAL")
    results["val"] = val_m
    print("\nVAL:", json.dumps(val_m, indent=2, default=str))

    def val_gate(m: dict) -> tuple[bool, list[str]]:
        if m["n"] == 0:
            return False, ["n=0"]
        reasons = []
        ok_sign = m["total_r"] > 0
        ok_n = m["n"] >= 30
        ok_t = m["t_stat_day_clustered"] is not None and abs(m["t_stat_day_clustered"]) >= 1.5
        ok_pf = m["profit_factor"] > 1.0
        if not ok_sign: reasons.append("sign not net-positive in registered (reversal) direction")
        if not ok_n: reasons.append(f"N={m['n']} < 30")
        if not ok_t: reasons.append(f"|t|={m['t_stat_day_clustered']} < 1.5")
        if not ok_pf: reasons.append(f"profit_factor={m['profit_factor']:.3f} <= 1.0")
        return (ok_sign and ok_n and ok_t and ok_pf), reasons

    passed, reasons = val_gate(val_m)
    results["val_gate_passed"] = passed
    results["val_gate_reasons_if_failed"] = reasons
    print(f"\nVAL GATE: {'PASS' if passed else 'FAIL'}  {reasons}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "hab1_relstrength_reversal_result.json"

    if not passed:
        results["verdict"] = "REJECTED_AT_VAL"
        results["test"] = None
        with open(out_path, "w") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"\nStopped at VAL gate -- sealed TEST NOT run (seal preserved). Written to {out_path}")
        return

    # ---- sealed TEST: single run, no tuning ----
    test_trades = simulate_split(VAL_END, n, FEE_ROUNDTRIP_PCT)
    test_m = metrics(test_trades, "TEST")
    results["test"] = test_m
    print("\nTEST (sealed):", json.dumps(test_m, indent=2, default=str))

    def restress(trades: list[dict], fee_pct: float) -> list[dict]:
        return [{**tr, "r_net": tr["r_gross"] - fee_pct / tr["stop_pct"]} for tr in trades]

    test_stress_m = metrics(restress(test_trades, FEE_STRESS_PCT), "TEST_STRESS_25bps")
    results["test_stress_25bps"] = test_stress_m

    def test_gate(m: dict, val_total_r: float) -> tuple[bool, list[str]]:
        if m["n"] == 0:
            return False, ["n=0"]
        reasons = []
        ok_sign = (m["total_r"] > 0) == (val_total_r > 0)
        ok_n = m["n"] >= 30
        ok_t = m["t_stat_day_clustered"] is not None and abs(m["t_stat_day_clustered"]) >= 2.0
        ok_pf = m["profit_factor"] > 1.3
        ok_month = all(abs(s) <= 0.70 for s in m["month_share"].values() if s is not None)
        ok_stress = test_stress_m["n"] > 0 and test_stress_m["profit_factor"] > 1.0
        if not ok_sign: reasons.append("sign flip vs VAL")
        if not ok_n: reasons.append(f"N={m['n']} < 30")
        if not ok_t: reasons.append(f"|t|={m['t_stat_day_clustered']} < 2.0")
        if not ok_pf: reasons.append(f"profit_factor={m['profit_factor']:.3f} <= 1.3")
        if not ok_month: reasons.append("single month > 70% of total R")
        if not ok_stress: reasons.append("fails 25bps cost stress (profit_factor <= 1.0)")
        return (ok_sign and ok_n and ok_t and ok_pf and ok_month and ok_stress), reasons

    passed_test, reasons_test = test_gate(test_m, val_m["total_r"])
    results["test_gate_passed"] = passed_test
    results["test_gate_reasons_if_failed"] = reasons_test
    results["verdict"] = "HISTORICAL_GREEN_CANDIDATE" if passed_test else "REJECTED_AT_TEST"

    print(f"\nTEST GATE: {'PASS' if passed_test else 'FAIL'}  {reasons_test}")
    print(f"\nVERDICT: {results['verdict']}")

    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
