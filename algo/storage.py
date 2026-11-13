"""SQLite persistence for trades, positions, equity snapshots and events."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .config import DATA_DIR
from .market import now_ist

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    reason TEXT,
    pnl REAL,
    charges REAL,
    mode TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    atr REAL NOT NULL,
    peak REAL NOT NULL,
    opened_at TEXT NOT NULL,
    mode TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (DATA_DIR / "state.db")
        self.path.parent.mkdir(exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------ kv
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_kv(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            self._conn.commit()

    def get_json(self, key: str, default):
        raw = self.get_kv(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json(self, key: str, value) -> None:
        self.set_kv(key, json.dumps(value))

    # --------------------------------------------------------------- events
    def add_event(self, level: str, message: str) -> dict:
        ts = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO events(ts, level, message) VALUES(?, ?, ?)", (ts, level, message)
            )
            self._conn.commit()
            return {"id": cur.lastrowid, "ts": ts, "level": level, "message": message}

    def events(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- trades
    def add_trade(
        self,
        symbol: str,
        side: str,
        qty: int,
        price: float,
        reason: str = "",
        pnl: float | None = None,
        charges: float | None = None,
        mode: str = "paper",
    ) -> dict:
        ts = now_ist().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO trades(ts, symbol, side, qty, price, reason, pnl, charges, mode)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, symbol, side, qty, price, reason, pnl, charges, mode),
            )
            self._conn.commit()
            trade_id = cur.lastrowid
        return {
            "id": trade_id,
            "ts": ts,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price,
            "reason": reason,
            "pnl": pnl,
            "charges": charges,
            "mode": mode,
        }

    def trades(self, limit: int = 60) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def realised_pnl_today(self) -> float:
        today = now_ist().strftime("%Y-%m-%d")
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE pnl IS NOT NULL AND ts LIKE ?",
                (f"{today}%",),
            ).fetchone()
        return float(row["total"] or 0.0)

    def realised_pnl_total(self) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0) AS total FROM trades WHERE pnl IS NOT NULL"
            ).fetchone()
        return float(row["total"] or 0.0)

    # ------------------------------------------------------------ positions
    def save_position(self, position: dict) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO positions(symbol, qty, entry_price, stop, target, atr, peak, opened_at, mode, reason)
                   VALUES(:symbol, :qty, :entry_price, :stop, :target, :atr, :peak, :opened_at, :mode, :reason)
                   ON CONFLICT(symbol) DO UPDATE SET
                     qty = excluded.qty, entry_price = excluded.entry_price, stop = excluded.stop,
                     target = excluded.target, atr = excluded.atr, peak = excluded.peak,
                     mode = excluded.mode, reason = excluded.reason""",
                position,
            )
            self._conn.commit()

    def delete_position(self, symbol: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            self._conn.commit()

    def open_positions(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM positions").fetchall()
        return [dict(row) for row in rows]

    def clear_positions(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM positions")
            self._conn.commit()

    # ---------------------------------------------------------------- reset
    def reset_paper(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM trades WHERE mode = 'paper'")
            self._conn.execute("DELETE FROM positions WHERE mode = 'paper'")
            self._conn.commit()
