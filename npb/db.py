"""
Penyimpanan SQLite. Gantiin riwayat.csv 200 ribu baris yang di-commit ke git.

Unit akuntansi = GROUP: satu keputusan trading. Copytrade = 1 order per group;
arbitrage = beberapa leg (1 order per token) dalam satu group. PnL group baru
dihitung setelah SEMUA market di dalamnya settle.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    condition_id TEXT,
    token_id TEXT,
    outcome TEXT,
    market TEXT,
    ref_price REAL,
    mid REAL,
    ask REAL,
    edge_est REAL,
    decision TEXT,
    reason TEXT,
    extra TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    strategy TEXT NOT NULL,
    mode TEXT NOT NULL,
    group_id TEXT NOT NULL,
    signal_id INTEGER,
    condition_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT,
    market TEXT,
    end_date TEXT,
    shares_req REAL,
    limit_price REAL,
    status TEXT NOT NULL,
    shares REAL DEFAULT 0,
    cost REAL DEFAULT 0,
    fee REAL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS orders_group ON orders(group_id);
CREATE INDEX IF NOT EXISTS orders_cid ON orders(condition_id);
CREATE INDEX IF NOT EXISTS orders_mode ON orders(mode, status);
CREATE TABLE IF NOT EXISTS resolutions (
    condition_id TEXT PRIMARY KEY,
    payouts TEXT NOT NULL,
    resolved_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS marks (
    signal_id INTEGER NOT NULL,
    horizon_sec INTEGER NOT NULL,
    ts REAL NOT NULL,
    mid REAL,
    PRIMARY KEY (signal_id, horizon_sec)
);
CREATE TABLE IF NOT EXISTS trader_stats (
    wallet TEXT PRIMARY KEY,
    markets INTEGER,
    mean_edge REAL,
    t_stat REAL,
    shrunk_edge REAL,
    updated_at REAL,
    norm_edge REAL,
    holdout_n INTEGER,
    holdout_edge REAL
);
CREATE TABLE IF NOT EXISTS trader_cursor (
    wallet TEXT PRIMARY KEY,
    last_ts REAL
);
CREATE TABLE IF NOT EXISTS log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT,
    source TEXT,
    message TEXT
);
"""


@dataclass
class Group:
    group_id: str
    strategy: str
    mode: str
    ts: float
    end_date: str
    cost: float
    fee: float
    shares: float
    payout: float | None      # None = belum semua leg settle
    legs: int

    @property
    def total_cost(self) -> float:
        return self.cost + self.fee

    @property
    def resolved(self) -> bool:
        return self.payout is not None

    @property
    def pnl(self) -> float | None:
        return None if self.payout is None else self.payout - self.total_cost

    @property
    def avg_price(self) -> float:
        """Harga rata-rata per share (masuk akal buat group 1 leg)."""
        return self.cost / self.shares if self.shares else 0.0


def new_group_id() -> str:
    return uuid.uuid4().hex[:16]


class DB:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """Tambah kolom baru ke DB lama (branch `data` bawa DB dari versi sebelumnya)."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(trader_stats)")}
        for col, typ in (("norm_edge", "REAL"), ("holdout_n", "INTEGER"), ("holdout_edge", "REAL")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE trader_stats ADD COLUMN {col} {typ}")

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def backup_to(self, dest: str) -> None:
        """Snapshot konsisten (aman walau bot lagi nulis) ke satu file tanpa WAL."""
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        out = sqlite3.connect(dest)
        try:
            with self._lock:
                self.conn.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")
        finally:
            out.close()

    # ── meta / flags ─────────────────────────────────────────────────────────
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.execute("DELETE FROM meta WHERE key=?", (key,))
        else:
            self.execute("INSERT INTO meta(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def killed(self) -> str | None:
        """Alasan kill switch kalau aktif, None kalau bot boleh jalan."""
        return self.get_meta("kill")

    def promoted(self, strategy: str) -> bool:
        return self.get_meta(f"promoted:{strategy}") == "1"

    def log(self, level: str, source: str, message: str) -> None:
        self.execute("INSERT INTO log(ts, level, source, message) VALUES(?,?,?,?)",
                     (time.time(), level, source, message[:2000]))

    # ── signals ──────────────────────────────────────────────────────────────
    def signal_seen(self, key: str) -> bool:
        return bool(self.query("SELECT 1 FROM signals WHERE key=?", (key,)))

    def add_signal(self, strategy: str, key: str, *, condition_id="", token_id="",
                   outcome="", market="", ref_price=None, mid=None, ask=None,
                   edge_est=None, decision="", reason="", extra: dict | None = None) -> int | None:
        cur = self.execute(
            "INSERT OR IGNORE INTO signals(ts, strategy, key, condition_id, token_id, outcome, "
            "market, ref_price, mid, ask, edge_est, decision, reason, extra) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), strategy, key, condition_id, token_id, outcome, market[:200],
             ref_price, mid, ask, edge_est, decision, reason[:500],
             json.dumps(extra or {})))
        return cur.lastrowid if cur.rowcount else None

    def add_mark(self, signal_id: int, horizon_sec: int, mid: float | None) -> None:
        self.execute("INSERT OR IGNORE INTO marks(signal_id, horizon_sec, ts, mid) VALUES(?,?,?,?)",
                     (signal_id, horizon_sec, time.time(), mid))

    def signals_needing_mark(self, horizon_sec: int, limit: int = 200) -> list[sqlite3.Row]:
        return self.query(
            "SELECT s.* FROM signals s WHERE s.token_id != '' AND s.ts <= ? "
            "AND NOT EXISTS (SELECT 1 FROM marks m WHERE m.signal_id=s.id AND m.horizon_sec=?) "
            "AND s.ts > ? ORDER BY s.ts LIMIT ?",
            (time.time() - horizon_sec, horizon_sec, time.time() - horizon_sec - 6 * 3600, limit))

    # ── orders ───────────────────────────────────────────────────────────────
    def add_order(self, *, strategy: str, mode: str, group_id: str, signal_id: int | None,
                  condition_id: str, token_id: str, outcome: str, market: str, end_date: str,
                  shares_req: float, limit_price: float, status: str, shares: float = 0.0,
                  cost: float = 0.0, fee: float = 0.0, error: str = "") -> int:
        cur = self.execute(
            "INSERT INTO orders(ts, strategy, mode, group_id, signal_id, condition_id, token_id, "
            "outcome, market, end_date, shares_req, limit_price, status, shares, cost, fee, error) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), strategy, mode, group_id, signal_id, condition_id, token_id, outcome,
             market[:200], end_date or "", shares_req, limit_price, status, shares, cost, fee,
             error[:500]))
        return int(cur.lastrowid)

    def unresolved_condition_ids(self) -> list[str]:
        """Market yang belum settle, end date paling lama dulu (paling mungkin settle)."""
        rows = self.query(
            "SELECT o.condition_id, MIN(COALESCE(NULLIF(o.end_date, ''), '9999')) AS e "
            "FROM orders o WHERE o.status='filled' "
            "AND NOT EXISTS (SELECT 1 FROM resolutions r WHERE r.condition_id=o.condition_id) "
            "GROUP BY o.condition_id ORDER BY e, o.condition_id")
        return [r["condition_id"] for r in rows]

    def set_resolution(self, condition_id: str, payouts: dict[str, float]) -> None:
        self.execute("INSERT OR REPLACE INTO resolutions(condition_id, payouts, resolved_at) "
                     "VALUES(?,?,?)", (condition_id, json.dumps(payouts), time.time()))

    def resolution(self, condition_id: str) -> dict[str, float] | None:
        rows = self.query("SELECT payouts FROM resolutions WHERE condition_id=?", (condition_id,))
        return json.loads(rows[0]["payouts"]) if rows else None

    def groups(self, strategy: str | None = None, mode: str | None = None) -> list[Group]:
        sql = ("SELECT o.*, r.payouts FROM orders o LEFT JOIN resolutions r "
               "ON r.condition_id=o.condition_id WHERE o.status='filled'")
        params: list = []
        if strategy:
            sql += " AND o.strategy=?"
            params.append(strategy)
        if mode:
            sql += " AND o.mode=?"
            params.append(mode)
        sql += " ORDER BY o.ts"
        by: dict[str, Group] = {}
        for r in self.query(sql, tuple(params)):
            g = by.get(r["group_id"])
            if g is None:
                g = by[r["group_id"]] = Group(r["group_id"], r["strategy"], r["mode"], r["ts"],
                                              r["end_date"] or "", 0.0, 0.0, 0.0, 0.0, 0)
            g.cost += r["cost"]
            g.fee += r["fee"]
            g.shares += r["shares"]
            g.legs += 1
            if r["end_date"] and r["end_date"] > g.end_date:
                g.end_date = r["end_date"]
            if g.payout is not None:
                if r["payouts"] is None:
                    g.payout = None
                else:
                    g.payout += json.loads(r["payouts"]).get(r["token_id"], 0.0) * r["shares"]
        return list(by.values())

    # Agregat per group di SQL. Dulu tiap cek risiko nge-load SEMUA order ke Python
    # (+1 query per group) — makin lama bot jalan, makin lambat tiap order.
    _GROUP_AGG = (
        "SELECT o.group_id, SUM(o.cost + o.fee) AS cost, MAX(o.end_date) AS end_date, "
        "SUM(COALESCE(json_extract(r.payouts, '$.\"' || o.token_id || '\"'), 0) * o.shares) "
        "AS payout, SUM(CASE WHEN r.condition_id IS NULL THEN 1 ELSE 0 END) AS open_legs, "
        "MAX(r.resolved_at) AS resolved_at "
        "FROM orders o LEFT JOIN resolutions r ON r.condition_id=o.condition_id "
        "WHERE o.status='filled'")

    def _agg(self, mode: str | None, having: str, params: tuple = ()) -> list[sqlite3.Row]:
        sql, p = self._GROUP_AGG, []
        if mode:
            sql += " AND o.mode=?"
            p.append(mode)
        return self.query(sql + " GROUP BY o.group_id HAVING " + having, tuple(p) + params)

    def market_exposure(self, condition_id: str, mode: str | None = None) -> float:
        sql = ("SELECT COALESCE(SUM(cost + fee), 0) AS t FROM orders o WHERE status='filled' "
               "AND condition_id=? AND NOT EXISTS "
               "(SELECT 1 FROM resolutions r WHERE r.condition_id=o.condition_id)")
        params: tuple = (condition_id,)
        if mode:
            sql += " AND mode=?"
            params += (mode,)
        return float(self.query(sql, params)[0]["t"])

    def open_exposure(self, zombie_days: int, mode: str | None = None) -> float:
        """Modal di group yang belum settle. Group yang end date-nya lewat > zombie_days
        hari (UTC) gak dihitung."""
        rows = self._agg(mode, "open_legs > 0 AND (MAX(o.end_date) = '' OR MAX(o.end_date) IS NULL "
                               "OR MAX(o.end_date) >= date('now', ?))", (f"-{int(zombie_days)} days",))
        return sum(r["cost"] for r in rows)

    def settled_group_pnls(self, mode: str | None = None,
                           since_ts: float | None = None) -> list[tuple[float, float]]:
        """[(waktu settle terakhir, PnL)] untuk group yang semua leg-nya settle."""
        having, params = "open_legs = 0", ()
        if since_ts is not None:
            having += " AND MAX(r.resolved_at) >= ?"
            params = (since_ts,)
        return [(r["resolved_at"], r["payout"] - r["cost"]) for r in self._agg(mode, having, params)]

    def realized_pnl_since(self, since_ts: float, mode: str | None = None) -> float:
        """PnL group yang settle sejak since_ts (pakai waktu resolusi terakhir)."""
        return sum(p for _, p in self.settled_group_pnls(mode, since_ts))

    def open_positions(self, mode: str | None = None) -> list[sqlite3.Row]:
        """Per token yang belum settle: share & modal (buat mark-to-market)."""
        sql = ("SELECT o.token_id, SUM(o.shares) AS shares, SUM(o.cost + o.fee) AS cost "
               "FROM orders o WHERE o.status='filled' AND NOT EXISTS "
               "(SELECT 1 FROM resolutions r WHERE r.condition_id=o.condition_id)")
        params: tuple = ()
        if mode:
            sql += " AND o.mode=?"
            params = (mode,)
        return self.query(sql + " GROUP BY o.token_id", params)

    # ── traders ──────────────────────────────────────────────────────────────
    def upsert_trader_stats(self, wallet: str, markets: int, mean_edge: float, t_stat: float,
                            shrunk_edge: float, *, norm_edge: float | None = None,
                            holdout_n: int = 0, holdout_edge: float = 0.0) -> None:
        self.execute(
            "INSERT OR REPLACE INTO trader_stats(wallet, markets, mean_edge, t_stat, shrunk_edge, "
            "updated_at, norm_edge, holdout_n, holdout_edge) VALUES(?,?,?,?,?,?,?,?,?)",
            (wallet, markets, mean_edge, t_stat, shrunk_edge, time.time(), norm_edge,
             holdout_n, holdout_edge))

    def trader_stats(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM trader_stats ORDER BY shrunk_edge DESC")

    def trader_last_ts(self, wallet: str) -> float | None:
        rows = self.query("SELECT last_ts FROM trader_cursor WHERE wallet=?", (wallet,))
        return rows[0]["last_ts"] if rows else None

    def set_trader_last_ts(self, wallet: str, ts: float) -> None:
        self.execute("INSERT OR REPLACE INTO trader_cursor(wallet, last_ts) VALUES(?,?)",
                     (wallet, ts))
