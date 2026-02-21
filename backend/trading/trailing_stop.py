from __future__ import annotations

import asyncio
import time
from typing import Dict, List, Callable, Optional

from data.db import save_position, get_open_positions, get_setting
from config import STRATEGY

DB_SAVE_INTERVAL = 5


class TrailingStopEngine:
    """
    Monitors open positions and triggers exit when trailing stop or SL is hit.
    Runs as an async loop checking prices every second.
    """

    def __init__(self):
        self._on_exit: Optional[Callable] = None
        self._on_cycle_sell: Optional[Callable] = None
        self._on_log: Optional[Callable] = None
        self._running = False
        self._positions: Dict[str, Dict] = {}
        self._get_price: Optional[Callable] = None
        self._last_db_save: Dict[str, float] = {}
        self._cycle_cooldowns: Dict[str, float] = {}  # pos_id -> cooldown timestamp

    def set_price_source(self, fn: Callable):
        self._get_price = fn

    def set_exit_handler(self, fn: Callable):
        self._on_exit = fn

    def set_cycle_sell_handler(self, fn: Callable):
        self._on_cycle_sell = fn

    def set_log_handler(self, fn: Callable):
        self._on_log = fn

    def track_position(self, pos: Dict):
        self._positions[pos["id"]] = pos

    def untrack_position(self, pos_id: str):
        self._positions.pop(pos_id, None)
        self._last_db_save.pop(pos_id, None)
        self._cycle_cooldowns.pop(pos_id, None)

    def update_position(self, pos: Dict):
        if pos["id"] in self._positions:
            self._positions[pos["id"]] = pos

    async def start(self):
        self._running = True
        for pos in get_open_positions():
            self._positions[pos["id"]] = pos
        n = len(self._positions)
        print(f"[Trail] Engine started, tracking {n} position(s)")
        for pid, p in self._positions.items():
            print(f"  [Trail] {p['symbol']} {p['direction']} avg={p['avg_price']} qty={p['quantity']}")

        tick = 0
        while self._running:
            await self._check_all()
            tick += 1
            if tick % 30 == 0 and self._positions:
                for pid, p in self._positions.items():
                    sym = p["symbol"]
                    pr = self._get_price(sym) if self._get_price else 0
                    ta = "ON" if p.get("trail_active") else "off"
                    pk = p.get("peak_price", 0)
                    pnl = p.get("unrealized_pnl", 0)
                    print(f"  [Trail] {sym} price={pr:.4f} peak={pk:.4f} trail={ta} pnl={pnl:+.2f}%")
                    if self._on_log:
                        self._on_log("STATUS", f"{sym} price=${pr:.4f} peak=${pk:.4f} trail={ta} pnl={pnl:+.2f}%")
            await asyncio.sleep(1)

    async def stop(self):
        self._running = False

    def _should_save_to_db(self, pos_id: str, force: bool = False) -> bool:
        if force:
            self._last_db_save[pos_id] = time.time()
            return True
        now = time.time()
        last = self._last_db_save.get(pos_id, 0)
        if now - last >= DB_SAVE_INTERVAL:
            self._last_db_save[pos_id] = now
            return True
        return False

    async def _check_all(self):
        if not self._get_price:
            return

        to_remove = []
        for pos_id, pos in list(self._positions.items()):
            try:
                if pos.get("status") != "OPEN":
                    to_remove.append(pos_id)
                    continue

                symbol = pos["symbol"]
                price = self._get_price(symbol)
                if price <= 0:
                    continue

                action = self._evaluate(pos, price)
                if action == "EXIT":
                    print(f"  [Trail] EXIT triggered: {symbol} @ {price}")
                    if self._on_log:
                        self._on_log("TRAIL", f"{symbol} trailing stop EXIT @ ${price:.4f}")
                    to_remove.append(pos_id)
                    if self._on_exit:
                        await self._on_exit(pos, "TRAIL")
                elif action == "SL":
                    print(f"  [Trail] SL triggered: {symbol} @ {price}")
                    if self._on_log:
                        self._on_log("TRAIL", f"{symbol} stop-loss HIT @ ${price:.4f}")
                    to_remove.append(pos_id)
                    if self._on_exit:
                        await self._on_exit(pos, "SL")
                elif action == "CYCLE_SELL":
                    cycle_cooldown_sec = int(get_setting("cycle_cooldown_sec", "60"))
                    self._cycle_cooldowns[pos_id] = time.time() + cycle_cooldown_sec
                    pos["cycle_cooldown_until"] = int(time.time()) + cycle_cooldown_sec
                    print(f"  [Trail] CYCLE_SELL triggered: {symbol} @ {price} (cooldown {cycle_cooldown_sec}s)")
                    if self._on_log:
                        self._on_log("TRAIL", f"{symbol} cycle sell @ ${price:.4f} (next in {cycle_cooldown_sec}s)")
                    if self._on_cycle_sell:
                        await self._on_cycle_sell(pos)
            except Exception as e:
                print(f"  [Trail] Error processing {pos.get('symbol', '?')}: {e}")

        for pid in to_remove:
            self._positions.pop(pid, None)
            self._last_db_save.pop(pid, None)
            self._cycle_cooldowns.pop(pid, None)

    def _evaluate(self, pos: Dict, price: float) -> str:
        direction = pos["direction"]
        avg_price = pos["avg_price"]
        leverage = pos.get("leverage", STRATEGY["leverage"])
        is_long = direction == "LONG"

        # Liquidation check
        liq_margin = 0.005
        liq_pct = (1.0 / leverage - liq_margin) * 100
        if is_long and price <= avg_price * (1 - liq_pct / 100):
            return "SL"
        if not is_long and price >= avg_price * (1 + liq_pct / 100):
            return "SL"

        # Dust position cleanup: close entirely if notional < $10
        pos_notional = pos.get("quantity", 0) * price
        if pos_notional < 10.0 and pos.get("cycles", 0) > 0:
            print(f"  [Trail] Dust cleanup: {pos['symbol']} notional=${pos_notional:.2f} → EXIT")
            if self._on_log:
                self._on_log("TRAIL", f"{pos['symbol']} 잔여 포지션 정리 (${pos_notional:.2f})")
            return "EXIT"

        # Stop loss - read from DB settings (user-configurable)
        sl_pct = float(get_setting("sl_pct", str(STRATEGY["sl_pct"])))
        if is_long and price <= avg_price * (1 - sl_pct / 100):
            return "SL"
        if not is_long and price >= avg_price * (1 + sl_pct / 100):
            return "SL"

        # Trailing stop logic
        if STRATEGY["exit_mode"] != "trailing":
            tp_pct = STRATEGY["tp_pct"]
            if is_long and price >= avg_price * (1 + tp_pct / 100):
                return "EXIT"
            if not is_long and price <= avg_price * (1 - tp_pct / 100):
                return "EXIT"
            return ""

        act_pct = float(get_setting("trail_activation_pct", str(STRATEGY["trail_activation_pct"])))
        default_dist_pct = float(get_setting("trail_distance_pct", str(STRATEGY["trail_distance_pct"])))
        tier2_act_pct = float(get_setting("trail_tier2_activation_pct", str(STRATEGY.get("trail_tier2_activation_pct", 3.0))))
        tier2_dist_pct = float(get_setting("trail_tier2_distance_pct", str(STRATEGY.get("trail_tier2_distance_pct", 1.5))))
        tier3_act_pct = float(get_setting("trail_tier3_activation_pct", str(STRATEGY.get("trail_tier3_activation_pct", 5.0))))
        tier3_dist_pct = float(get_setting("trail_tier3_distance_pct", str(STRATEGY.get("trail_tier3_distance_pct", 0.5))))

        peak = pos.get("peak_price", avg_price)
        trail_active = bool(pos.get("trail_active", 0))
        trail_tier = int(pos.get("trail_tier", 1))

        peak_changed = False
        state_changed = False

        # EMA price calculation (smoothing out spikes)
        alpha = float(get_setting("peak_ema_alpha", str(STRATEGY.get("peak_ema_alpha", 0.1))))
        if "ema_price" not in pos:
            pos["ema_price"] = price
        else:
            pos["ema_price"] = alpha * price + (1 - alpha) * pos["ema_price"]
        ema_price = pos["ema_price"]

        if is_long:
            if ema_price > peak:
                pos["peak_price"] = ema_price
                peak = ema_price
                peak_changed = True

            profit_pct = (peak - avg_price) / avg_price * 100
            if not trail_active and profit_pct >= act_pct:
                pos["trail_active"] = 1
                trail_active = True
                state_changed = True
                print(f"  [Trail] ACTIVATED {pos['symbol']} profit={profit_pct:.2f}% peak={peak}")
                if self._on_log:
                    self._on_log("TRAIL", f"{pos['symbol']} trailing activated at +{profit_pct:.2f}%, peak=${peak:.4f}")

            dist_pct = default_dist_pct
            new_tier = 1
            if profit_pct >= tier3_act_pct:
                dist_pct = tier3_dist_pct
                new_tier = 3
            elif profit_pct >= tier2_act_pct:
                dist_pct = tier2_dist_pct
                new_tier = 2
            
            if new_tier > trail_tier:
                pos["trail_tier"] = new_tier
                trail_tier = new_tier
                state_changed = True
                print(f"  [Trail] TIER {new_tier} ACTIVATED {pos['symbol']} dist={dist_pct}%")
                if self._on_log:
                    self._on_log("TRAIL", f"{pos['symbol']} trailing Tier {new_tier} activated. Distance: {dist_pct}%")

            if trail_active:
                trail_stop = peak * (1 - dist_pct / 100)
                if price <= trail_stop:
                    return "EXIT"
        else:
            if ema_price < peak or peak <= 0:
                pos["peak_price"] = ema_price
                peak = ema_price
                peak_changed = True

            profit_pct = (avg_price - peak) / avg_price * 100
            if not trail_active and profit_pct >= act_pct:
                pos["trail_active"] = 1
                trail_active = True
                state_changed = True
                print(f"  [Trail] ACTIVATED {pos['symbol']} profit={profit_pct:.2f}% peak={peak}")
                if self._on_log:
                    self._on_log("TRAIL", f"{pos['symbol']} trailing activated at +{profit_pct:.2f}%, peak=${peak:.4f}")

            dist_pct = default_dist_pct
            new_tier = 1
            if profit_pct >= tier3_act_pct:
                dist_pct = tier3_dist_pct
                new_tier = 3
            elif profit_pct >= tier2_act_pct:
                dist_pct = tier2_dist_pct
                new_tier = 2
            
            if new_tier > trail_tier:
                pos["trail_tier"] = new_tier
                trail_tier = new_tier
                state_changed = True
                print(f"  [Trail] TIER {new_tier} ACTIVATED {pos['symbol']} dist={dist_pct}%")
                if self._on_log:
                    self._on_log("TRAIL", f"{pos['symbol']} trailing Tier {new_tier} activated. Distance: {dist_pct}%")

            if trail_active:
                trail_stop = peak * (1 + dist_pct / 100)
                if price >= trail_stop:
                    return "EXIT"

        # Save to DB only on state change or periodically
        if state_changed or (peak_changed and self._should_save_to_db(pos["id"])):
            try:
                save_position(pos)
            except Exception as e:
                print(f"  [Trail] save_position error: {e}")

        # Cycle sell check: scale-ins exist, price returned to avg, cooldown passed
        cycle_mode = get_setting("cycle_mode", str(STRATEGY.get("cycle_mode", True)))
        if pos.get("num_entries", 1) > 1 and cycle_mode not in (False, "false", "False", "0", ""):
            pos_id = pos.get("id", "")
            now = time.time()

            # Engine-level cooldown (in-memory, authoritative)
            engine_cooldown = self._cycle_cooldowns.get(pos_id, 0)
            db_cooldown = pos.get("cycle_cooldown_until", 0)
            cooldown_until = max(engine_cooldown, db_cooldown)

            if now > cooldown_until:
                min_notional = 5.0
                pos_notional = pos.get("quantity", 0) * price
                if pos_notional < min_notional:
                    return ""

                if is_long and price >= avg_price:
                    return "CYCLE_SELL"
                if not is_long and price <= avg_price:
                    return "CYCLE_SELL"

        # Update unrealized PnL (in-memory only)
        if is_long:
            unrealized = (price - avg_price) / avg_price * 100 * leverage
        else:
            unrealized = (avg_price - price) / avg_price * 100 * leverage
        pos["unrealized_pnl"] = round(unrealized, 4)

        return ""
