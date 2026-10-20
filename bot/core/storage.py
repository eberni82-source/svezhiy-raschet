"""Хранилище на SQLite: один файл, ничего устанавливать не нужно."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

from .forecast import Product, Sale

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    username TEXT, first_name TEXT,
    business TEXT, city TEXT, lat REAL, lon REAL,
    tz TEXT DEFAULT 'Europe/Moscow', utc_offset INTEGER DEFAULT 10800,
    morning TEXT DEFAULT '06:00', evening TEXT DEFAULT '17:00', closing TEXT DEFAULT '21:00',
    open_h REAL DEFAULT 7, close_h REAL DEFAULT 21,
    last_morning TEXT, last_evening TEXT, last_closing TEXT,
    is_demo INTEGER DEFAULT 0, muted INTEGER DEFAULT 0,
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS products (
    chat_id INTEGER, name TEXT, price REAL, cost REAL,
    PRIMARY KEY (chat_id, name)
);
CREATE TABLE IF NOT EXISTS sales (
    chat_id INTEGER, day TEXT, product TEXT,
    baked REAL, sold REAL, soldout TEXT, rain INTEGER,
    PRIMARY KEY (chat_id, day, product)
);
CREATE TABLE IF NOT EXISTS plans (
    chat_id INTEGER, day TEXT, product TEXT, qty INTEGER, mean REAL,
    PRIMARY KEY (chat_id, day, product)
);
CREATE TABLE IF NOT EXISTS events (
    chat_id INTEGER, day TEXT, factor REAL,
    PRIMARY KEY (chat_id, day)
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, item TEXT, qty TEXT, expires TEXT,
    status TEXT DEFAULT 'active', created_at TEXT
);
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER, username TEXT, business TEXT, city TEXT, kind TEXT, contact TEXT,
    created_at TEXT
);
"""


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- чаты ---
    def upsert_chat(self, chat_id: int, username: str | None, first_name: str | None):
        self.db.execute(
            "INSERT INTO chats (chat_id, username, first_name, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
            (chat_id, username, first_name, datetime.utcnow().isoformat(timespec="seconds")))
        self.db.commit()

    def chat(self, chat_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()

    def chats(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM chats").fetchall()

    def set_chat(self, chat_id: int, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE chats SET {cols} WHERE chat_id=?", (*fields.values(), chat_id))
        self.db.commit()

    # --- товары и продажи ---
    def products(self, chat_id: int) -> dict[str, Product]:
        rows = self.db.execute("SELECT name, price, cost FROM products WHERE chat_id=? ORDER BY name", (chat_id,))
        return {r["name"]: Product(r["name"], r["price"], r["cost"]) for r in rows}

    def upsert_product(self, chat_id: int, p: Product):
        self.db.execute(
            "INSERT INTO products (chat_id, name, price, cost) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_id, name) DO UPDATE SET price=COALESCE(excluded.price, price), "
            "cost=COALESCE(excluded.cost, cost)", (chat_id, p.name, p.price, p.cost))

    def upsert_sale(self, chat_id: int, s: Sale):
        self.db.execute(
            "INSERT INTO sales (chat_id, day, product, baked, sold, soldout, rain) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, day, product) DO UPDATE SET baked=excluded.baked, sold=excluded.sold, "
            "soldout=excluded.soldout, rain=COALESCE(excluded.rain, rain)",
            (chat_id, s.day.isoformat(), s.product, s.baked, s.sold, s.soldout,
             None if s.rain is None else int(s.rain)))

    def import_sales(self, chat_id: int, sales: list[Sale], products: dict[str, Product]):
        for p in products.values():
            self.upsert_product(chat_id, p)
        for s in sales:
            self.upsert_sale(chat_id, s)
        self.db.commit()

    def clear_sales(self, chat_id: int):
        for t in ("sales", "products", "plans", "events", "batches"):
            self.db.execute(f"DELETE FROM {t} WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def sales(self, chat_id: int) -> list[Sale]:
        rows = self.db.execute("SELECT * FROM sales WHERE chat_id=? ORDER BY day", (chat_id,))
        return [Sale(date.fromisoformat(r["day"]), r["product"], r["sold"], r["baked"], r["soldout"],
                     None if r["rain"] is None else bool(r["rain"])) for r in rows]

    def sale(self, chat_id: int, day: date, product: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sales WHERE chat_id=? AND day=? AND product=?",
                               (chat_id, day.isoformat(), product)).fetchone()

    # --- планы и события ---
    def save_plan(self, chat_id: int, day: date, recs):
        for r in recs:
            self.db.execute("INSERT OR REPLACE INTO plans (chat_id, day, product, qty, mean) VALUES (?,?,?,?,?)",
                            (chat_id, day.isoformat(), r.product, r.qty, r.mean))
        self.db.commit()

    def plan(self, chat_id: int, day: date) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM plans WHERE chat_id=? AND day=? ORDER BY product",
                               (chat_id, day.isoformat())).fetchall()

    def set_event(self, chat_id: int, day: date, factor: float | None):
        if factor is None:
            self.db.execute("DELETE FROM events WHERE chat_id=? AND day=?", (chat_id, day.isoformat()))
        else:
            self.db.execute("INSERT OR REPLACE INTO events (chat_id, day, factor) VALUES (?,?,?)",
                            (chat_id, day.isoformat(), factor))
        self.db.commit()

    def event(self, chat_id: int, day: date) -> float:
        r = self.db.execute("SELECT factor FROM events WHERE chat_id=? AND day=?",
                            (chat_id, day.isoformat())).fetchone()
        return r["factor"] if r else 1.0

    # --- партии и сроки годности ---
    def add_batch(self, chat_id: int, item: str, qty: str, expires: date) -> int:
        cur = self.db.execute("INSERT INTO batches (chat_id, item, qty, expires, created_at) VALUES (?,?,?,?,?)",
                              (chat_id, item, qty, expires.isoformat(), datetime.utcnow().isoformat(timespec="seconds")))
        self.db.commit()
        return cur.lastrowid

    def batches(self, chat_id: int) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM batches WHERE chat_id=? AND status='active' ORDER BY expires, id",
                               (chat_id,)).fetchall()

    def close_batch(self, chat_id: int, batch_id: int, status: str):
        self.db.execute("UPDATE batches SET status=? WHERE chat_id=? AND id=?", (status, chat_id, batch_id))
        self.db.commit()

    # --- заявки на пилот ---
    def add_lead(self, chat_id: int, username: str | None, data: dict) -> int:
        cur = self.db.execute(
            "INSERT INTO leads (chat_id, username, business, city, kind, contact, created_at) VALUES (?,?,?,?,?,?,?)",
            (chat_id, username, data.get("business"), data.get("city"), data.get("kind"), data.get("contact"),
             datetime.utcnow().isoformat(timespec="seconds")))
        self.db.commit()
        return cur.lastrowid

    def leads(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def stats(self) -> dict:
        q = lambda sql: self.db.execute(sql).fetchone()[0]
        return {
            "chats": q("SELECT COUNT(*) FROM chats"),
            "with_sales": q("SELECT COUNT(DISTINCT chat_id) FROM sales"),
            "leads": q("SELECT COUNT(*) FROM leads"),
            "batches": q("SELECT COUNT(*) FROM batches WHERE status='active'"),
        }
