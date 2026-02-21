from __future__ import annotations

import asyncio
import json
import time
import hmac
import hashlib
from typing import Callable, List, Optional

import requests
import websockets

from config import BINANCE_WS_URL, BINANCE_FUTURES_URL, BINANCE_API_KEY, BINANCE_API_SECRET


async def kline_stream(
    symbols: List[str],
    timeframe: str,
    on_kline: Callable,
):
    streams = "/".join(f"{s.lower()}@kline_{timeframe}" for s in symbols)
    url = f"{BINANCE_WS_URL}/stream?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=60, ping_timeout=30) as ws:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        k = data.get("k", {})
                        if not k:
                            continue
                        symbol = k["s"]
                        kline = {
                            "timestamp": k["t"],
                            "open": float(k["o"]),
                            "high": float(k["h"]),
                            "low": float(k["l"]),
                            "close": float(k["c"]),
                            "volume": float(k["v"]),
                            "is_closed": k["x"],
                        }
                        await on_kline(symbol, kline)
                    except (KeyError, ValueError):
                        continue
        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            print(f"[MarketWS] Disconnected: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[MarketWS] Unexpected error: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)


async def mark_price_stream(
    symbols: List[str],
    on_price: Callable,
):
    """Subscribe to mark price updates for all symbols (1s interval)."""
    streams = "/".join(f"{s.lower()}@markPrice@1s" for s in symbols)
    url = f"{BINANCE_WS_URL}/stream?streams={streams}"

    while True:
        try:
            async with websockets.connect(url, ping_interval=60, ping_timeout=30) as ws:
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data", {})
                        await on_price(data.get("s", ""), float(data.get("p", 0)))
                    except (KeyError, ValueError):
                        continue
        except (websockets.ConnectionClosed, ConnectionError, OSError):
            await asyncio.sleep(5)
        except Exception:
            await asyncio.sleep(10)


def _get_listen_key() -> Optional[str]:
    if not BINANCE_API_KEY:
        return None
    resp = requests.post(
        f"{BINANCE_FUTURES_URL}/fapi/v1/listenKey",
        headers={"X-MBX-APIKEY": BINANCE_API_KEY},
        timeout=10,
    )
    if resp.status_code == 200:
        return resp.json().get("listenKey")
    return None


async def user_data_stream(on_event: Callable):
    """Connect to Binance User Data Stream for order/position updates."""
    listen_key = _get_listen_key()
    if not listen_key:
        print("[UserWS] No API key configured, skipping user data stream")
        return

    url = f"{BINANCE_WS_URL}/ws/{listen_key}"

    async def _keepalive():
        while True:
            await asyncio.sleep(30 * 60)
            try:
                requests.put(
                    f"{BINANCE_FUTURES_URL}/fapi/v1/listenKey",
                    headers={"X-MBX-APIKEY": BINANCE_API_KEY},
                    timeout=10,
                )
            except Exception:
                pass

    while True:
        try:
            async with websockets.connect(url, ping_interval=60, ping_timeout=30) as ws:
                keepalive_task = asyncio.create_task(_keepalive())
                try:
                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                            await on_event(event)
                        except (KeyError, ValueError):
                            continue
                finally:
                    keepalive_task.cancel()
        except (websockets.ConnectionClosed, ConnectionError, OSError) as e:
            print(f"[UserWS] Disconnected: {e}. Reconnecting in 5s...")
            listen_key = _get_listen_key()
            if listen_key:
                url = f"{BINANCE_WS_URL}/ws/{listen_key}"
            await asyncio.sleep(5)
        except Exception as e:
            print(f"[UserWS] Unexpected: {e}. Reconnecting in 10s...")
            await asyncio.sleep(10)
