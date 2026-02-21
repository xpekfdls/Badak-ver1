from __future__ import annotations

import json
import time
from typing import Dict, Optional, Callable

from models.schemas import Signal
from trading.order_manager import (
    open_position, close_position_order, scale_in_order, partial_close_order,
)
from trading.binance_account import get_account_balance
from data.db import get_open_positions, get_setting, set_setting, save_position, get_today_pnl, get_consecutive_losses
from config import STRATEGY, TRADE_DEFAULTS


class AutoTrader:
    def __init__(self):
        self._broadcast: Optional[Callable] = None
        self._trailing_engine = None
        self._cooldowns: Dict[str, float] = {}
        self._log: Optional[Callable] = None
        self._load_cooldowns()

    def set_broadcast(self, fn: Callable):
        self._broadcast = fn

    def set_trailing_engine(self, engine):
        self._trailing_engine = engine

    def set_log_handler(self, fn: Callable):
        self._log = fn

    # --- Settings helpers ---

    def _get_buy_mode(self) -> str:
        return get_setting("buy_mode", TRADE_DEFAULTS["buy_mode"])

    def _get_position_size_pct(self) -> float:
        return float(get_setting("position_size_pct", str(TRADE_DEFAULTS["position_size_pct"])))

    def _get_max_positions(self) -> int:
        return int(get_setting("max_open_positions", str(TRADE_DEFAULTS["max_open_positions"])))

    def _get_leverage(self) -> int:
        return int(get_setting("leverage", str(STRATEGY["leverage"])))

    def _get_target_symbol(self) -> str:
        return get_setting("target_symbol", TRADE_DEFAULTS["target_symbol"]).strip().upper()

    def _get_cooldown_seconds(self) -> int:
        return int(get_setting("cooldown_seconds", str(TRADE_DEFAULTS["cooldown_seconds"])))

    def _get_operating_fund(self) -> float:
        """Returns the operating fund amount in USDT."""
        mode = get_setting("operating_fund_mode", TRADE_DEFAULTS["operating_fund_mode"])
        if mode == "fixed":
            return float(get_setting("operating_fund_amount", str(TRADE_DEFAULTS["operating_fund_amount"])))
        try:
            balance = get_account_balance()
            return float(balance["availableBalance"])
        except Exception as e:
            print(f"[AutoTrader] operating fund API failed: {e}")
            if self._log:
                self._log("ERROR", f"운용자금 조회 실패: {e}")
            return 0

    def _load_cooldowns(self):
        try:
            raw = get_setting("trade_cooldowns", "{}")
            data = json.loads(raw)
            now = time.time()
            self._cooldowns = {k: v for k, v in data.items() if v > now}
        except Exception:
            self._cooldowns = {}

    def _persist_cooldowns(self):
        now = time.time()
        active = {k: v for k, v in self._cooldowns.items() if v > now}
        set_setting("trade_cooldowns", json.dumps(active))

    def _is_in_cooldown(self, symbol: str) -> bool:
        until = self._cooldowns.get(symbol, 0)
        if time.time() < until:
            return True
        if until > 0:
            self._cooldowns.pop(symbol, None)
        return False

    def _set_cooldown(self, symbol: str):
        cd = self._get_cooldown_seconds()
        if cd > 0:
            self._cooldowns[symbol] = time.time() + cd
            self._persist_cooldowns()
            print(f"[AutoTrader] Cooldown set: {symbol} for {cd}s")

    # --- Signal handling ---

    async def on_signal(self, signal: Signal):
        mode = self._get_buy_mode()
        if mode == "manual":
            if self._log:
                self._log("DECIDE", f"{signal.symbol} {signal.direction} {signal.tick_count}T → SKIP (manual mode)")
            return

        target = self._get_target_symbol()
        if target and signal.symbol != target:
            if self._log:
                self._log("DECIDE", f"{signal.symbol} → SKIP (target={target})")
            return

        if mode in ("auto", "semi"):
            await self.auto_scale_in_check(signal)
        if mode == "auto":
            await self._auto_buy(signal)

    def _check_daily_loss_limit(self) -> bool:
        """Returns True if daily loss limit exceeded -> should stop trading."""
        limit = float(get_setting("daily_loss_limit", "0"))
        if limit <= 0:
            return False
        today_pnl = get_today_pnl()
        if today_pnl < -abs(limit):
            return True
        consec = get_consecutive_losses()
        max_consec = int(get_setting("max_consecutive_losses", "5"))
        if max_consec > 0 and consec >= max_consec:
            return True
        return False

    async def _auto_buy(self, signal: Signal):
        if self._check_daily_loss_limit():
            if self._get_buy_mode() != "manual":
                set_setting("buy_mode", "manual")
                print("[AutoTrader] Daily loss limit reached, switching to manual")
                if self._log:
                    self._log("DECIDE", f"{signal.symbol} → BLOCKED (daily loss limit)")
                if self._broadcast:
                    await self._broadcast({
                        "type": "warning",
                        "data": {"message": "일일 손실 한도 도달 - 자동매매 중지"}
                    })
            return

        if self._is_in_cooldown(signal.symbol):
            if self._log:
                self._log("DECIDE", f"{signal.symbol} → SKIP (cooldown)")
            return

        open_pos = get_open_positions()
        from data.db import get_active_orders
        active_orders = get_active_orders()
        pending_entries = [o for o in active_orders if o.get("purpose") == "ENTRY"]
        
        total_slots_used = len(open_pos) + len(pending_entries)
        max_positions = self._get_max_positions()
        
        if total_slots_used >= max_positions:
            if self._log:
                self._log("DECIDE", f"{signal.symbol} → SKIP (max positions {total_slots_used}/{max_positions})")
            return
        for p in open_pos:
            if p["symbol"] == signal.symbol and p["direction"] == signal.direction:
                if self._log:
                    self._log("DECIDE", f"{signal.symbol} → SKIP (already {signal.direction})")
                return

        fund = self._get_operating_fund()
        usdt_amount = fund * self._get_position_size_pct() / 100.0

        try:
            balance = get_account_balance()
            available = float(balance["availableBalance"])
        except Exception:
            available = 0

        if usdt_amount > available:
            print(f"[AutoTrader] Insufficient balance: need {usdt_amount:.2f}, have {available:.2f}")
            if self._log:
                self._log("DECIDE", f"{signal.symbol} → FAIL (balance: ${available:.2f} < ${usdt_amount:.2f})")
            if self._broadcast:
                await self._broadcast({
                    "type": "warning",
                    "data": {"message": f"잔고 부족: 필요 {usdt_amount:.2f} USDT, 가용 {available:.2f} USDT"}
                })
            return
        if usdt_amount < 5:
            return

        scenario_str = ""
        if signal.scenarios:
            scenario_str = ",".join(f"{s.scenario}:{s.pct}%" for s in signal.scenarios)

        lev = self._get_leverage()
        if self._log:
            self._log("DECIDE", f"{signal.symbol} {signal.direction} → AUTO BUY "
                      f"${usdt_amount:.2f} margin (x{lev}=${usdt_amount*lev:.0f} position)")

        result = open_position(
            symbol=signal.symbol,
            direction=signal.direction,
            leverage=self._get_leverage(),
            usdt_amount=usdt_amount,
            current_price=signal.price,
            signal_tick=signal.tick_count,
            signal_scenario=scenario_str,
        )
        if result and self._trailing_engine and result.get("status") == "OPEN":
            self._trailing_engine.track_position(result)
        if result and self._broadcast:
            if result.get("status") == "OPEN":
                await self._broadcast({"type": "position_opened", "data": result})
            else:
                await self._broadcast({"type": "order_submitted", "data": result})
        print(f"[AutoTrader] Submitted {signal.direction} {signal.symbol} "
              f"@ {signal.price} (${usdt_amount:.2f})")

    # --- Manual operations ---

    async def manual_open(
        self, symbol: str, direction: str, usdt_amount: float,
        current_price: float, leverage: int = 0, signal_tick: int = 0,
        order_type: str = "MARKET", limit_price: float = 0,
    ) -> Optional[Dict]:
        if leverage <= 0:
            leverage = self._get_leverage()
        result = open_position(
            symbol=symbol, direction=direction, leverage=leverage,
            usdt_amount=usdt_amount, current_price=current_price,
            signal_tick=signal_tick, order_type=order_type, limit_price=limit_price,
        )
        if result and self._trailing_engine and result.get("status") == "OPEN":
            self._trailing_engine.track_position(result)
        return result

    async def manual_close(self, pos_id: str, reason: str = "MANUAL") -> Optional[Dict]:
        positions = get_open_positions()
        pos = next((p for p in positions if p["id"] == pos_id), None)
        if not pos:
            return None
        result = close_position_order(pos, reason)
        if self._trailing_engine:
            self._trailing_engine.untrack_position(pos_id)
        if result and self._broadcast:
            if result.get("exit_reason") or result.get("realized_pnl") is not None:
                await self._broadcast({"type": "position_closed", "data": result})
        self._set_cooldown(pos["symbol"])
        return result

    async def handle_trailing_exit(self, pos: Dict, reason: str):
        result = close_position_order(pos, reason)
        if result and self._broadcast:
            if result.get("exit_reason") or result.get("realized_pnl") is not None:
                await self._broadcast({"type": "position_closed", "data": result})
        self._set_cooldown(pos["symbol"])

    async def handle_cycle_sell(self, pos: Dict):
        cycle_sell_pct = float(get_setting("cycle_sell_pct", str(STRATEGY.get("cycle_sell_pct", 50.0))))
        result = partial_close_order(pos, cycle_sell_pct)
        if self._trailing_engine and result and isinstance(result, dict) and result.get("status") == "OPEN":
            self._trailing_engine.update_position(result)
        if result and self._broadcast:
            await self._broadcast({"type": "position_updated", "data": result})

    # --- Scale-in ---

    async def manual_scale_in(self, pos_id: str, usdt_amount: float, current_price: float) -> Optional[Dict]:
        positions = get_open_positions()
        pos = next((p for p in positions if p["id"] == pos_id), None)
        if not pos:
            return None
        max_entries = int(get_setting("max_entries", str(STRATEGY.get("max_entries", 3))))
        if pos.get("num_entries", 1) >= max_entries:
            return None
        result = scale_in_order(pos, usdt_amount, current_price)
        if self._trailing_engine and result and result.get("status") == "OPEN":
            self._trailing_engine.update_position(result)
        return result

    async def auto_scale_in_check(self, signal: Signal):
        positions = get_open_positions()
        max_entries = int(get_setting("max_entries", str(STRATEGY.get("max_entries", 3))))
        entry_interval = int(get_setting("entry_interval", str(STRATEGY.get("entry_interval", 2))))
        scale_mult = float(get_setting("scale_multiplier", str(STRATEGY.get("scale_multiplier", 2.0))))

        for pos in positions:
            if pos["symbol"] != signal.symbol or pos["direction"] != signal.direction:
                continue
            if pos.get("status") != "OPEN":
                continue
            if pos.get("num_entries", 1) >= max_entries:
                continue

            last_tick = pos.get("last_entry_tick", STRATEGY.get("entry_tick", 3))
            if signal.tick_count - last_tick < entry_interval:
                continue

            entry_num = pos.get("num_entries", 1)
            leverage = pos.get("leverage", self._get_leverage())

            # Base margin always explicit: Fund * (PosSize / 100)
            fund = self._get_operating_fund()
            base_margin = fund * (self._get_position_size_pct() / 100.0)

            # Example: Base 70, scale_mult 2.0
            # entry_num 1 (1st scale-in, total 2) => 70 * (2.0 ^ 1) = 140
            # entry_num 2 (2nd scale-in, total 3) => 70 * (2.0 ^ 2) = 280
            usdt_amount = base_margin * (scale_mult ** entry_num)

            try:
                balance = get_account_balance()
                available = float(balance["availableBalance"])
            except Exception:
                continue

            if usdt_amount > available:
                print(f"[AutoTrader] Scale-in skipped: need {usdt_amount:.2f}, have {available:.2f}")
                if self._broadcast:
                    await self._broadcast({
                        "type": "warning",
                        "data": {"message": f"물타기 잔고 부족: 필요 {usdt_amount:.2f} USDT"}
                    })
                continue
            if usdt_amount < 5:
                continue

            if self._log:
                self._log("DECIDE", f"{signal.symbol} scale-in #{entry_num+1}: "
                          f"${usdt_amount:.2f} margin (x{leverage}=${usdt_amount*leverage:.0f} position)")

            result = scale_in_order(pos, usdt_amount, signal.price)
            if result and isinstance(result, dict):
                if result.get("status") == "OPEN":
                    result["last_entry_tick"] = signal.tick_count
                    save_position(result)
                    if self._trailing_engine:
                        self._trailing_engine.update_position(result)
                if self._broadcast:
                    await self._broadcast({"type": "position_updated", "data": result})
            print(f"[AutoTrader] Scale-in #{entry_num+1} on {signal.symbol} "
                  f"(tick {signal.tick_count}, ${usdt_amount:.2f} margin, x{leverage}=${usdt_amount*leverage:.0f})")

    # --- Emergency ---

    async def emergency_close_all(self) -> Dict:
        """Kill switch: close all positions safely."""
        set_setting("buy_mode", "manual")
        print("[AutoTrader] EMERGENCY: buy_mode set to manual")

        results = {"closed": [], "failed": []}
        positions = get_open_positions()

        for pos in positions:
            try:
                if self._trailing_engine:
                    self._trailing_engine.untrack_position(pos["id"])
                result = close_position_order(pos, "EMERGENCY")
                if result:
                    results["closed"].append(pos["symbol"])
                    if self._broadcast:
                        if result.get("exit_reason") or result.get("realized_pnl") is not None:
                            await self._broadcast({"type": "position_closed", "data": result})
                else:
                    results["failed"].append(pos["symbol"])
            except Exception as e:
                results["failed"].append(f"{pos['symbol']}: {e}")
                print(f"[EMERGENCY] Failed to close {pos['symbol']}: {e}")

        if self._broadcast:
            await self._broadcast({"type": "emergency_complete", "data": results})

        print(f"[EMERGENCY] Done: closed={results['closed']}, failed={results['failed']}")
        return results
