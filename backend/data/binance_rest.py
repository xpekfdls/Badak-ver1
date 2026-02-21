from __future__ import annotations

import time
from typing import List, Dict

import requests
import pandas as pd
from config import BINANCE_FUTURES_URL


def get_top_symbols(
    n: int = 30,
    min_age_days: int = 90,
    min_daily_range_pct: float = 10.0,
    min_quote_volume: float = 100_000_000,
) -> List[Dict]:
    """Top N USDT perpetuals filtered by volatility and liquidity.

    Returns list of dicts: {symbol, daily_range_pct, quote_volume, price_change_pct}
    """
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - min_age_days * 86_400_000

    info = requests.get(f"{BINANCE_FUTURES_URL}/fapi/v1/exchangeInfo", timeout=15).json()
    perpetuals = set()
    for s in info["symbols"]:
        if (
            s.get("quoteAsset") == "USDT"
            and s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("onboardDate", now_ms) <= cutoff_ms
        ):
            perpetuals.add(s["symbol"])

    tickers = requests.get(f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr", timeout=15).json()
    results = []
    for t in tickers:
        if t["symbol"] not in perpetuals:
            continue
        qv = float(t.get("quoteVolume", 0))
        hi = float(t.get("highPrice", 0))
        lo = float(t.get("lowPrice", 0))
        daily_range = (hi - lo) / lo * 100 if lo > 0 else 0

        if qv < min_quote_volume:
            continue
        if daily_range < min_daily_range_pct:
            continue

        results.append({
            "symbol": t["symbol"],
            "daily_range_pct": round(daily_range, 2),
            "quote_volume": qv,
            "price_change_pct": round(float(t.get("priceChangePercent", 0)), 2),
        })

    results.sort(key=lambda x: x["quote_volume"], reverse=True)
    return results[:n]


def fetch_24h_changes(symbols: List[str]) -> Dict[str, float]:
    """Fetch 24h price change percent for given symbols."""
    try:
        tickers = requests.get(f"{BINANCE_FUTURES_URL}/fapi/v1/ticker/24hr", timeout=10).json()
        sym_set = set(symbols)
        return {
            t["symbol"]: round(float(t.get("priceChangePercent", 0)), 2)
            for t in tickers if t["symbol"] in sym_set
        }
    except Exception:
        return {}


def get_symbol_info() -> dict:
    """Get exchange info for precision/filters."""
    info = requests.get(f"{BINANCE_FUTURES_URL}/fapi/v1/exchangeInfo", timeout=15).json()
    result = {}
    for s in info["symbols"]:
        filters = {f["filterType"]: f for f in s.get("filters", [])}
        result[s["symbol"]] = {
            "pricePrecision": s.get("pricePrecision", 2),
            "quantityPrecision": s.get("quantityPrecision", 3),
            "filters": filters,
        }
    return result


def download_klines(
    symbol: str, timeframe: str, start_ts: int, end_ts: int,
) -> pd.DataFrame:
    url = f"{BINANCE_FUTURES_URL}/fapi/v1/klines"
    all_klines: list = []
    current = start_ts

    while current < end_ts:
        params = {
            "symbol": symbol, "interval": timeframe,
            "startTime": current, "endTime": end_ts, "limit": 1500,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        klines = resp.json()
        if not klines:
            break

        all_klines.extend(klines)
        current = klines[-1][6] + 1

        if len(klines) < 1500:
            break
        time.sleep(0.2)

    if not all_klines:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    cols = [
        "timestamp", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(all_klines, columns=cols)
    df = df[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    df["timestamp"] = df["timestamp"].astype('int64')
    return df
