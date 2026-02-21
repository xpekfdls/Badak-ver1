from __future__ import annotations

import sqlite3
import json
from typing import Optional, List, Dict
from config import DB_PATH, MONITOR_DB_PATH


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_monitor_conn() -> Optional[sqlite3.Connection]:
    if MONITOR_DB_PATH.exists():
        conn = sqlite3.connect(str(MONITOR_DB_PATH))
        conn.execute("PRAGMA journal_mode=WAL")
        return conn
    return None


def init_db() -> None:
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            leverage INTEGER DEFAULT 10,
            entry_price REAL NOT NULL,
            avg_price REAL NOT NULL,
            quantity REAL NOT NULL,
            unrealized_pnl REAL DEFAULT 0,
            entry_time INTEGER NOT NULL,
            signal_tick INTEGER DEFAULT 0,
            signal_scenario TEXT DEFAULT '',
            status TEXT DEFAULT 'OPEN',
            num_entries INTEGER DEFAULT 1,
            cycles INTEGER DEFAULT 0,
            peak_price REAL DEFAULT 0,
            trail_active INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            leverage INTEGER DEFAULT 10,
            entry_price REAL NOT NULL,
            exit_price REAL NOT NULL,
            quantity REAL NOT NULL,
            entry_time INTEGER NOT NULL,
            exit_time INTEGER NOT NULL,
            realized_pnl REAL DEFAULT 0,
            pnl_pct REAL DEFAULT 0,
            fee REAL DEFAULT 0,
            exit_reason TEXT DEFAULT '',
            signal_tick INTEGER DEFAULT 0,
            signal_scenario TEXT DEFAULT '',
            num_entries INTEGER DEFAULT 1,
            cycles INTEGER DEFAULT 0,
            notes TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_time ON trades(exit_time);
        CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
        CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            binance_order_id INTEGER DEFAULT 0,
            client_order_id TEXT DEFAULT '',
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            quantity REAL NOT NULL,
            price REAL DEFAULT 0,
            stop_price REAL DEFAULT 0,
            expected_price REAL DEFAULT 0,
            filled_qty REAL DEFAULT 0,
            avg_fill_price REAL DEFAULT 0,
            status TEXT DEFAULT 'PENDING',
            reduce_only INTEGER DEFAULT 0,
            position_id TEXT DEFAULT '',
            purpose TEXT DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            error_msg TEXT DEFAULT '',
            slippage_pct REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
        CREATE INDEX IF NOT EXISTS idx_orders_binance ON orders(binance_order_id);
        CREATE INDEX IF NOT EXISTS idx_orders_client ON orders(client_order_id);
    """)

    # Migrate: add slippage column to trades if missing
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN slippage_pct REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migrate: add last_entry_tick to positions for scale-in interval tracking
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN last_entry_tick INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migrate: add cycle_cooldown_until to positions for cycle sell cooldown
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN cycle_cooldown_until INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass

    # Migrate: add leverage/signal columns to orders if missing
    for col, typedef in [
        ("leverage", "INTEGER DEFAULT 0"),
        ("signal_tick", "INTEGER DEFAULT 0"),
        ("signal_scenario", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE orders ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass

    conn.commit()
    conn.close()


def get_setting(key: str, default: str = "") -> str:
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    conn = get_conn()
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


def get_settings_dict() -> Dict:
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    conn.close()
    result = {}
    for k, v in rows:
        try:
            result[k] = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            result[k] = v
    return result


def save_position(pos: Dict) -> None:
    conn = get_conn()
    cols = list(pos.keys())
    placeholders = ",".join(["?"] * len(cols))
    col_names = ",".join(cols)
    conn.execute(
        f"INSERT OR REPLACE INTO positions({col_names}) VALUES({placeholders})",
        list(pos.values()),
    )
    conn.commit()
    conn.close()


def get_open_positions() -> List[Dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='OPEN' ORDER BY entry_time DESC"
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM positions LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def close_position(pos_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE positions SET status='CLOSED' WHERE id=?", (pos_id,))
    conn.commit()
    conn.close()


def save_trade(trade: Dict) -> None:
    conn = get_conn()
    cols = list(trade.keys())
    placeholders = ",".join(["?"] * len(cols))
    col_names = ",".join(cols)
    conn.execute(
        f"INSERT OR REPLACE INTO trades({col_names}) VALUES({placeholders})",
        list(trade.values()),
    )
    conn.commit()
    conn.close()


def get_trades(limit: int = 100, offset: int = 0, symbol: str = "") -> List[Dict]:
    conn = get_conn()
    q = "SELECT * FROM trades WHERE 1=1"
    p: list = []
    if symbol:
        q += " AND symbol=?"
        p.append(symbol)
    q += " ORDER BY exit_time DESC LIMIT ? OFFSET ?"
    p.extend([limit, offset])
    rows = conn.execute(q, p).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM trades LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def get_trade_stats() -> Dict:
    conn = get_conn()
    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as total_pnl,
            AVG(realized_pnl) as avg_pnl,
            MAX(realized_pnl) as best_trade,
            MIN(realized_pnl) as worst_trade,
            AVG(pnl_pct) as avg_pnl_pct
        FROM trades
    """).fetchone()
    conn.close()

    if not row or row[0] == 0:
        return {"total": 0, "wins": 0, "win_rate": 0, "total_pnl": 0,
                "avg_pnl": 0, "best_trade": 0, "worst_trade": 0, "avg_pnl_pct": 0,
                "profit_factor": 0, "max_drawdown": 0, "avg_hold_time": 0}

    total_wins = row[1] or 0
    stats = {
        "total": row[0],
        "wins": total_wins,
        "losses": row[0] - total_wins,
        "win_rate": round(total_wins / row[0] * 100, 1),
        "total_pnl": round(row[2] or 0, 2),
        "avg_pnl": round(row[3] or 0, 2),
        "best_trade": round(row[4] or 0, 2),
        "worst_trade": round(row[5] or 0, 2),
        "avg_pnl_pct": round(row[6] or 0, 2),
    }

    # Profit Factor
    conn2 = get_conn()
    pf_row = conn2.execute("""
        SELECT
            COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN realized_pnl ELSE 0 END), 0),
            COALESCE(ABS(SUM(CASE WHEN realized_pnl < 0 THEN realized_pnl ELSE 0 END)), 0.01)
        FROM trades
    """).fetchone()
    stats["profit_factor"] = round(pf_row[0] / max(pf_row[1], 0.01), 2)

    # Max Drawdown
    dd_rows = conn2.execute(
        "SELECT realized_pnl FROM trades ORDER BY exit_time"
    ).fetchall()
    conn2.close()

    peak = 0.0
    running = 0.0
    max_dd = 0.0
    for (pnl,) in dd_rows:
        running += pnl
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd
    stats["max_drawdown"] = round(max_dd, 2)

    return stats


def get_daily_stats() -> List[Dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            date(exit_time / 1000, 'unixepoch') as day,
            COUNT(*) as trades,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as pnl
        FROM trades
        GROUP BY day
        ORDER BY day DESC
        LIMIT 30
    """).fetchall()
    conn.close()
    return [
        {"date": r[0], "trades": r[1], "wins": r[2], "pnl": round(r[3] or 0, 2)}
        for r in rows
    ]


def get_today_pnl() -> float:
    """Get sum of today's realized PnL."""
    conn = get_conn()
    row = conn.execute("""
        SELECT COALESCE(SUM(realized_pnl), 0)
        FROM trades
        WHERE date(exit_time / 1000, 'unixepoch') = date('now')
    """).fetchone()
    conn.close()
    return float(row[0]) if row else 0.0


def get_consecutive_losses() -> int:
    """Count consecutive losses from most recent trade backwards."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT realized_pnl FROM trades ORDER BY exit_time DESC LIMIT 20"
    ).fetchall()
    conn.close()
    count = 0
    for (pnl,) in rows:
        if pnl < 0:
            count += 1
        else:
            break
    return count


def get_symbol_stats() -> List[Dict]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            symbol,
            COUNT(*) as trades,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(realized_pnl) as pnl,
            AVG(pnl_pct) as avg_pnl_pct
        FROM trades
        GROUP BY symbol
        ORDER BY pnl DESC
    """).fetchall()
    conn.close()
    return [
        {"symbol": r[0], "trades": r[1], "wins": r[2], "pnl": round(r[3] or 0, 2),
         "win_rate": round((r[2] or 0) / r[1] * 100, 1), "avg_pnl_pct": round(r[4] or 0, 2)}
        for r in rows
    ]


# --- Orders CRUD ---

def save_order(order: Dict) -> None:
    conn = get_conn()
    cols = list(order.keys())
    placeholders = ",".join(["?"] * len(cols))
    col_names = ",".join(cols)
    conn.execute(
        f"INSERT OR REPLACE INTO orders({col_names}) VALUES({placeholders})",
        list(order.values()),
    )
    conn.commit()
    conn.close()


def get_order(order_id: str) -> Optional[Dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        conn.close()
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM orders LIMIT 0").description]
    conn.close()
    return dict(zip(cols, row))


def get_order_by_binance_id(binance_order_id: int) -> Optional[Dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM orders WHERE binance_order_id=?", (binance_order_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM orders LIMIT 0").description]
    conn.close()
    return dict(zip(cols, row))


def get_order_by_client_id(client_order_id: str) -> Optional[Dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    cols = [d[0] for d in conn.execute("SELECT * FROM orders LIMIT 0").description]
    conn.close()
    return dict(zip(cols, row))


def get_active_orders() -> List[Dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE status IN ('PENDING','NEW','PARTIALLY_FILLED') "
        "ORDER BY created_at DESC"
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM orders LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def get_orders_for_position(position_id: str) -> List[Dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE position_id=? ORDER BY created_at",
        (position_id,),
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM orders LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def update_order_status(order_id: str, status: str, **kwargs) -> None:
    conn = get_conn()
    import time as _t
    sets = ["status=?", "updated_at=?"]
    vals: list = [status, int(_t.time() * 1000)]
    for k, v in kwargs.items():
        sets.append(f"{k}=?")
        vals.append(v)
    vals.append(order_id)
    conn.execute(f"UPDATE orders SET {','.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()


def get_recent_orders(limit: int = 50) -> List[Dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    cols = [d[0] for d in conn.execute("SELECT * FROM orders LIMIT 0").description]
    conn.close()
    return [dict(zip(cols, r)) for r in rows]


def lookup_scenario_stats(symbol: str, direction: str, entry_tick: int, timeframe: str = "5m") -> List[Dict]:
    conn = get_monitor_conn()
    if not conn:
        return []
    try:
        rows = conn.execute(
            "SELECT scenario, count, pct, avg_pnl, median_pnl, "
            "avg_max_gain, avg_max_loss, avg_hold "
            "FROM scenario_stats WHERE symbol=? AND timeframe=? AND direction=? AND min_tick=? "
            "ORDER BY scenario",
            (symbol, timeframe, direction, entry_tick),
        ).fetchall()
        return [
            {"scenario": r[0], "count": r[1], "pct": r[2], "avg_pnl": r[3],
             "median_pnl": r[4], "avg_max_gain": r[5], "avg_max_loss": r[6], "avg_hold": r[7]}
            for r in rows
        ]
    except Exception:
        return []
    finally:
        conn.close()
