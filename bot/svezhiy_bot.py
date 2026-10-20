"""
«Свежий расчёт» — Telegram-бот в ОДНОМ файле.
План производства на утро, вечерние скидки, сроки годности, рассылки.

Как запустить:
1. Установите Python 3.9+ с python.org (на Windows отметьте «Add Python to PATH»).
2. Сохраните этот файл как svezhiy_bot.py в отдельную папку.
3. Впишите токен от @BotFather и свой Telegram ID в BOT_TOKEN и ADMIN_IDS ниже
   (или положите рядом файл .env с BOT_TOKEN=... и ADMIN_IDS=...).
4. В терминале в этой папке:  python svezhiy_bot.py
Никаких pip install не нужно.

ВНИМАНИЕ: если вписали токен в файл — не выкладывайте этот файл в открытый доступ.
"""
from __future__ import annotations

# ----------------------------------------------------------------------
# НАСТРОЙКИ
# ----------------------------------------------------------------------
BOT_TOKEN = ""   # например "1234567890:AAH..." — токен от @BotFather
ADMIN_IDS = ""   # ваш Telegram ID от @userinfobot, несколько — через запятую

import csv
import html
import io
import json
import logging
import math
import os
import random
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# ======================================================================
# core/forecast.py
# ======================================================================
# Модель «Свежий расчёт»: прогноз спроса + оптимальный объём выпечки.
#
# Логика (одинаковая в боте и на сайте):
# 1. Спрос дня. Если товар закончился раньше закрытия, реальный спрос был больше
#    продаж: добавляем поправку sold * (1 + 0.5 * доля_дня_после_распродажи).
# 2. Влияние дождя считается по истории всех товаров точки (если есть отметки
#    погоды); иначе берётся -10%.
# 3. Базовый прогноз = уровень (средний спрос за 14 дней) × профиль недели
#    (во сколько раз этот день недели выше среднего за 8 недель). Всё очищено
#    от влияния дождя.
# 4. Объём выпечки по модели «газетчика»: печём столько, чтобы вероятность
#    продать всё была равна доле (цена - себестоимость) / цена.
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

DEFAULT_RAIN_EFFECT = -0.10
SHARE_AFTER_EVENING = 0.22      # доля дневных продаж после вечерней проверки (17:00)
DISCOUNT_THRESHOLD = 1.2        # если на витрине больше ожидаемого остатка × 1.2 — скидка


@dataclass
class Sale:
    day: date
    product: str
    sold: float
    baked: float | None = None
    soldout: str | None = None   # "15:40" — время, когда товар закончился
    rain: bool | None = None


@dataclass
class Product:
    name: str
    price: float | None = None
    cost: float | None = None


@dataclass
class Recommendation:
    product: str
    mean: float
    sd: float
    qty: int
    ratio: float
    reasons: list[str] = field(default_factory=list)
    history_days: int = 0


def parse_time(s: str | None) -> float | None:
    if not s:
        return None
    s = str(s).strip().replace(".", ":")
    if ":" not in s:
        return None
    try:
        h, m = s.split(":")[:2]
        return int(h) + int(m) / 60
    except ValueError:
        return None


def demand_estimate(s: Sale, open_h: float = 7, close_h: float = 21) -> float:
    """Оценка реального спроса с поправкой на раннюю распродажу."""
    t = parse_time(s.soldout)
    if t is None:
        return float(s.sold)
    f = min(1.0, max(0.0, (t - open_h) / (close_h - open_h)))
    return s.sold * (1 + 0.5 * (1 - f))


def rain_effect(sales: list[Sale], open_h=7, close_h=21) -> tuple[float, bool]:
    """Влияние дождя по истории: (эффект, посчитан_по_данным)."""
    by_key: dict[tuple[str, int], list[float]] = {}
    for s in sales:
        if s.rain is False:
            by_key.setdefault((s.product, s.day.weekday()), []).append(demand_estimate(s, open_h, close_h))
    ratios = []
    for s in sales:
        if s.rain:
            dry = by_key.get((s.product, s.day.weekday()))
            if dry and len(dry) >= 2:
                base = sum(dry) / len(dry)
                if base > 0:
                    ratios.append(demand_estimate(s, open_h, close_h) / base)
    if len(ratios) < 3:
        return DEFAULT_RAIN_EFFECT, False
    eff = sum(ratios) / len(ratios) - 1
    return max(-0.35, min(0.2, eff)), True


def critical_ratio(price: float | None, cost: float | None) -> float:
    if not price or cost is None or price <= 0:
        return 0.6
    return max(0.3, min(0.95, (price - cost) / price))


def forecast(sales: list[Sale], product: Product, target: date, rain_tomorrow: bool = False,
             event_factor: float = 1.0, open_h=7, close_h=21, rain_eff: float | None = None) -> Recommendation:
    hist = sorted([s for s in sales if s.product == product.name and s.day < target], key=lambda s: s.day, reverse=True)
    if rain_eff is None:
        rain_eff, _ = rain_effect(sales, open_h, close_h)
    reasons: list[str] = []

    if not hist:
        return Recommendation(product.name, 0, 0, 0, critical_ratio(product.price, product.cost),
                              ["нет истории продаж"], 0)

    # спрос, очищенный от дождя, за последние 8 недель
    window = [s for s in hist if (target - s.day).days <= 56]
    clean = {}
    for s in window:
        d = demand_estimate(s, open_h, close_h)
        clean[s.day] = clean.get(s.day, 0) + (d / (1 + rain_eff) if s.rain else d)

    # уровень: среднее за последние 14 дней (быстро ловит рост и спад)
    recent = [v for day, v in clean.items() if (target - day).days <= 14]
    if len(recent) < 5:
        recent = list(clean.values())
    level = sum(recent) / len(recent)

    # профиль недели: во сколько раз этот день недели выше среднего
    overall = sum(clean.values()) / len(clean)
    def wd_factor(wd: int) -> float:
        vals = [v for day, v in clean.items() if day.weekday() == wd]
        if len(vals) < 2 or overall <= 0:
            return 1.0
        return (sum(vals) / len(vals)) / overall
    wf = wd_factor(target.weekday())
    if len(clean) < 14:
        reasons.append("истории меньше 2 недель — прогноз грубый")
    elif abs(wf - 1) >= 0.08:
        reasons.append(f"{WEEKDAYS[target.weekday()]}: обычно {(wf - 1) * 100:+.0f}% к среднему дню")

    # разброс: насколько дни отклонялись от модели «уровень × профиль»
    resid = []
    for day, v in clean.items():
        if (target - day).days <= 28:
            f = wd_factor(day.weekday())
            if f > 0:
                resid.append(v / f - level)
    var = (sum(r * r for r in resid) / len(resid)) * 1.25 * wf * wf if len(resid) >= 4 else 0

    base = level * wf
    last_same = next((s for s in hist if s.day.weekday() == target.weekday()), None)
    sold_out_recent = last_same if last_same and last_same.soldout and (target - last_same.day).days <= 7 else None
    pts = window
    mean = base
    if rain_tomorrow:
        mean *= 1 + rain_eff
        reasons.append(f"дождь: {rain_eff * 100:+.0f}% к спросу")
    if event_factor != 1:
        mean *= event_factor
        reasons.append(f"событие: {(event_factor - 1) * 100:+.0f}%")
    if sold_out_recent:
        reasons.append(f"{WEEKDAYS_SHORT[sold_out_recent.day.weekday()]} {sold_out_recent.day:%d.%m} закончились в {sold_out_recent.soldout} — спрос был выше продаж")

    scale = mean / base if base else 1
    sd = max(math.sqrt(var) * scale, math.sqrt(max(mean, 0)))
    if len(clean) < 14:
        sd *= 1.3
    ratio = critical_ratio(product.price, product.cost)
    z = NormalDist().inv_cdf(ratio)
    qty = max(0, int(math.floor(mean + z * sd + 0.5)))
    if product.price and product.cost is not None:
        if ratio >= 0.6:
            reasons.append(f"маржа {ratio * 100:.0f}% — берём с запасом")
        elif ratio <= 0.45:
            reasons.append(f"маржа {ratio * 100:.0f}% — впритык, остаток дорого обходится")
    return Recommendation(product.name, mean, sd, qty, ratio, reasons, len(pts))


def evening_threshold(rec: Recommendation) -> int:
    """Если в 17:00 на витрине больше этого числа — ставим скидку."""
    return max(1, int(math.ceil(rec.mean * SHARE_AFTER_EVENING * DISCOUNT_THRESHOLD)))


def discount_advice(rec: Recommendation, left_now: int) -> tuple[int, str]:
    expected = rec.mean * SHARE_AFTER_EVENING
    if left_now <= expected * DISCOUNT_THRESHOLD:
        return 0, f"обычно после 17:00 уходит ~{expected:.0f} шт — скидка не нужна"
    pct = 30 if left_now <= expected * 2 else 50
    return pct, f"обычно после 17:00 уходит ~{expected:.0f} шт, на витрине {left_now} — скидка {pct}%"


@dataclass
class BacktestResult:
    days: int
    waste_now: float
    waste_new: float
    lost_now: float
    lost_new: float
    waste_units_now: float
    waste_units_new: float
    produced_now: float
    produced_new: float


def backtest(sales: list[Sale], products: dict[str, Product], days: int = 28, open_h=7, close_h=21) -> BacktestResult | None:
    """Что было бы за последние N дней, если бы пекли по рекомендации."""
    with_baked = [s for s in sales if s.baked is not None]
    if not with_baked:
        return None
    all_days = sorted({s.day for s in sales})
    test_days = all_days[-days:]
    rain_eff, _ = rain_effect(sales, open_h, close_h)
    r = BacktestResult(len(test_days), 0, 0, 0, 0, 0, 0, 0, 0)
    for d in test_days:
        past = [s for s in sales if s.day < d]
        for s in [x for x in with_baked if x.day == d]:
            p = products.get(s.product) or Product(s.product)
            price = p.price or 0
            cost = p.cost if p.cost is not None else price * 0.4
            rec = forecast(past, p, d, bool(s.rain), 1.0, open_h, close_h, rain_eff)
            dem = demand_estimate(s, open_h, close_h)
            sold_new = min(rec.qty, dem)
            r.waste_now += (s.baked - s.sold) * cost
            r.waste_new += (rec.qty - sold_new) * cost
            r.waste_units_now += s.baked - s.sold
            r.waste_units_new += rec.qty - sold_new
            r.lost_now += max(0, dem - s.sold) * (price - cost)
            r.lost_new += max(0, dem - rec.qty) * (price - cost)
            r.produced_now += s.baked
            r.produced_new += rec.qty
    return r


def expiry_status(expires: date, today: date) -> tuple[str, int]:
    left = (expires - today).days
    if left < 0:
        return "просрочено", left
    if left == 0:
        return "истекает сегодня", left
    if left == 1:
        return "истекает завтра", left
    if left <= 2:
        return "скоро", left
    return "в порядке", left


def expiry_tip(item: str) -> str:
    n = item.lower()
    if any(k in n for k in ("слив", "молок", "творог", "сметан", "сыр", "кефир", "йогурт", "маскарпоне")):
        return "пустите сегодня в крем, начинку или сырники"
    if any(k in n for k in ("ягод", "вишн", "клубник", "яблок", "фрукт", "банан", "груш")):
        return "используйте в начинку или соус сегодня"
    if any(k in n for k in ("курин", "куриц", "ветчин", "мяс", "колбас", "рыб", "лосос")):
        return "в сэндвичи и пироги сегодня, остаток — списать"
    if any(k in n for k in ("яйц", "яиц")):
        return "в тесто или выпечку сегодня"
    return "используйте в первую очередь или продайте со скидкой"

# ======================================================================
# core/csv_import.py
# ======================================================================
# Разбор CSV/выгрузки из кассы: русские или английские заголовки, разделитель ; или ,
ALIASES = {
    "date": ["дата", "date", "день"],
    "product": ["товар", "product", "позиция", "наименование", "item"],
    "sold": ["продано", "sold", "продажи", "количество", "qty", "кол-во"],
    "baked": ["испечено", "baked", "произведено", "выпечено", "приготовлено", "заказано", "закуплено", "поставлено"],
    "soldout": ["закончилось", "soldout", "распродано_в", "закончился"],
    "price": ["цена", "price"],
    "cost": ["себестоимость", "cost", "себес"],
    "rain": ["дождь", "rain", "осадки"],
}


class ImportErrorRu(Exception):
    pass


def _col(header: list[str], key: str) -> int | None:
    norm = [h.strip().lower().replace("ё", "е") for h in header]
    for a in ALIASES[key]:
        if a in norm:
            return norm.index(a)
    return None


def _num(v: str) -> float | None:
    v = (v or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _date(v: str) -> date:
    v = v.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            pass
    raise ImportErrorRu(f"не понял дату «{v}» — нужен формат 2026-10-17 или 17.10.2026")


def parse_csv(text: str) -> tuple[list[Sale], dict[str, Product]]:
    text = text.lstrip("﻿")
    first = text.splitlines()[0] if text.strip() else ""
    delim = ";" if first.count(";") >= first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = list(reader)
    if len(rows) < 2:
        raise ImportErrorRu("файл пустой — нужна строка заголовков и хотя бы одна строка продаж")
    header = rows[0]
    idx = {k: _col(header, k) for k in ALIASES}
    for need in ("date", "product", "sold"):
        if idx[need] is None:
            raise ImportErrorRu("нет обязательной колонки: " + ALIASES[need][0] +
                                ". Нужны как минимум: дата; товар; продано")
    sales: list[Sale] = []
    products: dict[str, Product] = {}
    for n, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        get = lambda k: r[idx[k]] if idx[k] is not None and idx[k] < len(r) else ""
        try:
            d = _date(get("date"))
        except ImportErrorRu as e:
            raise ImportErrorRu(f"строка {n}: {e}")
        name = get("product").strip()
        sold = _num(get("sold"))
        if not name or sold is None:
            continue
        rain_raw = get("rain").strip().lower()
        rain = None if rain_raw == "" else rain_raw in ("1", "да", "yes", "true", "дождь")
        sales.append(Sale(d, name, sold, _num(get("baked")), get("soldout").strip() or None, rain))
        price, cost = _num(get("price")), _num(get("cost"))
        p = products.setdefault(name, Product(name))
        if price is not None:
            p.price = price
        if cost is not None:
            p.cost = cost
    if not sales:
        raise ImportErrorRu("не нашёл ни одной строки с продажами")
    return sales, products

# ======================================================================
# core/storage.py
# ======================================================================
# Хранилище на SQLite: один файл, ничего устанавливать не нужно.
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

# ======================================================================
# core/tg.py
# ======================================================================
# Минимальный клиент Telegram Bot API на стандартной библиотеке (без pip install).
log = logging.getLogger("tg")


class TelegramError(Exception):
    def __init__(self, description: str, code: int = 0, retry_after: int | None = None):
        super().__init__(description)
        self.code = code
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org"):
        self.token = token
        self.base = f"{api_base.rstrip('/')}/bot{token}"
        self.file_base = f"{api_base.rstrip('/')}/file/bot{token}"

    def call(self, method: str, timeout: float = 30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/{method}", data=data,
                                     headers={"Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode("utf-8"))
                except Exception:
                    raise TelegramError(f"HTTP {e.code}", e.code)
            except (urllib.error.URLError, OSError) as e:  # нет интернета, Telegram недоступен, таймаут
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise TelegramError(f"нет связи с Telegram: {getattr(e, 'reason', e)}", -1)
            if body.get("ok"):
                return body.get("result")
            retry = (body.get("parameters") or {}).get("retry_after")
            if retry and attempt < 2:
                time.sleep(retry)
                continue
            raise TelegramError(body.get("description", "unknown error"), body.get("error_code", 0), retry)
        raise TelegramError("too many retries")

    # --- удобные обёртки ---
    def send(self, chat_id: int, text: str, keyboard=None, reply_keyboard=None, remove_keyboard=False):
        markup = None
        if keyboard is not None:
            markup = {"inline_keyboard": keyboard}
        elif reply_keyboard is not None:
            markup = {"keyboard": reply_keyboard, "resize_keyboard": True, "is_persistent": True}
        elif remove_keyboard:
            markup = {"remove_keyboard": True}
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         reply_markup=markup, disable_web_page_preview=True)

    def edit(self, chat_id: int, message_id: int, text: str, keyboard=None):
        try:
            return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                             parse_mode="HTML", reply_markup={"inline_keyboard": keyboard} if keyboard else None)
        except TelegramError as e:
            if "not modified" in str(e):
                return None
            raise

    def answer(self, callback_id: str, text: str | None = None):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError:
            pass

    def updates(self, offset: int | None, timeout: int = 25):
        """Длинный опрос: ждём новые сообщения до timeout секунд."""
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        data = json.dumps(params).encode()
        req = urllib.request.Request(f"{self.base}/getUpdates", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            body = json.loads(r.read().decode("utf-8"))
        if not body.get("ok"):
            raise TelegramError(body.get("description", "getUpdates failed"), body.get("error_code", 0))
        return body["result"]

    def download(self, file_id: str, max_bytes: int = 5_000_000) -> bytes:
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            raise TelegramError("файл слишком большой (до 5 МБ)")
        with urllib.request.urlopen(f"{self.file_base}/{info['file_path']}", timeout=60) as r:
            return r.read(max_bytes + 1)

# ======================================================================
# core/weather.py
# ======================================================================
# Погода и часовой пояс через Open-Meteo (бесплатно, без ключа).
log = logging.getLogger("weather")
RAIN_MM = 1.0  # от 1 мм осадков за день считаем, что был дождь


def _get(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def geocode(city: str) -> dict | None:
    """Город → координаты и часовой пояс. None, если не нашли или нет сети."""
    q = urllib.parse.urlencode({"name": city, "count": 1, "language": "ru", "format": "json"})
    try:
        data = _get(f"https://geocoding-api.open-meteo.com/v1/search?{q}")
    except Exception as e:
        log.warning("geocode failed: %s", e)
        return None
    res = (data.get("results") or [None])[0]
    if not res:
        return None
    return {"name": res.get("name", city), "lat": res["latitude"], "lon": res["longitude"],
            "tz": res.get("timezone") or "Europe/Moscow"}


class WeatherCache:
    """Осадки по дням: 14 дней назад + 3 дня вперёд. Обновляем раз в 3 часа."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.cache: dict[tuple[float, float], tuple[float, dict[date, float], int]] = {}

    def daily(self, lat: float | None, lon: float | None, now_ts: float) -> tuple[dict[date, float], int | None]:
        if not self.enabled or lat is None or lon is None:
            return {}, None
        key = (round(lat, 2), round(lon, 2))
        hit = self.cache.get(key)
        if hit and now_ts - hit[0] < 3 * 3600:
            return hit[1], hit[2]
        q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "daily": "precipitation_sum",
                                    "past_days": 14, "forecast_days": 3, "timezone": "auto"})
        try:
            data = _get(f"https://api.open-meteo.com/v1/forecast?{q}")
        except Exception as e:
            log.warning("weather failed: %s", e)
            return (hit[1], hit[2]) if hit else ({}, None)
        days = data.get("daily", {})
        out = {date.fromisoformat(d): (p or 0.0) for d, p in zip(days.get("time", []), days.get("precipitation_sum", []))}
        offset = data.get("utc_offset_seconds")
        self.cache[key] = (now_ts, out, offset)
        return out, offset

    def is_rain(self, lat, lon, day: date, now_ts: float) -> bool | None:
        daily, _ = self.daily(lat, lon, now_ts)
        if day not in daily:
            return None
        return daily[day] >= RAIN_MM

# ======================================================================
# Демо-данные условной пекарни (генерируются, это НЕ реальные продажи)
# ======================================================================
_DEMO_PRODUCTS = [
    ("Круассан классический", 120, 40, 46, "weekend"), ("Багет", 90, 34, 30, "flat"),
    ("Хлеб пшеничный", 70, 32, 38, "flat"), ("Синнабон", 160, 55, 18, "weekend"),
    ("Пирожок с капустой", 60, 22, 34, "office"), ("Эклер", 110, 45, 22, "weekend"),
    ("Слойка с вишней", 95, 33, 26, "flat"), ("Сэндвич с курицей", 220, 110, 20, "office"),
]
_DEMO_PROFILES = {"weekend": [0.85, 0.85, 0.9, 0.95, 1.1, 1.35, 1.25],
                  "flat": [0.95, 0.95, 1.0, 1.0, 1.08, 1.1, 1.0],
                  "office": [1.1, 1.1, 1.1, 1.1, 1.05, 0.7, 0.6]}
_DEMO_GUT_WEEK = [1.0, 1.0, 1.0, 1.0, 1.1, 1.2, 1.2]
_DEMO_GUT_BIAS = {"Круассан классический": 1.05, "Багет": 1.25, "Хлеб пшеничный": 1.3, "Синнабон": 1.2,
                  "Пирожок с капустой": 1.2, "Эклер": 1.0, "Слойка с вишней": 1.1, "Сэндвич с курицей": 1.25}
_DEMO_HOURLY = [0.04, 0.10, 0.10, 0.07, 0.06, 0.09, 0.10, 0.07, 0.06, 0.06, 0.08, 0.08, 0.06, 0.03]


def demo_csv_text() -> str:
    """8 недель продаж 8 позиций. Пекарь печёт по привычке, погоду не учитывает."""
    rnd = random.Random(42)
    end, days = date(2026, 10, 17), 56
    start = end - timedelta(days=days - 1)

    def soldout_time(fraction):
        acc = 0.0
        for i, share in enumerate(_DEMO_HOURLY):
            if acc + share >= fraction:
                return f"{7 + i:02d}:{(int(60 * (fraction - acc) / share) // 10) * 10:02d}"
            acc += share
        return "20:50"

    weather = {}
    for i in range(days):
        d = start + timedelta(days=i)
        weather[d] = rnd.random() < 0.3 if d.month < 10 else rnd.random() < 0.4
    rows = ["дата;товар;испечено;продано;закончилось;цена;себестоимость;дождь"]
    for i in range(days):
        d = start + timedelta(days=i)
        rain = weather[d]
        for name, price, cost, base, prof in _DEMO_PRODUCTS:
            lam = base * _DEMO_PROFILES[prof][d.weekday()] * (1 + 0.002 * i) * (0.84 if rain else 1.0)
            demand = max(0, int(round(rnd.gauss(lam, math.sqrt(lam) * 1.25))))
            baked = int(round(base * _DEMO_GUT_BIAS[name] * _DEMO_GUT_WEEK[d.weekday()] * (1 + rnd.gauss(0, 0.08))))
            sold = min(demand, baked)
            so = soldout_time(baked / demand) if demand > baked and demand > 0 else ""
            rows.append(f"{d.isoformat()};{name};{baked};{sold};{so};{price};{cost};{int(rain)}")
    return "\n".join(rows) + "\n"

# ======================================================================
# Бот
# ======================================================================
ROOT = Path(__file__).resolve().parent
log = logging.getLogger("bot")
esc = html.escape

MENU = [["План на завтра", "Итоги дня"], ["Скидка сейчас", "Сроки годности"], ["Добавить партию", "Помощь"]]
BUTTON_TO_CMD = {"План на завтра": "/plan", "Итоги дня": "/itogi", "Скидка сейчас": "/skidka",
                 "Сроки годности": "/batches", "Добавить партию": "/batch", "Помощь": "/help"}
SOLDOUT_SLOTS = [("до 12:00", "11:00"), ("12–15", "13:30"), ("15–18", "16:30"), ("после 18", "19:30")]
KINDS = ["Пекарня", "Кофейня", "Кулинария", "Столовая", "Другое"]

HELP = """<b>Что умеет бот</b>

Каждый день сам пришлёт:
• в {morning} — план выпечки на сегодня и продукты с истекающим сроком;
• в {evening} — при каком остатке на витрине ставить скидку;
• в {closing} — просьбу отметить итоги дня (1 минута, бот на этом учится).

<b>Команды</b>
/plan — план на завтра
/itogi — отметить итоги дня
/skidka — нужна ли скидка прямо сейчас
/batch — добавить партию продуктов со сроком годности
/batches — что скоро испортится
/event — отметить событие завтра (праздник, ярмарка)
/products — позиции, цены, себестоимость
/price Круассан 120 40 — задать цену и себестоимость
/time утро 06:00 — поменять время рассылки (утро, вечер, итоги)
/city — сменить город (для погоды и часового пояса)
/demo — загрузить демо-данные пекарни
/stop — выключить рассылку, /start — включить

<b>Свои продажи</b> — пришлите CSV-файл (выгрузку из кассы или Excel → «Сохранить как CSV») с колонками:
<code>дата;товар;продано</code> и по желанию <code>испечено;закончилось;цена;себестоимость;дождь</code>"""


ENV_NAMES = (".env", ".env.txt", "env", "env.txt", ".env.example")


def load_env(folder: Path) -> list[Path]:
    """Читает настройки из .env рядом с ботом. Понимает частые ошибки Windows:
    файл .env.txt, кодировку с BOM, пробелы и кавычки вокруг значения."""
    found = []
    for name in ENV_NAMES:
        p = folder / name
        if not p.is_file():
            continue
        found.append(p)
        for line in p.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = line.strip().lstrip("\ufeff")
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip().lstrip("\ufeff").upper()
            v = v.strip().strip('"').strip("'").strip()
            if v and not os.environ.get(k):
                os.environ[k] = v
    return found


def fmt_day(d: date) -> str:
    return f"{WEEKDAYS_SHORT[d.weekday()]}, {d:%d.%m}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(n) % 100
    if 11 <= n <= 14:
        return many
    return one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def pct(x: float) -> str:
    return f"{x:+.0f}%".replace("-", "−")


def money(x: float) -> str:
    return f"{x:,.0f}".replace(",", " ") + " ₽"


class FreshBot:
    def __init__(self, token: str, admins: set[int], db_path: str, api_base: str, weather_on: bool = True):
        self.tg = Telegram(token, api_base)
        self.db = Storage(db_path)
        self.admins = admins
        self.weather = WeatherCache(weather_on)
        self.state: dict[int, dict] = {}
        self.offset: int | None = None
        self.last_tick = 0.0

    # ------------------------------------------------------------------ время
    def tz(self, chat) -> timezone:
        if ZoneInfo and chat["tz"]:
            try:
                return ZoneInfo(chat["tz"])
            except Exception:
                pass
        return timezone(timedelta(seconds=chat["utc_offset"] or 10800))

    def now(self, chat) -> datetime:
        return datetime.now(self.tz(chat))

    def today(self, chat) -> date:
        return self.now(chat).date()

    # --------------------------------------------------------------- прогноз
    def recommendations(self, chat, day: date) -> tuple[list[Recommendation], dict]:
        cid = chat["chat_id"]
        sales = self.db.sales(cid)
        products = self.db.products(cid)
        for s in sales:  # позиции из продаж, у которых нет карточки товара
            products.setdefault(s.product, Product(s.product))
        rain = self.weather.is_rain(chat["lat"], chat["lon"], day, time.time()) if not chat["is_demo"] else None
        event = self.db.event(cid, day)
        eff, learned = rain_effect(sales, chat["open_h"], chat["close_h"])
        recs = [forecast(sales, p, day, bool(rain), event, chat["open_h"], chat["close_h"], eff)
                for p in products.values()]
        recs = [r for r in recs if r.history_days > 0]
        recs.sort(key=lambda r: -r.qty)
        habit = {}
        for r in recs:
            same = [s.baked for s in sales if s.product == r.product and s.baked is not None
                    and s.day.weekday() == day.weekday() and 0 < (day - s.day).days <= 28]
            if same:
                habit[r.product] = sum(same) / len(same)
        return recs, {"rain": rain, "event": event, "rain_eff": eff, "learned": learned,
                      "habit": habit, "products": products}

    def plan_text(self, chat, day: date, title: str) -> str | None:
        recs, info = self.recommendations(chat, day)
        if not recs:
            return None
        self.db.save_plan(chat["chat_id"], day, recs)
        head = [f"<b>{title} · {fmt_day(day)}</b>"]
        if info["rain"]:
            head.append(f"Ожидается дождь — спрос ниже на {abs(info['rain_eff']) * 100:.0f}%")
        elif info["rain"] is False:
            head.append("Без осадков")
        if info["event"] != 1:
            head.append(f"Событие: {(info['event'] - 1) * 100:+.0f}% к спросу")
        lines = ["\n".join(head), "", "<b>Испечь</b>"]
        total = 0
        habit_total = 0.0
        for r in recs[:40]:
            total += r.qty
            habit_total += info["habit"].get(r.product, r.qty)
            why = [x for x in r.reasons if not x.startswith("дождь") and not x.startswith("событие")][:2]
            line = f"• {esc(r.product)} — <b>{r.qty}</b> шт"
            if why:
                line += f"\n   <i>{esc('; '.join(why))}</i>"
            lines.append(line)
        lines.append(f"\nВсего: <b>{total}</b> шт")
        if info["habit"] and habit_total > total * 1.03:
            lines.append(f"Обычно в этот день пекли ~{habit_total:.0f} шт — план на {habit_total - total:.0f} шт меньше")
        today = self.today(chat)
        exp = self.expiry_lines(chat, today, horizon=max(1, (day - today).days + 1))
        if exp:
            lines += ["", "<b>Сроки годности</b>"] + exp
        return "\n".join(lines)

    def expiry_lines(self, chat, day: date, horizon: int = 1) -> list[str]:
        out = []
        for b in self.db.batches(chat["chat_id"]):
            status, left = expiry_status(date.fromisoformat(b["expires"]), day)
            if left <= horizon:
                tip = "спишите" if left < 0 else expiry_tip(b["item"])
                out.append(f"• {esc(b['item'])} ({esc(b['qty'])}) — {status} → {tip}")
        return out

    # -------------------------------------------------------------- рассылки
    def tick(self):
        """Раз в ~20 секунд проверяем, кому пора отправить утренний/вечерний/итоговый пост."""
        if time.time() - self.last_tick < 20:
            return
        self.last_tick = time.time()
        for chat in self.db.chats():
            if chat["muted"] or not self.db.products(chat["chat_id"]):
                continue
            now = self.now(chat)
            today = now.date().isoformat()
            for kind in ("morning", "evening", "closing"):
                h, m = map(int, (chat[kind] or "00:00").split(":"))
                due = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if chat[f"last_{kind}"] == today or now < due or now - due > timedelta(hours=2):
                    continue
                self.db.set_chat(chat["chat_id"], **{f"last_{kind}": today})
                try:
                    getattr(self, f"job_{kind}")(self.db.chat(chat["chat_id"]))
                except TelegramError as e:
                    log.warning("send failed to %s: %s", chat["chat_id"], e)
                    if e.code == 403:  # пользователь заблокировал бота
                        self.db.set_chat(chat["chat_id"], muted=1)

    def job_morning(self, chat):
        text = self.plan_text(chat, self.today(chat), "План на сегодня")
        if text:
            self.tg.send(chat["chat_id"], text)

    def job_evening(self, chat):
        day = self.today(chat)
        rows = self.db.plan(chat["chat_id"], day)
        if not rows:
            return
        lines = [f"<b>{chat['evening']} · проверьте витрину</b>", "Если сейчас на витрине больше, чем указано, — ставьте скидку 30%:"]
        for r in rows:
            rec = Recommendation(r["product"], r["mean"], 0, r["qty"], 0)
            lines.append(f"• {esc(r['product'])} — больше {evening_threshold(rec)} шт")
        lines.append("\nТочный совет по позиции: /skidka")
        exp = self.expiry_lines(chat, day, horizon=0)
        if exp:
            lines += ["", "<b>Истекает сегодня</b>"] + exp
        self.tg.send(chat["chat_id"], "\n".join(lines))

    def job_closing(self, chat):
        self.itogi(chat, scheduled=True)

    # ------------------------------------------------------------- обработка
    def run(self):
        while True:  # проверяем токен и связь с Telegram
            try:
                me = self.tg.call("getMe")
                log.info("Бот @%s запущен. Остановить: Ctrl+C", me.get("username") if isinstance(me, dict) else "?")
                break
            except TelegramError as e:
                if e.code in (401, 404):
                    raise SystemExit("Telegram не принял токен. Скопируйте токен из @BotFather заново целиком "
                                     "(вида 1234567890:AAH...) — без пробелов и лишних символов.")
                log.warning("%s. Пробую снова через 10 секунд… Проверьте интернет; "
                            "если Telegram у вас открывается только через VPN — включите его.", e)
                time.sleep(10)
        try:
            self.tg.call("setMyCommands", commands=[
                {"command": "plan", "description": "План выпечки на завтра"},
                {"command": "itogi", "description": "Отметить итоги дня"},
                {"command": "skidka", "description": "Нужна ли скидка сейчас"},
                {"command": "batch", "description": "Добавить партию со сроком"},
                {"command": "batches", "description": "Что скоро испортится"},
                {"command": "event", "description": "Событие завтра"},
                {"command": "help", "description": "Помощь"},
            ])
        except Exception as e:
            log.warning("setMyCommands: %s", e)
        while True:
            try:
                updates = self.tg.updates(self.offset, timeout=20)
            except Exception as e:
                log.warning("getUpdates: %s", e)
                time.sleep(3)
                updates = []
            for u in updates:
                self.offset = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception:
                    log.exception("update failed")
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")

    def handle(self, u: dict):
        if "callback_query" in u:
            return self.on_callback(u["callback_query"])
        msg = u.get("message")
        if not msg or msg.get("chat", {}).get("type") != "private":
            return
        cid = msg["chat"]["id"]
        frm = msg.get("from", {})
        self.db.upsert_chat(cid, frm.get("username"), frm.get("first_name"))
        chat = self.db.chat(cid)
        if "document" in msg:
            return self.on_document(chat, msg["document"])
        text = (msg.get("text") or "").strip()
        if not text:
            return
        text = BUTTON_TO_CMD.get(text, text)
        if text.startswith("/"):
            self.state.pop(cid, None)
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@")[0].lower()
            handler = getattr(self, "cmd_" + cmd[1:], None)
            if handler:
                return handler(chat, arg.strip()) if self._takes_arg(handler) else handler(chat)
            return self.tg.send(cid, "Не знаю такую команду. Список команд: /help")
        st = self.state.get(cid)
        if st:
            return getattr(self, "st_" + st["name"])(chat, text, st)
        self.tg.send(cid, "Выберите действие в меню ниже или посмотрите /help", reply_keyboard=MENU)

    @staticmethod
    def _takes_arg(fn) -> bool:
        return fn.__code__.co_argcount >= 3

    # ------------------------------------------------------------- команды
    def cmd_start(self, chat, arg: str = ""):
        cid = chat["chat_id"]
        self.db.set_chat(cid, muted=0)
        if arg == "pilot":
            self.state[cid] = {"name": "lead_business", "data": {}}
            return self.tg.send(cid, "Заявка на бесплатный месяц пилота.\n\n<b>1/4.</b> Как называется ваше заведение?",
                                remove_keyboard=True)
        if arg == "demo":  # кнопка «Попробовать в Telegram» на сайте
            self.tg.send(cid, "Привет! Я <b>Свежий расчёт</b>. Покажу, как работаю, на демо-данных условной пекарни.",
                         reply_keyboard=MENU)
            return self.cmd_demo(self.db.chat(cid))
        self.tg.send(cid, "Привет! Я <b>Свежий расчёт</b>. Каждое утро присылаю план: сколько испечь и закупить, "
                          "чтобы меньше выбрасывать и реже оставаться с пустой витриной. "
                          "Вечером подскажу, когда ставить скидку, и напомню о продуктах с истекающим сроком.",
                     reply_keyboard=MENU)
        if not chat["city"]:
            self.state[cid] = {"name": "city"}
            return self.tg.send(cid, "В каком городе ваше заведение? Нужен для погоды и часового пояса.")
        self.after_setup(chat)

    def after_setup(self, chat):
        if self.db.products(chat["chat_id"]):
            return self.tg.send(chat["chat_id"], "Данные на месте. План на завтра: /plan")
        self.tg.send(chat["chat_id"],
                     "Чтобы я начал считать, пришлите CSV-файл с продажами (колонки: <code>дата;товар;продано</code>) "
                     "или посмотрите, как всё работает, на демо-данных: /demo")

    def st_city(self, chat, text, st):
        cid = chat["chat_id"]
        g = geocode(text)
        if g:
            _, offset = self.weather.daily(g["lat"], g["lon"], time.time())  # запасной вариант для Windows без tzdata
            self.db.set_chat(cid, city=g["name"], lat=g["lat"], lon=g["lon"], tz=g["tz"],
                             utc_offset=offset if offset is not None else chat["utc_offset"])
            self.tg.send(cid, f"Город: <b>{esc(g['name'])}</b>, часовой пояс {esc(g['tz'])}.")
        else:
            self.db.set_chat(cid, city=text[:60])
            self.tg.send(cid, f"Записал город «{esc(text[:60])}». Не смог узнать часовой пояс, поставил московское время. "
                              "Поменять: /tz +2 (разница с Москвой в часах).")
        self.state.pop(cid, None)
        self.after_setup(self.db.chat(cid))

    def cmd_city(self, chat):
        self.state[chat["chat_id"]] = {"name": "city"}
        self.tg.send(chat["chat_id"], "Напишите город:")

    def cmd_tz(self, chat, arg: str = ""):
        cid = chat["chat_id"]
        m = re.fullmatch(r"([+-]?\d{1,2})", arg.replace(" ", ""))
        if m:
            offset = (3 + int(m.group(1))) * 3600
            self.db.set_chat(cid, tz="", utc_offset=offset)
            return self.tg.send(cid, f"Часовой пояс: Москва {int(m.group(1)):+d} ч (UTC{offset // 3600:+d}).")
        if arg and ZoneInfo:
            try:
                ZoneInfo(arg)
                self.db.set_chat(cid, tz=arg)
                return self.tg.send(cid, f"Часовой пояс: {esc(arg)}")
            except Exception:
                pass
        self.tg.send(cid, "Пример: <code>/tz +2</code> (Екатеринбург = Москва +2) или <code>/tz Asia/Yekaterinburg</code>")

    def cmd_help(self, chat):
        self.tg.send(chat["chat_id"], HELP.format(morning=chat["morning"], evening=chat["evening"],
                                                  closing=chat["closing"]), reply_keyboard=MENU)

    def cmd_stop(self, chat):
        self.db.set_chat(chat["chat_id"], muted=1)
        self.tg.send(chat["chat_id"], "Рассылка выключена. Включить снова: /start")

    def cmd_demo(self, chat, arg: str = ""):
        cid = chat["chat_id"]
        if self.db.sales(cid) and not chat["is_demo"] and arg != "да":
            return self.tg.send(cid, "У вас уже есть свои данные. Заменить их демо-данными?",
                                keyboard=[[{"text": "Да, заменить", "callback_data": "demo:yes"},
                                           {"text": "Отмена", "callback_data": "x"}]])
        demo_file = ROOT / "data" / "demo_sales.csv"
        text = demo_file.read_text(encoding="utf-8") if demo_file.exists() else demo_csv_text()
        sales, products = parse_csv(text)
        shift = (self.today(chat) - timedelta(days=1)) - max(s.day for s in sales)
        for s in sales:
            s.day += shift
        self.db.clear_sales(cid)
        self.db.import_sales(cid, sales, products)
        self.db.set_chat(cid, is_demo=1)
        today = self.today(chat)
        for item, qty, days in [("Сливки 33%", "2 л", 1), ("Творог 5%", "3 кг", 2),
                                ("Филе куриное", "1,5 кг", 0), ("Вишня замороженная", "2 кг", 20)]:
            self.db.add_batch(cid, item, qty, today + timedelta(days=days))
        self.plan_text(self.db.chat(cid), today, "План")  # сохраняем план на сегодня для вечерних советов
        bt = backtest(sales, products)
        lines = [f"Загрузил <b>демо-данные</b> условной пекарни: {len(products)} позиций, 8 недель продаж. "
                 "Это не реальные продажи, а пример, чтобы посмотреть, как работает бот."]
        if bt:
            lines += ["", "<b>Если бы последние 4 недели пекли по плану бота</b>",
                      f"Списания: {money(bt.waste_now)} → <b>{money(bt.waste_new)}</b> "
                      f"({pct((bt.waste_new / bt.waste_now - 1) * 100)})",
                      f"Недополученная прибыль, когда товар кончился: {money(bt.lost_now)} → {money(bt.lost_new)}",
                      f"Итого потери: {money(bt.waste_now + bt.lost_now)} → <b>{money(bt.waste_new + bt.lost_new)}</b> "
                      f"({pct(((bt.waste_new + bt.lost_new) / (bt.waste_now + bt.lost_now) - 1) * 100)})",
                      "<i>Бот печёт впритык там, где остаток дороже упущенной продажи, поэтому иногда товар заканчивается раньше.</i>"]
        self.tg.send(cid, "\n".join(lines), reply_keyboard=MENU)
        self.cmd_plan(self.db.chat(cid))

    def cmd_plan(self, chat):
        day = self.today(chat) + timedelta(days=1)
        text = self.plan_text(chat, day, "План на завтра")
        if not text:
            return self.tg.send(chat["chat_id"], "Пока нет продаж. Пришлите CSV-файл или попробуйте /demo")
        self.tg.send(chat["chat_id"], text, keyboard=[[{"text": "Завтра событие / праздник", "callback_data": "ev"}]])

    def cmd_event(self, chat):
        self.tg.send(chat["chat_id"], "Что будет завтра? Поправлю прогноз по всем позициям.", keyboard=[
            [{"text": "+10%", "callback_data": "e:1.1"}, {"text": "+20%", "callback_data": "e:1.2"},
             {"text": "+30%", "callback_data": "e:1.3"}],
            [{"text": "−20% (меньше людей)", "callback_data": "e:0.8"}, {"text": "Сбросить", "callback_data": "e:1"}]])

    def cmd_products(self, chat):
        products = self.db.products(chat["chat_id"])
        if not products:
            return self.tg.send(chat["chat_id"], "Позиций пока нет. Пришлите CSV или /demo")
        lines = ["<b>Позиции</b> (цена / себестоимость)"]
        for p in products.values():
            price = f"{p.price:.0f}" if p.price else "?"
            cost = f"{p.cost:.0f}" if p.cost is not None else "?"
            lines.append(f"• {esc(p.name)} — {price} / {cost} ₽")
        lines.append("\nПоменять: <code>/price Круассан 120 40</code>")
        self.tg.send(chat["chat_id"], "\n".join(lines))

    def cmd_price(self, chat, arg: str = ""):
        cid = chat["chat_id"]
        m = re.fullmatch(r"(.+?)\s+(\d+(?:[.,]\d+)?)\s+(\d+(?:[.,]\d+)?)", arg)
        if not m:
            return self.tg.send(cid, "Формат: <code>/price Название цена себестоимость</code>, например <code>/price Круассан 120 40</code>")
        name, price, cost = m.group(1).strip(), float(m.group(2).replace(",", ".")), float(m.group(3).replace(",", "."))
        products = self.db.products(cid)
        match = next((n for n in products if n.lower() == name.lower()), None) or \
            next((n for n in products if n.lower().startswith(name.lower())), None) or name
        self.db.upsert_product(cid, Product(match, price, cost))
        self.db.db.commit()
        self.tg.send(cid, f"{esc(match)}: цена {price:.0f} ₽, себестоимость {cost:.0f} ₽")

    def cmd_time(self, chat, arg: str = ""):
        m = re.fullmatch(r"(утро|вечер|итоги)\s+(\d{1,2})[:.](\d{2})", arg.lower())
        if not m:
            return self.tg.send(chat["chat_id"], f"Сейчас: утро {chat['morning']}, вечер {chat['evening']}, итоги {chat['closing']}.\n"
                                                 "Поменять: <code>/time утро 06:30</code>")
        field = {"утро": "morning", "вечер": "evening", "итоги": "closing"}[m.group(1)]
        value = f"{int(m.group(2)):02d}:{m.group(3)}"
        self.db.set_chat(chat["chat_id"], **{field: value})
        self.tg.send(chat["chat_id"], f"Готово: {m.group(1)} в {value}")

    # --- итоги дня
    def cmd_itogi(self, chat):
        self.itogi(chat, scheduled=False)

    def itogi(self, chat, scheduled: bool = False):
        cid = chat["chat_id"]
        day = self.today(chat)
        rows = self.db.plan(cid, day)
        if not rows:
            if not scheduled:
                self.tg.send(cid, "На сегодня нет плана. Он появляется утром или по команде /plan.")
            return
        self.tg.send(cid, self.itogi_text(cid, day), keyboard=self.itogi_kb(cid, day))

    def itogi_text(self, cid, day):
        done = sum(1 for r in self.db.plan(cid, day) if self.db.sale(cid, day, r["product"]))
        total = len(self.db.plan(cid, day))
        return (f"<b>Итоги дня · {fmt_day(day)}</b>\nОтметьте по каждой позиции: закончилась (и когда) или сколько осталось. "
                f"Так прогноз станет точнее.\n\nОтмечено: {done} из {total}")

    def itogi_kb(self, cid, day):
        kb = []
        for i, r in enumerate(self.db.plan(cid, day)):
            s = self.db.sale(cid, day, r["product"])
            mark = ""
            if s:
                mark = f" ✓ кончились {s['soldout']}" if s["soldout"] else f" ✓ осталось {s['baked'] - s['sold']:.0f}"
            kb.append([{"text": f"{r['product']}{mark}", "callback_data": f"d:{i}"}])
        return kb

    def record(self, chat, day: date, product: str, left: int | None, soldout: str | None):
        cid = chat["chat_id"]
        row = next((r for r in self.db.plan(cid, day) if r["product"] == product), None)
        baked = row["qty"] if row else 0
        sold = baked if soldout else max(0, baked - (left or 0))
        rain = self.weather.is_rain(chat["lat"], chat["lon"], day, time.time())
        self.db.upsert_sale(cid, Sale(day, product, sold, baked, soldout, rain))
        self.db.db.commit()

    def st_left(self, chat, text, st):
        cid = chat["chat_id"]
        if not re.fullmatch(r"\d{1,4}", text):
            return self.tg.send(cid, "Напишите число, например 4")
        self.record(chat, st["day"], st["product"], int(text), None)
        self.state.pop(cid, None)
        self.tg.send(cid, self.itogi_text(cid, st["day"]), keyboard=self.itogi_kb(cid, st["day"]))

    # --- скидка сейчас
    def cmd_skidka(self, chat):
        cid = chat["chat_id"]
        rows = self.db.plan(cid, self.today(chat))
        if not rows:
            return self.tg.send(cid, "На сегодня нет плана — сначала /plan")
        kb = [[{"text": r["product"], "callback_data": f"s:{i}"}] for i, r in enumerate(rows)]
        self.tg.send(cid, "По какой позиции подсказать?", keyboard=kb)

    def st_skidka(self, chat, text, st):
        cid = chat["chat_id"]
        if not re.fullmatch(r"\d{1,4}", text):
            return self.tg.send(cid, "Напишите число штук на витрине, например 12")
        rec = Recommendation(st["product"], st["mean"], 0, 0, 0)
        pct, why = discount_advice(rec, int(text))
        self.state.pop(cid, None)
        head = f"<b>{esc(st['product'])}: скидка {pct}%</b>" if pct else f"<b>{esc(st['product'])}: без скидки</b>"
        self.tg.send(cid, f"{head}\n{why}")

    # --- партии и сроки годности
    def cmd_batch(self, chat):
        self.state[chat["chat_id"]] = {"name": "batch_item", "data": {}}
        self.tg.send(chat["chat_id"], "Что за продукт? Например: <i>Сливки 33%</i>")

    def st_batch_item(self, chat, text, st):
        st["data"]["item"] = text[:60]
        st["name"] = "batch_qty"
        self.tg.send(chat["chat_id"], "Сколько? Например: <i>2 л</i> или <i>3 кг</i>")

    def st_batch_qty(self, chat, text, st):
        st["data"]["qty"] = text[:20]
        st["name"] = "batch_exp"
        kb = [[{"text": t, "callback_data": f"b:{d}"} for t, d in row] for row in
              [[("Сегодня", 0), ("Завтра", 1), ("+2 дня", 2)], [("+3 дня", 3), ("+5 дней", 5), ("+7 дней", 7)]]]
        self.tg.send(chat["chat_id"], "До какого числа годен? Нажмите кнопку или напишите дату (например 25.10)", keyboard=kb)

    def st_batch_exp(self, chat, text, st):
        m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?", text)
        if not m:
            return self.tg.send(chat["chat_id"], "Не понял дату. Напишите, например, 25.10")
        today = self.today(chat)
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            d = date(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return self.tg.send(chat["chat_id"], "Такой даты нет. Напишите, например, 25.10")
        if not m.group(3) and d < today - timedelta(days=30):
            d = d.replace(year=year + 1)
        self.save_batch(chat, st, d)

    def save_batch(self, chat, st, d: date):
        cid = chat["chat_id"]
        self.db.add_batch(cid, st["data"]["item"], st["data"]["qty"], d)
        self.state.pop(cid, None)
        status, left = expiry_status(d, self.today(chat))
        self.tg.send(cid, f"Записал: {esc(st['data']['item'])}, {esc(st['data']['qty'])}, годен до {d:%d.%m} ({status}). "
                          "Напомню заранее. Все партии: /batches", reply_keyboard=MENU)

    def cmd_batches(self, chat):
        cid = chat["chat_id"]
        rows = self.db.batches(cid)
        if not rows:
            return self.tg.send(cid, "Партий нет. Добавить: /batch")
        today = self.today(chat)
        lines = ["<b>Партии и сроки</b>"]
        kb = []
        for b in rows:
            d = date.fromisoformat(b["expires"])
            status, left = expiry_status(d, today)
            mark = "🔴" if left <= 0 else "🟡" if left <= 2 else "🟢"
            line = f"{mark} {esc(b['item'])} ({esc(b['qty'])}) — до {d:%d.%m}, {status}"
            if left <= 1:
                line += f"\n    → {'спишите' if left < 0 else expiry_tip(b['item'])}"
            lines.append(line)
            kb.append([{"text": f"Использовано: {b['item'][:22]}", "callback_data": f"u:{b['id']}"},
                       {"text": "Списано", "callback_data": f"w:{b['id']}"}])
        self.tg.send(cid, "\n".join(lines), keyboard=kb)

    # --- загрузка CSV
    def on_document(self, chat, doc):
        cid = chat["chat_id"]
        name = (doc.get("file_name") or "").lower()
        if not name.endswith((".csv", ".txt")):
            return self.tg.send(cid, "Нужен CSV-файл. В Excel: Файл → Сохранить как → CSV (разделители — точка с запятой).")
        try:
            raw = self.tg.download(doc["file_id"])
        except TelegramError as e:
            return self.tg.send(cid, f"Не смог скачать файл: {esc(str(e))}")
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        try:
            sales, products = parse_csv(text)
        except ImportErrorRu as e:
            return self.tg.send(cid, f"Не получилось прочитать файл: {esc(str(e))}")
        if chat["is_demo"]:
            self.db.clear_sales(cid)
            self.db.set_chat(cid, is_demo=0)
        self.db.import_sales(cid, sales, products)
        days = sorted({s.day for s in sales})
        lines = [f"Загружено: <b>{len(sales)}</b> {plural(len(sales), 'строка', 'строки', 'строк')}, "
                 f"{len(products)} {plural(len(products), 'позиция', 'позиции', 'позиций')}, "
                 f"с {days[0]:%d.%m.%Y} по {days[-1]:%d.%m.%Y}."]
        if len(days) < 14:
            lines.append("Данных меньше 2 недель — первые прогнозы будут грубыми, со временем станут точнее.")
        no_price = [p.name for p in self.db.products(cid).values() if not p.price or p.cost is None]
        if no_price:
            lines.append(f"Нет цены или себестоимости у {len(no_price)} {plural(len(no_price), 'позиции', 'позиций', 'позиций')} — пока считаю, что маржа 60%. "
                         "Задать: <code>/price Название цена себестоимость</code>")
        bt = backtest(self.db.sales(cid), self.db.products(cid))
        if bt and bt.waste_now > 0:
            lines.append(f"\nПо колонке «испечено»: за последние {bt.days} дн. списания {money(bt.waste_now)}. "
                         f"По плану бота было бы ~{money(bt.waste_new)}.")
        lines.append("\nПлан на завтра: /plan")
        self.tg.send(cid, "\n".join(lines), reply_keyboard=MENU)

    # --- заявка на пилот (с сайта: t.me/<бот>?start=pilot)
    def st_lead_business(self, chat, text, st):
        st["data"]["business"] = text[:80]
        st["name"] = "lead_city"
        self.tg.send(chat["chat_id"], "<b>2/4.</b> Город?")

    def st_lead_city(self, chat, text, st):
        st["data"]["city"] = text[:60]
        st["name"] = "lead_kind"
        self.tg.send(chat["chat_id"], "<b>3/4.</b> Тип заведения:",
                     keyboard=[[{"text": k, "callback_data": f"k:{i}"}] for i, k in enumerate(KINDS)])

    def st_lead_kind(self, chat, text, st):
        st["data"]["kind"] = text[:40]
        self.ask_contact(chat, st)

    def ask_contact(self, chat, st):
        st["name"] = "lead_contact"
        self.tg.send(chat["chat_id"], "<b>4/4.</b> Телефон для связи? Можно написать «нет» — тогда ответим здесь, в Telegram.")

    def st_lead_contact(self, chat, text, st):
        cid = chat["chat_id"]
        st["data"]["contact"] = "" if text.lower() in ("нет", "-", "no") else text[:40]
        lead_id = self.db.add_lead(cid, chat["username"], st["data"])
        self.state.pop(cid, None)
        self.tg.send(cid, "Спасибо! Заявка принята, свяжемся в течение дня. "
                          "Пока можно посмотреть, как работает бот, на демо-данных: /demo", reply_keyboard=MENU)
        d = st["data"]
        who = f"@{chat['username']}" if chat["username"] else f"id {cid}"
        for admin in self.admins:
            try:
                self.tg.send(admin, f"<b>Новая заявка #{lead_id}</b>\n{esc(d['business'])}, {esc(d['city'])}, "
                                    f"{esc(d.get('kind', ''))}\nКонтакт: {esc(d['contact'] or who)} ({who})")
            except TelegramError:
                pass

    # --- админ: рассылка и статистика
    def is_admin(self, chat) -> bool:
        return chat["chat_id"] in self.admins

    def cmd_broadcast(self, chat, arg: str = ""):
        cid = chat["chat_id"]
        if not self.is_admin(chat):
            return self.tg.send(cid, "Команда только для администратора.")
        if not arg:
            return self.tg.send(cid, "Напишите текст после команды: <code>/broadcast Завтра обновление — добавили прогноз для кофеен</code>")
        n = sum(1 for c in self.db.chats() if not c["muted"])
        self.state[cid] = {"name": "noop", "text": arg}
        self.tg.send(cid, f"<b>Предпросмотр</b>\n\n{esc(arg)}\n\nОтправить {n} получателям?",
                     keyboard=[[{"text": f"Отправить ({n})", "callback_data": "bc:go"}, {"text": "Отмена", "callback_data": "x"}]])

    def do_broadcast(self, admin_id: int, text: str):
        ok = fail = 0
        for c in self.db.chats():
            if c["muted"]:
                continue
            try:
                self.tg.send(c["chat_id"], esc(text))
                ok += 1
            except TelegramError as e:
                fail += 1
                if e.code == 403:
                    self.db.set_chat(c["chat_id"], muted=1)
            time.sleep(0.05)  # лимит Telegram — до ~30 сообщений в секунду
        self.tg.send(admin_id, f"Рассылка завершена: доставлено {ok}, не доставлено {fail}.")

    def cmd_stats(self, chat):
        if not self.is_admin(chat):
            return self.tg.send(chat["chat_id"], "Команда только для администратора.")
        s = self.db.stats()
        lines = [f"<b>Статистика</b>\nПользователей: {s['chats']}\nС данными продаж: {s['with_sales']}\n"
                 f"Заявок на пилот: {s['leads']}\nАктивных партий: {s['batches']}"]
        for l in self.db.leads(5):
            lines.append(f"#{l['id']} {esc(l['business'] or '')}, {esc(l['city'] or '')} — {esc(l['contact'] or '@' + (l['username'] or '?'))}")
        self.tg.send(chat["chat_id"], "\n".join(lines))

    def st_noop(self, chat, text, st):
        self.state.pop(chat["chat_id"], None)
        self.handle({"message": {"chat": {"id": chat["chat_id"], "type": "private"}, "from": {}, "text": text}})

    # ------------------------------------------------------------- кнопки
    def on_callback(self, cq: dict):
        cid = cq["message"]["chat"]["id"]
        mid = cq["message"]["message_id"]
        data = cq.get("data", "")
        chat = self.db.chat(cid)
        self.tg.answer(cq["id"])
        if not chat:
            return
        key, _, arg = data.partition(":")
        st = self.state.get(cid)
        if key == "x":
            self.state.pop(cid, None)
            return self.tg.edit(cid, mid, "Отменено.")
        if key == "demo":
            return self.cmd_demo(chat, "да")
        if key == "ev":
            return self.cmd_event(chat)
        if key == "e":
            f = float(arg)
            day = self.today(chat) + timedelta(days=1)
            self.db.set_event(cid, day, None if f == 1 else f)
            self.tg.edit(cid, mid, "Событие сброшено." if f == 1 else f"Отметил: завтра {(f - 1) * 100:+.0f}% к спросу.")
            return self.cmd_plan(chat)
        if key == "d":  # итоги: выбрали позицию
            day = self.today(chat)
            rows = self.db.plan(cid, day)
            i = int(arg)
            if i >= len(rows):
                return
            p = rows[i]["product"]
            kb = [[{"text": f"Кончились {t}", "callback_data": f"o:{i}:{v}"} for t, v in SOLDOUT_SLOTS[:2]],
                  [{"text": f"Кончились {t}", "callback_data": f"o:{i}:{v}"} for t, v in SOLDOUT_SLOTS[2:]],
                  [{"text": "Остались — введу число", "callback_data": f"l:{i}"}],
                  [{"text": "← Назад", "callback_data": "back"}]]
            return self.tg.edit(cid, mid, f"<b>{esc(p)}</b> — испекли {rows[i]['qty']} шт. Как прошёл день?", kb)
        if key == "o":
            i, t = arg.split(":", 1)
            day = self.today(chat)
            rows = self.db.plan(cid, day)
            self.record(chat, day, rows[int(i)]["product"], None, t)
            return self.tg.edit(cid, mid, self.itogi_text(cid, day), self.itogi_kb(cid, day))
        if key == "l":
            day = self.today(chat)
            rows = self.db.plan(cid, day)
            p = rows[int(arg)]["product"]
            self.state[cid] = {"name": "left", "day": day, "product": p}
            return self.tg.send(cid, f"Сколько осталось: <b>{esc(p)}</b>? Напишите число.")
        if key == "back":
            day = self.today(chat)
            return self.tg.edit(cid, mid, self.itogi_text(cid, day), self.itogi_kb(cid, day))
        if key == "s":
            rows = self.db.plan(cid, self.today(chat))
            r = rows[int(arg)]
            self.state[cid] = {"name": "skidka", "product": r["product"], "mean": r["mean"]}
            return self.tg.send(cid, f"Сколько сейчас на витрине: <b>{esc(r['product'])}</b>?")
        if key == "b" and st and st["name"] == "batch_exp":
            return self.save_batch(chat, st, self.today(chat) + timedelta(days=int(arg)))
        if key in ("u", "w"):
            self.db.close_batch(cid, int(arg), "used" if key == "u" else "written_off")
            self.tg.edit(cid, mid, "Отметил." + (" Списание учтено." if key == "w" else ""))
            return self.cmd_batches(chat)
        if key == "k" and st and st["name"] == "lead_kind":
            st["data"]["kind"] = KINDS[int(arg)]
            self.tg.edit(cid, mid, f"Тип: {KINDS[int(arg)]}")
            return self.ask_contact(chat, st)
        if key == "bc" and st and st.get("text") and self.is_admin(chat):
            text = st["text"]
            self.state.pop(cid, None)
            self.tg.edit(cid, mid, "Отправляю…")
            return self.do_broadcast(cid, text)


def main():
    found = load_env(ROOT)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = (os.environ.get("BOT_TOKEN") or BOT_TOKEN).strip().strip('"').strip("'")
    if not re.fullmatch(r"\d{5,}:[\w-]{20,}", token):
        read = "\n".join(f"  - {p.name}" for p in found) or "  (ни одного: файла .env рядом с ботом нет)"
        files = ", ".join(sorted(p.name for p in ROOT.iterdir() if p.is_file()))
        hint = f"\nСейчас вместо токена записано: «{token[:15]}…» — это не похоже на токен.\n" if token else ""
        raise SystemExit(
            "Не нашёл токен бота.\n"
            f"Папка бота: {ROOT}\n"
            f"Файлы в папке: {files}\n"
            f"Прочитал настройки из:\n{read}\n{hint}\n"
            "Как исправить (любой способ):\n"
            f"  1) в папке {ROOT} создайте файл с именем .env (не .env.txt) и строкой\n"
            "     BOT_TOKEN=1234567890:AAH...ваш токен\n"
            "  2) или впишите токен в начале файла бота: BOT_TOKEN = \"1234567890:AAH...\"")
    admins = {int(x) for x in re.findall(r"-?\d+", os.environ.get("ADMIN_IDS") or ADMIN_IDS)}
    bot = FreshBot(token, admins, os.environ.get("DB_PATH", str(ROOT / "fresh.db")),
                   os.environ.get("API_BASE", "https://api.telegram.org"),
                   os.environ.get("WEATHER", "on") != "off")
    bot.run()


if __name__ == "__main__":
    main()
