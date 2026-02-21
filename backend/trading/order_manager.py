from __future__ import annotations

import uuid
import time
from typing import Optional, Dict

from trading.binance_account import (
    place_order, cancel_all_orders, get_mark_price,
    set_leverage as _set_leverage, cancel_order,
    set_margin_type as _set_margin_type, get_open_orders,
)
from data.db import (
    save_order, update_order_status, get_order,
    save_position, save_trade, close_position, get_open_positions,
)
from data.binance_rest import get_symbol_info
from config import STRATEGY

_symbol_info: Dict = {}
_order_tracker = None


def set_order_tracker(tracker):
    global _order_tracker
    _order_tracker = tracker


def _get_precision(symbol: str):
    global _symbol_info
    if not _symbol_info:
        try:
            _symbol_info = get_symbol_info()
        except Exception:
            pass
    info = _symbol_info.get(symbol, {})
    return info.get("pricePrecision", 2), info.get("quantityPrecision", 3)


def _round_qty(symbol: str, qty: float) -> float:
    _, qp = _get_precision(symbol)
    return round(qty, qp)


def _round_price(symbol: str, price: float) -> float:
    pp, _ = _get_precision(symbol)
    return round(price, pp)


def _get_min_notional(symbol: str) -> float:
    global _symbol_info
    if not _symbol_info:
        try:
            _symbol_info = get_symbol_info()
        except Exception:
            pass
    info = _symbol_info.get(symbol, {})
    f = info.get("filters", {}).get("MIN_NOTIONAL", {})
    return float(f.get("notional", 100))


def _get_min_qty(symbol: str) -> float:
    global _symbol_info
    if not _symbol_info:
        try:
            _symbol_info = get_symbol_info()
        except Exception:
            pass
    info = _symbol_info.get(symbol, {})
    f = info.get("filters", {}).get("LOT_SIZE", {})
    return float(f.get("minQty", 0.001))


def _make_client_id(prefix: str = "cat") -> str:
    return f"{prefix}_{str(uuid.uuid4())[:8]}"


class OrderError(Exception):
    """Raised when order validation fails with a user-friendly message."""
    pass


def open_position(
    symbol: str,
    direction: str,
    leverage: int,
    usdt_amount: float,
    current_price: float,
    signal_tick: int = 0,
    signal_scenario: str = "",
    order_type: str = "MARKET",
    limit_price: float = 0,
) -> Optional[Dict]:
    """Submit an order to open a position."""
    from data.db import get_open_positions, get_setting
    from config import TRADE_DEFAULTS

    open_pos = get_open_positions()
    max_positions = int(get_setting("max_open_positions", str(TRADE_DEFAULTS["max_open_positions"])))
    if len(open_pos) >= max_positions:
        raise OrderError(f"Max {max_positions} positions reached")

    for p in open_pos:
        if p["symbol"] == symbol and p["direction"] == direction and p["status"] == "OPEN":
            raise OrderError(f"Already have {direction} on {symbol}")

    try:
        _set_margin_type(symbol, "ISOLATED")
    except Exception:
        pass

    try:
        _set_leverage(symbol, leverage)
    except Exception as e:
        print(f"[OrderMgr] Leverage set failed: {e}")

    side = "BUY" if direction == "LONG" else "SELL"
    notional = usdt_amount * leverage
    qty = _round_qty(symbol, notional / current_price)

    min_notional = _get_min_notional(symbol)
    if notional < min_notional:
        min_usdt = min_notional / leverage
        raise OrderError(
            f"Amount too small. {symbol} requires min ${min_notional:.0f} notional "
            f"(${min_usdt:.1f} USDT at {leverage}x leverage)"
        )
    if qty <= 0:
        min_usdt = current_price * _get_min_qty(symbol) / leverage
        raise OrderError(
            f"Amount too small for {symbol}. Minimum ~${min_usdt:.1f} USDT at {leverage}x"
        )

    client_oid = _make_client_id("entry")
    pos_id = str(uuid.uuid4())[:8]
    now_ms = int(time.time() * 1000)

    order_record = {
        "id": str(uuid.uuid4())[:8],
        "binance_order_id": 0,
        "client_order_id": client_oid,
        "symbol": symbol,
        "side": side,
        "order_type": order_type,
        "quantity": qty,
        "price": _round_price(symbol, limit_price) if limit_price else 0,
        "stop_price": 0,
        "expected_price": current_price,
        "filled_qty": 0,
        "avg_fill_price": 0,
        "status": "PENDING",
        "reduce_only": 0,
        "position_id": pos_id,
        "purpose": "ENTRY",
        "created_at": now_ms,
        "updated_at": now_ms,
        "error_msg": "",
        "slippage_pct": 0,
        "leverage": leverage,
        "signal_tick": signal_tick,
        "signal_scenario": signal_scenario,
    }
    save_order(order_record)

    try:
        result = place_order(
            symbol, side, order_type, quantity=qty,
            price=_round_price(symbol, limit_price) if limit_price else 0,
            client_order_id=client_oid,
        )

        binance_oid = int(result.get("orderId", 0))
        remote_status = result.get("status", "NEW")
        filled_qty = float(result.get("executedQty", 0))
        avg_price = float(result.get("avgPrice", 0))

        update_order_status(
            order_record["id"], remote_status,
            binance_order_id=binance_oid,
            filled_qty=filled_qty,
            avg_fill_price=avg_price,
        )

        if remote_status == "FILLED" and filled_qty > 0 and avg_price > 0:
            slippage = abs(avg_price - current_price) / current_price * 100
            update_order_status(
                order_record["id"], "FILLED",
                slippage_pct=round(slippage, 4),
            )
            pos = _create_position_from_fill(
                pos_id, symbol, direction, leverage, avg_price, filled_qty,
                signal_tick, signal_scenario, now_ms,
            )
            _place_server_sl(symbol, direction, avg_price, leverage)
            return pos
        else:
            order_record["binance_order_id"] = binance_oid
            if _order_tracker and order_type == "MARKET":
                _order_tracker.register_pending_market(order_record["id"])
            return order_record

    except Exception as e:
        update_order_status(order_record["id"], "ERROR", error_msg=str(e)[:200])
        print(f"[OrderMgr] Order failed: {e}")
        _attempt_recovery(order_record, symbol, client_oid)
        return None


def close_position_order(pos: Dict, exit_reason: str = "MANUAL") -> Optional[Dict]:
    """Submit a market order to close a position."""
    symbol = pos["symbol"]
    direction = pos["direction"]
    qty = abs(pos["quantity"])

    side = "SELL" if direction == "LONG" else "BUY"
    client_oid = _make_client_id("exit")
    now_ms = int(time.time() * 1000)
    current_price = 0
    try:
        current_price = get_mark_price(symbol)
    except Exception:
        pass

    order_record = {
        "id": str(uuid.uuid4())[:8],
        "binance_order_id": 0,
        "client_order_id": client_oid,
        "symbol": symbol,
        "side": side,
        "order_type": "MARKET",
        "quantity": _round_qty(symbol, qty),
        "price": 0,
        "stop_price": 0,
        "expected_price": current_price,
        "filled_qty": 0,
        "avg_fill_price": 0,
        "status": "PENDING",
        "reduce_only": 1,
        "position_id": pos["id"],
        "purpose": exit_reason,
        "created_at": now_ms,
        "updated_at": now_ms,
        "error_msg": "",
        "slippage_pct": 0,
    }
    save_order(order_record)

    try:
        result = place_order(
            symbol, side, "MARKET",
            quantity=_round_qty(symbol, qty),
            reduce_only=True,
            client_order_id=client_oid,
        )

        binance_oid = int(result.get("orderId", 0))
        remote_status = result.get("status", "NEW")
        filled_qty = float(result.get("executedQty", 0))
        avg_price = float(result.get("avgPrice", 0))

        update_order_status(
            order_record["id"], remote_status,
            binance_order_id=binance_oid,
            filled_qty=filled_qty,
            avg_fill_price=avg_price,
        )

        if remote_status == "FILLED" and avg_price > 0:
            slippage = abs(avg_price - current_price) / current_price * 100 if current_price else 0
            update_order_status(
                order_record["id"], "FILLED",
                slippage_pct=round(slippage, 4),
            )
            trade = _build_trade_from_fill(pos, avg_price, filled_qty, exit_reason, slippage)
            save_trade(trade)
            close_position(pos["id"])
            return trade
        else:
            if _order_tracker:
                _order_tracker.register_pending_market(order_record["id"])
            return order_record

    except Exception as e:
        update_order_status(order_record["id"], "ERROR", error_msg=str(e)[:200])
        print(f"[OrderMgr] Close order failed: {e}")
        return None


def scale_in_order(pos: Dict, usdt_amount: float, current_price: float) -> Optional[Dict]:
    """Submit a market order for scale-in on existing position."""
    symbol = pos["symbol"]
    direction = pos["direction"]
    side = "BUY" if direction == "LONG" else "SELL"
    add_qty = _round_qty(symbol, usdt_amount * pos["leverage"] / current_price)
    if add_qty <= 0:
        return None

    client_oid = _make_client_id("scale")
    now_ms = int(time.time() * 1000)

    order_record = {
        "id": str(uuid.uuid4())[:8],
        "binance_order_id": 0,
        "client_order_id": client_oid,
        "symbol": symbol,
        "side": side,
        "order_type": "MARKET",
        "quantity": add_qty,
        "price": 0,
        "stop_price": 0,
        "expected_price": current_price,
        "filled_qty": 0,
        "avg_fill_price": 0,
        "status": "PENDING",
        "reduce_only": 0,
        "position_id": pos["id"],
        "purpose": "SCALE_IN",
        "created_at": now_ms,
        "updated_at": now_ms,
        "error_msg": "",
        "slippage_pct": 0,
    }
    save_order(order_record)

    try:
        result = place_order(symbol, side, "MARKET", quantity=add_qty, client_order_id=client_oid)
        binance_oid = int(result.get("orderId", 0))
        remote_status = result.get("status", "NEW")
        filled_qty = float(result.get("executedQty", 0))
        avg_price = float(result.get("avgPrice", 0))

        update_order_status(
            order_record["id"], remote_status,
            binance_order_id=binance_oid,
            filled_qty=filled_qty,
            avg_fill_price=avg_price,
        )

        if remote_status == "FILLED" and avg_price > 0:
            old_qty = pos["quantity"]
            new_qty = old_qty + filled_qty
            new_avg = (pos["avg_price"] * old_qty + avg_price * filled_qty) / new_qty
            was_trail_active = bool(pos.get("trail_active", 0))
            pos["quantity"] = new_qty
            pos["avg_price"] = round(new_avg, 8)
            pos["num_entries"] = pos.get("num_entries", 1) + 1
            pos["peak_price"] = new_avg
            pos["trail_active"] = 0
            save_position(pos)
            _update_server_sl(symbol, pos["direction"], new_avg, pos["leverage"])
            if was_trail_active:
                print(f"[OrderMgr] Scale-in reset trailing: {symbol} new_avg={new_avg:.6f}")
            print(f"[OrderMgr] Scale-in filled: {symbol} +{filled_qty} qty, "
                  f"avg {pos['avg_price']:.6f}, entries={pos['num_entries']}")
            return pos
        else:
            if _order_tracker:
                _order_tracker.register_pending_market(order_record["id"])
            return order_record

    except Exception as e:
        update_order_status(order_record["id"], "ERROR", error_msg=str(e)[:200])
        print(f"[OrderMgr] Scale-in order failed: {e}")
        return None


def partial_close_order(pos: Dict, close_pct: float = 50.0) -> Optional[Dict]:
    """Submit a market order for cycle sell (partial close). Records PnL."""
    symbol = pos["symbol"]
    direction = pos["direction"]
    close_qty = _round_qty(symbol, pos["quantity"] * close_pct / 100)
    if close_qty <= 0:
        return None

    side = "SELL" if direction == "LONG" else "BUY"
    client_oid = _make_client_id("cycle")
    now_ms = int(time.time() * 1000)
    current_price = 0
    try:
        current_price = get_mark_price(symbol)
    except Exception:
        pass

    order_record = {
        "id": str(uuid.uuid4())[:8],
        "binance_order_id": 0,
        "client_order_id": client_oid,
        "symbol": symbol,
        "side": side,
        "order_type": "MARKET",
        "quantity": close_qty,
        "price": 0,
        "stop_price": 0,
        "expected_price": current_price,
        "filled_qty": 0,
        "avg_fill_price": 0,
        "status": "PENDING",
        "reduce_only": 1,
        "position_id": pos["id"],
        "purpose": "CYCLE_SELL",
        "created_at": now_ms,
        "updated_at": now_ms,
        "error_msg": "",
        "slippage_pct": 0,
    }
    save_order(order_record)

    try:
        result = place_order(symbol, side, "MARKET", quantity=close_qty,
                             reduce_only=True, client_order_id=client_oid)
        binance_oid = int(result.get("orderId", 0))
        remote_status = result.get("status", "NEW")
        filled_qty = float(result.get("executedQty", 0))
        avg_price = float(result.get("avgPrice", 0))

        update_order_status(
            order_record["id"], remote_status,
            binance_order_id=binance_oid,
            filled_qty=filled_qty,
        )

        if remote_status == "FILLED":
            if avg_price > 0 and filled_qty > 0:
                slippage = abs(avg_price - current_price) / current_price * 100 if current_price else 0
                trade = _build_trade_from_fill(pos, avg_price, filled_qty, "CYCLE_SELL", slippage)
                save_trade(trade)

            pos["quantity"] = pos["quantity"] - filled_qty
            pos["cycles"] = pos.get("cycles", 0) + 1
            pos["num_entries"] = 1
            from data.db import get_setting
            cycle_cd = int(get_setting("cycle_cooldown_sec", "60"))
            pos["cycle_cooldown_until"] = int(time.time()) + cycle_cd
            try:
                save_position(pos)
            except Exception as save_err:
                print(f"[OrderMgr] cycle sell save_position error: {save_err}")
            return pos
        else:
            if _order_tracker:
                _order_tracker.register_pending_market(order_record["id"])
            return order_record

    except Exception as e:
        update_order_status(order_record["id"], "ERROR", error_msg=str(e)[:200])
        return None


def cancel_open_order(order_id: str) -> bool:
    """Cancel a pending/new order on Binance."""
    order = get_order(order_id)
    if not order or order["status"] not in ("NEW", "PARTIALLY_FILLED", "PENDING"):
        return False
    binance_oid = order.get("binance_order_id", 0)
    if binance_oid:
        try:
            cancel_order(order["symbol"], binance_oid)
        except Exception as e:
            print(f"[OrderMgr] Cancel failed: {e}")
            return False
    update_order_status(order_id, "CANCELED")
    return True


# --- Internal helpers ---

def _create_position_from_fill(
    pos_id: str, symbol: str, direction: str, leverage: int,
    fill_price: float, fill_qty: float,
    signal_tick: int, signal_scenario: str, entry_time: int,
) -> Dict:
    pos = {
        "id": pos_id,
        "symbol": symbol,
        "direction": direction,
        "leverage": leverage,
        "entry_price": fill_price,
        "avg_price": fill_price,
        "quantity": fill_qty,
        "unrealized_pnl": 0,
        "entry_time": entry_time,
        "signal_tick": signal_tick,
        "signal_scenario": signal_scenario,
        "status": "OPEN",
        "num_entries": 1,
        "cycles": 0,
        "peak_price": fill_price,
        "trail_active": 0,
        "last_entry_tick": signal_tick,
    }
    save_position(pos)
    return pos


def _build_trade_from_fill(
    pos: Dict, exit_price: float, qty: float,
    exit_reason: str, slippage_pct: float,
) -> Dict:
    avg_price = pos["avg_price"]
    leverage = pos["leverage"]
    direction = pos["direction"]

    if direction == "LONG":
        price_pnl_pct = (exit_price - avg_price) / avg_price * 100
    else:
        price_pnl_pct = (avg_price - exit_price) / avg_price * 100

    fee_pct = STRATEGY["fee_pct"] * leverage * 2
    margin_pnl_pct = price_pnl_pct * leverage - fee_pct
    realized_pnl = avg_price * qty * margin_pnl_pct / 100 / leverage
    fee = avg_price * qty * fee_pct / 100 / leverage

    return {
        "id": str(uuid.uuid4())[:8],
        "symbol": pos["symbol"],
        "direction": direction,
        "leverage": leverage,
        "entry_price": pos["entry_price"],
        "exit_price": exit_price,
        "quantity": qty,
        "entry_time": pos["entry_time"],
        "exit_time": int(time.time() * 1000),
        "realized_pnl": round(realized_pnl, 4),
        "pnl_pct": round(margin_pnl_pct, 4),
        "fee": round(fee, 4),
        "exit_reason": exit_reason,
        "signal_tick": pos.get("signal_tick", 0),
        "signal_scenario": pos.get("signal_scenario", ""),
        "num_entries": pos.get("num_entries", 1),
        "cycles": pos.get("cycles", 0),
        "notes": "",
        "slippage_pct": round(slippage_pct, 4),
    }


def _place_server_sl(symbol: str, direction: str, avg_price: float, leverage: int):
    try:
        from data.db import get_setting
        sl_pct = float(get_setting("sl_pct", str(STRATEGY.get("sl_pct", 5.0))))
        is_long = direction == "LONG"
        if is_long:
            stop_price = _round_price(symbol, avg_price * (1 - sl_pct / 100))
            side = "SELL"
        else:
            stop_price = _round_price(symbol, avg_price * (1 + sl_pct / 100))
            side = "BUY"
        place_order(symbol, side, "STOP_MARKET", stop_price=stop_price, reduce_only=True)
        print(f"[OrderMgr] Server SL placed: {symbol} {side} @ {stop_price}")
    except Exception as e:
        print(f"[OrderMgr] Server SL failed: {e}")


def _update_server_sl(symbol: str, direction: str, new_avg: float, leverage: int):
    """Cancel existing STOP_MARKET orders only, then place new SL."""
    try:
        orders = get_open_orders(symbol)
        for o in orders:
            if o.get("type") == "STOP_MARKET":
                try:
                    cancel_order(symbol, int(o["orderId"]))
                except Exception:
                    pass
    except Exception:
        pass
    _place_server_sl(symbol, direction, new_avg, leverage)


def _attempt_recovery(order_record: Dict, symbol: str, client_oid: str):
    """If API call failed, check if the order actually went through on Binance."""
    try:
        orders = get_open_orders(symbol)
        for o in orders:
            if o.get("clientOrderId") == client_oid:
                update_order_status(
                    order_record["id"], o.get("status", "NEW"),
                    binance_order_id=int(o.get("orderId", 0)),
                )
                print(f"[OrderMgr] Recovered order {client_oid}: {o.get('status')}")
                return
    except Exception:
        pass
