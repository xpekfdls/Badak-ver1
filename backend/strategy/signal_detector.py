from __future__ import annotations

import asyncio
import time
import uuid
from typing import List, Dict, Callable, Optional
from collections import deque

import pandas as pd

from config import STRATEGY, TIMEFRAME, CANDLE_BUFFER_SIZE, FILTER
from data.binance_rest import get_top_symbols, download_klines, fetch_24h_changes
from data.market_ws import kline_stream, mark_price_stream
from data.db import lookup_scenario_stats
from strategy.tick_counter import count_ticks
from models.schemas import Signal, ScenarioSummary, CoinState


TF_SECONDS = {"1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800}


class SignalDetector:
    def __init__(self):
        self.symbols: List[str] = []
        self.timeframe: str = TIMEFRAME
        self.buffers: Dict[str, deque] = {}
        self.states: Dict[str, CoinState] = {}
        self.signals: List[Signal] = []
        self._listeners: List[Callable] = []
        self._running = False
        self._ws_task: Optional[asyncio.Task] = None
        self._mark_price_task: Optional[asyncio.Task] = None
        self._current_prices: Dict[str, float] = {}

    def add_listener(self, fn: Callable):
        self._listeners.append(fn)

    def remove_listener(self, fn: Callable):
        if fn in self._listeners:
            self._listeners.remove(fn)

    def get_price(self, symbol: str) -> float:
        return self._current_prices.get(symbol, 0)

    async def start(self):
        loop = asyncio.get_event_loop()
        print("[Detector] Fetching top symbols...")
        coin_list = await loop.run_in_executor(
            None,
            lambda: get_top_symbols(
                n=FILTER["max_coins"],
                min_age_days=FILTER["min_age_days"],
                min_daily_range_pct=FILTER["min_daily_range_pct"],
                min_quote_volume=FILTER["min_quote_volume"],
            ),
        )
        self.symbols = [c["symbol"] for c in coin_list]
        print(f"[Detector] Monitoring {len(self.symbols)} coins "
              f"(range>={FILTER['min_daily_range_pct']}%, vol>=${FILTER['min_quote_volume']/1e6:.0f}M)")
        for c in coin_list[:10]:
            print(f"  {c['symbol']:12s}  range={c['daily_range_pct']:5.1f}%  "
                  f"vol=${c['quote_volume']/1e6:.0f}M  chg={c['price_change_pct']:+.1f}%")
        if len(coin_list) > 10:
            print(f"  ... and {len(coin_list) - 10} more")

        self._init_buffers()
        for c in coin_list:
            sym = c["symbol"]
            if sym in self.states:
                self.states[sym].change_24h = c.get("price_change_pct", 0)
        await loop.run_in_executor(None, self._load_24h_changes)
        await self._load_initial_candles()

        self._running = True
        self._ws_task = asyncio.create_task(
            kline_stream(self.symbols, self.timeframe, self._on_kline)
        )
        self._mark_price_task = asyncio.create_task(
            mark_price_stream(self.symbols, self._on_mark_price)
        )
        self._change_refresh_task = asyncio.create_task(self._refresh_24h_loop())
        print(f"[Detector] WebSocket started ({self.timeframe}, kline + markPrice)")

    async def stop(self):
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        if self._mark_price_task:
            self._mark_price_task.cancel()
            self._mark_price_task = None
        if hasattr(self, '_change_refresh_task') and self._change_refresh_task:
            self._change_refresh_task.cancel()
            self._change_refresh_task = None

    async def switch_timeframe(self, new_tf: str):
        if new_tf == self.timeframe:
            return
        print(f"[Detector] Switching {self.timeframe} -> {new_tf}")
        self.timeframe = new_tf

        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None

        self._init_buffers()
        await self._load_initial_candles()
        self.signals.clear()

        self._ws_task = asyncio.create_task(
            kline_stream(self.symbols, self.timeframe, self._on_kline)
        )
        self._restart_mark_price_stream()

    async def add_symbol(self, symbol: str):
        sym = symbol.upper()
        if sym in self.symbols:
            return False
        self.symbols.append(sym)
        self.buffers[sym] = deque(maxlen=CANDLE_BUFFER_SIZE)
        self.states[sym] = CoinState(symbol=sym)

        end_ts = int(time.time() * 1000)
        tf_sec = TF_SECONDS.get(self.timeframe, 300)
        start_ts = end_ts - CANDLE_BUFFER_SIZE * tf_sec * 1000
        try:
            df = download_klines(sym, self.timeframe, start_ts, end_ts)
            for _, row in df.iterrows():
                self.buffers[sym].append({
                    "timestamp": int(row["timestamp"]),
                    "open": row["open"], "high": row["high"],
                    "low": row["low"], "close": row["close"],
                    "volume": row["volume"],
                })
            if self.buffers[sym]:
                self.states[sym].price = self.buffers[sym][-1]["close"]
        except Exception as e:
            print(f"[Detector] Failed to load {sym}: {e}")

        if self._ws_task:
            self._ws_task.cancel()
        self._ws_task = asyncio.create_task(
            kline_stream(self.symbols, self.timeframe, self._on_kline)
        )
        self._restart_mark_price_stream()
        return True

    async def remove_symbol(self, symbol: str):
        sym = symbol.upper()
        if sym not in self.symbols:
            return False
        self.symbols.remove(sym)
        self.buffers.pop(sym, None)
        self.states.pop(sym, None)

        if self._ws_task:
            self._ws_task.cancel()
        self._ws_task = asyncio.create_task(
            kline_stream(self.symbols, self.timeframe, self._on_kline)
        )
        self._restart_mark_price_stream()
        return True

    def update_price(self, symbol: str, price: float):
        """External caller can push a mark price update."""
        if price > 0:
            self._current_prices[symbol] = price

    async def _on_mark_price(self, symbol: str, price: float):
        if price > 0:
            self._current_prices[symbol] = price

    def _restart_mark_price_stream(self):
        if self._mark_price_task:
            self._mark_price_task.cancel()
        self._mark_price_task = asyncio.create_task(
            mark_price_stream(self.symbols, self._on_mark_price)
        )

    def _load_24h_changes(self):
        changes = fetch_24h_changes(self.symbols)
        for sym, pct in changes.items():
            if sym in self.states:
                self.states[sym].change_24h = pct

    async def _refresh_24h_loop(self):
        while self._running:
            await asyncio.sleep(60)
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._load_24h_changes)
            except Exception:
                pass

    def get_state(self) -> List[dict]:
        return [s.to_dict() for s in self.states.values()]

    def get_signals(self, limit: int = 100) -> List[dict]:
        return [s.to_dict() for s in self.signals[-limit:]]

    def _init_buffers(self):
        for sym in self.symbols:
            self.buffers[sym] = deque(maxlen=CANDLE_BUFFER_SIZE)
            if sym not in self.states:
                self.states[sym] = CoinState(symbol=sym)
            self.states[sym].bear_tick = 0
            self.states[sym].bull_tick = 0

    async def _load_initial_candles(self):
        end_ts = int(time.time() * 1000)
        tf_sec = TF_SECONDS.get(self.timeframe, 300)
        start_ts = end_ts - CANDLE_BUFFER_SIZE * tf_sec * 1000

        for sym in self.symbols:
            try:
                df = download_klines(sym, self.timeframe, start_ts, end_ts)
                self.buffers[sym].clear()
                for _, row in df.iterrows():
                    self.buffers[sym].append({
                        "timestamp": int(row["timestamp"]),
                        "open": row["open"], "high": row["high"],
                        "low": row["low"], "close": row["close"],
                        "volume": row["volume"],
                    })
                if len(self.buffers[sym]) > 0:
                    price = self.buffers[sym][-1]["close"]
                    self.states[sym].price = price
                    self._current_prices[sym] = price
            except Exception as e:
                print(f"[Detector] Failed to load {sym}: {e}")

        print(f"[Detector] Initial candles loaded ({self.timeframe}, {len(self.symbols)} coins)")

    async def _on_kline(self, symbol: str, kline: dict):
        if symbol not in self.buffers:
            return

        state = self.states[symbol]
        state.price = kline["close"]
        state.volume = kline["volume"]
        state.last_update = time.time()
        self._current_prices[symbol] = kline["close"]

        if kline["is_closed"]:
            buf = self.buffers[symbol]
            candle = {k: kline[k] for k in ("timestamp", "open", "high", "low", "close", "volume")}

            if buf and buf[-1]["timestamp"] == candle["timestamp"]:
                buf[-1] = candle
            else:
                buf.append(candle)

            await self._check_signal(symbol)

    async def _check_signal(self, symbol: str):
        buf = self.buffers[symbol]
        if len(buf) < 20:
            return

        df = pd.DataFrame(list(buf))
        df = count_ticks(df, STRATEGY["tick_threshold_pct"], STRATEGY["reset_ratio"])

        last = df.iloc[-1]
        entry_tick = STRATEGY["entry_tick"]

        for direction, tick_col, new_col in [
            ("bear", "bear_tick", "bear_new_tick"),
            ("bull", "bull_tick", "bull_new_tick"),
        ]:
            if last[new_col] and last[tick_col] >= entry_tick:
                state = self.states[symbol]
                if direction == "bear":
                    state.bear_tick = int(last[tick_col])
                else:
                    state.bull_tick = int(last[tick_col])

                scenario_data = lookup_scenario_stats(
                    symbol, direction, entry_tick, self.timeframe
                )
                scenarios = [ScenarioSummary(**s) for s in scenario_data]
                total_cases = sum(s.count for s in scenarios)

                signal = Signal(
                    id=str(uuid.uuid4())[:8],
                    symbol=symbol,
                    direction="LONG" if direction == "bear" else "SHORT",
                    tick_count=int(last[tick_col]),
                    price=float(last["close"]),
                    timestamp=time.time(),
                    timeframe=self.timeframe,
                    scenarios=scenarios,
                    total_cases=total_cases,
                )
                self.signals.append(signal)
                if len(self.signals) > 500:
                    self.signals = self.signals[-500:]

                for fn in self._listeners:
                    try:
                        await fn(signal)
                    except Exception:
                        pass

        self.states[symbol].bear_tick = int(last["bear_tick"])
        self.states[symbol].bull_tick = int(last["bull_tick"])
