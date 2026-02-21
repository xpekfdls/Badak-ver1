from __future__ import annotations

from typing import List, Dict
import numpy as np
import pandas as pd
from config import SCENARIO_LOOKAHEAD, SCENARIO_RULES


def classify_outcomes(
    df: pd.DataFrame,
    entry_tick: int = 3,
    lookahead: int = SCENARIO_LOOKAHEAD,
) -> List[Dict]:
    results: List[Dict] = []
    n = len(df)
    closes = df["close"].values.astype(np.float64)
    timestamps = df["timestamp"].values
    bear_new = df["bear_new_tick"].values
    bull_new = df["bull_new_tick"].values
    bear_tc = df["bear_tick"].values
    bull_tc = df["bull_tick"].values

    for direction, new_arr, tc_arr in [("bear", bear_new, bear_tc), ("bull", bull_new, bull_tc)]:
        for i in range(n):
            if not new_arr[i] or tc_arr[i] < entry_tick:
                continue
            if i + lookahead >= n:
                break

            entry_price = closes[i]
            future = closes[i + 1 : i + 1 + lookahead]
            if len(future) == 0:
                continue

            outcome = _classify_single(future, entry_price, direction, lookahead)
            outcome["timestamp"] = int(timestamps[i])
            outcome["direction"] = direction
            outcome["tick_count"] = int(tc_arr[i])
            outcome["entry_price"] = float(entry_price)
            results.append(outcome)

    return results


def _classify_single(
    future: np.ndarray, entry: float, direction: str, lookahead: int
) -> Dict:
    if direction == "bear":
        pnl_series = (future - entry) / entry * 100
    else:
        pnl_series = (entry - future) / entry * 100

    max_gain = float(pnl_series.max())
    max_loss = float(pnl_series.min())
    final_pnl = float(pnl_series[-1]) if len(pnl_series) else 0.0
    peak_idx = int(pnl_series.argmax()) + 1

    r = SCENARIO_RULES
    scenario = "D"

    if peak_idx <= r["A_immediate_max_candles"] and max_gain >= r["A_immediate_min_gain"]:
        scenario = "A"
    elif peak_idx <= r["B_sideways_max_candles"] and max_gain >= r["B_sideways_min_gain"]:
        scenario = "B"
    elif max_loss <= r["C_deeper_min_drop"] and max_gain >= 0:
        scenario = "C"

    return {
        "scenario": scenario,
        "max_gain_pct": round(max_gain, 4),
        "max_loss_pct": round(max_loss, 4),
        "final_pnl_pct": round(final_pnl, 4),
        "peak_candles": peak_idx,
    }


def aggregate_stats(outcomes: List[Dict], symbol: str, timeframe: str, entry_tick: int) -> List[Dict]:
    stats: List[Dict] = []
    for direction in ("bear", "bull"):
        relevant = [o for o in outcomes if o["direction"] == direction and o["tick_count"] >= entry_tick]
        total = len(relevant)
        if total == 0:
            continue

        by_scenario: Dict[str, List[Dict]] = {}
        for o in relevant:
            by_scenario.setdefault(o["scenario"], []).append(o)

        for sc in ("A", "B", "C", "D"):
            items = by_scenario.get(sc, [])
            cnt = len(items)
            if cnt == 0:
                stats.append({
                    "symbol": symbol, "timeframe": timeframe,
                    "direction": direction, "min_tick": entry_tick,
                    "scenario": sc, "count": 0, "pct": 0,
                    "avg_pnl": 0, "median_pnl": 0,
                    "avg_max_gain": 0, "avg_max_loss": 0, "avg_hold": 0,
                })
                continue

            gains = np.array([o["max_gain_pct"] for o in items])
            losses = np.array([o["max_loss_pct"] for o in items])
            finals = np.array([o["final_pnl_pct"] for o in items])
            holds = np.array([o["peak_candles"] for o in items])

            stats.append({
                "symbol": symbol, "timeframe": timeframe,
                "direction": direction, "min_tick": entry_tick,
                "scenario": sc,
                "count": cnt,
                "pct": round(cnt / total * 100, 1),
                "avg_pnl": round(float(finals.mean()), 4),
                "median_pnl": round(float(np.median(finals)), 4),
                "avg_max_gain": round(float(gains.mean()), 4),
                "avg_max_loss": round(float(losses.mean()), 4),
                "avg_hold": int(holds.mean()),
            })

    return stats
