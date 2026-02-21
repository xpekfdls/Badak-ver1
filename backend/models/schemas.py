from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional
import time
import uuid


@dataclass
class ScenarioSummary:
    scenario: str
    count: int = 0
    pct: float = 0
    avg_pnl: float = 0
    median_pnl: float = 0
    avg_max_gain: float = 0
    avg_max_loss: float = 0
    avg_hold: int = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class Signal:
    id: str
    symbol: str
    direction: str
    tick_count: int
    price: float
    timestamp: float
    timeframe: str = "5m"
    scenarios: List[ScenarioSummary] = field(default_factory=list)
    total_cases: int = 0

    def to_dict(self):
        return {
            "id": self.id,
            "symbol": self.symbol,
            "direction": self.direction,
            "tick_count": self.tick_count,
            "price": self.price,
            "timestamp": self.timestamp,
            "timeframe": self.timeframe,
            "scenarios": [s.to_dict() for s in self.scenarios],
            "total_cases": self.total_cases,
        }


@dataclass
class CoinState:
    symbol: str
    price: float = 0
    bear_tick: int = 0
    bull_tick: int = 0
    volume: float = 0
    last_update: float = 0
    change_24h: float = 0

    def to_dict(self):
        return asdict(self)


@dataclass
class Position:
    id: str
    symbol: str
    direction: str
    leverage: int = 10
    entry_price: float = 0
    avg_price: float = 0
    quantity: float = 0
    unrealized_pnl: float = 0
    entry_time: int = 0
    signal_tick: int = 0
    signal_scenario: str = ""
    status: str = "OPEN"
    num_entries: int = 1
    cycles: int = 0
    peak_price: float = 0
    trail_active: int = 0

    def to_dict(self):
        d = asdict(self)
        if self.avg_price > 0 and self.quantity > 0:
            mark = self.unrealized_pnl / (self.avg_price * self.quantity) * 100 if self.avg_price * self.quantity else 0
            d["pnl_pct"] = round(mark, 2)
        else:
            d["pnl_pct"] = 0
        return d

    @staticmethod
    def from_dict(d: dict) -> Position:
        return Position(**{k: v for k, v in d.items() if k in Position.__dataclass_fields__})


@dataclass
class TradeRecord:
    id: str
    symbol: str
    direction: str
    leverage: int = 10
    entry_price: float = 0
    exit_price: float = 0
    quantity: float = 0
    entry_time: int = 0
    exit_time: int = 0
    realized_pnl: float = 0
    pnl_pct: float = 0
    fee: float = 0
    exit_reason: str = ""
    signal_tick: int = 0
    signal_scenario: str = ""
    num_entries: int = 1
    cycles: int = 0
    notes: str = ""

    def to_dict(self):
        return asdict(self)
