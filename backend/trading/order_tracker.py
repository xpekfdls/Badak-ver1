from __future__ import annotations

import asyncio
import time
import uuid
from typing import Dict, Optional, Callable, List

from data.db import (
    save_order, get_order, get_order_by_binance_id, get_order_by_client_id,
    get_active_orders, update_order_status,
    save_position, get_open_positions, close_position, save_trade,
)
from trading.binance_account import get_order_status as _query_order, get_positions as get_binance_positions
from config import STRATEGY


class OrderTracker:
    """
    Processes Binance User Data Stream ORDER_TRADE_UPDATE events
    to confirm fills, handle partial fills, and detect cancellations.
    Also runs a periodic position sync as a safety net.
    """

    def __init__(self):
        self._on_position_opened: Optional[Callable] = None
        self._on_position_closed: Optional[Callable] = None
        self._on_position_updated: Optional[Callable] = None
        self._on_order_update: Optional[Callable] = None
        self._trailing_engine = None
        self._get_price: Optional[Callable] = None
        self._pending_market_orders: Dict[str, float] = {}

    def set_callbacks(
        self,
        on_position_opened: Callable = None,
        on_position_closed: Callable = None,
        on_position_updated: Callable = None,
        on_order_update: Callable = None,
    ):
        self._on_position_opened = on_position_opened
        self._on_position_closed = on_position_closed
        self._on_position_updated = on_position_updated
        self._on_order_update = on_order_update

    def set_trailing_engine(self, engine):
        self._trailing_engine = engine

    def set_price_source(self, fn: Callable):
        self._get_price = fn

    async def on_user_event(self, event: Dict):
        """Entry point for all User Data Stream events."""
        event_type = event.get("e", "")
        if event_type == "ORDER_TRADE_UPDATE":
            await self._handle_order_update(event.get("o", {}))
        elif event_type == "ACCOUNT_UPDATE":
            pass

    async def _handle_order_update(self, o: Dict):
        """Process a single ORDER_TRADE_UPDATE payload from Binance."""
        binance_oid = int(o.get("i", 0))
        client_oid = o.get("c", "")
        symbol = o.get("s", "")
        side = o.get("S", "")
        order_type = o.get("o", "")
        status = o.get("X", "")
        filled_qty = float(o.get("z", 0))
        avg_price = float(o.get("ap", 0))
        last_filled_qty = float(o.get("l", 0))
        last_filled_price = float(o.get("L", 0))
        orig_qty = float(o.get("q", 0))

        # Find our order record
        order = get_order_by_client_id(client_oid) if client_oid else None
        if not order and binance_oid:
            order = get_order_by_binance_id(binance_oid)

        if not order:
            return

        # Calculate slippage
        slippage_pct = 0.0
        expected = order.get("expected_price", 0)
        if expected and avg_price and expected > 0:
            slippage_pct = abs(avg_price - expected) / expected * 100

        # Update order record
        update_order_status(
            order["id"], status,
            filled_qty=filled_qty,
            avg_fill_price=avg_price,
            binance_order_id=binance_oid,
            slippage_pct=round(slippage_pct, 4),
        )

        # Remove from pending market tracking
        self._pending_market_orders.pop(order["id"], None)

        # Broadcast order state change to frontend
        if self._on_order_update:
            updated = get_order(order["id"])
            await self._on_order_update(updated)

        purpose = order.get("purpose", "")
        position_id = order.get("position_id", "")

        if status == "FILLED":
            await self._on_filled(order, avg_price, filled_qty, slippage_pct, purpose, position_id)
        elif status == "PARTIALLY_FILLED":
            await self._on_partial_fill(order, avg_price, filled_qty, orig_qty, purpose, position_id)
        elif status in ("CANCELED", "EXPIRED"):
            await self._on_canceled(order, purpose, position_id)

    async def _on_filled(
        self, order: Dict, fill_price: float, fill_qty: float,
        slippage_pct: float, purpose: str, position_id: str,
    ):
        symbol = order["symbol"]
        side = order["side"]
        leverage = int(order.get("leverage", STRATEGY["leverage"]))

        if purpose == "ENTRY":
            direction = "LONG" if side == "BUY" else "SHORT"
            pos_id = position_id or str(uuid.uuid4())[:8]
            pos = {
                "id": pos_id,
                "symbol": symbol,
                "direction": direction,
                "leverage": leverage,
                "entry_price": fill_price,
                "avg_price": fill_price,
                "quantity": fill_qty,
                "unrealized_pnl": 0,
                "entry_time": int(time.time() * 1000),
                "signal_tick": order.get("signal_tick", 0),
                "signal_scenario": order.get("signal_scenario", ""),
                "status": "OPEN",
                "num_entries": 1,
                "cycles": 0,
                "peak_price": fill_price,
                "trail_active": 0,
            }
            save_position(pos)
            update_order_status(order["id"], "FILLED", position_id=pos_id)

            if self._trailing_engine:
                self._trailing_engine.track_position(pos)
            if self._on_position_opened:
                await self._on_position_opened(pos)

        elif purpose == "SCALE_IN":
            positions = get_open_positions()
            pos = next((p for p in positions if p["id"] == position_id), None)
            if pos:
                old_qty = pos["quantity"]
                new_qty = old_qty + fill_qty
                new_avg = (pos["avg_price"] * old_qty + fill_price * fill_qty) / new_qty
                pos["quantity"] = new_qty
                pos["avg_price"] = round(new_avg, 8)
                pos["num_entries"] = pos.get("num_entries", 1) + 1
                pos["peak_price"] = new_avg
                pos["trail_active"] = 0
                save_position(pos)
                if self._trailing_engine:
                    self._trailing_engine.update_position(pos)
                if self._on_position_updated:
                    await self._on_position_updated(pos)

        elif purpose in ("EXIT", "SL", "TRAIL"):
            positions = get_open_positions()
            pos = next((p for p in positions if p["id"] == position_id), None)
            if pos:
                trade = self._build_trade_record(pos, fill_price, fill_qty, purpose, slippage_pct)
                save_trade(trade)
                close_position(pos["id"])
                if self._trailing_engine:
                    self._trailing_engine.untrack_position(pos["id"])
                if self._on_position_closed:
                    await self._on_position_closed(trade)

        elif purpose == "CYCLE_SELL":
            positions = get_open_positions()
            pos = next((p for p in positions if p["id"] == position_id), None)
            if pos:
                pos["quantity"] = pos["quantity"] - fill_qty
                pos["cycles"] = pos.get("cycles", 0) + 1
                save_position(pos)
                if self._trailing_engine:
                    self._trailing_engine.update_position(pos)
                if self._on_position_updated:
                    await self._on_position_updated(pos)

    async def _on_partial_fill(
        self, order: Dict, avg_price: float, filled_so_far: float,
        orig_qty: float, purpose: str, position_id: str,
    ):
        fill_pct = filled_so_far / orig_qty * 100 if orig_qty else 0
        print(f"[OrderTracker] Partial fill {order['symbol']}: "
              f"{filled_so_far}/{orig_qty} ({fill_pct:.1f}%)")

        # For ENTRY partial fills, create/update position with filled portion
        if purpose == "ENTRY" and filled_so_far > 0:
            positions = get_open_positions()
            existing = next(
                (p for p in positions if p["id"] == position_id), None
            ) if position_id else None

            if not existing:
                direction = "LONG" if order["side"] == "BUY" else "SHORT"
                pos_id = position_id or str(uuid.uuid4())[:8]
                pos = {
                    "id": pos_id,
                    "symbol": order["symbol"],
                    "direction": direction,
                    "leverage": int(order.get("leverage", STRATEGY["leverage"])),
                    "entry_price": avg_price,
                    "avg_price": avg_price,
                    "quantity": filled_so_far,
                    "unrealized_pnl": 0,
                    "entry_time": int(time.time() * 1000),
                    "signal_tick": 0,
                    "signal_scenario": "",
                    "status": "OPEN",
                    "num_entries": 1,
                    "cycles": 0,
                    "peak_price": avg_price,
                    "trail_active": 0,
                }
                save_position(pos)
                update_order_status(order["id"], "PARTIALLY_FILLED", position_id=pos_id)
                if self._trailing_engine:
                    self._trailing_engine.track_position(pos)
                if self._on_position_opened:
                    await self._on_position_opened(pos)
            else:
                existing["quantity"] = filled_so_far
                existing["avg_price"] = avg_price
                save_position(existing)
                if self._trailing_engine:
                    self._trailing_engine.update_position(existing)

    async def _on_canceled(self, order: Dict, purpose: str, position_id: str):
        print(f"[OrderTracker] Order canceled/expired: {order['symbol']} {purpose}")
        if self._on_order_update:
            updated = get_order(order["id"])
            await self._on_order_update(updated)

    def _build_trade_record(
        self, pos: Dict, exit_price: float, qty: float,
        reason: str, slippage_pct: float,
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
            "exit_reason": reason,
            "signal_tick": pos.get("signal_tick", 0),
            "signal_scenario": pos.get("signal_scenario", ""),
            "num_entries": pos.get("num_entries", 1),
            "cycles": pos.get("cycles", 0),
            "notes": "",
            "slippage_pct": round(slippage_pct, 4),
        }

    # --- Market order fallback: poll for fill if User Data Stream is slow ---

    def register_pending_market(self, order_id: str):
        self._pending_market_orders[order_id] = time.time()

    async def check_pending_market_orders(self):
        """Called periodically. For MARKET orders not confirmed within 5s, poll Binance."""
        now = time.time()
        to_remove = []
        for oid, created in list(self._pending_market_orders.items()):
            if now - created < 5:
                continue
            order = get_order(oid)
            if not order or order["status"] in ("FILLED", "CANCELED", "EXPIRED", "ERROR"):
                to_remove.append(oid)
                continue

            binance_oid = order.get("binance_order_id", 0)
            if not binance_oid:
                to_remove.append(oid)
                continue

            try:
                result = _query_order(order["symbol"], binance_oid)
                remote_status = result.get("status", "")
                if remote_status in ("FILLED", "PARTIALLY_FILLED", "CANCELED", "EXPIRED"):
                    filled_qty = float(result.get("executedQty", 0))
                    avg_price = float(result.get("avgPrice", 0))
                    expected = order.get("expected_price", 0)
                    slippage = abs(avg_price - expected) / expected * 100 if expected else 0

                    update_order_status(
                        oid, remote_status,
                        filled_qty=filled_qty,
                        avg_fill_price=avg_price,
                        slippage_pct=round(slippage, 4),
                    )

                    if remote_status == "FILLED":
                        await self._on_filled(
                            order, avg_price, filled_qty, slippage,
                            order.get("purpose", ""), order.get("position_id", ""),
                        )
                    to_remove.append(oid)
            except Exception as e:
                print(f"[OrderTracker] Poll failed for {oid}: {e}")

        for oid in to_remove:
            self._pending_market_orders.pop(oid, None)

    # --- Periodic position sync (safety net) ---

    async def sync_positions(self):
        """Compare Binance positions with local DB and reconcile."""
        try:
            binance_pos = get_binance_positions()
        except Exception as e:
            print(f"[Sync] Binance query failed: {e}")
            return

        local_pos = get_open_positions()
        local_map = {p["symbol"]: p for p in local_pos}
        binance_map = {}

        for bp in binance_pos:
            sym = bp["symbol"]
            amt = float(bp.get("positionAmt", 0))
            if amt == 0:
                continue
            binance_map[sym] = bp

        # Binance has it, local doesn't -> recover
        for sym, bp in binance_map.items():
            if sym not in local_map:
                amt = float(bp.get("positionAmt", 0))
                entry_price = float(bp.get("entryPrice", 0))
                direction = "LONG" if amt > 0 else "SHORT"
                pos = {
                    "id": str(uuid.uuid4())[:8],
                    "symbol": sym,
                    "direction": direction,
                    "leverage": int(bp.get("leverage", 10)),
                    "entry_price": entry_price,
                    "avg_price": entry_price,
                    "quantity": abs(amt),
                    "unrealized_pnl": float(bp.get("unRealizedProfit", 0)),
                    "entry_time": int(time.time() * 1000),
                    "signal_tick": 0,
                    "signal_scenario": "synced",
                    "status": "OPEN",
                    "num_entries": 1,
                    "cycles": 0,
                    "peak_price": entry_price,
                    "trail_active": 0,
                }
                save_position(pos)
                if self._trailing_engine:
                    self._trailing_engine.track_position(pos)
                print(f"[Sync] Recovered: {sym} {direction} {abs(amt)}")

        # Local has it, Binance doesn't -> close stale
        for sym, lp in local_map.items():
            if sym not in binance_map:
                close_position(lp["id"])
                if self._trailing_engine:
                    self._trailing_engine.untrack_position(lp["id"])
                print(f"[Sync] Closed stale: {sym}")

        # Both have it, quantity mismatch -> correct local (preserve trailing state)
        for sym in set(local_map) & set(binance_map):
            bp = binance_map[sym]
            lp = local_map[sym]
            remote_qty = abs(float(bp.get("positionAmt", 0)))
            local_qty = lp["quantity"]
            if abs(remote_qty - local_qty) / max(local_qty, 0.0001) > 0.01:
                mem_pos = None
                if self._trailing_engine:
                    mem_pos = self._trailing_engine._positions.get(lp["id"])

                lp["quantity"] = remote_qty
                lp["avg_price"] = float(bp.get("entryPrice", lp["avg_price"]))

                if mem_pos:
                    lp["peak_price"] = mem_pos.get("peak_price", lp.get("peak_price", lp["avg_price"]))
                    lp["trail_active"] = mem_pos.get("trail_active", lp.get("trail_active", 0))
                    lp["cycle_cooldown_until"] = mem_pos.get("cycle_cooldown_until", lp.get("cycle_cooldown_until", 0))

                save_position(lp)
                if self._trailing_engine:
                    self._trailing_engine.update_position(lp)
                print(f"[Sync] Corrected qty: {sym} {local_qty}->{remote_qty}")
