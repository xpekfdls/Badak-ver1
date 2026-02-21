from __future__ import annotations

import numpy as np
import pandas as pd


def count_ticks(
    df: pd.DataFrame, tick_threshold_pct: float = 0.5, reset_ratio: float = 0.7
) -> pd.DataFrame:
    n = len(df)
    closes = df["close"].values.astype(np.float64)
    opens = df["open"].values.astype(np.float64)

    bear_tick = np.zeros(n, dtype=np.int32)
    bull_tick = np.zeros(n, dtype=np.int32)
    bear_new = np.zeros(n, dtype=np.bool_)
    bull_new = np.zeros(n, dtype=np.bool_)

    _scan(closes, opens, n, tick_threshold_pct, reset_ratio, bear_tick, bear_new, True)
    _scan(closes, opens, n, tick_threshold_pct, reset_ratio, bull_tick, bull_new, False)

    result = df.copy()
    result["bear_tick"] = bear_tick
    result["bull_tick"] = bull_tick
    result["bear_new_tick"] = bear_new
    result["bull_new_tick"] = bull_new
    return result


def _scan(closes, opens, n, thresh, reset_ratio, tick_arr, new_arr, is_bear):
    active = False
    ref = count = 0
    extreme = 0.0

    for i in range(1, n):
        body = closes[i] - opens[i]
        prev_body = closes[i - 1] - opens[i - 1]
        target = body < 0 if is_bear else body > 0
        prev_opp = prev_body > 0 if is_bear else prev_body < 0
        opp = body > 0 if is_bear else body < 0

        if not active:
            if target and prev_opp:
                active, ref, count, extreme = True, closes[i - 1], 0, closes[i]
                move = _pct(ref, closes[i], is_bear)
                if move >= thresh:
                    count = 1
                    new_arr[i] = True
                    ref = extreme = closes[i]
                tick_arr[i] = count
        else:
            if is_bear:
                extreme = min(extreme, closes[i])
            else:
                extreme = max(extreme, closes[i])

            if _pct(ref, closes[i], is_bear) >= thresh:
                count += 1
                new_arr[i] = True
                ref = extreme = closes[i]

            if opp:
                drop = (ref - extreme) if is_bear else (extreme - ref)
                rec = (closes[i] - extreme) if is_bear else (extreme - closes[i])
                if drop <= 0 or rec / drop >= reset_ratio:
                    active, count = False, 0

            if count == 0:
                past = closes[i] >= ref if is_bear else closes[i] <= ref
                if past:
                    active = False

            tick_arr[i] = count


def _pct(ref, cur, is_bear):
    if ref == 0:
        return 0.0
    return (ref - cur) / ref * 100 if is_bear else (cur - ref) / ref * 100
