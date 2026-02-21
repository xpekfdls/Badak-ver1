from __future__ import annotations

import time
import hmac
import hashlib
import threading
from typing import Dict, List, Optional
from urllib.parse import urlencode
from collections import deque

import requests

from config import BINANCE_FUTURES_URL, BINANCE_API_KEY, BINANCE_API_SECRET

_request_times: deque = deque(maxlen=1200)
_rate_lock = threading.Lock()
MAX_REQUESTS_PER_MINUTE = 1100


def _rate_limit_check():
    """Simple sliding window rate limiter."""
    with _rate_lock:
        now = time.time()
        while _request_times and _request_times[0] < now - 60:
            _request_times.popleft()
        if len(_request_times) >= MAX_REQUESTS_PER_MINUTE:
            sleep_time = 60 - (now - _request_times[0]) + 0.1
            if sleep_time > 0:
                print(f"[RateLimit] Sleeping {sleep_time:.1f}s")
                time.sleep(sleep_time)
        _request_times.append(time.time())


def _sign(params: dict) -> dict:
    params["timestamp"] = int(time.time() * 1000)
    query = urlencode(params)
    signature = hmac.new(
        BINANCE_API_SECRET.encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    params["signature"] = signature
    return params


def _headers() -> dict:
    return {"X-MBX-APIKEY": BINANCE_API_KEY}


def is_configured() -> bool:
    return bool(BINANCE_API_KEY and BINANCE_API_SECRET)


def get_account_balance() -> Dict:
    if not is_configured():
        return {"totalWalletBalance": "0", "availableBalance": "0", "assets": []}
    _rate_limit_check()
    params = _sign({})
    resp = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v2/balance",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    usdt = next((a for a in data if a["asset"] == "USDT"), None)
    return {
        "totalWalletBalance": usdt["balance"] if usdt else "0",
        "availableBalance": usdt["availableBalance"] if usdt else "0",
        "unrealizedPnl": usdt["crossUnPnl"] if usdt else "0",
    }


def get_positions() -> List[Dict]:
    if not is_configured():
        return []
    params = _sign({})
    resp = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v2/positionRisk",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return [p for p in resp.json() if float(p.get("positionAmt", 0)) != 0]


def set_leverage(symbol: str, leverage: int) -> Dict:
    if not is_configured():
        return {"leverage": leverage, "symbol": symbol}
    params = _sign({"symbol": symbol, "leverage": leverage})
    resp = requests.post(
        f"{BINANCE_FUTURES_URL}/fapi/v1/leverage",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def set_margin_type(symbol: str, margin_type: str = "CROSSED") -> Dict:
    if not is_configured():
        return {}
    params = _sign({"symbol": symbol, "marginType": margin_type})
    resp = requests.post(
        f"{BINANCE_FUTURES_URL}/fapi/v1/marginType",
        params=params, headers=_headers(), timeout=10,
    )
    if resp.status_code == 200:
        return resp.json()
    return {}


def place_order(
    symbol: str,
    side: str,
    order_type: str = "MARKET",
    quantity: float = 0,
    price: float = 0,
    reduce_only: bool = False,
    stop_price: float = 0,
    client_order_id: str = "",
    time_in_force: str = "",
) -> Dict:
    if not is_configured():
        return {"orderId": 0, "status": "FILLED" if order_type == "MARKET" else "SIMULATED",
                "symbol": symbol, "side": side,
                "origQty": str(quantity), "executedQty": str(quantity),
                "avgPrice": str(price), "price": str(price)}

    _rate_limit_check()
    params = {
        "symbol": symbol,
        "side": side,
        "type": order_type,
        "quantity": str(quantity),
    }
    if client_order_id:
        params["newClientOrderId"] = client_order_id
    if reduce_only:
        params["reduceOnly"] = "true"
    if order_type == "LIMIT":
        params["price"] = str(price)
        params["timeInForce"] = time_in_force or "GTC"
    if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET") and stop_price:
        params["stopPrice"] = str(stop_price)
        params.pop("quantity", None)
        params["closePosition"] = "true"

    params = _sign(params)
    resp = requests.post(
        f"{BINANCE_FUTURES_URL}/fapi/v1/order",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def cancel_order(symbol: str, order_id: int) -> Dict:
    if not is_configured():
        return {}
    params = _sign({"symbol": symbol, "orderId": order_id})
    resp = requests.delete(
        f"{BINANCE_FUTURES_URL}/fapi/v1/order",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def cancel_all_orders(symbol: str) -> Dict:
    if not is_configured():
        return {}
    params = _sign({"symbol": symbol})
    resp = requests.delete(
        f"{BINANCE_FUTURES_URL}/fapi/v1/allOpenOrders",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_open_orders(symbol: str = "") -> List[Dict]:
    if not is_configured():
        return []
    params = {}
    if symbol:
        params["symbol"] = symbol
    params = _sign(params)
    resp = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/openOrders",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_order_status(symbol: str, order_id: int) -> Dict:
    if not is_configured():
        return {}
    params = _sign({"symbol": symbol, "orderId": order_id})
    resp = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/order",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_order_by_client_id(symbol: str, client_order_id: str) -> Dict:
    if not is_configured():
        return {}
    params = _sign({"symbol": symbol, "origClientOrderId": client_order_id})
    resp = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/order",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_mark_price(symbol: str) -> float:
    resp = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/premiumIndex",
        params={"symbol": symbol}, timeout=10,
    )
    resp.raise_for_status()
    return float(resp.json()["markPrice"])


def get_user_trades(symbol: str = "", limit: int = 100, start_time: int = 0) -> List[Dict]:
    """Fetch actual trade fills from Binance Futures."""
    if not is_configured():
        return []
    _rate_limit_check()
    params: dict = {"limit": limit}
    if symbol:
        params["symbol"] = symbol
    if start_time:
        params["startTime"] = start_time
    params = _sign(params)
    resp = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/userTrades",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def get_income_history(
    income_type: str = "", symbol: str = "", limit: int = 100, start_time: int = 0,
) -> List[Dict]:
    """Fetch income records (REALIZED_PNL, COMMISSION, FUNDING_FEE, etc.)."""
    if not is_configured():
        return []
    _rate_limit_check()
    params: dict = {"limit": limit}
    if income_type:
        params["incomeType"] = income_type
    if symbol:
        params["symbol"] = symbol
    if start_time:
        params["startTime"] = start_time
    params = _sign(params)
    resp = requests.get(
        f"{BINANCE_FUTURES_URL}/fapi/v1/income",
        params=params, headers=_headers(), timeout=10,
    )
    resp.raise_for_status()
    return resp.json()
