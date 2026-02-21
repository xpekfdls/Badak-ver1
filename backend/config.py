from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
DB_PATH = PROJECT_DIR / "trader.db"

BINANCE_FUTURES_URL = "https://fapi.binance.com"
BINANCE_WS_URL = "wss://fstream.binance.com"

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET", "")

MONITOR_DB_PATH = Path("e:/ToyProject/coin-monitor/monitor.db")

TOP_N_COINS = 30
TIMEFRAME = "5m"
CANDLE_BUFFER_SIZE = 100

FILTER = {
    "min_daily_range_pct": 10.0,
    "min_quote_volume": 100_000_000,
    "min_age_days": 90,
    "max_coins": 30,
}

STRATEGY = {
    "tick_threshold_pct": 0.5,
    "reset_ratio": 0.7,
    "entry_tick": 3,
    "leverage": 10,
    "tp_pct": 1.0,
    "sl_pct": 5.0,
    "trail_activation_pct": 1.0,
    "trail_distance_pct": 0.5,
    "exit_mode": "trailing",
    "max_entries": 3,
    "entry_interval": 2,
    "scale_multiplier": 2.0,
    "cycle_mode": True,
    "cycle_sell_pct": 50.0,
    "max_hold": 300,
    "fee_pct": 0.04,
}

SCENARIO_LOOKAHEAD = 300
SCENARIO_RULES = {
    "A_immediate_max_candles": 10,
    "A_immediate_min_gain": 1.0,
    "B_sideways_max_candles": 60,
    "B_sideways_min_gain": 0.5,
    "C_deeper_min_drop": -1.0,
}

TRADE_DEFAULTS = {
    "position_size_pct": 11.0,
    "max_open_positions": 5,
    "buy_mode": "semi",                # auto / semi / manual
    "sell_mode": "trailing",           # trailing / manual
    "operating_fund_mode": "fixed",    # "fixed" = 고정금액, "balance" = 전재산
    "operating_fund_amount": 100.0,    # fixed 모드일 때 운용 금액 (USDT)
    "target_symbol": "",               # 매매 대상 코인 ("" = 제한 없음)
    "cooldown_seconds": 300,           # 청산 후 재진입 대기 (초)
}
