from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from data.db import (
    init_db, get_open_positions, get_trades, get_trade_stats,
    get_setting, set_setting, get_settings_dict, lookup_scenario_stats,
    get_daily_stats, get_symbol_stats,
    get_active_orders, get_recent_orders,
)
from data.binance_rest import download_klines
from trading.binance_account import (
    get_account_balance, get_positions as get_binance_positions,
    set_leverage, get_open_orders, get_mark_price, is_configured,
)
from trading.order_manager import (
    close_position_order, cancel_open_order, set_order_tracker, OrderError,
)
from trading.auto_trader import AutoTrader
from trading.trailing_stop import TrailingStopEngine
from trading.order_tracker import OrderTracker
from strategy.signal_detector import SignalDetector
from strategy.tick_counter import count_ticks
from data.market_ws import user_data_stream
from models.schemas import Signal
from config import STRATEGY, TIMEFRAME, TRADE_DEFAULTS

FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

detector = SignalDetector()
auto_trader = AutoTrader()
trailing_engine = TrailingStopEngine()
order_tracker = OrderTracker()
ws_clients: List[WebSocket] = []
_system_logs: List[dict] = []
MAX_LOGS = 500


def add_log(category: str, message: str, data: dict = None):
    """Add a system log entry and broadcast to clients."""
    entry = {
        "ts": time.time(),
        "cat": category,
        "msg": message,
        "data": data or {},
    }
    _system_logs.append(entry)
    if len(_system_logs) > MAX_LOGS:
        _system_logs[:] = _system_logs[-MAX_LOGS:]
    asyncio.ensure_future(broadcast({"type": "system_log", "data": entry}))


async def broadcast(msg):
    if isinstance(msg, dict):
        text = json.dumps(msg, default=str)
    else:
        text = json.dumps(msg, default=str)
    dead = []
    for ws in ws_clients:
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)


async def broadcast_signal(signal: Signal):
    sc_parts = [f"{s.scenario}:{s.pct:.0f}%" for s in (signal.scenarios or [])]
    sc_info = f" ({', '.join(sc_parts)})" if sc_parts else f" ({signal.total_cases} cases)"
    add_log("SIGNAL", f"{signal.symbol} {signal.direction} {signal.tick_count}T @ ${signal.price:.4f}{sc_info}")
    await broadcast({"type": "signal", "data": signal.to_dict()})


async def on_position_opened(pos):
    sym = pos.get("symbol", "")
    d = pos.get("direction", "")
    avg = pos.get("avg_price", 0)
    qty = pos.get("quantity", 0)
    lev = pos.get("leverage", 10)
    position_value = avg * qty
    margin_used = position_value / lev if lev else position_value
    sl_pct = float(get_setting("sl_pct", "5"))
    trail_act = float(get_setting("trail_activation_pct", "1"))
    is_long = d == "LONG"
    sl_price = avg * (1 - sl_pct / 100) if is_long else avg * (1 + sl_pct / 100)
    trail_price = avg * (1 + trail_act / 100) if is_long else avg * (1 - trail_act / 100)
    add_log("OPEN", f"{sym} {d} x{lev} entry=${avg:.4f} qty={qty} "
            f"(${margin_used:.2f} margin, ${position_value:.2f} position)", {
        "sl_price": round(sl_price, 6),
        "trail_act_price": round(trail_price, 6),
        "sl_pct": sl_pct,
        "trail_act_pct": trail_act,
        "margin": round(margin_used, 2),
        "position_value": round(position_value, 2),
    })
    await broadcast({"type": "position_opened", "data": pos})

async def on_position_closed(trade):
    sym = trade.get("symbol", "")
    reason = trade.get("exit_reason", "")
    pnl = trade.get("realized_pnl", 0)
    pnl_pct = trade.get("pnl_pct", 0)
    entry = trade.get("entry_price", 0)
    exit_p = trade.get("exit_price", 0)
    qty = trade.get("quantity", 0)
    add_log("CLOSE", f"{sym} {reason} entry=${entry:.4f}→exit=${exit_p:.4f} "
            f"qty={qty} PnL={pnl_pct:+.2f}% (${pnl:+.2f})")
    await broadcast({"type": "position_closed", "data": trade})

async def on_position_updated(pos):
    await broadcast({"type": "position_updated", "data": pos})

async def on_order_update(order):
    status = order.get("status", "")
    sym = order.get("symbol", "")
    side = order.get("side", "")
    purpose = order.get("purpose", "")
    if status == "FILLED":
        fill_price = order.get("avg_fill_price", 0)
        fill_qty = order.get("filled_qty", order.get("quantity", 0))
        fill_value = fill_price * fill_qty if fill_price else 0
        add_log("ORDER", f"{sym} {side} {purpose} FILLED @ ${fill_price:.4f} "
                f"qty={fill_qty} (${fill_value:.2f})")
    elif status in ("CANCELED", "EXPIRED", "ERROR"):
        add_log("ORDER", f"{sym} {side} {purpose} {status}", {"error": order.get("error_msg", "")})
    await broadcast({"type": "order_update", "data": order})
    slippage = order.get("slippage_pct", 0)
    if slippage > 0.1:
        add_log("WARN", f"{sym} slippage {slippage:.3f}%")
        await broadcast({
            "type": "slippage_warning",
            "data": {
                "symbol": order.get("symbol"),
                "slippage_pct": slippage,
                "expected": order.get("expected_price"),
                "actual": order.get("avg_fill_price"),
            }
        })


async def _periodic_tasks():
    """Background loop: check pending market orders + position sync."""
    while True:
        try:
            await order_tracker.check_pending_market_orders()
        except Exception as e:
            print(f"[Periodic] Pending check error: {e}")

        await asyncio.sleep(5)


async def _position_sync_loop():
    """Every 30s, sync local positions with Binance."""
    while True:
        await asyncio.sleep(30)
        try:
            await order_tracker.sync_positions()
        except Exception as e:
            print(f"[Sync] Error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()

    # Wire order tracker
    order_tracker.set_callbacks(
        on_position_opened=on_position_opened,
        on_position_closed=on_position_closed,
        on_position_updated=on_position_updated,
        on_order_update=on_order_update,
    )
    order_tracker.set_trailing_engine(trailing_engine)
    order_tracker.set_price_source(detector.get_price)
    set_order_tracker(order_tracker)

    # Wire signal detector + auto trader
    detector.add_listener(broadcast_signal)
    detector.add_listener(auto_trader.on_signal)
    auto_trader.set_broadcast(broadcast)
    auto_trader.set_trailing_engine(trailing_engine)
    auto_trader.set_log_handler(add_log)

    # Wire trailing stop
    trailing_engine.set_price_source(detector.get_price)
    trailing_engine.set_exit_handler(auto_trader.handle_trailing_exit)
    trailing_engine.set_cycle_sell_handler(auto_trader.handle_cycle_sell)
    trailing_engine.set_log_handler(add_log)

    # Start background tasks
    detector_task = asyncio.create_task(detector.start())
    trailing_task = asyncio.create_task(trailing_engine.start())
    user_stream_task = asyncio.create_task(user_data_stream(order_tracker.on_user_event))
    periodic_task = asyncio.create_task(_periodic_tasks())
    sync_task = asyncio.create_task(_position_sync_loop())

    # Initial position sync on startup
    try:
        await order_tracker.sync_positions()
        print("[Startup] Position sync completed")
    except Exception as e:
        print(f"[Startup] Position sync failed: {e}")

    yield

    await detector.stop()
    await trailing_engine.stop()
    for t in (detector_task, trailing_task, user_stream_task, periodic_task, sync_task):
        t.cancel()


app = FastAPI(title="Coin Auto Trader", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


# --- Pages ---

@app.get("/")
async def index():
    return FileResponse(str(FRONTEND_DIR / "index.html"))


# --- Account ---

@app.get("/api/account")
def api_account():
    try:
        bal = get_account_balance()
    except Exception:
        bal = {"totalWalletBalance": "0", "availableBalance": "0", "unrealizedPnl": "0"}
    seed = float(get_setting("seed_money", "0"))
    if seed <= 0:
        seed = float(bal.get("totalWalletBalance", 0))
        if seed > 0:
            set_setting("seed_money", str(round(seed, 2)))
    bal["seedMoney"] = seed
    return {"configured": is_configured(), "balance": bal}


@app.post("/api/leverage/{symbol}/{leverage}")
def api_set_leverage(symbol: str, leverage: int):
    if leverage < 1 or leverage > 125:
        raise HTTPException(400, "Leverage must be 1-125")
    try:
        result = set_leverage(symbol, leverage)
        set_setting("leverage", str(leverage))
        return result
    except Exception as e:
        raise HTTPException(400, str(e))


# --- Settings ---

@app.get("/api/settings")
def api_get_settings():
    saved = get_settings_dict()
    defaults = {
        "buy_mode": TRADE_DEFAULTS["buy_mode"],
        "sell_mode": TRADE_DEFAULTS["sell_mode"],
        "position_size_pct": TRADE_DEFAULTS["position_size_pct"],
        "max_open_positions": TRADE_DEFAULTS["max_open_positions"],
        "leverage": STRATEGY["leverage"],
        "trail_activation_pct": STRATEGY["trail_activation_pct"],
        "trail_distance_pct": STRATEGY["trail_distance_pct"],
        "sl_pct": STRATEGY["sl_pct"],
        "max_entries": STRATEGY["max_entries"],
        "scale_multiplier": STRATEGY["scale_multiplier"],
        "cycle_mode": STRATEGY["cycle_mode"],
        "cycle_sell_pct": STRATEGY["cycle_sell_pct"],
        "seed_money": 1000,
        "operating_fund_mode": TRADE_DEFAULTS["operating_fund_mode"],
        "operating_fund_amount": TRADE_DEFAULTS["operating_fund_amount"],
        "target_symbol": TRADE_DEFAULTS["target_symbol"],
        "cooldown_seconds": TRADE_DEFAULTS["cooldown_seconds"],
        "entry_interval": STRATEGY["entry_interval"],
        "daily_loss_limit": 0,
        "max_consecutive_losses": 5,
    }
    defaults.update(saved)
    return defaults


class SettingUpdate(BaseModel):
    key: str
    value: str

@app.post("/api/settings")
def api_update_setting(body: SettingUpdate):
    set_setting(body.key, body.value)
    return {"status": "ok", "key": body.key, "value": body.value}


# --- Market Data / Signals ---

@app.get("/api/state")
def api_state():
    return detector.get_state()

@app.get("/api/signals")
def api_signals(limit: int = 100):
    return detector.get_signals(limit)

TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "4h": 14400, "1d": 86400,
}

@app.get("/api/chart/{symbol}")
def api_chart(symbol: str, timeframe: str = "", bars: int = 500):
    tf = timeframe if timeframe else detector.timeframe
    tf_sec = TF_SECONDS.get(tf, 300)
    end_ts = int(time.time() * 1000)
    start_ts = end_ts - bars * tf_sec * 1000
    df = download_klines(symbol, tf, start_ts, end_ts)
    if df.empty:
        return []
    df = count_ticks(df, STRATEGY["tick_threshold_pct"], STRATEGY["reset_ratio"])
    result = []
    for _, r in df.iterrows():
        result.append({
            "time": int(r["timestamp"]) // 1000,
            "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"]),
            "volume": float(r["volume"]),
            "bear_tick": int(r["bear_tick"]), "bull_tick": int(r["bull_tick"]),
            "bear_new": bool(r["bear_new_tick"]), "bull_new": bool(r["bull_new_tick"]),
        })
    return result


@app.get("/api/ticker/{symbol}")
def api_ticker(symbol: str):
    import requests as _req
    try:
        resp = _req.get(
            f"https://fapi.binance.com/fapi/v1/ticker/24hr",
            params={"symbol": symbol}, timeout=5,
        )
        d = resp.json()
        return {
            "high": float(d.get("highPrice", 0)),
            "low": float(d.get("lowPrice", 0)),
            "volume": float(d.get("quoteVolume", 0)),
            "change_pct": float(d.get("priceChangePercent", 0)),
            "last_price": float(d.get("lastPrice", 0)),
        }
    except Exception:
        return {"high": 0, "low": 0, "volume": 0, "change_pct": 0, "last_price": 0}

@app.get("/api/scenario_stats/{symbol}")
def api_scenario_stats(symbol: str, direction: str = "bear", tick: int = 3):
    return lookup_scenario_stats(symbol, direction, tick, detector.timeframe)

@app.post("/api/timeframe/{tf}")
async def api_set_timeframe(tf: str):
    if tf not in ("1m", "3m", "5m", "15m", "30m"):
        raise HTTPException(400, "Invalid timeframe")
    await detector.switch_timeframe(tf)
    await broadcast({"type": "tf_changed", "data": {"timeframe": tf}})
    return {"timeframe": tf}

@app.post("/api/symbol/add/{symbol}")
async def api_add_symbol(symbol: str):
    ok = await detector.add_symbol(symbol)
    if not ok:
        return {"status": "already_exists"}
    return {"status": "added", "symbol": symbol.upper()}

@app.post("/api/symbol/remove/{symbol}")
async def api_remove_symbol(symbol: str):
    ok = await detector.remove_symbol(symbol)
    if not ok:
        return {"status": "not_found"}
    return {"status": "removed", "symbol": symbol.upper()}


# --- Trading ---

class OrderRequest(BaseModel):
    symbol: str
    direction: str
    usdt_amount: float
    leverage: int = 0
    price: float = 0
    order_type: str = "MARKET"

@app.post("/api/order/open")
async def api_open_order(req: OrderRequest):
    price = req.price or detector.get_price(req.symbol)
    if price <= 0:
        try:
            price = get_mark_price(req.symbol)
        except Exception:
            raise HTTPException(400, "Cannot get current price")

    limit_price = req.price if req.order_type == "LIMIT" else 0
    try:
        result = await auto_trader.manual_open(
            symbol=req.symbol,
            direction=req.direction.upper(),
            usdt_amount=req.usdt_amount,
            current_price=price,
            leverage=req.leverage,
            order_type=req.order_type,
            limit_price=limit_price,
        )
    except OrderError as e:
        raise HTTPException(400, str(e))
    if not result:
        raise HTTPException(400, "Failed to submit order (exchange rejected)")
    if result.get("status") == "OPEN":
        await broadcast({"type": "position_opened", "data": result})
    else:
        await broadcast({"type": "order_submitted", "data": result})
    return result


@app.post("/api/order/close/{pos_id}")
async def api_close_order(pos_id: str):
    result = await auto_trader.manual_close(pos_id, "MANUAL")
    if not result:
        raise HTTPException(404, "Position not found")
    return result


class ScaleInRequest(BaseModel):
    usdt_amount: float
    price: float = 0

@app.post("/api/order/scale_in/{pos_id}")
async def api_scale_in(pos_id: str, req: ScaleInRequest):
    positions = get_open_positions()
    pos = next((p for p in positions if p["id"] == pos_id), None)
    if not pos:
        raise HTTPException(404, "Position not found")
    price = req.price or detector.get_price(pos["symbol"])
    if price <= 0:
        price = get_mark_price(pos["symbol"])

    result = await auto_trader.manual_scale_in(pos_id, req.usdt_amount, price)
    if not result:
        raise HTTPException(400, "Scale-in failed (max entries reached)")
    await broadcast({"type": "position_updated", "data": result})
    return result


@app.post("/api/order/cancel/{order_id}")
async def api_cancel_order(order_id: str):
    ok = cancel_open_order(order_id)
    if not ok:
        raise HTTPException(400, "Cannot cancel order")
    await broadcast({"type": "order_canceled", "data": {"id": order_id}})
    return {"status": "canceled"}


@app.post("/api/emergency/close_all")
async def api_emergency_close_all():
    result = await auto_trader.emergency_close_all()
    return result


# --- Orders ---

@app.get("/api/orders/active")
def api_active_orders():
    return get_active_orders()

@app.get("/api/orders/recent")
def api_recent_orders(limit: int = 50):
    return get_recent_orders(limit)


# --- Positions ---

@app.get("/api/positions")
def api_positions():
    positions = get_open_positions()
    for pos in positions:
        price = detector.get_price(pos["symbol"])
        if price > 0:
            avg = pos["avg_price"]
            lev = pos["leverage"]
            if pos["direction"] == "LONG":
                pnl = (price - avg) / avg * 100 * lev
            else:
                pnl = (avg - price) / avg * 100 * lev
            pos["unrealized_pnl"] = round(pnl, 4)
            pos["mark_price"] = price
    return positions


# --- Trade Journal ---

@app.get("/api/trades")
def api_trades(limit: int = 100, offset: int = 0, symbol: str = ""):
    return get_trades(limit, offset, symbol)

@app.get("/api/trades/stats")
def api_trade_stats():
    stats = get_trade_stats()
    seed = float(get_setting("seed_money", "1000"))
    stats["seed_money"] = seed
    stats["total_return_pct"] = round(stats["total_pnl"] / seed * 100, 2) if seed else 0
    return stats

@app.get("/api/trades/daily")
def api_daily_stats():
    return get_daily_stats()

@app.get("/api/trades/by_symbol")
def api_symbol_stats():
    return get_symbol_stats()

@app.get("/api/trades/equity_curve")
def api_equity_curve():
    trades = get_trades(limit=10000)
    trades.sort(key=lambda t: t["exit_time"])
    seed = float(get_setting("seed_money", "1000"))
    curve = [{"time": 0, "equity": seed}]
    running = seed
    for t in trades:
        running += t["realized_pnl"]
        curve.append({
            "time": t["exit_time"],
            "equity": round(running, 2),
            "pnl": t["realized_pnl"],
            "symbol": t["symbol"],
        })
    return curve


@app.get("/api/trades/binance")
def api_binance_trades(symbol: str = "", limit: int = 50):
    """Hybrid trade history: Binance income data + local metadata."""
    from trading.binance_account import get_income_history, is_configured
    from data.db import get_order_by_binance_id

    if not is_configured():
        return get_trades(limit, 0, symbol)

    try:
        incomes = get_income_history(
            income_type="REALIZED_PNL", symbol=symbol, limit=limit,
        )
    except Exception as e:
        print(f"[API] Binance income fetch failed: {e}")
        return get_trades(limit, 0, symbol)

    results = []
    for inc in incomes:
        pnl = float(inc.get("income", 0))
        if abs(pnl) < 0.0001:
            continue

        trade_id = inc.get("tradeId", "")
        sym = inc.get("symbol", "")
        ts = int(inc.get("time", 0))
        order_id = int(inc.get("orderId", 0) or 0)

        local_order = get_order_by_binance_id(order_id) if order_id else None
        purpose = local_order.get("purpose", "") if local_order else ""
        signal_tick = local_order.get("signal_tick", 0) if local_order else 0

        results.append({
            "source": "binance",
            "symbol": sym,
            "time": ts,
            "realized_pnl": round(pnl, 4),
            "order_id": order_id,
            "exit_reason": purpose or ("CYCLE_SELL" if abs(pnl) < 1 else ""),
            "signal_tick": signal_tick,
            "info": inc.get("info", ""),
        })

    results.sort(key=lambda x: x["time"], reverse=True)
    return results


@app.get("/api/trades/binance/summary")
def api_binance_summary(days: int = 7):
    """Summary stats from Binance income history."""
    from trading.binance_account import get_income_history, is_configured

    if not is_configured():
        return api_trade_stats()

    start_ms = int((time.time() - days * 86400) * 1000)

    try:
        pnl_records = get_income_history(income_type="REALIZED_PNL", limit=1000, start_time=start_ms)
        fee_records = get_income_history(income_type="COMMISSION", limit=1000, start_time=start_ms)
        funding_records = get_income_history(income_type="FUNDING_FEE", limit=1000, start_time=start_ms)
    except Exception as e:
        print(f"[API] Binance summary fetch failed: {e}")
        return api_trade_stats()

    total_pnl = sum(float(r.get("income", 0)) for r in pnl_records)
    total_fee = sum(float(r.get("income", 0)) for r in fee_records)
    total_funding = sum(float(r.get("income", 0)) for r in funding_records)
    wins = sum(1 for r in pnl_records if float(r.get("income", 0)) > 0)
    total = len([r for r in pnl_records if abs(float(r.get("income", 0))) > 0.0001])

    seed = float(get_setting("seed_money", "1000"))
    return {
        "source": "binance",
        "days": days,
        "total_trades": total,
        "wins": wins,
        "win_rate": round(wins / total * 100, 1) if total else 0,
        "total_pnl": round(total_pnl, 4),
        "total_fee": round(total_fee, 4),
        "total_funding": round(total_funding, 4),
        "net_pnl": round(total_pnl + total_fee + total_funding, 4),
        "seed_money": seed,
        "return_pct": round((total_pnl + total_fee + total_funding) / seed * 100, 2) if seed else 0,
    }


# --- System Logs ---

@app.get("/api/logs")
def api_logs(limit: int = 200):
    """Return recent system logs (newest first)."""
    return _system_logs[-limit:][::-1]


# --- WebSocket ---

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
            except Exception:
                pass
    except WebSocketDisconnect:
        pass
    finally:
        if ws in ws_clients:
            ws_clients.remove(ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
