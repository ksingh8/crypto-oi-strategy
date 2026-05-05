import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager
import os

DB_PATH = os.getenv("DB_PATH", "trading.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    entry_price REAL NOT NULL,
    tp_price REAL NOT NULL,
    sl_price REAL NOT NULL,
    exit_price REAL,
    exit_reason TEXT,
    confidence REAL,
    pnl_pct REAL,
    pnl_usd REAL,
    position_size_usd REAL DEFAULT 100,
    reasons TEXT,
    indicators TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    duration_minutes REAL,
    strategy_version TEXT DEFAULT 'v1_oi'
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    confidence REAL,
    entry_price REAL,
    tp_price REAL,
    sl_price REAL,
    reasons TEXT,
    indicators TEXT,
    acted_on INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    strategy_version TEXT DEFAULT 'v1_oi'
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    price REAL NOT NULL,
    oi REAL,
    funding_rate REAL,
    ls_ratio REAL,
    buy_vol REAL,
    sell_vol REAL,
    taker_ratio REAL,
    timestamp TEXT NOT NULL
);
"""

MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN strategy_version TEXT DEFAULT 'v1_oi'",
    "ALTER TABLE signals ADD COLUMN strategy_version TEXT DEFAULT 'v1_oi'",
]

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        # Run migrations (ignore errors if column already exists)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
                conn.commit()
            except Exception:
                pass  # column already exists

def save_signal(signal, symbol: str, strategy_version: str = 'v2_sr') -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO signals (symbol, direction, confidence, entry_price, tp_price, sl_price,
                                 reasons, indicators, created_at, strategy_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, signal.direction, signal.confidence, signal.entry_price,
              signal.tp_price, signal.sl_price,
              json.dumps(signal.reasons), json.dumps(signal.indicators),
              datetime.utcnow().isoformat(), strategy_version))
        return cur.lastrowid

def open_trade(signal, symbol: str, position_size_usd: float = 100,
               strategy_version: str = 'v2_sr') -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO trades (symbol, direction, status, entry_price, tp_price, sl_price,
                                confidence, reasons, indicators, position_size_usd, opened_at,
                                strategy_version)
            VALUES (?, ?, 'OPEN', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, signal.direction, signal.entry_price, signal.tp_price, signal.sl_price,
              signal.confidence, json.dumps(signal.reasons), json.dumps(signal.indicators),
              position_size_usd, datetime.utcnow().isoformat(), strategy_version))
        return cur.lastrowid

def close_trade(trade_id: int, exit_price: float, exit_reason: str):
    with get_db() as conn:
        trade = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not trade:
            return
        now      = datetime.utcnow()
        opened_at = datetime.fromisoformat(trade["opened_at"])
        duration  = (now - opened_at).total_seconds() / 60
        if trade["direction"] == "LONG":
            pnl_pct = (exit_price - trade["entry_price"]) / trade["entry_price"] * 100
        else:
            pnl_pct = (trade["entry_price"] - exit_price) / trade["entry_price"] * 100
        pnl_usd = trade["position_size_usd"] * pnl_pct / 100
        conn.execute("""
            UPDATE trades SET status = 'CLOSED', exit_price = ?, exit_reason = ?,
                pnl_pct = ?, pnl_usd = ?, closed_at = ?, duration_minutes = ?
            WHERE id = ?
        """, (exit_price, exit_reason, round(pnl_pct, 4), round(pnl_usd, 4),
              now.isoformat(), round(duration, 1), trade_id))

def get_open_trades(symbol: str = None) -> list:
    with get_db() as conn:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' AND symbol = ? ORDER BY opened_at DESC",
                (symbol,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades WHERE status = 'OPEN' ORDER BY opened_at DESC").fetchall()
        return [dict(r) for r in rows]

def get_all_trades(limit: int = 200, strategy_version: str = None) -> list:
    with get_db() as conn:
        if strategy_version and strategy_version != 'all':
            rows = conn.execute(
                "SELECT * FROM trades WHERE strategy_version = ? ORDER BY opened_at DESC LIMIT ?",
                (strategy_version, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM trades ORDER BY opened_at DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["reasons"]    = json.loads(d["reasons"])    if d["reasons"]    else []
            d["indicators"] = json.loads(d["indicators"]) if d["indicators"] else {}
            result.append(d)
        return result

def get_recent_signals(limit: int = 50, strategy_version: str = None) -> list:
    with get_db() as conn:
        if strategy_version and strategy_version != 'all':
            rows = conn.execute(
                "SELECT * FROM signals WHERE strategy_version = ? ORDER BY created_at DESC LIMIT ?",
                (strategy_version, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["reasons"]    = json.loads(d["reasons"])    if d["reasons"]    else []
            d["indicators"] = json.loads(d["indicators"]) if d["indicators"] else {}
            result.append(d)
        return result

def save_price_snapshot(symbol: str, price: float, oi: float = None,
                        funding_rate: float = None, ls_ratio: float = None,
                        buy_vol: float = None, sell_vol: float = None,
                        taker_ratio: float = None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO price_history (symbol, price, oi, funding_rate, ls_ratio,
                                       buy_vol, sell_vol, taker_ratio, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, price, oi, funding_rate, ls_ratio,
              buy_vol, sell_vol, taker_ratio, datetime.utcnow().isoformat()))

def get_stats(strategy_version: str = None) -> dict:
    with get_db() as conn:
        if strategy_version and strategy_version != 'all':
            where = f"WHERE strategy_version = '{strategy_version}'"
        else:
            where = ""
        total      = conn.execute(f"SELECT COUNT(*) as c FROM trades {where}").fetchone()["c"]
        closed     = conn.execute(f"SELECT COUNT(*) as c FROM trades {where} {'AND' if where else 'WHERE'} status = 'CLOSED'").fetchone()["c"]
        open_count = conn.execute(f"SELECT COUNT(*) as c FROM trades {where} {'AND' if where else 'WHERE'} status = 'OPEN'").fetchone()["c"]

        closed_where = (where + " AND status = 'CLOSED'") if where else "WHERE status = 'CLOSED'"
        win_loss = conn.execute(f"""
            SELECT
                SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl_usd) as total_pnl,
                AVG(pnl_pct) as avg_pnl_pct,
                MAX(pnl_pct) as best_trade,
                MIN(pnl_pct) as worst_trade
            FROM trades {closed_where}
        """).fetchone()

        return {
            "total_trades":    total,
            "closed_trades":   closed,
            "open_trades":     open_count,
            "wins":            win_loss["wins"]   or 0,
            "losses":          win_loss["losses"] or 0,
            "win_rate":        round((win_loss["wins"] or 0) / closed * 100, 1) if closed > 0 else 0,
            "total_pnl_usd":   round(win_loss["total_pnl"]   or 0, 2),
            "avg_pnl_pct":     round(win_loss["avg_pnl_pct"] or 0, 3),
            "best_trade_pct":  round(win_loss["best_trade"]  or 0, 3),
            "worst_trade_pct": round(win_loss["worst_trade"] or 0, 3),
            "strategy_version": strategy_version or 'all',
        }

def get_price_history(symbol: str, limit: int = 200) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM price_history WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?
        """, (symbol, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

def had_recent_sl(symbol: str, direction: str, minutes: int = 30,
                  strategy_version: str = None) -> bool:
    with get_db() as conn:
        if strategy_version:
            row = conn.execute(
                """SELECT COUNT(*) AS c FROM trades
                   WHERE symbol = ? AND direction = ? AND exit_reason = 'SL'
                   AND strategy_version = ?
                   AND closed_at >= datetime('now', ?)
                """,
                (symbol, direction, strategy_version, f'-{minutes} minutes')
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT COUNT(*) AS c FROM trades
                   WHERE symbol = ? AND direction = ? AND exit_reason = 'SL'
                   AND closed_at >= datetime('now', ?)
                """,
                (symbol, direction, f'-{minutes} minutes')
            ).fetchone()
        return (row['c'] or 0) > 0
