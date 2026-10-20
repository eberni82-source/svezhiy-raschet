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
    shelf_life INTEGER, default_qty REAL, active INTEGER DEFAULT 1,
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
CREATE TABLE IF NOT EXISTS login_codes (
    code TEXT PRIMARY KEY, chat_id INTEGER, expires REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, chat_id INTEGER, created REAL
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
        # миграция старых баз: новые колонки товаров
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(products)")}
        for col, typ in (("shelf_life", "INTEGER"), ("default_qty", "REAL"), ("active", "INTEGER DEFAULT 1")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE products ADD COLUMN {col} {typ}")
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
    def products(self, chat_id: int, include_inactive: bool = False) -> dict[str, Product]:
        sql = "SELECT * FROM products WHERE chat_id=?" + ("" if include_inactive else " AND COALESCE(active, 1)=1")
        rows = self.db.execute(sql + " ORDER BY name", (chat_id,))
        return {r["name"]: Product(r["name"], r["price"], r["cost"], r["shelf_life"], r["default_qty"]) for r in rows}

    def inactive_products(self, chat_id: int) -> set[str]:
        return {r["name"] for r in self.db.execute(
            "SELECT name FROM products WHERE chat_id=? AND active=0", (chat_id,))}

    def upsert_product(self, chat_id: int, p: Product):
        self.db.execute(
            "INSERT INTO products (chat_id, name, price, cost, shelf_life, default_qty, active) VALUES (?,?,?,?,?,?,1) "
            "ON CONFLICT(chat_id, name) DO UPDATE SET price=COALESCE(excluded.price, price), "
            "cost=COALESCE(excluded.cost, cost), shelf_life=COALESCE(excluded.shelf_life, shelf_life), "
            "default_qty=COALESCE(excluded.default_qty, default_qty), active=1",
            (chat_id, p.name, p.price, p.cost, p.shelf_life, p.default_qty))

    def save_product(self, chat_id: int, p: Product, old_name: str | None = None):
        """Сохранить карточку товара из кабинета (значения заменяются целиком)."""
        if old_name and old_name != p.name:
            for t in ("sales", "plans"):
                self.db.execute(f"UPDATE {t} SET product=? WHERE chat_id=? AND product=?", (p.name, chat_id, old_name))
            self.db.execute("DELETE FROM products WHERE chat_id=? AND name=?", (chat_id, old_name))
        self.db.execute(
            "INSERT INTO products (chat_id, name, price, cost, shelf_life, default_qty, active) VALUES (?,?,?,?,?,?,1) "
            "ON CONFLICT(chat_id, name) DO UPDATE SET price=excluded.price, cost=excluded.cost, "
            "shelf_life=excluded.shelf_life, default_qty=excluded.default_qty, active=1",
            (chat_id, p.name, p.price, p.cost, p.shelf_life, p.default_qty))
        self.db.commit()

    def set_product_active(self, chat_id: int, name: str, active: bool):
        self.db.execute("INSERT INTO products (chat_id, name, active) VALUES (?,?,?) "
                        "ON CONFLICT(chat_id, name) DO UPDATE SET active=excluded.active", (chat_id, name, int(active)))
        if not active:  # убираем из сегодняшних и будущих планов
            self.db.execute("DELETE FROM plans WHERE chat_id=? AND product=? AND day>=date('now','-1 day')", (chat_id, name))
        self.db.commit()

    def history_days(self, chat_id: int) -> dict[str, int]:
        return {r["product"]: r["n"] for r in self.db.execute(
            "SELECT product, COUNT(DISTINCT day) AS n FROM sales WHERE chat_id=? GROUP BY product", (chat_id,))}

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

    def batch_count(self, chat_id: int) -> int:
        return self.db.execute("SELECT COUNT(*) FROM batches WHERE chat_id=? AND status='active'", (chat_id,)).fetchone()[0]

    def close_batch(self, chat_id: int, batch_id: int, status: str):
        self.db.execute("UPDATE batches SET status=? WHERE chat_id=? AND id=?", (status, chat_id, batch_id))
        self.db.commit()

    # --- вход в веб-кабинет ---
    def create_code(self, chat_id: int, ttl: int = 600) -> str:
        import secrets, time as _t
        self.db.execute("DELETE FROM login_codes WHERE expires < ? OR chat_id=?", (_t.time(), chat_id))
        while True:
            code = f"{secrets.randbelow(900000) + 100000}"
            if not self.db.execute("SELECT 1 FROM login_codes WHERE code=?", (code,)).fetchone():
                break
        self.db.execute("INSERT INTO login_codes (code, chat_id, expires) VALUES (?,?,?)", (code, chat_id, _t.time() + ttl))
        self.db.commit()
        return code

    def use_code(self, code: str) -> str | None:
        """Код из бота → токен сессии (код одноразовый)."""
        import secrets, time as _t
        r = self.db.execute("SELECT chat_id FROM login_codes WHERE code=? AND expires>=?", (code.strip(), _t.time())).fetchone()
        if not r:
            return None
        self.db.execute("DELETE FROM login_codes WHERE code=?", (code.strip(),))
        token = secrets.token_hex(24)
        self.db.execute("INSERT INTO sessions (token, chat_id, created) VALUES (?,?,?)", (token, r["chat_id"], _t.time()))
        self.db.commit()
        return token

    def session_chat(self, token: str | None) -> int | None:
        import time as _t
        if not token:
            return None
        r = self.db.execute("SELECT chat_id FROM sessions WHERE token=? AND created>=?",
                            (token, _t.time() - 30 * 86400)).fetchone()
        return r["chat_id"] if r else None

    def drop_session(self, token: str):
        self.db.execute("DELETE FROM sessions WHERE token=?", (token,))
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
