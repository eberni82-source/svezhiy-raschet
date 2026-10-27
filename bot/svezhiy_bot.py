"""
"Свежий расчёт" - Telegram-бот одним файлом.
План производства на утро, вечерние скидки, сроки годности, рассылки.

Как запустить:
1. Установите Python 3.9+ с python.org (на Windows отметьте "Add Python to PATH").
2. Сохраните этот файл как svezhiy_bot.py в отдельную папку.
3. Впишите токен от @BotFather и свой Telegram ID в BOT_TOKEN и ADMIN_IDS ниже
   (или положите рядом файл .env с BOT_TOKEN=... и ADMIN_IDS=...).
4. В терминале в этой папке:  python svezhiy_bot.py
Никаких pip install не нужно.

Если вписали токен в файл, не выкладывайте его в открытый доступ.
"""
from __future__ import annotations

# Настройки
BOT_TOKEN = ""   # токен от @BotFather, например "1234567890:AAH..."
ADMIN_IDS = ""   # ваш Telegram ID от @userinfobot, несколько - через запятую

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
import secrets
import threading
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from statistics import NormalDist
from typing import NamedTuple

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

# bot/core/forecast.py
# Модель "Свежий расчёт": прогноз спроса + оптимальный объём выпечки.
#
# Логика (одинаковая в боте и на сайте):
# 1. Спрос дня. Если товар закончился раньше закрытия, реальный спрос был больше
#    продаж: добавляем поправку sold * (1 + 0.5 * доля_дня_после_распродажи).
# 2. Влияние дождя считается по истории всех товаров точки (если есть отметки
#    погоды); иначе берётся -10%.
# 3. Базовый прогноз = уровень * профиль недели (во сколько раз этот день
#    недели выше среднего за 8 недель). Уровень - взвешенное среднее за 14 дней,
#    каждый день приведён к среднему дню недели; вчера весит 1, позавчера 0.9
#    и так далее, поэтому свежие продажи влияют сильнее. Всё очищено от дождя.
# 4. Сезон. Уровень по двум неделям сам идёт за сезоном. Сверху две поправки:
#    температура - по истории модель узнаёт, как холод и жара меняют спрос на
#    каждую позицию (нужно от двух месяцев данных с температурой);
#    прошлый год - как менялся спрос всей точки в эти же дни год назад
#    (праздники, дачный сезон, каникулы). Обе включаются, только если эффект
#    заметно больше случайного разброса.
# 5. Объём выпечки по модели "газетчика": печём столько, чтобы вероятность
#    продать всё была равна доле (цена - себестоимость) / цена.
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

DEFAULT_RAIN_EFFECT = -0.10
SHARE_AFTER_EVENING = 0.22      # доля дневных продаж после вечерней проверки (17:00)
DISCOUNT_THRESHOLD = 1.2        # если на витрине больше ожидаемого остатка * 1.2 - скидка


@dataclass
class Sale:
    day: date
    product: str
    sold: float
    baked: float | None = None
    soldout: str | None = None   # "15:40" - время, когда товар закончился
    rain: bool | None = None
    temp: float | None = None    # средняя температура дня, °C


@dataclass
class Product:
    name: str
    price: float | None = None
    cost: float | None = None
    shelf_life: int | None = None      # срок хранения, дней
    default_qty: float | None = None   # обычный объём в день - для нового товара без истории


@dataclass
class Recommendation:
    product: str
    mean: float
    sd: float
    qty: int
    ratio: float
    reasons: list[str] = field(default_factory=list)
    history_days: int = 0
    parts: dict = field(default_factory=dict)   # множители прогноза, см. forecast()


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


def _mean(xs):
    return sum(xs) / len(xs)


LEVEL_DAYS, LEVEL_DECAY = 14, 0.9
TEMP_WINDOW, TEMP_NEAR, TEMP_MIN_DAYS, TEMP_MIN_T = 365, 15, 60, 2.5


def temperature_slope(hist, target: date, clean_value, wd_factor) -> float:
    """На сколько (доля) меняется спрос на каждый градус. 0 - если данных мало или связь ненадёжна."""
    n_days = TEMP_WINDOW
    y = [None] * (n_days + 1)       # индекс = сколько дней назад
    tt = [None] * (n_days + 1)
    for s in hist:
        age = (target - s.day).days
        if 1 <= age <= n_days:
            f = wd_factor(s.day.weekday()) or 1.0
            y[age] = (y[age] or 0) + clean_value(s) / f
            if s.temp is not None:
                tt[age] = s.temp

    def prefix(arr):
        sums, cnts = [0.0], [0]
        for v in arr:
            sums.append(sums[-1] + (v if v is not None else 0.0))
            cnts.append(cnts[-1] + (1 if v is not None else 0))
        return sums, cnts
    ys, yc = prefix(y)
    ts, tc = prefix(tt)
    xs_, rs_ = [], []
    for age in range(1, n_days + 1):
        if y[age] is None or tt[age] is None:
            continue
        lo, hi = max(0, age - TEMP_NEAR), min(n_days, age + TEMP_NEAR) + 1
        if yc[hi] - yc[lo] == 0 or tc[hi] - tc[lo] == 0:
            continue
        local_y = (ys[hi] - ys[lo]) / (yc[hi] - yc[lo])
        local_t = (ts[hi] - ts[lo]) / (tc[hi] - tc[lo])
        if local_y > 0:
            xs_.append(tt[age] - local_t)
            rs_.append(y[age] / local_y)
    n = len(xs_)
    if n < TEMP_MIN_DAYS:
        return 0.0
    mx, mr = _mean(xs_), _mean(rs_)
    vx = _mean([(x - mx) ** 2 for x in xs_])
    if vx < 4:
        return 0.0
    slope = _mean([(x - mx) * (r - mr) for x, r in zip(xs_, rs_)]) / vx
    res = sum((r - mr - slope * (x - mx)) ** 2 for x, r in zip(xs_, rs_)) / (n - 2)
    se = math.sqrt(res / (n * vx)) if res > 0 else 0
    if se == 0 or abs(slope / se) < TEMP_MIN_T:
        return 0.0
    return max(-0.02, min(0.02, slope * n / (n + 30)))


def season_change(sales: list[Sale], target: date, open_h=7, close_h=21, rain_eff=DEFAULT_RAIN_EFFECT) -> float:
    """Сезон по прошлому году: во сколько раз спрос всей точки в неделю с этой даты год назад
    отличался от двух недель до неё. 1.0 - если года истории нет или разница в пределах шума."""
    total: dict[int, float] = {}
    for s in sales:
        age = (target - s.day).days
        if 1 <= age <= 56 or 359 <= age <= 379:
            d = demand_estimate(s, open_h, close_h)
            total[age] = total.get(age, 0) + (d / (1 + rain_eff) if s.rain else d)
    if any(a not in total for a in range(359, 380)):
        return 1.0
    after = [total[a] for a in range(359, 366)]    # неделя с этой даты год назад
    before = [total[a] for a in range(366, 380)]   # две недели до неё
    # шум: разница одинаковых дней недели через неделю, последние 8 недель
    pairs = [((total[a] - total[a + 7]) / ((total[a] + total[a + 7]) / 2)) ** 2 / 2
             for a in range(1, 50) if a in total and a + 7 in total and total[a] + total[a + 7] > 0]
    if len(pairs) < 7 or _mean(before) <= 0:
        return 1.0
    se = math.sqrt(_mean(pairs) * (1 / 7 + 1 / 14))
    ratio = _mean(after) / _mean(before)
    return ratio if abs(ratio - 1) >= max(0.08, 3 * se) else 1.0


def forecast(sales: list[Sale], product: Product, target: date, rain_tomorrow: bool = False,
             event_factor: float = 1.0, open_h=7, close_h=21, rain_eff: float | None = None,
             temp_tomorrow: float | None = None) -> Recommendation:
    hist = sorted([s for s in sales if s.product == product.name and s.day < target], key=lambda s: s.day, reverse=True)
    if rain_eff is None:
        rain_eff, _ = rain_effect(sales, open_h, close_h)
    reasons: list[str] = []

    if not hist:
        cr = critical_ratio(product.price, product.cost)
        if product.default_qty:
            dq = float(product.default_qty)
            return Recommendation(product.name, dq, math.sqrt(dq), max(0, int(math.floor(dq + 0.5))), cr,
                                  ["новый товар: истории пока нет, берём ваш обычный объём"], 0)
        return Recommendation(product.name, 0, 0, 0, cr, ["нет истории продаж"], 0)

    def clean_value(s: Sale) -> float:
        d = demand_estimate(s, open_h, close_h)
        return d / (1 + rain_eff) if s.rain else d

    # спрос, очищенный от дождя, за последние 8 недель
    clean: dict[date, float] = {}
    temps: dict[date, float] = {}
    for s in hist:
        if (target - s.day).days <= 56:
            clean[s.day] = clean.get(s.day, 0) + clean_value(s)
            if s.temp is not None:
                temps[s.day] = s.temp

    # профиль недели: во сколько раз этот день недели выше среднего
    overall = _mean(list(clean.values()))

    def wd_factor(wd: int) -> float:
        vals = [v for day, v in clean.items() if day.weekday() == wd]
        if len(vals) < 2 or overall <= 0:
            return 1.0
        return _mean(vals) / overall

    # уровень: последние 14 дней, каждый приведён к среднему дню недели;
    # чем ближе день, тем больше вес: вчера - 1, позавчера - 0.9, и так далее
    recent_days = [day for day in clean if (target - day).days <= LEVEL_DAYS]
    if len(recent_days) < 5:
        recent_days = list(clean)
    weights = {d: LEVEL_DECAY ** ((target - d).days - 1) for d in recent_days if wd_factor(d.weekday()) > 0}
    wsum = sum(weights.values())
    level = sum(w * clean[d] / wd_factor(d.weekday()) for d, w in weights.items()) / wsum if wsum else 0.0
    wf = wd_factor(target.weekday())
    if len(clean) < 14:
        reasons.append("истории меньше 2 недель — прогноз грубый")
    elif abs(wf - 1) >= 0.08:
        reasons.append(f"{WEEKDAYS[target.weekday()]}: обычно {(wf - 1) * 100:+.0f}% к среднему дню")

    # разброс: насколько дни отклонялись от модели "уровень * профиль"
    resid = []
    for day, v in clean.items():
        if (target - day).days <= 28:
            f = wd_factor(day.weekday())
            if f > 0:
                resid.append(v / f - level)
    var = (sum(r * r for r in resid) / len(resid)) * 1.25 * wf * wf if len(resid) >= 4 else 0
    base = level * wf

    # температура: связь ищем по истории позиции, день сравниваем с соседними +-15 днями
    temp_f, diff = 1.0, None
    temp_days = [d for d in weights if d in temps]
    if temp_tomorrow is not None and temp_days:
        slope = temperature_slope(hist, target, clean_value, wd_factor)
        if slope:
            level_temp = sum(weights[d] * temps[d] for d in temp_days) / sum(weights[d] for d in temp_days)
            diff = max(-12.0, min(12.0, temp_tomorrow - level_temp))
            temp_f = max(0.75, min(1.3, 1 + slope * diff))
            if abs(temp_f - 1) >= 0.03:
                reasons.append(f"{'холоднее' if diff < 0 else 'теплее'} на {abs(diff):.0f}°: {(temp_f - 1) * 100:+.0f}% к спросу")

    # сезон: как менялся спрос в эти дни год назад
    yoy = season_change(sales, target, open_h, close_h, rain_eff)
    yoy_f = max(0.6, min(1.6, 1 + 0.7 * (yoy - 1)))
    if yoy != 1:
        reasons.append(f"сезон: год назад в эти дни спрос {'вырос' if yoy > 1 else 'снизился'} на {abs(yoy - 1) * 100:.0f}%")

    last_same = next((s for s in hist if s.day.weekday() == target.weekday()), None)
    sold_out_recent = last_same if last_same and last_same.soldout and (target - last_same.day).days <= 7 else None
    mean = base * temp_f * yoy_f
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
    # из чего сложился прогноз: уровень × день недели × температура × сезон × дождь × событие
    yesterday = target - timedelta(days=1)
    parts = {"level": level, "days": len(weights), "recent": weights[yesterday] / wsum if yesterday in weights else 0.0,
             "week": wf, "temp": temp_f, "delta": diff, "season": yoy_f, "rain": 1 + rain_eff if rain_tomorrow else 1.0,
             "event": event_factor, "z": z}
    return Recommendation(product.name, mean, sd, qty, ratio, reasons, len(clean), parts)


def evening_threshold(rec: Recommendation) -> int:
    """Если в 17:00 на витрине больше этого числа - ставим скидку."""
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
            rec = forecast(past, p, d, bool(s.rain), 1.0, open_h, close_h, rain_eff, s.temp)
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

# bot/core/csv_import.py
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
    "temp": ["температура", "temp", "темп", "t"],
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
        sales.append(Sale(d, name, sold, _num(get("baked")), get("soldout").strip() or None, rain, _num(get("temp"))))
        price, cost = _num(get("price")), _num(get("cost"))
        p = products.setdefault(name, Product(name))
        if price is not None:
            p.price = price
        if cost is not None:
            p.cost = cost
    if not sales:
        raise ImportErrorRu("не нашёл ни одной строки с продажами")
    return sales, products

# bot/core/storage.py
# Хранилище на SQLite: один файл, ничего устанавливать не нужно.
SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    chat_id INTEGER PRIMARY KEY,
    username TEXT, first_name TEXT,
    business TEXT, city TEXT, lat REAL, lon REAL,
    tz TEXT DEFAULT 'Asia/Yekaterinburg', utc_offset INTEGER DEFAULT 18000,
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
    baked REAL, sold REAL, soldout TEXT, rain INTEGER, temp REAL,
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
        # миграция старых баз: новые колонки
        for table, col, typ in (("products", "shelf_life", "INTEGER"), ("products", "default_qty", "REAL"),
                                ("products", "active", "INTEGER DEFAULT 1"), ("sales", "temp", "REAL")):
            if col not in {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

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
            "INSERT INTO sales (chat_id, day, product, baked, sold, soldout, rain, temp) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, day, product) DO UPDATE SET baked=excluded.baked, sold=excluded.sold, "
            "soldout=excluded.soldout, rain=COALESCE(excluded.rain, rain), temp=COALESCE(excluded.temp, temp)",
            (chat_id, s.day.isoformat(), s.product, s.baked, s.sold, s.soldout,
             None if s.rain is None else int(s.rain), s.temp))

    def put_sale(self, chat_id: int, s: Sale):
        """Строка из таблицы кабинета: значения заменяются целиком, пустые тоже."""
        self.db.execute(
            "INSERT INTO sales (chat_id, day, product, baked, sold, soldout, rain, temp) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(chat_id, day, product) DO UPDATE SET baked=excluded.baked, sold=excluded.sold, "
            "soldout=excluded.soldout, rain=excluded.rain, temp=excluded.temp",
            (chat_id, s.day.isoformat(), s.product, s.baked, s.sold, s.soldout,
             None if s.rain is None else int(s.rain), s.temp))

    def delete_sale(self, chat_id: int, day: date, product: str):
        self.db.execute("DELETE FROM sales WHERE chat_id=? AND day=? AND product=?", (chat_id, day.isoformat(), product))

    def import_sales(self, chat_id: int, sales: list[Sale], products: dict[str, Product]):
        for p in products.values():
            self.upsert_product(chat_id, p)
        for s in sales:
            self.upsert_sale(chat_id, s)
        self.db.commit()

    def save_weather(self, chat_id: int, sales: list[Sale]):
        self.db.executemany(
            "UPDATE sales SET rain=COALESCE(rain, ?), temp=COALESCE(temp, ?) WHERE chat_id=? AND day=? AND product=?",
            [(None if s.rain is None else int(s.rain), s.temp, chat_id, s.day.isoformat(), s.product) for s in sales])
        self.db.commit()

    def clear_sales(self, chat_id: int):
        for t in ("sales", "products", "plans", "events", "batches"):
            self.db.execute(f"DELETE FROM {t} WHERE chat_id=?", (chat_id,))
        self.db.commit()

    def sales(self, chat_id: int) -> list[Sale]:
        rows = self.db.execute("SELECT * FROM sales WHERE chat_id=? ORDER BY day", (chat_id,))
        return [Sale(date.fromisoformat(r["day"]), r["product"], r["sold"], r["baked"], r["soldout"],
                     None if r["rain"] is None else bool(r["rain"]), r["temp"]) for r in rows]

    def sale(self, chat_id: int, day: date, product: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sales WHERE chat_id=? AND day=? AND product=?",
                               (chat_id, day.isoformat(), product)).fetchone()

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

    def update_batch(self, chat_id: int, batch_id: int, item: str, qty: str, expires: date):
        self.db.execute("UPDATE batches SET item=?, qty=?, expires=? WHERE chat_id=? AND id=? AND status='active'",
                        (item, qty, expires.isoformat(), chat_id, batch_id))
        self.db.commit()

    def close_batch(self, chat_id: int, batch_id: int, status: str):
        self.db.execute("UPDATE batches SET status=? WHERE chat_id=? AND id=?", (status, chat_id, batch_id))
        self.db.commit()

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
        """Код из бота -> токен сессии (код одноразовый)."""
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

# bot/core/tg.py
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

# bot/core/weather.py
# Погода и часовой пояс через Open-Meteo (бесплатно, без ключа).
log = logging.getLogger("weather")
RAIN_MM = 1.0  # от 1 мм осадков за день считаем, что был дождь
DAILY = "precipitation_sum,temperature_2m_max,temperature_2m_min"


class Day(NamedTuple):
    rain: bool | None
    temp: float | None   # средняя температура дня, °C


def _get(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _days(data: dict) -> dict[date, Day]:
    d = data.get("daily", {})
    out = {}
    for day, mm, hi, lo in zip(d.get("time", []), d.get("precipitation_sum", []),
                               d.get("temperature_2m_max", []), d.get("temperature_2m_min", [])):
        temp = round((hi + lo) / 2, 1) if hi is not None and lo is not None else None
        out[date.fromisoformat(day)] = Day(None if mm is None else mm >= RAIN_MM, temp)
    return out


def geocode(city: str) -> dict | None:
    """Город -> координаты и часовой пояс. None, если не нашли или нет сети."""
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


def history(lat: float | None, lon: float | None, start: date, end: date) -> dict[date, Day]:
    """Архив погоды за прошлые даты - чтобы загруженная история продаж сразу была с погодой."""
    if lat is None or lon is None or start > end:
        return {}
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "daily": DAILY, "timezone": "auto",
                                "start_date": start.isoformat(), "end_date": end.isoformat()})
    try:
        return _days(_get(f"https://archive-api.open-meteo.com/v1/archive?{q}", timeout=20))
    except Exception as e:
        log.warning("weather history failed: %s", e)
        return {}


class WeatherCache:
    """Осадки и температура по дням: 92 дня назад + 3 дня вперёд. Обновляем раз в 3 часа."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.cache: dict[tuple[float, float], tuple[float, dict[date, Day], int | None]] = {}

    def daily(self, lat: float | None, lon: float | None, now_ts: float) -> tuple[dict[date, Day], int | None]:
        if not self.enabled or lat is None or lon is None:
            return {}, None
        key = (round(lat, 2), round(lon, 2))
        hit = self.cache.get(key)
        if hit and now_ts - hit[0] < 3 * 3600:
            return hit[1], hit[2]
        q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "daily": DAILY,
                                    "past_days": 92, "forecast_days": 3, "timezone": "auto"})
        try:
            data = _get(f"https://api.open-meteo.com/v1/forecast?{q}")
        except Exception as e:
            log.warning("weather failed: %s", e)
            return (hit[1], hit[2]) if hit else ({}, None)
        out, offset = _days(data), data.get("utc_offset_seconds")
        self.cache[key] = (now_ts, out, offset)
        return out, offset

    def day(self, lat, lon, day: date, now_ts: float) -> Day:
        return self.daily(lat, lon, now_ts)[0].get(day, Day(None, None))

    def fill(self, lat, lon, sales, now_ts: float, archive: bool = False) -> list:
        """Дописывает дождь и температуру в продажи, где их нет. Возвращает дополненные строки."""
        missing = [s for s in sales if s.rain is None or s.temp is None]
        if not missing or not self.enabled:
            return []
        known = dict(self.daily(lat, lon, now_ts)[0])
        if archive:
            old = [s.day for s in missing if s.day not in known]
            if old:
                # архив отстаёт от сегодняшнего дня на несколько дней, свежие дни берём из прогноза
                known = {**history(lat, lon, min(old), min(max(old), date.today() - timedelta(days=2))), **known}
        filled = []
        for s in missing:
            w = known.get(s.day)
            if w:
                s.rain = w.rain if s.rain is None else s.rain
                s.temp = w.temp if s.temp is None else s.temp
                filled.append(s)
        return filled

# bot/core/web.py
# Личный кабинет заведения: веб-сервер на стандартной библиотеке.
#
# Работает в том же процессе, что и бот, и читает ту же базу: всё, что отмечено
# в Telegram, сразу видно в кабинете, и наоборот. Вход - по коду из бота (/web).
log = logging.getLogger("web")
KABINET_HTML: str | None = None   # однофайловая версия бота подставляет сюда страницу кабинета
COOKIE = "sr_session"
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _minutes(hhmm: str) -> int:
    h, m = map(int, (hhmm or "00:00").split(":"))
    return h * 60 + m


def phase_of(chat, now) -> str:
    """Где мы в рабочем дне: ночь -> утро (план) -> вечер (витрина) -> итоги."""
    t = now.hour * 60 + now.minute
    if t < _minutes(chat["morning"]):
        return "night"
    if t < _minutes(chat["evening"]):
        return "morning"
    if t < _minutes(chat["closing"]):
        return "evening"
    return "closing"


def _wnum(v, lo=None, hi=None, integer=False):
    if v in (None, ""):
        return None
    try:
        x = float(str(v).replace(",", ".").replace(" ", ""))
    except ValueError:
        raise ValueError(f"не число: {v}")
    if math.isnan(x) or (lo is not None and x < lo) or (hi is not None and x > hi):
        raise ValueError(f"значение вне диапазона: {v}")
    return int(round(x)) if integer else x


def _wdate(v, field="дата") -> date:
    try:
        return date.fromisoformat(str(v or ""))
    except ValueError:
        raise ValueError(f"{field}: нужна дата")


def _wname(v, field="название") -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip()
    if not s:
        raise ValueError(f"пустое поле: {field}")
    return s[:60]


class Cabinet:
    """Вся логика кабинета поверх бота: состояние дня и действия."""

    def __init__(self, bot):
        self.bot = bot
        self._bt_cache: dict[int, tuple[int, dict | None]] = {}

    def _item(self, r: Recommendation, qty=None, mean=None) -> dict:
        q = r.qty if qty is None else qty
        m = r.mean if mean is None else mean
        return {"product": r.product, "qty": q, "mean": round(m, 2), "sd": round(r.sd, 2),
                "reasons": r.reasons, "new": r.history_days == 0,
                "threshold": evening_threshold(Recommendation(r.product, m, 0, q, 0))}

    def _summary(self, cid: int) -> dict | None:
        sales = self.bot.db.sales(cid)
        key = len(sales)
        hit = self._bt_cache.get(cid)
        if hit and hit[0] == key:
            return hit[1]
        bt = backtest(sales, self.bot.db.products(cid, include_inactive=True)) if sales else None
        res = None
        if bt and bt.waste_now > 0:
            res = {"days": bt.days, "waste_now": round(bt.waste_now), "waste_new": round(bt.waste_new),
                   "lost_now": round(bt.lost_now), "lost_new": round(bt.lost_new),
                   "share_now": round(bt.waste_units_now / bt.produced_now * 100, 1) if bt.produced_now else None,
                   "share_new": round(bt.waste_units_new / bt.produced_new * 100, 1) if bt.produced_new else None}
        self._bt_cache[cid] = (key, res)
        return res

    def _products(self, cid: int) -> list[dict]:
        db = self.bot.db
        hist = db.history_days(cid)
        products = [{"name": p.name, "price": p.price, "cost": p.cost, "shelf_life": p.shelf_life,
                     "default_qty": p.default_qty, "history_days": hist.get(p.name, 0)}
                    for p in db.products(cid).values()]
        known = {p["name"] for p in products}
        for name in sorted(hist):  # позиции из продаж без карточки товара
            if name not in known and name not in db.inactive_products(cid):
                products.append({"name": name, "price": None, "cost": None, "shelf_life": None,
                                 "default_qty": None, "history_days": hist[name]})
        products.sort(key=lambda p: p["name"].lower())
        return products

    def _batches(self, cid: int, today: date) -> list[dict]:
        batches = []
        for x in self.bot.db.batches(cid):
            exp = date.fromisoformat(x["expires"])
            st, left = expiry_status(exp, today)
            level = "bad" if left <= 0 else "warn" if left <= 2 else "ok"
            batches.append({"id": x["id"], "item": x["item"], "qty": x["qty"], "expires": x["expires"],
                            "label": st, "left": left, "level": level,
                            "tip": ("спишите" if left < 0 else expiry_tip(x["item"])) if left <= 1 else None})
        return batches

    def state(self, chat) -> dict:
        b, db = self.bot, self.bot.db
        cid = chat["chat_id"]
        now = b.now(chat)
        today = now.date()
        tomorrow = today + timedelta(days=1)

        # план на сегодня: количество фиксируется утром, причины считаем заново
        recs_t, info_t = b.recommendations(chat, today)
        saved = {r["product"]: r for r in db.plan(cid, today)}
        missing = [r for r in recs_t if r.product not in saved]
        if missing:
            db.save_plan(cid, today, missing)
            saved = {r["product"]: r for r in db.plan(cid, today)}
        items = []
        for r in recs_t:
            row = saved.get(r.product)
            it = self._item(r, row["qty"] if row else None, row["mean"] if row else None)
            s = db.sale(cid, today, r.product)
            it["record"] = None if not s else {
                "soldout": s["soldout"], "baked": s["baked"],
                "left": None if s["soldout"] else max(0, round((s["baked"] or 0) - (s["sold"] or 0)))}
            items.append(it)
        items.sort(key=lambda x: -x["qty"])

        recs_m, info_m = b.recommendations(chat, tomorrow)
        tom = [dict(self._item(r), habit=round(info_m["habit"][r.product]) if r.product in info_m["habit"] else None)
               for r in recs_m]

        products = self._products(cid)
        batches = self._batches(cid, today)

        sales = db.sales(cid)
        days = sorted({s.day for s in sales})
        return {
            "mode": "server",
            "business": {"name": chat["business"] or "", "city": chat["city"] or "", "tz": chat["tz"] or "",
                         "is_example": bool(chat["is_demo"])},
            "now": now.strftime("%Y-%m-%dT%H:%M"), "phase": phase_of(chat, now),
            "times": {"morning": chat["morning"], "evening": chat["evening"], "closing": chat["closing"]},
            "today": {"date": today.isoformat(), "rain": info_t["rain"], "temp": info_t["temp"],
                      "event": info_t["event"], "items": items},
            "tomorrow": {"date": tomorrow.isoformat(), "rain": info_m["rain"], "temp": info_m["temp"],
                         "event": info_m["event"],
                         "rain_effect": round(info_m["rain_eff"], 3), "items": tom},
            "products": products,
            "removed": sorted(db.inactive_products(cid)),
            "batches": batches,
            "data": {"rows": len(sales), "first": days[0].isoformat() if days else None,
                     "last": days[-1].isoformat() if days else None},
            "summary": self._summary(cid),
        }

    def sheet(self, chat) -> dict:
        """Данные для таблиц кабинета: продажи, товары, партии и план на 7 дней."""
        b, db = self.bot, self.bot.db
        cid = chat["chat_id"]
        today = b.today(chat)
        sales = [{"day": s.day.isoformat(), "product": s.product, "baked": s.baked, "sold": s.sold,
                  "soldout": s.soldout, "rain": s.rain, "temp": s.temp} for s in db.sales(cid)]
        days = [today + timedelta(days=i) for i in range(7)]
        fixed = {r["product"]: r["qty"] for r in db.plan(cid, today)}
        week: dict[str, list[int]] = {}
        for i, d in enumerate(days):
            recs, _ = b.recommendations(chat, d)
            for r in recs:
                week.setdefault(r.product, [0] * 7)[i] = fixed.get(r.product, r.qty) if i == 0 else r.qty
        rows = [{"product": p, "qty": q} for p, q in sorted(week.items(), key=lambda kv: -sum(kv[1]))]
        return {"today": today.isoformat(), "sales": sales, "products": self._products(cid),
                "batches": self._batches(cid, today), "week": {"days": [d.isoformat() for d in days], "rows": rows}}

    def act(self, chat, path: str, body: dict) -> dict:
        b, db = self.bot, self.bot.db
        cid = chat["chat_id"]
        today = b.today(chat)
        if path == "/api/product":
            old = body.get("old_name") or None
            p = Product(_wname(body.get("name")), _wnum(body.get("price"), 0, 1e6), _wnum(body.get("cost"), 0, 1e6),
                        _wnum(body.get("shelf_life"), 0, 3650, integer=True), _wnum(body.get("default_qty"), 0, 1e5))
            if p.price is not None and p.cost is not None and p.cost > p.price:
                raise ValueError("себестоимость больше цены — проверьте цифры")
            db.save_product(cid, p, old)
            return {"ok": True}
        if path == "/api/product/remove":
            db.set_product_active(cid, _wname(body.get("name")), False)
            return {"ok": True}
        if path == "/api/product/restore":
            db.set_product_active(cid, _wname(body.get("name")), True)
            return {"ok": True}
        if path == "/api/batch":
            item = _wname(body.get("item"), "продукт")
            qty = str(body.get("qty") or "").strip()[:20]
            exp = body.get("expires")
            if not exp:
                made = date.fromisoformat(body.get("made") or today.isoformat())
                p = db.products(cid).get(item)
                if not p or not p.shelf_life:
                    raise ValueError("укажите дату «годен до» или срок хранения у товара")
                exp = (made + timedelta(days=p.shelf_life)).isoformat()
            db.add_batch(cid, item, qty, date.fromisoformat(exp))
            return {"ok": True}
        if path == "/api/batch/close":
            status = body.get("status")
            if status not in ("used", "written_off", "deleted"):
                raise ValueError("неизвестное действие")
            db.close_batch(cid, int(body.get("id")), status)
            return {"ok": True}
        if path == "/api/sales":
            ups, dels = body.get("upsert") or [], body.get("delete") or []
            if len(ups) + len(dels) > 20000:
                raise ValueError("слишком много строк за раз, загрузите файлом")
            rows = []
            for x in ups:
                product = _wname(x.get("product"), "товар")
                sold = _wnum(x.get("sold"), 0, 1e5)
                if sold is None:
                    raise ValueError(f"{product}: не указано, сколько продано")
                baked = _wnum(x.get("baked"), 0, 1e5)
                if baked is not None and sold > baked:
                    raise ValueError(f"{product}: продано больше, чем испечено")
                soldout = x.get("soldout") or None
                if soldout and not TIME_RE.match(soldout):
                    raise ValueError("время в формате 15:30")
                rain = x.get("rain")
                rows.append(Sale(_wdate(x.get("day")), product, sold, baked, soldout,
                                 None if rain is None else bool(rain), _wnum(x.get("temp"), -60, 50)))
            for x in dels:
                db.delete_sale(cid, _wdate(x.get("day")), _wname(x.get("product"), "товар"))
            for s in rows:
                db.put_sale(cid, s)
            db.db.commit()
            self._bt_cache.pop(cid, None)
            return {"ok": True, "rows": len(rows)}
        if path == "/api/batch/edit":
            db.update_batch(cid, int(body.get("id")), _wname(body.get("item"), "продукт"),
                            str(body.get("qty") or "").strip()[:20], _wdate(body.get("expires"), "годен до"))
            return {"ok": True}
        if path == "/api/record":
            product = _wname(body.get("product"))
            soldout = body.get("soldout")
            if soldout and not TIME_RE.match(soldout):
                raise ValueError("время в формате 15:30")
            left = None if soldout else _wnum(body.get("left"), 0, 1e5, integer=True)
            if not soldout and left is None:
                raise ValueError("укажите остаток или время, когда товар закончился")
            b.record(chat, today, product, left, soldout)
            self._bt_cache.pop(cid, None)
            return {"ok": True}
        if path == "/api/event":
            f = _wnum(body.get("factor"), 0.3, 3)
            db.set_event(cid, today + timedelta(days=1), None if not f or abs(f - 1) < 1e-9 else f)
            return {"ok": True}
        if path == "/api/settings":
            fields = {}
            if "business" in body:
                fields["business"] = str(body.get("business") or "").strip()[:80]
            for k in ("morning", "evening", "closing"):
                if body.get(k):
                    if not TIME_RE.match(body[k]):
                        raise ValueError("время в формате 06:00")
                    h, m = body[k].split(":")
                    fields[k] = f"{int(h):02d}:{m}"
            if fields:
                db.set_chat(cid, **fields)
            return {"ok": True}
        if path == "/api/upload":
            text = str(body.get("csv") or "")
            if len(text) > 5_000_000:
                raise ValueError("файл больше 5 МБ")
            try:
                sales, products = parse_csv(text)
            except ImportErrorRu as e:
                raise ValueError(str(e))
            if chat["is_demo"]:
                db.clear_sales(cid)
                db.set_chat(cid, is_demo=0)
            b.weather.fill(chat["lat"], chat["lon"], sales, time.time(), archive=True)
            db.import_sales(cid, sales, products)
            self._bt_cache.pop(cid, None)
            return {"ok": True, "rows": len(sales), "products": len(products)}
        if path == "/api/example":
            b.load_example(chat)
            self._bt_cache.pop(cid, None)
            return {"ok": True}
        raise LookupError(path)


def make_handler(cab: Cabinet, page: str):
    bot = cab.bot

    class Handler(BaseHTTPRequestHandler):
        server_version = "Svezhiy/1.0"

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, obj=None, ctype="application/json; charset=utf-8", body: bytes | None = None,
                  headers: dict | None = None):
            data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _token(self) -> str | None:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c[COOKIE].value if COOKIE in c else None

        def _chat(self):
            cid = bot.db.session_chat(self._token())
            return bot.db.chat(cid) if cid else None

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/kabinet", "/kabinet.html"):
                return self._send(200, ctype="text/html; charset=utf-8", body=page.encode("utf-8"))
            if path == "/api/ping":
                return self._send(200, {"ok": True, "app": "svezhiy"})
            if path == "/api/state":
                with bot.lock:
                    chat = self._chat()
                    if not chat:
                        return self._send(401, {"error": "login"})
                    return self._send(200, cab.state(chat))
            if path == "/api/sheet":
                with bot.lock:
                    chat = self._chat()
                    if not chat:
                        return self._send(401, {"error": "login"})
                    return self._send(200, cab.sheet(chat))
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return self._send(415, {"error": "нужен JSON"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 6_000_000:
                    return self._send(413, {"error": "слишком большой запрос"})
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "неверный запрос"})
            with bot.lock:
                if path == "/api/login":
                    token = bot.db.use_code(str(body.get("code") or ""))
                    if not token:
                        return self._send(403, {"error": "Код не подошёл или устарел. Получите новый в боте командой /web"})
                    cookie = f"{COOKIE}={token}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Lax"
                    return self._send(200, {"ok": True}, headers={"Set-Cookie": cookie})
                if path == "/api/logout":
                    if self._token():
                        bot.db.drop_session(self._token())
                    return self._send(200, {"ok": True}, headers={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})
                chat = self._chat()
                if not chat:
                    return self._send(401, {"error": "login"})
                try:
                    res = cab.act(chat, path, body)
                except LookupError:
                    return self._send(404, {"error": "not found"})
                except (ValueError, TypeError) as e:
                    return self._send(400, {"error": str(e) or "неверные данные"})
                except Exception:
                    log.exception("web action failed")
                    return self._send(500, {"error": "внутренняя ошибка"})
                return self._send(200, res)

    return Handler


def load_page(root: Path) -> str:
    f = root / "web" / "kabinet.html"
    if f.is_file():
        return f.read_text(encoding="utf-8")
    if KABINET_HTML:
        return KABINET_HTML
    return "<!doctype html><meta charset=utf-8><p>Нет файла web/kabinet.html рядом с ботом.</p>"


def start_web(bot, host: str, port: int, root: Path) -> ThreadingHTTPServer | None:
    cab = Cabinet(bot)
    try:
        srv = ThreadingHTTPServer((host, port), make_handler(cab, load_page(root)))
    except OSError as e:
        log.warning("Кабинет не запущен: порт %s занят или недоступен (%s). Укажите другой: WEB_PORT=8090", port, e)
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="web", daemon=True).start()
    log.info("Личный кабинет: http://%s:%s", "localhost" if host in ("127.0.0.1", "0.0.0.0") else host, port)
    return srv

# страница личного кабинета
KABINET_HTML = "<!doctype html>\n<html lang=\"ru\">\n<head>\n<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width,initial-scale=1,viewport-fit=cover\">\n<link rel=\"icon\" type=\"image/svg+xml\" href=\"data:image/svg+xml,%3Csvg%20xmlns%3D%27http%3A//www.w3.org/2000/svg%27%20viewBox%3D%270%200%2032%2032%27%3E%3Crect%20width%3D%2732%27%20height%3D%2732%27%20rx%3D%278%27%20fill%3D%27%232346C8%27/%3E%3Ctext%20x%3D%2716%27%20y%3D%2722.5%27%20text-anchor%3D%27middle%27%20font-family%3D%27Arial%2Csans-serif%27%20font-size%3D%2719%27%20font-weight%3D%27700%27%20fill%3D%27%23fff%27%3E%D0%A1%3C/text%3E%3C/svg%3E\">\n<style>:root{padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n</head>\n<body>\n<title>Кабинет «Свежий расчёт»</title>\n<meta name=\"description\" content=\"Личный кабинет заведения: план на день, витрина, итоги, товары и сроки годности.\">\n<meta name=\"color-scheme\" content=\"dark\">\n<meta name=\"theme-color\" content=\"#2E261D\">\n<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">\n<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>\n<link rel=\"stylesheet\" href=\"https://fonts.googleapis.com/css2?family=Golos+Text:wght@400..800&display=swap\">\n<style>\n:root {\n  color-scheme: dark;\n  --bg: #15120F; --surface: #1D1915; --ink: #F1EBE3; --ink-2: #B9AFA4; --ink-3: #8E847A; --line: #352E28;\n  --kraft: #2E261D; --kraft-2: #3A3025; --kraft-ink: #EBDCC0; --paper: #F4EFE4;\n  --stamp: #93A8FF; --stamp-ink: #0E1533; --stamp-soft: #222A4C;\n  --ok: #6CC08F; --ok-soft: #1C3326; --warn: #E3AB4E; --warn-soft: #3A2C14; --bad: #F2907A; --bad-soft: #3E211A;\n  --shadow: 0 1px 0 rgba(0,0,0,.3), 0 14px 30px -12px rgba(0,0,0,.7);\n  --f-display: \"Golos Text\", system-ui, -apple-system, \"Segoe UI\", Roboto, Arial, sans-serif;\n  --f-body: \"Golos Text\", system-ui, -apple-system, \"Segoe UI\", Roboto, Arial, sans-serif;\n  --f-mono: \"Golos Text\", system-ui, -apple-system, \"Segoe UI\", Roboto, Arial, sans-serif;\n}\n* { box-sizing: border-box; }\nbody { background: var(--bg); color: var(--ink); font: 400 15px/1.5 var(--f-body); margin: 0; -webkit-font-smoothing: antialiased; }\nh1, h2, h3 { margin: 0; text-wrap: balance; }\np { margin: 0; }\na { color: inherit; }\n:focus-visible { outline: 2px solid var(--stamp); outline-offset: 2px; border-radius: 6px; }\n@media (prefers-reduced-motion: reduce) { *, *::before, *::after { animation: none !important; transition: none !important; } }\n.wrap { max-width: 1160px; margin: 0 auto; padding-left: max(16px, env(safe-area-inset-left, 0px)); padding-right: max(16px, env(safe-area-inset-right, 0px)); }\n.num, table, .mono { font-variant-numeric: tabular-nums; }\n.mono { font-family: var(--f-mono); }\n\n/* шапка */\n.top { background: var(--kraft); color: var(--kraft-ink); }\n.top-in { display: flex; align-items: center; gap: 12px 20px; padding-block: 14px; flex-wrap: wrap; }\n.brand { display: flex; align-items: center; gap: 10px; text-decoration: none; color: inherit; margin-right: auto; min-width: 0; }\n.brand-name { font: 700 18px/1.1 var(--f-display); }\n.brand small { display: block; font: 500 13px/1.2 var(--f-body); opacity: .8; }\n.clock { font: 600 14px var(--f-mono); white-space: nowrap; }\n.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 40px; padding: 0 16px; border-radius: 999px; font: 600 14px/1 var(--f-body); border: 1.5px solid transparent; cursor: pointer; text-decoration: none; background: none; color: inherit; white-space: nowrap; }\n.btn-primary { background: var(--stamp); color: var(--stamp-ink); }\n.btn-ghost { border-color: currentColor; }\n.btn-quiet { color: var(--ink-2); border-color: var(--line); background: var(--surface); }\n.btn-quiet:hover { color: var(--ink); border-color: var(--ink-3); }\n.btn-danger { color: var(--bad); border-color: var(--bad-soft); background: var(--surface); }\n.btn-sm { min-height: 32px; padding: 0 12px; font-size: 13px; }\n.btn:disabled { opacity: .5; cursor: default; }\n\n\n/* лента дня */\n.day { margin-top: 22px; background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 16px 18px 14px; box-shadow: var(--shadow); }\n.day-head { display: flex; justify-content: space-between; align-items: baseline; gap: 8px 16px; flex-wrap: wrap; margin-bottom: 14px; }\n.day-head h1 { font: 700 clamp(22px, 3vw, 28px)/1.15 var(--f-display); }\n.day-head .upd { font-size: 13px; color: var(--ink-3); display: inline-flex; align-items: center; gap: 6px; }\n.pulse { width: 7px; height: 7px; border-radius: 50%; background: var(--ok); animation: pulse 2s ease-in-out infinite; }\n@keyframes pulse { 50% { opacity: .3; } }\n.ribbon { position: relative; display: grid; grid-template-columns: var(--cols); gap: 4px; }\n.seg { position: relative; text-align: left; border: 0; cursor: pointer; padding: 10px 12px 9px; border-radius: 10px; background: var(--bg); color: var(--ink-2); font: 500 13px/1.25 var(--f-body); min-width: 0; }\n.seg b { display: block; font: 600 15px/1.2 var(--f-body); color: var(--ink); }\n.seg .t { font: 600 12px var(--f-mono); color: var(--ink-3); }\n.seg.past { opacity: .62; }\n.seg.now { background: var(--stamp-soft); }\n.seg.now b { color: var(--stamp); }\n.seg[aria-pressed=\"true\"] { box-shadow: inset 0 0 0 2px var(--stamp); }\n.now-tag { font: 700 12.5px/1 var(--f-mono); background: var(--stamp); color: var(--stamp-ink); padding: 3px 6px; border-radius: 5px; margin-left: 6px; vertical-align: 1px; }\n.track { position: relative; height: 4px; background: var(--line); border-radius: 2px; margin-top: 10px; }\n.track i { position: absolute; left: 0; top: 0; bottom: 0; background: var(--stamp); border-radius: 2px; }\n.track span { position: absolute; top: 50%; width: 12px; height: 12px; border-radius: 50%; background: var(--stamp); border: 2px solid var(--surface); transform: translate(-50%, -50%); }\n.seg { overflow: hidden; }\n.hint .mono { overflow-wrap: anywhere; }\n@media (max-width: 620px) {\n  .ribbon { grid-template-columns: repeat(3, minmax(0, 1fr)) !important; }\n  .seg { padding: 8px; } .seg .desc { display: none; }\n  .now-tag { display: table; margin: 3px 0 2px; }\n}\n\n/* вкладки */\n.tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--line); margin: 22px 0 18px; overflow-x: auto; }\n.tabs button { border: 0; background: none; font: 600 15px/1 var(--f-body); color: var(--ink-3); padding: 12px 12px 14px; cursor: pointer; border-bottom: 2.5px solid transparent; margin-bottom: -1px; white-space: nowrap; }\n.tabs button[aria-selected=\"true\"] { color: var(--ink); border-bottom-color: var(--stamp); }\n.tabs .cnt { font: 600 11px var(--f-mono); background: var(--bad-soft); color: var(--bad); border-radius: 999px; padding: 2px 6px; margin-left: 4px; }\n\n.view { padding-bottom: 56px; display: grid; gap: 18px; }\n.card { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 18px; min-width: 0; }\n.card h2 { font: 700 20px/1.25 var(--f-display); margin-bottom: 4px; }\n.card .sub { color: var(--ink-2); font-size: 14px; margin-bottom: 14px; }\n.kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; }\n.kpi { background: var(--surface); border: 1px solid var(--line); border-radius: 14px; padding: 14px 16px; }\n.kpi b { display: block; font: 700 30px/1.05 var(--f-display); }\n.kpi span { color: var(--ink-2); font-size: 13px; }\n.kpi.warn b { color: var(--warn); }\n.kpi.bad b { color: var(--bad); }\n\n.table-wrap { overflow-x: auto; }\ntable.t { width: 100%; border-collapse: collapse; font-size: 14.5px; }\n.t th { text-align: left; font: 600 12.5px/1.2 var(--f-mono); color: var(--ink-3); padding: 0 8px 10px; border-bottom: 1px solid var(--line); white-space: nowrap; }\n.t td { padding: 11px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }\n.t tr:last-child td { border-bottom: 0; }\n.t .r { text-align: right; white-space: nowrap; }\n.t .name { font-weight: 600; }\n.t .why { font-size: 13px; color: var(--ink-2); margin-top: 2px; }\n.t .big { font: 700 20px/1 var(--f-mono); }\n.t .muted { color: var(--ink-3); }\n.t tfoot td { font-weight: 700; border-top: 1px solid var(--line); border-bottom: 0; }\n.chip { display: inline-flex; align-items: center; gap: 5px; font: 600 12px/1 var(--f-body); padding: 5px 9px; border-radius: 999px; white-space: nowrap; }\n.chip.ok { background: var(--ok-soft); color: var(--ok); }\n.chip.warn { background: var(--warn-soft); color: var(--warn); }\n.chip.bad { background: var(--bad-soft); color: var(--bad); }\n.chip.new { background: var(--stamp-soft); color: var(--stamp); }\n.chip.mute { background: var(--bg); color: var(--ink-3); }\n\ninput[type=\"text\"], input[type=\"number\"], input[type=\"date\"], input[type=\"time\"], select {\n  font: 500 15px/1.2 var(--f-body); color: var(--ink); background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 8px 10px; min-height: 40px; width: 100%; min-width: 0; }\ninput.small { width: 84px; }\n.form { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); align-items: end; }\n.form label { display: grid; gap: 4px; font-size: 12.5px; color: var(--ink-3); min-width: 0; }\n.form .wide { grid-column: span 2; }\n@media (max-width: 520px) { .form .wide { grid-column: auto; } }\n.err { color: var(--bad); font-weight: 600; font-size: 14px; }\n.hint { font-size: 13px; color: var(--ink-3); }\n.acts { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }\n.slots { display: flex; gap: 4px; flex-wrap: wrap; }\n.slot { border: 1px solid var(--line); background: var(--surface); color: var(--ink-2); font: 500 12.5px/1 var(--f-body); padding: 8px 9px; border-radius: 8px; cursor: pointer; min-height: 32px; }\n.slot:hover { border-color: var(--ink-3); color: var(--ink); }\n.left-in { display: flex; gap: 4px; align-items: center; }\n.left-in input { width: 70px; min-height: 32px; padding: 5px 8px; }\n.progress { height: 6px; background: var(--bg); border-radius: 3px; overflow: hidden; margin-top: 6px; }\n.progress i { display: block; height: 100%; background: var(--ok); }\n.removed { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }\n.empty { padding: 26px; text-align: center; color: var(--ink-3); border: 1px dashed var(--line); border-radius: 12px; }\n.grid2 { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 18px; }\n@media (max-width: 860px) { .grid2 { grid-template-columns: minmax(0, 1fr); } }\n.cmp { display: grid; grid-template-columns: 110px minmax(0, 1fr) 100px; gap: 8px; align-items: center; font-size: 14px; margin-top: 6px; }\n.cmp .bar { height: 12px; background: var(--bg); border-radius: 4px; overflow: hidden; }\n.cmp .bar i { display: block; height: 100%; border-radius: 0 4px 4px 0; }\n.cmp .v { text-align: right; font: 600 14px var(--f-mono); }\n\n/* вход */\n.login { max-width: 440px; margin: 48px auto; background: var(--surface); border: 1px solid var(--line); border-radius: 18px; padding: 26px; box-shadow: var(--shadow); display: grid; gap: 14px; }\n.login h1 { font: 700 26px/1.15 var(--f-display); }\n.login ol { margin: 0; padding-left: 20px; color: var(--ink-2); display: grid; gap: 6px; }\n.code-in { font: 600 26px/1 var(--f-mono) !important; letter-spacing: .3em; text-align: center; }\n\n.toast { position: fixed; left: 50%; bottom: max(20px, env(safe-area-inset-bottom, 0px)); transform: translateX(-50%); background: var(--ink); color: var(--bg); padding: 10px 16px; border-radius: 10px; font-weight: 600; z-index: 20; box-shadow: var(--shadow); max-width: calc(100% - 32px); }\n.loading { padding: 60px 0; text-align: center; color: var(--ink-3); }\n</style>\n\n<header class=\"top\">\n  <div class=\"wrap top-in\">\n    <a class=\"brand\" id=\"brand\" href=\"index.html\"><span><span class=\"brand-name\">Свежий расчёт</span><small id=\"biz\">Личный кабинет</small></span></a>\n    <span class=\"clock\" id=\"clock\"></span>\n    <span id=\"top-actions\"></span>\n  </div>\n</header>\n<main class=\"wrap\">\n  <div class=\"loading\" id=\"loading\">Загружаю кабинет…</div>\n  <section class=\"login\" id=\"login\" hidden aria-labelledby=\"login-h\">\n    <h1 id=\"login-h\">Вход в кабинет</h1>\n    <ol>\n      <li>Откройте бота <a id=\"login-bot\" href=\"https://t.me/svezhiy_raschet_bot\" target=\"_blank\" rel=\"noopener\">@svezhiy_raschet_bot</a>.</li>\n      <li>Отправьте команду <b>/web</b> или нажмите «Личный кабинет».</li>\n      <li>Введите код из 6 цифр, он действует 10 минут.</li>\n    </ol>\n    <form id=\"login-form\" style=\"display:grid;gap:10px\">\n      <input class=\"code-in\" id=\"code\" type=\"text\" inputmode=\"numeric\" autocomplete=\"one-time-code\" maxlength=\"6\" placeholder=\"000000\" aria-label=\"Код из бота\">\n      <button class=\"btn btn-primary\" type=\"submit\">Войти</button>\n      <p class=\"err\" id=\"login-err\" hidden></p>\n    </form>\n  </section>\n  <section id=\"app\" hidden>\n    <div class=\"day\" id=\"day\"></div>\n    <nav class=\"tabs\" role=\"tablist\" aria-label=\"Разделы кабинета\">\n      <button role=\"tab\" data-tab=\"today\" aria-selected=\"true\">Сегодня</button>\n      <button role=\"tab\" data-tab=\"tomorrow\" aria-selected=\"false\">Завтра</button>\n      <button role=\"tab\" data-tab=\"products\" aria-selected=\"false\">Товары</button>\n      <button role=\"tab\" data-tab=\"batches\" aria-selected=\"false\">Сроки годности</button>\n      <button role=\"tab\" data-tab=\"tables\" aria-selected=\"false\">Таблицы</button>\n      <button role=\"tab\" data-tab=\"data\" aria-selected=\"false\">Данные и настройки</button>\n    </nav>\n    <div class=\"view\" id=\"view\"></div>\n  </section>\n</main>\n<div class=\"toast\" id=\"toast\" role=\"status\" hidden></div>\n\n<script>// модель прогноза, та же что в bot/core/forecast.py\n(function (root) {\n  \"use strict\";\n  var WD = [\"понедельник\", \"вторник\", \"среда\", \"четверг\", \"пятница\", \"суббота\", \"воскресенье\"];\n  var WDS = [\"пн\", \"вт\", \"ср\", \"чт\", \"пт\", \"сб\", \"вс\"];\n  var DEFAULT_RAIN = -0.10, SHARE_EVENING = 0.22, DISCOUNT_K = 1.2;\n\n  // даты как номера дней (UTC), чтобы не зависеть от часового пояса\n  function dnum(iso) { var p = iso.split(\"-\"); return Math.round(Date.UTC(+p[0], +p[1] - 1, +p[2]) / 864e5); }\n  function diso(n) { return new Date(n * 864e5).toISOString().slice(0, 10); }\n  function wday(n) { return (new Date(n * 864e5).getUTCDay() + 6) % 7; }\n  function ddmm(n) { var s = diso(n); return s.slice(8, 10) + \".\" + s.slice(5, 7); }\n\n  function parseTime(s) {\n    if (!s) return null;\n    s = String(s).trim().replace(\".\", \":\");\n    if (s.indexOf(\":\") < 0) return null;\n    var p = s.split(\":\"), h = parseInt(p[0], 10), m = parseInt(p[1], 10);\n    if (isNaN(h) || isNaN(m)) return null;\n    return h + m / 60;\n  }\n  function demand(s, oh, ch) {\n    oh = oh == null ? 7 : oh; ch = ch == null ? 21 : ch;\n    var t = parseTime(s.soldout);\n    if (t == null) return s.sold;\n    var f = Math.min(1, Math.max(0, (t - oh) / (ch - oh)));\n    return s.sold * (1 + 0.5 * (1 - f));\n  }\n  function rainEffect(sales) {\n    var dry = {}, ratios = [];\n    sales.forEach(function (s) {\n      if (s.rain === false) { var k = s.product + \"|\" + wday(s.day); (dry[k] = dry[k] || []).push(demand(s)); }\n    });\n    sales.forEach(function (s) {\n      if (s.rain === true) {\n        var d = dry[s.product + \"|\" + wday(s.day)];\n        if (d && d.length >= 2) {\n          var b = d.reduce(function (a, x) { return a + x; }, 0) / d.length;\n          if (b > 0) ratios.push(demand(s) / b);\n        }\n      }\n    });\n    if (ratios.length < 3) return { eff: DEFAULT_RAIN, learned: false };\n    var e = ratios.reduce(function (a, x) { return a + x; }, 0) / ratios.length - 1;\n    return { eff: Math.max(-0.35, Math.min(0.2, e)), learned: true };\n  }\n  function criticalRatio(price, cost) {\n    if (!price || cost == null || price <= 0) return 0.6;\n    return Math.max(0.3, Math.min(0.95, (price - cost) / price));\n  }\n  // обратная функция нормального распределения (Acklam)\n  function invNorm(p) {\n    var a = [-39.69683028665376, 220.9460984245205, -275.9285104469687, 138.357751867269, -30.66479806614716, 2.506628277459239],\n        b = [-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972, -13.28068155288572],\n        c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783],\n        d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416], q, r;\n    if (p < 0.02425) { q = Math.sqrt(-2 * Math.log(p)); return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1); }\n    if (p > 1 - 0.02425) { q = Math.sqrt(-2 * Math.log(1 - p)); return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1); }\n    q = p - 0.5; r = q * q;\n    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);\n  }\n  function signed(x) { var v = Math.round(x); return (v > 0 ? \"+\" : v < 0 ? \"−\" : \"\") + Math.abs(v) + \"%\"; }\n\n  function avg(xs) { return xs.reduce(function (a, x) { return a + x; }, 0) / xs.length; }\n  function signedDeg(x) { return Math.abs(Math.round(x)) + \"°\"; }\n  var LEVEL_DAYS = 14, LEVEL_DECAY = 0.9;\n  var TEMP_WINDOW = 365, TEMP_NEAR = 15, TEMP_MIN_DAYS = 60, TEMP_MIN_T = 2.5;\n\n  // на сколько (доля) меняется спрос на каждый градус; 0 - если данных мало или связь ненадёжна\n  function temperatureSlope(hist, target, cleanValue, wf) {\n    var N = TEMP_WINDOW, y = new Array(N + 1).fill(null), tt = new Array(N + 1).fill(null);\n    hist.forEach(function (s) {\n      var age = target - s.day;\n      if (age >= 1 && age <= N) {\n        var f = wf(wday(s.day)) || 1;\n        y[age] = (y[age] || 0) + cleanValue(s) / f;\n        if (s.temp != null) tt[age] = s.temp;\n      }\n    });\n    function prefix(arr) {\n      var sums = [0], cnts = [0];\n      arr.forEach(function (v) { sums.push(sums[sums.length - 1] + (v != null ? v : 0)); cnts.push(cnts[cnts.length - 1] + (v != null ? 1 : 0)); });\n      return [sums, cnts];\n    }\n    var py = prefix(y), pt = prefix(tt), xs = [], rs = [];\n    for (var age = 1; age <= N; age++) {\n      if (y[age] == null || tt[age] == null) continue;\n      var lo = Math.max(0, age - TEMP_NEAR), hi = Math.min(N, age + TEMP_NEAR) + 1;\n      if (py[1][hi] - py[1][lo] === 0 || pt[1][hi] - pt[1][lo] === 0) continue;\n      var localY = (py[0][hi] - py[0][lo]) / (py[1][hi] - py[1][lo]);\n      var localT = (pt[0][hi] - pt[0][lo]) / (pt[1][hi] - pt[1][lo]);\n      if (localY > 0) { xs.push(tt[age] - localT); rs.push(y[age] / localY); }\n    }\n    var n = xs.length;\n    if (n < TEMP_MIN_DAYS) return 0;\n    var mx = avg(xs), mr = avg(rs), vx = 0, cov = 0, i;\n    for (i = 0; i < n; i++) { vx += (xs[i] - mx) * (xs[i] - mx); cov += (xs[i] - mx) * (rs[i] - mr); }\n    vx /= n; cov /= n;\n    if (vx < 4) return 0;\n    var slope = cov / vx, res = 0;\n    for (i = 0; i < n; i++) { var e = rs[i] - mr - slope * (xs[i] - mx); res += e * e; }\n    res /= (n - 2);\n    var se = res > 0 ? Math.sqrt(res / (n * vx)) : 0;\n    if (se === 0 || Math.abs(slope / se) < TEMP_MIN_T) return 0;\n    return Math.max(-0.02, Math.min(0.02, slope * n / (n + 30)));\n  }\n\n  // во сколько раз спрос точки год назад в неделю с этой даты отличался от двух недель до неё\n  function seasonChange(sales, target, rainEff) {\n    var total = {}, a, after = [], before = [], pairs = [];\n    sales.forEach(function (s) {\n      var age = target - s.day;\n      if ((age >= 1 && age <= 56) || (age >= 359 && age <= 379)) {\n        var d = demand(s);\n        total[age] = (total[age] || 0) + (s.rain === true ? d / (1 + rainEff) : d);\n      }\n    });\n    for (a = 359; a <= 379; a++) { if (!(a in total)) return 1; (a <= 365 ? after : before).push(total[a]); }\n    // шум: разница одинаковых дней недели через неделю, последние 8 недель\n    for (a = 1; a < 50; a++) {\n      if (a in total && (a + 7) in total && total[a] + total[a + 7] > 0) {\n        var r = (total[a] - total[a + 7]) / ((total[a] + total[a + 7]) / 2);\n        pairs.push(r * r / 2);\n      }\n    }\n    if (pairs.length < 7 || avg(before) <= 0) return 1;\n    var se = Math.sqrt(avg(pairs) * (1 / 7 + 1 / 14)), ratio = avg(after) / avg(before);\n    return Math.abs(ratio - 1) >= Math.max(0.08, 3 * se) ? ratio : 1;\n  }\n\n  function forecast(sales, product, target, rainTomorrow, eventFactor, rainEff, tempTomorrow) {\n    eventFactor = eventFactor || 1;\n    if (rainEff == null) rainEff = rainEffect(sales).eff;\n    var hist = sales.filter(function (s) { return s.product === product.name && s.day < target; })\n                    .sort(function (x, y) { return y.day - x.day; });\n    var cr = criticalRatio(product.price, product.cost), reasons = [];\n    if (!hist.length) {\n      if (product.default_qty) {\n        var dq = +product.default_qty;\n        return { product: product.name, mean: dq, sd: Math.sqrt(dq), qty: Math.max(0, Math.floor(dq + 0.5)), ratio: cr, reasons: [\"новый товар: истории пока нет, берём ваш обычный объём\"], n: 0 };\n      }\n      return { product: product.name, mean: 0, sd: 0, qty: 0, ratio: cr, reasons: [\"нет истории продаж\"], n: 0 };\n    }\n    function cleanValue(s) { var d = demand(s); return s.rain === true ? d / (1 + rainEff) : d; }\n\n    // спрос, очищенный от дождя, за последние 8 недель\n    var clean = {}, temps = {}, days = [];\n    hist.forEach(function (s) {\n      if (target - s.day > 56) return;\n      if (!(s.day in clean)) { clean[s.day] = 0; days.push(s.day); }\n      clean[s.day] += cleanValue(s);\n      if (s.temp != null) temps[s.day] = s.temp;\n    });\n    // профиль недели: во сколько раз этот день недели выше среднего\n    var overall = avg(days.map(function (k) { return clean[k]; }));\n    function wf(w) {\n      var v = days.filter(function (k) { return wday(k) === w; }).map(function (k) { return clean[k]; });\n      if (v.length < 2 || overall <= 0) return 1;\n      return avg(v) / overall;\n    }\n    // уровень: последние 14 дней, каждый приведён к среднему дню недели;\n    // чем ближе день, тем больше вес: вчера - 1, позавчера - 0.9, и так далее\n    var recentDays = days.filter(function (k) { return target - k <= LEVEL_DAYS; });\n    if (recentDays.length < 5) recentDays = days.slice();\n    var weights = {}, wsum = 0, level = 0;\n    recentDays.forEach(function (k) {\n      var g = wf(wday(k));\n      if (g <= 0) return;\n      weights[k] = Math.pow(LEVEL_DECAY, target - k - 1);\n      wsum += weights[k]; level += weights[k] * clean[k] / g;\n    });\n    level = wsum ? level / wsum : 0;\n    var f = wf(wday(target));\n    if (days.length < 14) reasons.push(\"истории меньше 2 недель — прогноз грубый\");\n    else if (Math.abs(f - 1) >= 0.08) reasons.push(WD[wday(target)] + \": обычно \" + signed((f - 1) * 100) + \" к среднему дню\");\n\n    var resid = [];\n    days.forEach(function (k) { if (target - k <= 28) { var g = wf(wday(k)); if (g > 0) resid.push(clean[k] / g - level); } });\n    var v = resid.length >= 4 ? resid.reduce(function (a, r) { return a + r * r; }, 0) / resid.length * 1.25 * f * f : 0;\n    var base = level * f;\n\n    // температура: связь ищем по истории позиции, день сравниваем с соседними +-15 днями\n    var tempF = 1, diff = null;\n    var tempDays = recentDays.filter(function (k) { return k in weights && k in temps; });\n    if (tempTomorrow != null && tempDays.length) {\n      var slope = temperatureSlope(hist, target, cleanValue, wf);\n      if (slope) {\n        var tw = 0, tsum = 0;\n        tempDays.forEach(function (k) { tw += weights[k]; tsum += weights[k] * temps[k]; });\n        diff = Math.max(-12, Math.min(12, tempTomorrow - tsum / tw));\n        tempF = Math.max(0.75, Math.min(1.3, 1 + slope * diff));\n        if (Math.abs(tempF - 1) >= 0.03) reasons.push((diff < 0 ? \"холоднее\" : \"теплее\") + \" на \" + signedDeg(diff) + \": \" + signed((tempF - 1) * 100) + \" к спросу\");\n      }\n    }\n\n    // сезон: как менялся спрос в эти дни год назад\n    var yoy = seasonChange(sales, target, rainEff), yoyF = Math.max(0.6, Math.min(1.6, 1 + 0.7 * (yoy - 1)));\n    if (yoy !== 1) reasons.push(\"сезон: год назад в эти дни спрос \" + (yoy > 1 ? \"вырос\" : \"снизился\") + \" на \" + Math.round(Math.abs(yoy - 1) * 100) + \"%\");\n\n    var lastSame = null;\n    for (var i = 0; i < hist.length; i++) if (wday(hist[i].day) === wday(target)) { lastSame = hist[i]; break; }\n    var mean = base * tempF * yoyF;\n    if (rainTomorrow) { mean *= 1 + rainEff; reasons.push(\"дождь: \" + signed(rainEff * 100) + \" к спросу\"); }\n    if (eventFactor !== 1) { mean *= eventFactor; reasons.push(\"событие: \" + signed((eventFactor - 1) * 100)); }\n    if (lastSame && lastSame.soldout && target - lastSame.day <= 7)\n      reasons.push(WDS[wday(lastSame.day)] + \" \" + ddmm(lastSame.day) + \" закончились в \" + lastSame.soldout + \" — спрос был выше продаж\");\n    var scale = base ? mean / base : 1;\n    var sd = Math.max(Math.sqrt(v) * scale, Math.sqrt(Math.max(mean, 0)));\n    if (days.length < 14) sd *= 1.3;\n    var z = invNorm(cr), qty = Math.max(0, Math.floor(mean + z * sd + 0.5));\n    if (product.price && product.cost != null) {\n      if (cr >= 0.6) reasons.push(\"маржа \" + Math.round(cr * 100) + \"% — берём с запасом\");\n      else if (cr <= 0.45) reasons.push(\"маржа \" + Math.round(cr * 100) + \"% — впритык, остаток дорого обходится\");\n    }\n    // из чего сложился прогноз: уровень × день недели × температура × сезон × дождь × событие\n    var parts = { level: level, days: Object.keys(weights).length, recent: (target - 1) in weights ? weights[target - 1] / wsum : 0,\n                  week: f, temp: tempF, delta: diff, season: yoyF, rain: rainTomorrow ? 1 + rainEff : 1, event: eventFactor, z: z };\n    return { product: product.name, mean: mean, sd: sd, qty: qty, ratio: cr, reasons: reasons, n: days.length, parts: parts };\n  }\n\n  function eveningThreshold(rec) { return Math.max(1, Math.ceil(rec.mean * SHARE_EVENING * DISCOUNT_K)); }\n  function discountAdvice(rec, left) {\n    var exp = rec.mean * SHARE_EVENING;\n    if (left <= exp * DISCOUNT_K) return { pct: 0, why: \"обычно после 17:00 уходит ~\" + Math.round(exp) + \" шт — скидка не нужна\" };\n    var p = left <= exp * 2 ? 30 : 50;\n    return { pct: p, why: \"обычно после 17:00 уходит ~\" + Math.round(exp) + \" шт, на витрине \" + left + \" — скидка \" + p + \"%\" };\n  }\n\n  function backtest(sales, products, nDays) {\n    nDays = nDays || 28;\n    var withBaked = sales.filter(function (s) { return s.baked != null; });\n    if (!withBaked.length) return null;\n    var allDays = Array.from(new Set(sales.map(function (s) { return s.day; }))).sort(function (a, b) { return a - b; });\n    var test = allDays.slice(-nDays), eff = rainEffect(sales).eff;\n    var r = { days: test.length, wasteNow: 0, wasteNew: 0, lostNow: 0, lostNew: 0, wasteUnitsNow: 0, wasteUnitsNew: 0, producedNow: 0, producedNew: 0, perDay: [] };\n    test.forEach(function (d) {\n      var past = sales.filter(function (s) { return s.day < d; }), day = { day: d, wasteNow: 0, wasteNew: 0 };\n      withBaked.forEach(function (s) {\n        if (s.day !== d) return;\n        var p = products[s.product] || { name: s.product }, price = p.price || 0, cost = p.cost != null ? p.cost : price * 0.4;\n        var rec = forecast(past, p, d, s.rain === true, 1, eff, s.temp), dem = demand(s), soldNew = Math.min(rec.qty, dem);\n        r.wasteNow += (s.baked - s.sold) * cost; r.wasteNew += (rec.qty - soldNew) * cost;\n        r.wasteUnitsNow += s.baked - s.sold; r.wasteUnitsNew += rec.qty - soldNew;\n        r.lostNow += Math.max(0, dem - s.sold) * (price - cost); r.lostNew += Math.max(0, dem - rec.qty) * (price - cost);\n        r.producedNow += s.baked; r.producedNew += rec.qty;\n        day.wasteNow += (s.baked - s.sold) * cost; day.wasteNew += (rec.qty - soldNew) * cost;\n      });\n      r.perDay.push(day);\n    });\n    return r;\n  }\n\n  function expiryStatus(expires, today) {\n    var left = expires - today;\n    if (left < 0) return { label: \"просрочено\", left: left, level: \"bad\" };\n    if (left === 0) return { label: \"истекает сегодня\", left: left, level: \"bad\" };\n    if (left === 1) return { label: \"истекает завтра\", left: left, level: \"warn\" };\n    if (left <= 2) return { label: \"скоро\", left: left, level: \"warn\" };\n    return { label: \"в порядке\", left: left, level: \"ok\" };\n  }\n  function expiryTip(item) {\n    var n = item.toLowerCase();\n    function has(arr) { return arr.some(function (k) { return n.indexOf(k) >= 0; }); }\n    if (has([\"слив\", \"молок\", \"творог\", \"сметан\", \"сыр\", \"кефир\", \"йогурт\", \"маскарпоне\"])) return \"пустите сегодня в крем, начинку или сырники\";\n    if (has([\"ягод\", \"вишн\", \"клубник\", \"яблок\", \"фрукт\", \"банан\", \"груш\"])) return \"используйте в начинку или соус сегодня\";\n    if (has([\"курин\", \"куриц\", \"ветчин\", \"мяс\", \"колбас\", \"рыб\", \"лосос\"])) return \"в сэндвичи и пироги сегодня, остаток — списать\";\n    if (has([\"яйц\", \"яиц\"])) return \"в тесто или выпечку сегодня\";\n    return \"используйте в первую очередь или продайте со скидкой\";\n  }\n\n  var ALIASES = {\n    date: [\"дата\", \"date\", \"день\"], product: [\"товар\", \"product\", \"позиция\", \"наименование\", \"item\"],\n    sold: [\"продано\", \"sold\", \"продажи\", \"количество\", \"qty\", \"кол-во\"], baked: [\"испечено\", \"baked\", \"произведено\", \"выпечено\", \"приготовлено\", \"заказано\", \"закуплено\", \"поставлено\"],\n    soldout: [\"закончилось\", \"soldout\", \"распродано_в\", \"закончился\"], price: [\"цена\", \"price\"],\n    cost: [\"себестоимость\", \"cost\", \"себес\"], rain: [\"дождь\", \"rain\", \"осадки\"], temp: [\"температура\", \"temp\", \"темп\", \"t\"]\n  };\n  function num(v) { v = String(v == null ? \"\" : v).replace(/[\\s ]/g, \"\").replace(\",\", \".\"); if (!v) return null; var x = Number(v); return isNaN(x) ? null : x; }\n  function parseDate(v) {\n    v = String(v).trim(); var m;\n    if ((m = v.match(/^(\\d{4})-(\\d{2})-(\\d{2})$/))) return dnum(v);\n    if ((m = v.match(/^(\\d{1,2})[.\\/](\\d{1,2})[.\\/](\\d{2,4})$/))) {\n      var y = +m[3]; if (y < 100) y += 2000;\n      return dnum(y + \"-\" + (\"0\" + m[2]).slice(-2) + \"-\" + (\"0\" + m[1]).slice(-2));\n    }\n    throw new Error(\"не понял дату «\" + v + \"» — нужен формат 2026-10-17 или 17.10.2026\");\n  }\n  function splitLine(line, delim) {\n    var out = [], cur = \"\", q = false;\n    for (var i = 0; i < line.length; i++) {\n      var ch = line[i];\n      if (ch === '\"') { if (q && line[i + 1] === '\"') { cur += '\"'; i++; } else q = !q; }\n      else if (ch === delim && !q) { out.push(cur); cur = \"\"; }\n      else cur += ch;\n    }\n    out.push(cur);\n    return out;\n  }\n  function parseCSV(text) {\n    text = text.replace(/^﻿/, \"\");\n    var lines = text.split(/\\r?\\n/).filter(function (l) { return l.trim(); });\n    if (lines.length < 2) throw new Error(\"файл пустой — нужна строка заголовков и хотя бы одна строка продаж\");\n    var first = lines[0], delim = (first.split(\";\").length >= first.split(\",\").length) ? \";\" : \",\";\n    var header = splitLine(first, delim).map(function (h) { return h.trim().toLowerCase().replace(/ё/g, \"е\"); });\n    var idx = {};\n    Object.keys(ALIASES).forEach(function (k) {\n      idx[k] = -1;\n      for (var i = 0; i < ALIASES[k].length; i++) { var j = header.indexOf(ALIASES[k][i]); if (j >= 0) { idx[k] = j; break; } }\n    });\n    [\"date\", \"product\", \"sold\"].forEach(function (k) {\n      if (idx[k] < 0) throw new Error(\"нет обязательной колонки «\" + ALIASES[k][0] + \"». Нужны как минимум: дата; товар; продано\");\n    });\n    var sales = [], products = {};\n    for (var n = 1; n < lines.length; n++) {\n      var r = splitLine(lines[n], delim);\n      var get = function (k) { return idx[k] >= 0 && idx[k] < r.length ? r[idx[k]] : \"\"; };\n      var day;\n      try { day = parseDate(get(\"date\")); } catch (e) { throw new Error(\"строка \" + (n + 1) + \": \" + e.message); }\n      var name = String(get(\"product\")).trim(), sold = num(get(\"sold\"));\n      if (!name || sold == null) continue;\n      var rr = String(get(\"rain\")).trim().toLowerCase();\n      var rain = rr === \"\" ? null : [\"1\", \"да\", \"yes\", \"true\", \"дождь\"].indexOf(rr) >= 0;\n      sales.push({ day: day, product: name, sold: sold, baked: num(get(\"baked\")), soldout: String(get(\"soldout\")).trim() || null, rain: rain, temp: num(get(\"temp\")) });\n      var p = products[name] || (products[name] = { name: name, price: null, cost: null });\n      var pr = num(get(\"price\")), co = num(get(\"cost\"));\n      if (pr != null) p.price = pr;\n      if (co != null) p.cost = co;\n    }\n    if (!sales.length) throw new Error(\"не нашёл ни одной строки с продажами\");\n    return { sales: sales, products: products };\n  }\n\n  // прогноз Open-Meteo: день -> { rain, temp }\n  function loadWeather(lat, lon, days) {\n    if (typeof fetch !== \"function\") return Promise.resolve({});\n    var url = \"https://api.open-meteo.com/v1/forecast?latitude=\" + lat + \"&longitude=\" + lon +\n      \"&daily=precipitation_sum,temperature_2m_max,temperature_2m_min&forecast_days=\" + (days || 3) + \"&timezone=auto\";\n    return fetch(url).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {\n      var d = j && j.daily, out = {};\n      if (d) d.time.forEach(function (iso, i) {\n        var hi = d.temperature_2m_max[i], lo = d.temperature_2m_min[i], mm = d.precipitation_sum[i];\n        out[dnum(iso)] = { rain: mm == null ? null : mm >= 1, temp: hi == null || lo == null ? null : Math.round((hi + lo) * 5) / 10 };\n      });\n      return out;\n    }).catch(function () { return {}; });\n  }\n  function deg(t) { var r = Math.round(t); return (r > 0 ? \"+\" : r < 0 ? \"−\" : \"\") + Math.abs(r) + \"°\"; }\n  // средняя температура за 14 дней до дня\n  function recentTemp(sales, day) {\n    var seen = {}, xs = [];\n    sales.forEach(function (s) { if (s.temp != null && day - s.day >= 1 && day - s.day <= 14 && !(s.day in seen)) { seen[s.day] = 1; xs.push(s.temp); } });\n    return xs.length ? avg(xs) : null;\n  }\n\n  var api = { dnum: dnum, diso: diso, wday: wday, ddmm: ddmm, WD: WD, WDS: WDS, demand: demand, rainEffect: rainEffect,\n    criticalRatio: criticalRatio, forecast: forecast, eveningThreshold: eveningThreshold, discountAdvice: discountAdvice,\n    backtest: backtest, expiryStatus: expiryStatus, expiryTip: expiryTip, parseCSV: parseCSV, signed: signed, SHARE_EVENING: SHARE_EVENING,\n    loadWeather: loadWeather, deg: deg, recentTemp: recentTemp };\n  if (typeof module !== \"undefined\" && module.exports) module.exports = api; else root.FreshModel = api;\n})(this);\n</script>\n<script>\n\"use strict\";\nvar BOT_USERNAME = \"svezhiy_raschet_bot\";\nvar DEMO_CSV = \"\";\n</script>\n<script>// таблицы как в Excel: ячейки, клавиатура, вставка из Excel, выгрузка для Excel\n(function (root) {\n  \"use strict\";\n  var WDS = [\"пн\", \"вт\", \"ср\", \"чт\", \"пт\", \"сб\", \"вс\"];\n\n  var CSS = [\n    \".xs-bar{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:center;margin:0 0 10px}\",\n    \".xs-tabs{display:flex;gap:2px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin:0 0 12px}\",\n    \".xs-tabs button{border:0;background:none;font:600 14px/1 var(--f-body);color:var(--ink-3);padding:10px 12px 12px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}\",\n    \".xs-tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--stamp)}\",\n    \".xs-bar select,.xs-bar .xs-btn{font:500 14px/1 var(--f-body);color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 11px;min-height:36px;cursor:pointer}\",\n    \".xs-bar .xs-btn:hover{border-color:var(--ink-3)}\",\n    \".xs-bar .xs-btn[disabled]{opacity:.45;cursor:default}\",\n    \".xs-bar .xs-sp{flex:1}\",\n    \".xs-bar label{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--ink-3)}\",\n    \".xs-file{position:relative;overflow:hidden}\",\n    \".xs-file input{position:absolute;inset:0;opacity:0;cursor:pointer}\",\n    \".xs-hint{font-size:12.5px;color:var(--ink-3);margin:8px 0 0}\",\n    \".xg{border:1px solid var(--line);border-radius:10px;background:var(--surface);outline:none;max-width:100%}\",\n    \".xg:focus-visible{box-shadow:0 0 0 2px var(--stamp-soft)}\",\n    \".xg-scroll{overflow:auto;max-height:min(68vh,620px);border-radius:10px}\",\n    \".xg-t{border-collapse:separate;border-spacing:0;font:400 14px/1.3 var(--f-body);font-variant-numeric:tabular-nums;color:var(--ink);table-layout:fixed;min-width:100%}\",\n    \".xg-t th,.xg-t td{border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:0 10px;height:34px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}\",\n    \".xg-t th:last-child,.xg-t td:last-child{border-right:0}\",\n    \".xg-t thead th{position:sticky;top:0;z-index:2;background:var(--bg);font:600 12.5px/1.2 var(--f-body);color:var(--ink-2);text-align:left;white-space:normal;height:40px}\",\n    \".xg-t th.r,.xg-t td.r{text-align:right}\",\n    \".xg-t .xg-n{position:sticky;left:0;z-index:1;background:var(--bg);color:var(--ink-3);font-size:12px;text-align:right;width:46px;padding:0 8px;user-select:none}\",\n    \".xg-t thead .xg-n{z-index:3}\",\n    \".xg-t td{cursor:cell;user-select:none}\",\n    \".xg-t td.ro{color:var(--ink-2);cursor:default}\",\n    \".xg-t td.in{background:var(--stamp-soft)}\",\n    \".xg-t td.cur{box-shadow:inset 0 0 0 2px var(--stamp)}\",\n    \".xg-t td.bad{background:var(--bad-soft)}\",\n    \".xg-t tr.draft .xg-n{color:var(--stamp)}\",\n    \".xg-t tr.add td{color:var(--ink-3)}\",\n    \".xg-t tfoot td{position:sticky;bottom:0;background:var(--bg);font-weight:600;border-top:1px solid var(--line);border-bottom:0}\",\n    \".xg-t .mute{color:var(--ink-3)}\",\n    \".xg-t .tag{font-size:12.5px;font-weight:600}\",\n    \".xg-t .tag.bad{color:var(--bad)}.xg-t .tag.warn{color:var(--warn)}.xg-t .tag.ok{color:var(--ok)}\",\n    \".xg-in{width:100%;height:32px;border:0;outline:0;background:transparent;font:inherit;color:inherit;padding:0;margin:0}\",\n    \".xg-empty{padding:28px 16px;color:var(--ink-3);text-align:center}\"\n  ].join(\"\\n\");\n\n  function injectCss() {\n    if (document.getElementById(\"xg-css\")) return;\n    var s = document.createElement(\"style\");\n    s.id = \"xg-css\"; s.textContent = CSS;\n    document.head.appendChild(s);\n  }\n  function esc(s) { return String(s == null ? \"\" : s).replace(/[&<>\"]/g, function (c) { return { \"&\": \"&amp;\", \"<\": \"&lt;\", \">\": \"&gt;\", '\"': \"&quot;\" }[c]; }); }\n  function pad(n) { return (\"0\" + n).slice(-2); }\n  function isoDay(y, m, d) {\n    var t = Date.UTC(y, m - 1, d), x = new Date(t);\n    if (x.getUTCFullYear() !== y || x.getUTCMonth() !== m - 1 || x.getUTCDate() !== d) return null;\n    return y + \"-\" + pad(m) + \"-\" + pad(d);\n  }\n  function weekday(iso) { return WDS[(new Date(iso + \"T00:00:00Z\").getUTCDay() + 6) % 7]; }\n  function nf(x, digits) {\n    if (x == null || x === \"\") return \"\";\n    var v = Math.round(x * Math.pow(10, digits || 0)) / Math.pow(10, digits || 0);\n    return String(v).replace(\".\", \",\").replace(\"-\", \"−\");\n  }\n\n  // значение из текста по типу колонки; ошибка — исключение с понятным текстом\n  function parse(col, raw) {\n    var s = String(raw == null ? \"\" : raw).replace(/ /g, \" \").trim();\n    if (s === \"\") {\n      if (col.required) throw new Error(\"«\" + col.title + \"» нельзя оставить пустым\");\n      return null;\n    }\n    var m;\n    if (col.type === \"number\") {\n      var x = Number(s.replace(/\\s/g, \"\").replace(\",\", \".\").replace(\"−\", \"-\"));\n      if (!isFinite(x)) throw new Error(\"«\" + col.title + \"»: нужно число, а не «\" + s + \"»\");\n      if (col.int && Math.round(x) !== x) throw new Error(\"«\" + col.title + \"»: нужно целое число\");\n      if (col.min != null && x < col.min) throw new Error(\"«\" + col.title + \"»: не меньше \" + col.min);\n      if (col.max != null && x > col.max) throw new Error(\"«\" + col.title + \"»: не больше \" + col.max);\n      return x;\n    }\n    if (col.type === \"date\") {\n      var iso = null, y = new Date().getFullYear();\n      if ((m = s.match(/^(\\d{4})-(\\d{1,2})-(\\d{1,2})$/))) iso = isoDay(+m[1], +m[2], +m[3]);\n      else if ((m = s.match(/^(\\d{1,2})[.\\/](\\d{1,2})(?:[.\\/](\\d{2}|\\d{4}))?$/))) iso = isoDay(m[3] ? (m[3].length === 2 ? 2000 + +m[3] : +m[3]) : y, +m[2], +m[1]);\n      if (!iso) throw new Error(\"«\" + col.title + \"»: дата в виде 23.10.2026\");\n      return iso;\n    }\n    if (col.type === \"time\") {\n      if (!(m = s.match(/^(\\d{1,2})[:.](\\d{2})$/)) || +m[1] > 23 || +m[2] > 59) throw new Error(\"«\" + col.title + \"»: время в виде 15:40\");\n      return pad(+m[1]) + \":\" + m[2];\n    }\n    if (col.type === \"bool\") {\n      var l = s.toLowerCase();\n      if ([\"да\", \"д\", \"1\", \"+\", \"yes\", \"y\", \"true\", \"дождь\", \"x\", \"х\", \"✓\"].indexOf(l) >= 0) return true;\n      if ([\"нет\", \"н\", \"0\", \"-\", \"no\", \"n\", \"false\"].indexOf(l) >= 0) return false;\n      throw new Error(\"«\" + col.title + \"»: да или нет\");\n    }\n    return s.slice(0, col.maxLength || 60);\n  }\n  function text(col, v) {\n    if (v == null || v === \"\") return \"\";\n    if (col.type === \"number\") return nf(v, col.digits || (col.int ? 0 : 2));\n    if (col.type === \"date\") return v.slice(8, 10) + \".\" + v.slice(5, 7) + \".\" + v.slice(0, 4);\n    if (col.type === \"bool\") return v ? \"да\" : \"нет\";\n    return String(v);\n  }\n\n  // буфер обмена: copy и paste приходят на body, поэтому ищем таблицу с фокусом\n  var clipboardReady = false;\n  function activeGrid() {\n    var a = document.activeElement, el = a && a.closest && a.closest(\".xg\");\n    return el && el.__grid && !el.__grid.editing ? el.__grid : null;\n  }\n  function listenClipboard() {\n    if (clipboardReady) return;\n    clipboardReady = true;\n    document.addEventListener(\"copy\", function (e) { var g = activeGrid(); if (g) g.onCopy(e); });\n    document.addEventListener(\"paste\", function (e) { var g = activeGrid(); if (g) g.onPaste(e); });\n  }\n\n  // ---------------------------------------------------------------- сетка\n  function Grid(el, o) {\n    injectCss();\n    this.el = el; this.o = o; this.rows = o.rows || [];\n    this.sel = { r: 0, c: 0, r2: 0, c2: 0 }; this.editing = null;\n    el.className = \"xg\"; el.tabIndex = 0;\n    el.setAttribute(\"role\", \"grid\"); el.setAttribute(\"aria-label\", o.label || \"Таблица\");\n    var self = this;\n    el.addEventListener(\"mousedown\", function (e) { self.onDown(e); });\n    el.addEventListener(\"dblclick\", function (e) { var td = e.target.closest(\"td[data-c]\"); if (td) self.startEdit(null); });\n    el.addEventListener(\"keydown\", function (e) { self.onKey(e); });\n    el.__grid = this;\n    listenClipboard();\n    this.render();\n  }\n  Grid.prototype.cols = function () { return this.o.columns; };\n  Grid.prototype.count = function () { return this.rows.length + (this.o.allowAdd ? 1 : 0); };\n  Grid.prototype.setRows = function (rows) {\n    this.rows = rows; this.editing = null;\n    var n = Math.max(0, this.count() - 1);\n    this.sel.r = Math.min(this.sel.r, n); this.sel.r2 = Math.min(this.sel.r2, n);\n    this.render();\n  };\n  Grid.prototype.render = function () {\n    var self = this, cols = this.cols(), h = [];\n    h.push('<div class=\"xg-scroll\"><table class=\"xg-t\"><colgroup><col style=\"width:46px\">');\n    cols.forEach(function (c) { h.push('<col style=\"width:' + (c.width || 120) + 'px\">'); });\n    h.push('</colgroup><thead><tr><th class=\"xg-n\" aria-hidden=\"true\"></th>');\n    cols.forEach(function (c) { h.push('<th class=\"' + (c.type === \"number\" || c.right ? \"r\" : \"\") + '\" scope=\"col\">' + esc(c.title) + \"</th>\"); });\n    h.push(\"</tr></thead><tbody>\");\n    this.rows.forEach(function (row, r) { h.push(self.rowHtml(row, r)); });\n    if (this.o.allowAdd) {\n      h.push('<tr class=\"add\"><td class=\"xg-n\">+</td>');\n      cols.forEach(function (c, ci) { h.push('<td data-r=\"' + self.rows.length + '\" data-c=\"' + ci + '\" class=\"' + (c.ro ? \"ro \" : \"\") + (c.type === \"number\" || c.right ? \"r\" : \"\") + '\">' + (ci === 0 ? '<span class=\"mute\">новая строка</span>' : \"\") + \"</td>\"); });\n      h.push(\"</tr>\");\n    }\n    h.push(\"</tbody>\");\n    var foot = this.o.footer ? this.o.footer(this.rows) : null;\n    if (foot) {\n      h.push('<tfoot><tr><td class=\"xg-n\"></td>');\n      cols.forEach(function (c, ci) { h.push('<td class=\"' + (c.type === \"number\" || c.right ? \"r\" : \"\") + '\">' + esc(foot[ci] == null ? \"\" : foot[ci]) + \"</td>\"); });\n      h.push(\"</tr></tfoot>\");\n    }\n    h.push(\"</table>\");\n    if (!this.rows.length && !this.o.allowAdd) h.push('<div class=\"xg-empty\">' + esc(this.o.empty || \"Пока пусто\") + \"</div>\");\n    h.push(\"</div>\");\n    var sc = this.el.querySelector(\".xg-scroll\"), top = sc ? sc.scrollTop : 0, left = sc ? sc.scrollLeft : 0;\n    this.el.innerHTML = h.join(\"\");\n    sc = this.el.querySelector(\".xg-scroll\"); sc.scrollTop = top; sc.scrollLeft = left;\n    this.paintSel(false);\n  };\n  Grid.prototype.rowHtml = function (row, r) {\n    var cols = this.cols(), h = '<tr class=\"' + (row.__draft ? \"draft\" : \"\") + '\"><td class=\"xg-n\">' + (row.__draft ? \"•\" : r + 1) + \"</td>\";\n    cols.forEach(function (c, ci) {\n      var v = c.value ? c.value(row) : row[c.key];\n      var html = c.html ? c.html(v, row) : esc(c.format ? c.format(v, row) : text(c, v));\n      h += '<td data-r=\"' + r + '\" data-c=\"' + ci + '\" class=\"' + (c.ro ? \"ro \" : \"\") + (c.type === \"number\" || c.right ? \"r\" : \"\") + '\">' + html + \"</td>\";\n    });\n    return h + \"</tr>\";\n  };\n  Grid.prototype.cell = function (r, c) { return this.el.querySelector('td[data-r=\"' + r + '\"][data-c=\"' + c + '\"]'); };\n  Grid.prototype.range = function () {\n    var s = this.sel;\n    return { r0: Math.min(s.r, s.r2), r1: Math.max(s.r, s.r2), c0: Math.min(s.c, s.c2), c1: Math.max(s.c, s.c2) };\n  };\n  Grid.prototype.paintSel = function (scroll) {\n    this.el.querySelectorAll(\"td.in,td.cur\").forEach(function (td) { td.classList.remove(\"in\", \"cur\"); });\n    var g = this.range(), single = g.r0 === g.r1 && g.c0 === g.c1;\n    if (!single) {\n      for (var r = g.r0; r <= g.r1; r++) for (var c = g.c0; c <= g.c1; c++) { var td = this.cell(r, c); if (td) td.classList.add(\"in\"); }\n    }\n    var cur = this.cell(this.sel.r2, this.sel.c2);\n    if (cur) {\n      cur.classList.add(\"cur\");\n      if (scroll) {\n        var sc = this.el.querySelector(\".xg-scroll\"), a = cur.getBoundingClientRect(), b = sc.getBoundingClientRect();\n        var headH = 40, numW = 46;\n        if (a.top < b.top + headH) sc.scrollTop -= b.top + headH - a.top;\n        else if (a.bottom > b.bottom - 34) sc.scrollTop += a.bottom - b.bottom + 34;\n        if (a.left < b.left + numW) sc.scrollLeft -= b.left + numW - a.left;\n        else if (a.right > b.right) sc.scrollLeft += a.right - b.right;\n      }\n    }\n    if (this.o.onSelect) this.o.onSelect(this.range());\n  };\n  Grid.prototype.move = function (dr, dc, extend) {\n    var n = this.count(), m = this.cols().length, s = this.sel;\n    var r = Math.max(0, Math.min(n - 1, s.r2 + dr)), c = Math.max(0, Math.min(m - 1, s.c2 + dc));\n    if (extend) { s.r2 = r; s.c2 = c; } else { s.r = s.r2 = r; s.c = s.c2 = c; }\n    this.paintSel(true);\n  };\n  Grid.prototype.onDown = function (e) {\n    var td = e.target.closest(\"td[data-c]\");\n    if (!td || this.editing && this.editing.td === td) return;\n    if (this.editing) this.commit(0, 0);\n    e.preventDefault();\n    this.el.focus({ preventScroll: true });\n    var r = +td.getAttribute(\"data-r\"), c = +td.getAttribute(\"data-c\"), s = this.sel, self = this;\n    if (e.shiftKey) { s.r2 = r; s.c2 = c; } else { s.r = s.r2 = r; s.c = s.c2 = c; }\n    this.paintSel(false);\n    var onMove = function (ev) {\n      var t = document.elementFromPoint(ev.clientX, ev.clientY), x = t && t.closest && t.closest(\"td[data-c]\");\n      if (x && self.el.contains(x)) { s.r2 = +x.getAttribute(\"data-r\"); s.c2 = +x.getAttribute(\"data-c\"); self.paintSel(false); }\n    };\n    var onUp = function () { document.removeEventListener(\"mousemove\", onMove); document.removeEventListener(\"mouseup\", onUp); };\n    document.addEventListener(\"mousemove\", onMove); document.addEventListener(\"mouseup\", onUp);\n  };\n  Grid.prototype.onKey = function (e) {\n    if (this.editing) return;\n    var k = e.key, mod = e.ctrlKey || e.metaKey;\n    if (mod && (k === \"a\" || k === \"A\" || k === \"ф\" || k === \"Ф\")) { e.preventDefault(); this.sel = { r: 0, c: 0, r2: Math.max(0, this.rows.length - 1), c2: this.cols().length - 1 }; this.paintSel(false); return; }\n    if (mod) return;\n    var map = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1], PageUp: [-10, 0], PageDown: [10, 0] };\n    if (map[k]) { e.preventDefault(); this.move(map[k][0], map[k][1], e.shiftKey); return; }\n    if (k === \"Tab\") { e.preventDefault(); this.move(0, e.shiftKey ? -1 : 1, false); return; }\n    if (k === \"Home\") { e.preventDefault(); this.move(0, -99, e.shiftKey); return; }\n    if (k === \"End\") { e.preventDefault(); this.move(0, 99, e.shiftKey); return; }\n    if (k === \"Enter\" || k === \"F2\") { e.preventDefault(); this.startEdit(null); return; }\n    if (k === \"Delete\" || k === \"Backspace\") { e.preventDefault(); this.clear(); return; }\n    if (k.length === 1 && !e.altKey) { e.preventDefault(); this.startEdit(k); }\n  };\n  Grid.prototype.startEdit = function (typed) {\n    var s = this.sel, col = this.cols()[s.c2], td = this.cell(s.r2, s.c2);\n    if (!col || !td) return;\n    if (col.ro) { if (this.o.onError) this.o.onError(\"Эта колонка считается сама\"); return; }\n    s.r = s.r2; s.c = s.c2; this.paintSel(false);\n    var row = this.rows[s.r2], v = row ? row[col.key] : null, self = this;\n    var inp = document.createElement(\"input\");\n    inp.className = \"xg-in\"; inp.type = \"text\"; inp.setAttribute(\"aria-label\", col.title);\n    if (col.type === \"number\") inp.inputMode = \"decimal\";\n    inp.value = typed != null ? typed : text(col, v);\n    td.innerHTML = \"\"; td.appendChild(inp);\n    this.editing = { td: td, inp: inp, r: s.r2, c: s.c2, typed: typed != null };\n    inp.focus();\n    if (typed == null) inp.select(); else inp.setSelectionRange(inp.value.length, inp.value.length);\n    inp.addEventListener(\"keydown\", function (e) {\n      if (e.key === \"Enter\") { e.preventDefault(); self.commit(1, 0); }\n      else if (e.key === \"Tab\") { e.preventDefault(); self.commit(0, e.shiftKey ? -1 : 1); }\n      else if (e.key === \"Escape\") { e.preventDefault(); self.cancel(); }\n      else if (self.editing && self.editing.typed && (e.key === \"ArrowDown\" || e.key === \"ArrowUp\")) { e.preventDefault(); self.commit(e.key === \"ArrowDown\" ? 1 : -1, 0); }\n      e.stopPropagation();\n    });\n    inp.addEventListener(\"blur\", function () { if (self.editing && self.editing.inp === inp) self.commit(0, 0, true); });\n  };\n  Grid.prototype.cancel = function () {\n    var ed = this.editing; if (!ed) return;\n    this.editing = null;\n    this.redrawRow(ed.r, this.rows[ed.r]);\n    this.el.focus({ preventScroll: true });\n    if (this.o.afterEdit) this.o.afterEdit();\n  };\n  Grid.prototype.redrawRow = function (r, row) {\n    var tr = this.cell(r, 0) && this.cell(r, 0).parentNode;\n    if (!tr) return this.render();\n    if (!row) { this.render(); return; }\n    var tmp = document.createElement(\"tbody\"); tmp.innerHTML = this.rowHtml(row, r);\n    tr.parentNode.replaceChild(tmp.firstChild, tr);\n    this.paintSel(false);\n  };\n  Grid.prototype.commit = function (dr, dc, blur) {\n    var ed = this.editing; if (!ed) return;\n    var col = this.cols()[ed.c], val;\n    try { val = parse(col, ed.inp.value); }\n    catch (err) {\n      if (this.o.onError) this.o.onError(err.message);\n      if (blur) { this.cancel(); return; }\n      ed.td.classList.add(\"bad\"); ed.inp.select(); return;\n    }\n    this.editing = null;\n    var row = this.rows[ed.r], old = row ? row[col.key] : null;\n    if (!row && val == null) this.render();\n    else if (row && (old === val || (old == null && val == null))) this.redrawRow(ed.r, row);\n    else this.apply([{ r: ed.r, key: col.key, value: val }]);\n    if (!blur) { this.el.focus({ preventScroll: true }); this.move(dr, dc, false); }\n    if (this.o.afterEdit) this.o.afterEdit();\n  };\n  // изменения: [{ r, key, value }]; новые строки создаются, если r за концом списка\n  Grid.prototype.apply = function (changes) {\n    var self = this, touched = [];\n    changes.forEach(function (ch) {\n      var row = self.rows[ch.r];\n      if (!row) {\n        row = self.o.newRow ? self.o.newRow() : {};\n        row.__draft = true;\n        self.rows.push(row);\n      }\n      if (touched.indexOf(row) < 0) touched.push(row);\n      row.__old = row.__old || {};\n      if (!(ch.key in row.__old)) row.__old[ch.key] = row[ch.key];\n      row[ch.key] = ch.value;\n    });\n    this.render();\n    if (this.o.onChange) this.o.onChange(touched);\n  };\n  Grid.prototype.clear = function () {\n    var g = this.range(), cols = this.cols(), ch = [];\n    for (var r = g.r0; r <= Math.min(g.r1, this.rows.length - 1); r++) {\n      for (var c = g.c0; c <= g.c1; c++) {\n        var col = cols[c];\n        if (col.ro || col.required) continue;\n        if (this.rows[r][col.key] != null) ch.push({ r: r, key: col.key, value: null });\n      }\n    }\n    if (ch.length) this.apply(ch);\n  };\n  Grid.prototype.tsv = function (g, header) {\n    var cols = this.cols(), out = [];\n    if (header) { var hd = []; for (var c0 = g.c0; c0 <= g.c1; c0++) hd.push(cols[c0].title); out.push(hd.join(\"\\t\")); }\n    for (var r = g.r0; r <= g.r1; r++) {\n      var row = this.rows[r]; if (!row) continue;\n      var line = [];\n      for (var c = g.c0; c <= g.c1; c++) {\n        var col = cols[c], v = col.value ? col.value(row) : row[col.key];\n        line.push((col.format ? col.format(v, row) : text(col, v)).replace(/[\\t\\n]/g, \" \"));\n      }\n      out.push(line.join(\"\\t\"));\n    }\n    return out.join(\"\\r\\n\");\n  };\n  Grid.prototype.onCopy = function (e) {\n    if (this.editing) return;\n    e.preventDefault();\n    e.clipboardData.setData(\"text/plain\", this.tsv(this.range(), false));\n    if (this.o.onInfo) this.o.onInfo(\"Скопировано — вставьте в Excel\");\n  };\n  Grid.prototype.onPaste = function (e) {\n    if (this.editing) return;\n    e.preventDefault();\n    var raw = (e.clipboardData || window.clipboardData).getData(\"text\"); if (!raw) return;\n    var lines = raw.replace(/\\r/g, \"\").split(\"\\n\");\n    if (lines.length > 1 && lines[lines.length - 1] === \"\") lines.pop();\n    var cols = this.cols(), g = this.range(), ch = [], errors = [], skipped = 0;\n    var start = g.r0, first = lines[0].split(\"\\t\");\n    // строка заголовков из Excel — пропускаем\n    var isTitle = function (x, i) { var c = cols[g.c0 + i], s = x.trim().toLowerCase(); return c && (s === c.title.toLowerCase() || s === String(c.csvTitle || \"\").toLowerCase()); };\n    if (first.some(isTitle)) { lines.shift(); if (!lines.length) return; first = lines[0].split(\"\\t\"); }\n    var limit = this.o.allowAdd ? Infinity : this.rows.length;\n    lines.forEach(function (line, i) {\n      var r = start + i; if (r >= limit) { skipped++; return; }\n      line.split(\"\\t\").forEach(function (cellText, j) {\n        var col = cols[g.c0 + j]; if (!col || col.ro) return;\n        try { ch.push({ r: r, key: col.key, value: parse(col, cellText) }); }\n        catch (err) { errors.push(\"строка \" + (i + 1) + \": \" + err.message); }\n      });\n    });\n    if (errors.length && this.o.onError) this.o.onError(\"Не вставлено \" + errors.length + \": \" + errors.slice(0, 2).join(\"; \"));\n    if (ch.length) {\n      this.sel = { r: start, c: g.c0, r2: start + lines.length - 1, c2: Math.min(cols.length - 1, g.c0 + first.length - 1) };\n      this.apply(ch);\n      if (this.o.onInfo && !errors.length) this.o.onInfo(\"Вставлено строк: \" + lines.length);\n    }\n  };\n  Grid.prototype.selectedRows = function () {\n    var g = this.range(), out = [];\n    for (var r = g.r0; r <= g.r1; r++) if (this.rows[r]) out.push(this.rows[r]);\n    return out;\n  };\n  Grid.prototype.focusNewRow = function () {\n    this.sel = { r: this.rows.length - 1, c: 0, r2: this.rows.length - 1, c2: 0 };\n    this.paintSel(true); this.el.focus({ preventScroll: true });\n  };\n\n  // ---------------------------------------------------------------- CSV для Excel\n  function csv(columns, rows) {\n    var cols = columns.filter(function (c) { return c.csv !== false; });\n    var q = function (s) { s = String(s == null ? \"\" : s); return /[;\"\\n]/.test(s) ? '\"' + s.replace(/\"/g, '\"\"') + '\"' : s; };\n    var out = [cols.map(function (c) { return q(c.csvTitle || c.title); }).join(\";\")];\n    rows.forEach(function (row) {\n      out.push(cols.map(function (c) { var v = c.value ? c.value(row) : row[c.key]; return q(c.format ? c.format(v, row) : text(c, v).replace(\"−\", \"-\")); }).join(\";\"));\n    });\n    return \"﻿\" + out.join(\"\\r\\n\") + \"\\r\\n\";\n  }\n  function download(name, content) {\n    var blob = new Blob([content], { type: \"text/csv;charset=utf-8\" }), a = document.createElement(\"a\");\n    a.href = URL.createObjectURL(blob); a.download = name;\n    document.body.appendChild(a); a.click(); a.remove();\n    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);\n  }\n\n  // ---------------------------------------------------------------- четыре листа\n  // adapter: today (ISO), load(), saveSales(upsert, remove), saveProduct(p, oldName), removeProduct(name),\n  //          addBatch(b), editBatch(b), deleteBatch(id), upload(text)?, toast(msg)\n  function Sheets(el, a) {\n    injectCss();\n    this.el = el; this.a = a; this.tab = \"sales\"; this.period = 14; this.product = \"\";\n    this.data = null; this.drafts = { sales: [], products: [], batches: [] }; this.busy = false;\n    var self = this;\n    el.innerHTML = '<div class=\"xs-tabs\" role=\"tablist\" aria-label=\"Листы\"></div><div class=\"xs-bar\"></div><div class=\"xs-grid\"></div><p class=\"xs-hint\"></p>';\n    el.addEventListener(\"click\", function (e) { self.onClick(e); });\n    el.addEventListener(\"change\", function (e) { self.onChangeCtl(e); });\n  }\n  var SHEETS = [[\"sales\", \"Продажи по дням\"], [\"products\", \"Товары\"], [\"batches\", \"Сроки годности\"], [\"week\", \"План на неделю\"]];\n  var HINT = \"Кликните ячейку и печатайте. Enter — изменить, стрелки и Tab — переход, Delete — очистить, Ctrl+C / Ctrl+V — копировать и вставить из Excel.\";\n\n  Sheets.prototype.load = function () {\n    var self = this;\n    return this.a.load().then(function (d) {\n      self.data = d;\n      if (self.grid && self.grid.editing) self.stale = true; else self.show();\n    }).catch(function (e) { self.err(e); });\n  };\n  Sheets.prototype.err = function (e) { this.a.toast(e && e.message ? e.message : \"Не получилось сохранить\"); };\n  Sheets.prototype.productNames = function () { return this.data.products.map(function (p) { return p.name; }).sort(function (x, y) { return x.localeCompare(y, \"ru\"); }); };\n\n  Sheets.prototype.salesColumns = function () {\n    return [\n      { key: \"day\", title: \"Дата\", type: \"date\", width: 120, required: true, csvTitle: \"дата\",\n        html: function (v) { return v ? '<span class=\"mute\">' + weekday(v) + \"</span> \" + esc(text({ type: \"date\" }, v)) : \"\"; } },\n      { key: \"product\", title: \"Товар\", type: \"text\", width: 200, required: true, csvTitle: \"товар\" },\n      { key: \"baked\", title: \"Испечено / заказано\", type: \"number\", min: 0, max: 100000, width: 120, csvTitle: \"испечено\" },\n      { key: \"sold\", title: \"Продано\", type: \"number\", min: 0, max: 100000, width: 96, required: true, csvTitle: \"продано\" },\n      { key: \"left\", title: \"Осталось\", type: \"number\", ro: true, width: 90, csv: false,\n        value: function (r) { return r.baked != null && r.sold != null && !r.soldout ? Math.max(0, r.baked - r.sold) : null; } },\n      { key: \"soldout\", title: \"Закончилось в\", type: \"time\", width: 110, csvTitle: \"закончилось\" },\n      { key: \"rain\", title: \"Дождь\", type: \"bool\", width: 76, csvTitle: \"дождь\" },\n      { key: \"temp\", title: \"Темп., °C\", type: \"number\", min: -60, max: 50, digits: 1, width: 92, csvTitle: \"температура\" }\n    ];\n  };\n  Sheets.prototype.productColumns = function () {\n    return [\n      { key: \"name\", title: \"Товар\", type: \"text\", width: 210, required: true },\n      { key: \"price\", title: \"Цена, ₽\", type: \"number\", min: 0, max: 1e6, width: 96 },\n      { key: \"cost\", title: \"Себестоимость, ₽\", type: \"number\", min: 0, max: 1e6, width: 130 },\n      { key: \"margin\", title: \"Маржа\", type: \"number\", ro: true, width: 80, csv: false,\n        value: function (p) { return p.price && p.cost != null ? Math.round((p.price - p.cost) / p.price * 100) : null; },\n        format: function (v) { return v == null ? \"\" : v + \"%\"; } },\n      { key: \"shelf_life\", title: \"Срок хранения, дней\", type: \"number\", int: true, min: 0, max: 3650, width: 140 },\n      { key: \"default_qty\", title: \"Обычно в день, шт\", type: \"number\", min: 0, max: 1e5, width: 130 },\n      { key: \"history_days\", title: \"История\", type: \"number\", ro: true, right: true, width: 100, csv: false,\n        format: function (v) { return v ? v + \" дн.\" : \"новый\"; } }\n    ];\n  };\n  Sheets.prototype.batchColumns = function () {\n    return [\n      { key: \"item\", title: \"Продукт\", type: \"text\", width: 210, required: true },\n      { key: \"qty\", title: \"Количество\", type: \"text\", width: 110, maxLength: 20 },\n      { key: \"expires\", title: \"Годен до\", type: \"date\", width: 120,\n        html: function (v) { return v ? '<span class=\"mute\">' + weekday(v) + \"</span> \" + esc(text({ type: \"date\" }, v)) : \"\"; } },\n      { key: \"label\", title: \"Статус\", type: \"text\", ro: true, width: 150,\n        html: function (v, b) { return v ? '<span class=\"tag ' + (b.level || \"\") + '\">' + esc(v) + \"</span>\" : \"\"; } },\n      { key: \"tip\", title: \"Что сделать\", type: \"text\", ro: true, width: 300 }\n    ];\n  };\n  Sheets.prototype.weekColumns = function () {\n    var days = this.data.week.days, cols = [{ key: \"product\", title: \"Товар\", type: \"text\", ro: true, width: 200 }];\n    days.forEach(function (d, i) {\n      cols.push({ key: \"d\" + i, title: weekday(d) + \" \" + d.slice(8, 10) + \".\" + d.slice(5, 7), type: \"number\", ro: true, width: 78,\n        value: function (r) { return r.qty[i]; } });\n    });\n    cols.push({ key: \"total\", title: \"За неделю\", type: \"number\", ro: true, width: 100, value: function (r) { return r.qty.reduce(function (x, y) { return x + y; }, 0); } });\n    return cols;\n  };\n\n  Sheets.prototype.salesRows = function () {\n    var d = this.data, a = this.a, self = this, from = null;\n    if (this.period !== \"all\") from = shift(a.today, -(+this.period - 1));\n    var rows = d.sales.filter(function (s) { return (!from || s.day >= from) && (!self.product || s.product === self.product); });\n    var order = {}; this.productNames().forEach(function (n, i) { order[n] = i; });\n    rows.sort(function (x, y) { return x.day < y.day ? -1 : x.day > y.day ? 1 : (order[x.product] || 0) - (order[y.product] || 0); });\n    return rows.concat(this.drafts.sales);\n  };\n\n  Sheets.prototype.show = function () {\n    if (!this.data) return;\n    var self = this, a = this.a;\n    this.el.querySelector(\".xs-tabs\").innerHTML = SHEETS.map(function (s) {\n      return '<button role=\"tab\" data-sheet=\"' + s[0] + '\" aria-selected=\"' + (self.tab === s[0]) + '\">' + s[1] + \"</button>\";\n    }).join(\"\");\n    var bar = [], t = this.tab;\n    if (t === \"sales\") {\n      bar.push('<label>Период<select data-ctl=\"period\">' + [[7, \"7 дней\"], [14, \"14 дней\"], [30, \"30 дней\"], [90, \"90 дней\"], [\"all\", \"Всё время\"]].map(function (o) {\n        return '<option value=\"' + o[0] + '\"' + (String(self.period) === String(o[0]) ? \" selected\" : \"\") + \">\" + o[1] + \"</option>\"; }).join(\"\") + \"</select></label>\");\n      bar.push('<label>Товар<select data-ctl=\"product\"><option value=\"\">Все</option>' + this.productNames().map(function (n) {\n        return '<option' + (self.product === n ? \" selected\" : \"\") + \">\" + esc(n) + \"</option>\"; }).join(\"\") + \"</select></label>\");\n    }\n    bar.push('<span class=\"xs-sp\"></span>');\n    if (t !== \"week\") {\n      bar.push('<button class=\"xs-btn\" data-do=\"add\">' + { sales: \"+ Строка\", products: \"+ Товар\", batches: \"+ Партия\" }[t] + \"</button>\");\n      bar.push('<button class=\"xs-btn\" data-do=\"del\" disabled>' + (t === \"products\" ? \"Убрать из планов\" : \"Удалить строки\") + \"</button>\");\n    }\n    if (t === \"sales\" && a.upload) bar.push('<span class=\"xs-btn xs-file\">Загрузить CSV<input type=\"file\" accept=\".csv,.txt,text/csv\" data-ctl=\"upload\" aria-label=\"Загрузить CSV\"></span>');\n    bar.push('<button class=\"xs-btn\" data-do=\"csv\">Скачать для Excel</button>');\n    this.el.querySelector(\".xs-bar\").innerHTML = bar.join(\"\");\n    this.el.querySelector(\".xs-hint\").textContent = t === \"week\"\n      ? \"План считается сам по истории продаж, дню недели, погоде и сезону. Выделите ячейки и нажмите Ctrl+C, чтобы вставить в Excel.\"\n      : HINT + (t === \"sales\" ? \" Строка сохраняется, когда заполнены дата, товар и «продано».\" : \"\");\n    var o = {\n      onError: function (m) { a.toast(m); }, onInfo: function (m) { a.toast(m); },\n      onSelect: function (g) { var b = self.el.querySelector('[data-do=\"del\"]'); if (b) b.disabled = !self.grid || g.r0 >= self.grid.rows.length; }\n    };\n    if (t === \"sales\") {\n      o.label = \"Продажи по дням\"; o.columns = this.salesColumns(); o.rows = this.salesRows(); o.allowAdd = true;\n      o.newRow = function () { return { day: a.today, product: self.product || null }; };\n      o.onChange = function (rows) { self.saveSales(rows); };\n      o.footer = function (rows) {\n        var sum = function (k) { return rows.reduce(function (x, r) { return x + (r[k] || 0); }, 0); };\n        var left = rows.reduce(function (x, r) { return x + (r.baked != null && r.sold != null && !r.soldout ? Math.max(0, r.baked - r.sold) : 0); }, 0);\n        var n = rows.length, w = n % 10 === 1 && n % 100 !== 11 ? \"строка\" : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? \"строки\" : \"строк\";\n        return [\"Итого\", n + \" \" + w, nf(sum(\"baked\")), nf(sum(\"sold\")), nf(left), \"\", \"\", \"\"];\n      };\n    } else if (t === \"products\") {\n      o.label = \"Товары\"; o.columns = this.productColumns(); o.allowAdd = true;\n      o.rows = this.data.products.slice().sort(function (x, y) { return x.name.localeCompare(y.name, \"ru\"); }).concat(this.drafts.products);\n      o.onChange = function (rows) { self.saveProducts(rows); };\n    } else if (t === \"batches\") {\n      o.label = \"Сроки годности\"; o.columns = this.batchColumns(); o.allowAdd = true;\n      o.rows = this.data.batches.slice().concat(this.drafts.batches);\n      o.onChange = function (rows) { self.saveBatches(rows); };\n    } else {\n      o.label = \"План на неделю\"; o.columns = this.weekColumns(); o.rows = this.data.week.rows; o.empty = \"Нет товаров для плана\";\n      o.footer = function (rows) {\n        var f = [\"Всего\"];\n        self.data.week.days.forEach(function (d, i) { f.push(nf(rows.reduce(function (x, r) { return x + r.qty[i]; }, 0))); });\n        f.push(nf(rows.reduce(function (x, r) { return x + r.qty.reduce(function (p, q) { return p + q; }, 0); }, 0)));\n        return f;\n      };\n    }\n    o.afterEdit = function () { if (self.stale) { self.stale = false; self.show(); } };\n    var prev = this.grid && this.shownTab === t ? this.grid : null, host = this.el.querySelector(\".xs-grid\");\n    var keep = prev ? { sel: prev.sel, focus: host.contains(document.activeElement), sc: host.querySelector(\".xg-scroll\") } : null;\n    var top = keep && keep.sc ? keep.sc.scrollTop : 0, left = keep && keep.sc ? keep.sc.scrollLeft : 0;\n    this.cols = o.columns; this.shownTab = t;\n    var gel = document.createElement(\"div\");\n    host.innerHTML = \"\"; host.appendChild(gel);\n    this.grid = new Grid(gel, o);\n    if (keep) {\n      var n = Math.max(0, this.grid.count() - 1), m = o.columns.length - 1, s = keep.sel;\n      this.grid.sel = { r: Math.min(s.r, n), c: Math.min(s.c, m), r2: Math.min(s.r2, n), c2: Math.min(s.c2, m) };\n      var sc = gel.querySelector(\".xg-scroll\"); sc.scrollTop = top; sc.scrollLeft = left;\n      this.grid.paintSel(false);\n      if (keep.focus) this.grid.el.focus({ preventScroll: true });\n    }\n  };\n\n  Sheets.prototype.onClick = function (e) {\n    var b = e.target.closest(\"[data-sheet],[data-do]\"); if (!b) return;\n    if (b.hasAttribute(\"data-sheet\")) { this.tab = b.getAttribute(\"data-sheet\"); this.show(); return; }\n    var d = b.getAttribute(\"data-do\"), self = this, t = this.tab;\n    if (d === \"csv\") {\n      var names = { sales: \"prodazhi\", products: \"tovary\", batches: \"sroki\", week: \"plan-na-nedelyu\" };\n      var rows = t === \"sales\" ? this.salesRows().filter(function (r) { return !r.__draft; }) : this.grid.rows.filter(function (r) { return !r.__draft; });\n      Sheets.saveFile(names[t] + \"-\" + this.a.today + \".csv\", csv(this.cols, rows), this.a.toast);\n    } else if (d === \"add\") {\n      var row = t === \"sales\" ? { day: this.a.today, product: this.product || null } : {};\n      row.__draft = true;\n      this.drafts[t].push(row);\n      this.show();\n      this.grid.focusNewRow();\n    } else if (d === \"del\") {\n      var sel = this.grid.selectedRows(); if (!sel.length) return;\n      var drafts = sel.filter(function (r) { return r.__draft; }), saved = sel.filter(function (r) { return !r.__draft; });\n      this.drafts[t] = this.drafts[t].filter(function (r) { return drafts.indexOf(r) < 0; });\n      if (!saved.length) { this.show(); return; }\n      var job;\n      if (t === \"sales\") job = this.a.saveSales([], saved.map(function (r) { return { day: r.day, product: r.product }; }));\n      else if (t === \"products\") job = chain(saved.map(function (p) { return function () { return self.a.removeProduct(p.name); }; }));\n      else job = chain(saved.map(function (x) { return function () { return self.a.deleteBatch(x.id); }; }));\n      job.then(function () {\n        self.a.toast(t === \"products\" ? \"Убрано из планов: \" + saved.length + \". История продаж сохранена.\" : \"Удалено строк: \" + saved.length);\n        return self.load();\n      }).catch(function (x) { self.err(x); self.load(); });\n    }\n  };\n  Sheets.prototype.onChangeCtl = function (e) {\n    var c = e.target.getAttribute(\"data-ctl\"), self = this; if (!c) return;\n    if (c === \"period\") { this.period = e.target.value; this.show(); }\n    else if (c === \"product\") { this.product = e.target.value; this.show(); }\n    else if (c === \"upload\" && e.target.files && e.target.files[0]) {\n      var rd = new FileReader();\n      rd.onload = function () {\n        var s;\n        try { s = new TextDecoder(\"utf-8\", { fatal: true }).decode(rd.result); } catch (x) { s = new TextDecoder(\"windows-1251\").decode(rd.result); }\n        self.a.upload(s).then(function (r) { self.a.toast(\"Загружено строк: \" + (r && r.rows != null ? r.rows : \"\")); return self.load(); }).catch(function (x) { self.err(x); });\n      };\n      rd.readAsArrayBuffer(e.target.files[0]); e.target.value = \"\";\n    }\n  };\n\n  function chain(jobs) { return jobs.reduce(function (p, f) { return p.then(f); }, Promise.resolve()); }\n  function shift(iso, days) { var t = Date.parse(iso + \"T00:00:00Z\") + days * 864e5; return new Date(t).toISOString().slice(0, 10); }\n  function clean(row, keys) { var o = {}; keys.forEach(function (k) { o[k] = row[k] == null ? null : row[k]; }); return o; }\n\n  Sheets.prototype.saveSales = function (rows) {\n    var self = this, up = [], del = [], keys = [\"day\", \"product\", \"baked\", \"sold\", \"soldout\", \"rain\", \"temp\"];\n    var fail = null;\n    rows.forEach(function (r) {\n      var old = r.__old || {}; delete r.__old;\n      if (r.day == null || !r.product || r.sold == null) {\n        if (!r.__draft) { fail = \"Дата, товар и «продано» должны быть заполнены\"; return; }\n        if (self.drafts.sales.indexOf(r) < 0) self.drafts.sales.push(r);\n        return;\n      }\n      if (r.baked != null && r.sold > r.baked) { fail = r.product + \": продано больше, чем испечено\"; return; }\n      if (!r.__draft && ((\"day\" in old && old.day !== r.day) || (\"product\" in old && old.product !== r.product))) {\n        del.push({ day: \"day\" in old ? old.day : r.day, product: \"product\" in old ? old.product : r.product });\n      }\n      up.push(clean(r, keys));\n    });\n    if (fail) { this.a.toast(fail); return this.load(); }\n    if (!up.length && !del.length) return;\n    this.drafts.sales = this.drafts.sales.filter(function (r) { return rows.indexOf(r) < 0 || r.day == null || !r.product || r.sold == null; });\n    return this.a.saveSales(up, del).then(function () { return self.load(); }).catch(function (x) { self.err(x); return self.load(); });\n  };\n  Sheets.prototype.saveProducts = function (rows) {\n    var self = this, jobs = [];\n    rows.forEach(function (p) {\n      var old = p.__old || {}; delete p.__old;\n      if (!p.name) { if (p.__draft && self.drafts.products.indexOf(p) < 0) self.drafts.products.push(p); return; }\n      if (p.price != null && p.cost != null && p.cost > p.price) { self.a.toast(p.name + \": себестоимость больше цены\"); return; }\n      var oldName = !p.__draft && \"name\" in old && old.name !== p.name ? old.name : null;\n      jobs.push(function () { return self.a.saveProduct(clean(p, [\"name\", \"price\", \"cost\", \"shelf_life\", \"default_qty\"]), oldName); });\n      self.drafts.products = self.drafts.products.filter(function (x) { return x !== p; });\n    });\n    if (!jobs.length) return this.show();\n    return chain(jobs).then(function () { return self.load(); }).catch(function (x) { self.err(x); return self.load(); });\n  };\n  Sheets.prototype.saveBatches = function (rows) {\n    var self = this, jobs = [];\n    rows.forEach(function (b) {\n      delete b.__old;\n      if (!b.item) { if (b.__draft && self.drafts.batches.indexOf(b) < 0) self.drafts.batches.push(b); return; }\n      if (b.__draft) {\n        var p = self.data.products.filter(function (x) { return x.name === b.item; })[0];\n        if (!b.expires && !(p && p.shelf_life)) {\n          self.a.toast(\"Укажите «годен до» — у товара «\" + b.item + \"» нет срока хранения\");\n          if (self.drafts.batches.indexOf(b) < 0) self.drafts.batches.push(b);\n          return;\n        }\n        jobs.push(function () { return self.a.addBatch({ item: b.item, qty: b.qty || \"\", expires: b.expires || null, made: self.a.today }); });\n        self.drafts.batches = self.drafts.batches.filter(function (x) { return x !== b; });\n      } else {\n        if (!b.expires) { self.a.toast(\"У партии должна быть дата «годен до»\"); return; }\n        jobs.push(function () { return self.a.editBatch({ id: b.id, item: b.item, qty: b.qty || \"\", expires: b.expires }); });\n      }\n    });\n    if (!jobs.length) return this.show();\n    return chain(jobs).then(function () { return self.load(); }).catch(function (x) { self.err(x); return self.load(); });\n  };\n\n  Sheets.saveFile = download;\n  root.Sheets = Sheets;\n  root.SheetGrid = Grid;\n})(this);\n</script>\n<script>(function () {\n  \"use strict\";\n  var M = window.FreshModel;\n  var $ = function (id) { return document.getElementById(id); };\n  function esc(s) { return String(s == null ? \"\" : s).replace(/[&<>\"]/g, function (c) { return { \"&\": \"&amp;\", \"<\": \"&lt;\", \">\": \"&gt;\", '\"': \"&quot;\" }[c]; }); }\n  function pad(n) { return (\"0\" + n).slice(-2); }\n  function fmt(x) { return Math.round(x).toLocaleString(\"ru-RU\"); }\n  function money(x) { return fmt(x) + \" ₽\"; }\n  function dayLabel(iso) { var n = M.dnum(iso); return M.WDS[M.wday(n)] + \", \" + M.ddmm(n); }\n  function mins(hhmm) { var p = String(hhmm || \"0:0\").split(\":\"); return (+p[0]) * 60 + (+p[1]); }\n  function plural(n, a, b, c) { n = Math.abs(n) % 100; if (n > 10 && n < 15) return c; n %= 10; return n === 1 ? a : n >= 2 && n <= 4 ? b : c; }\n  function phaseOf(times, m) {\n    if (m < mins(times.morning)) return \"night\";\n    if (m < mins(times.evening)) return \"morning\";\n    if (m < mins(times.closing)) return \"evening\";\n    return \"closing\";\n  }\n  var PART_OF = { night: \"plan\", morning: \"plan\", evening: \"vitrina\", closing: \"itogi\" };\n  var SLOTS = [[\"до 12\", \"11:00\"], [\"12–15\", \"13:30\"], [\"15–18\", \"16:30\"], [\"после 18\", \"19:30\"]];\n  var toastT = null;\n  function toast(msg) {\n    var t = $(\"toast\"); t.textContent = msg; t.hidden = false;\n    clearTimeout(toastT); toastT = setTimeout(function () { t.hidden = true; }, 2600);\n  }\n  var Local = (function () {\n    var KEY = \"svezhiy-kabinet-v2\", D = null, btCache = null, W = {};\n    function today() { var n = new Date(); return M.dnum(n.getFullYear() + \"-\" + pad(n.getMonth() + 1) + \"-\" + pad(n.getDate())); }\n    function save() { try { localStorage.setItem(KEY, JSON.stringify(D)); } catch (e) { /* без localStorage работаем в памяти */ } }\n    function example() {\n      var r = DEMO_CSV ? M.parseCSV(DEMO_CSV) : { sales: [], products: {} }, T = today();\n      var last = Math.max.apply(null, r.sales.map(function (s) { return s.day; }));\n      r.sales.forEach(function (s) { s.day += (T - 1) - last; });\n      var products = {};\n      Object.keys(r.products).forEach(function (k) {\n        var p = r.products[k];\n        products[k] = { name: k, price: p.price, cost: p.cost, shelf_life: (k === \"Эклер\" || k === \"Сэндвич с курицей\") ? 2 : 1, default_qty: null, active: true };\n      });\n      D = { v: 2, business: \"Пекарня\", example: true, times: { morning: \"06:00\", evening: \"17:00\", closing: \"21:00\" },\n            products: products, sales: r.sales, plans: {}, events: {}, nextId: 5,\n            batches: [[1, \"Сливки 33%\", \"2 л\", 1], [2, \"Творог 5%\", \"3 кг\", 2], [3, \"Филе куриное\", \"1,5 кг\", 0], [4, \"Вишня замороженная\", \"2 кг\", 20]]\n              .map(function (b) { return { id: b[0], item: b[1], qty: b[2], exp: T + b[3], status: \"active\" }; }) };\n      btCache = null; save();\n    }\n    function load() {\n      try { var raw = localStorage.getItem(KEY); if (raw) { var d = JSON.parse(raw); if (d && d.v === 2) D = d; } } catch (e) { /* нет доступа */ }\n      if (!D) example();\n      // пример в Челябинске, берём реальный прогноз\n      M.loadWeather(55.16, 61.44, 3).then(function (w) { W = w; if (Object.keys(w).length && S.st) refresh(true); });\n    }\n    function weather(day) { return (D.example && W[day]) || { rain: null, temp: null }; }\n    function prodMap(includeSalesOnly) {\n      var out = {}, removed = {};\n      Object.keys(D.products).forEach(function (k) { var p = D.products[k]; if (p.active) out[k] = p; else removed[k] = 1; });\n      if (includeSalesOnly) D.sales.forEach(function (s) { if (!out[s.product] && !removed[s.product]) out[s.product] = { name: s.product, price: null, cost: null }; });\n      return out;\n    }\n    function recs(day) {\n      var prods = prodMap(true), eff = M.rainEffect(D.sales).eff, ev = D.events[day] || 1, w = weather(day), list = [], habit = {};\n      Object.keys(prods).forEach(function (k) { var r = M.forecast(D.sales, prods[k], day, w.rain === true, ev, eff, w.temp); if (r.n > 0 || r.qty > 0) list.push(r); });\n      list.sort(function (a, b) { return b.qty - a.qty; });\n      list.forEach(function (r) {\n        var same = D.sales.filter(function (s) { return s.product === r.product && s.baked != null && M.wday(s.day) === M.wday(day) && day - s.day > 0 && day - s.day <= 28; });\n        if (same.length) habit[r.product] = same.reduce(function (a, s) { return a + s.baked; }, 0) / same.length;\n      });\n      return { recs: list, eff: eff, event: ev, habit: habit };\n    }\n    function item(r, qty, mean) {\n      var q = qty == null ? r.qty : qty, m = mean == null ? r.mean : mean;\n      return { product: r.product, qty: q, mean: m, sd: r.sd, reasons: r.reasons, \"new\": r.n === 0, threshold: M.eveningThreshold({ mean: m }) };\n    }\n    function sale(day, product) { for (var i = 0; i < D.sales.length; i++) if (D.sales[i].day === day && D.sales[i].product === product) return D.sales[i]; return null; }\n    function productList() {\n      var hist = {}, pm = prodMap(true);\n      D.sales.forEach(function (s) { (hist[s.product] = hist[s.product] || {})[s.day] = 1; });\n      return Object.keys(pm).map(function (k) {\n        var p = pm[k];\n        return { name: k, price: p.price, cost: p.cost, shelf_life: p.shelf_life == null ? null : p.shelf_life, default_qty: p.default_qty == null ? null : p.default_qty, history_days: hist[k] ? Object.keys(hist[k]).length : 0 };\n      }).sort(function (a, b) { return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1; });\n    }\n    function batchList(T) {\n      return D.batches.filter(function (b) { return b.status === \"active\"; }).sort(function (a, b) { return a.exp - b.exp || a.id - b.id; }).map(function (b) {\n        var st = M.expiryStatus(b.exp, T);\n        return { id: b.id, item: b.item, qty: b.qty, expires: M.diso(b.exp), label: st.label, left: st.left, level: st.left <= 0 ? \"bad\" : st.left <= 2 ? \"warn\" : \"ok\",\n                 tip: st.left <= 1 ? (st.left < 0 ? \"спишите\" : M.expiryTip(b.item)) : null };\n      });\n    }\n    function sheet() {\n      var T = today(), days = [], week = {}, sum = function (q) { return q.reduce(function (x, y) { return x + y; }, 0); };\n      for (var i = 0; i < 7; i++) {\n        days.push(M.diso(T + i));\n        recs(T + i).recs.forEach(function (r) {\n          var fixed = i === 0 && D.plans[T] && D.plans[T][r.product];\n          (week[r.product] = week[r.product] || [0, 0, 0, 0, 0, 0, 0])[i] = fixed ? fixed.qty : r.qty;\n        });\n      }\n      return {\n        today: M.diso(T), products: productList(), batches: batchList(T),\n        sales: D.sales.map(function (s) {\n          return { day: M.diso(s.day), product: s.product, baked: s.baked == null ? null : s.baked, sold: s.sold, soldout: s.soldout || null,\n                   rain: s.rain == null ? null : s.rain, temp: s.temp == null ? null : s.temp };\n        }),\n        week: { days: days, rows: Object.keys(week).map(function (k) { return { product: k, qty: week[k] }; }).sort(function (a, b) { return sum(b.qty) - sum(a.qty); }) }\n      };\n    }\n    function state() {\n      var now = new Date(), T = today(), m = now.getHours() * 60 + now.getMinutes();\n      Object.keys(D.plans).forEach(function (k) { if (+k < T - 14) delete D.plans[k]; });\n      var rt = recs(T), plan = D.plans[T] || (D.plans[T] = {}), changed = false;\n      rt.recs.forEach(function (r) { if (!plan[r.product]) { plan[r.product] = { qty: r.qty, mean: r.mean }; changed = true; } });\n      if (changed) save();\n      var items = rt.recs.map(function (r) {\n        var p = plan[r.product], it = item(r, p.qty, p.mean), s = sale(T, r.product);\n        it.record = s ? { soldout: s.soldout, baked: s.baked, left: s.soldout ? null : Math.max(0, Math.round((s.baked || 0) - s.sold)) } : null;\n        return it;\n      }).sort(function (a, b) { return b.qty - a.qty; });\n      var rm = recs(T + 1);\n      var tom = rm.recs.map(function (r) { var it = item(r); it.habit = rm.habit[r.product] != null ? Math.round(rm.habit[r.product]) : null; return it; });\n      var products = productList(), batches = batchList(T);\n      var removed = Object.keys(D.products).filter(function (k) { return !D.products[k].active; }).sort();\n      var days = D.sales.map(function (s) { return s.day; });\n      if (!btCache || btCache.n !== D.sales.length) {\n        var bt = D.sales.length ? M.backtest(D.sales, prodMap(false)) : null;\n        btCache = { n: D.sales.length, v: bt && bt.wasteNow > 0 ? { days: bt.days, waste_now: bt.wasteNow, waste_new: bt.wasteNew, lost_now: bt.lostNow, lost_new: bt.lostNew,\n          share_now: bt.wasteUnitsNow / bt.producedNow * 100, share_new: bt.wasteUnitsNew / bt.producedNew * 100 } : null };\n      }\n      return {\n        mode: \"local\", business: { name: D.business, city: D.example ? \"Челябинск\" : \"\", is_example: D.example },\n        now: M.diso(T) + \"T\" + pad(now.getHours()) + \":\" + pad(now.getMinutes()), phase: phaseOf(D.times, m), times: D.times,\n        today: { date: M.diso(T), rain: weather(T).rain, temp: weather(T).temp, event: D.events[T] || 1, items: items },\n        tomorrow: { date: M.diso(T + 1), rain: weather(T + 1).rain, temp: weather(T + 1).temp, event: D.events[T + 1] || 1, rain_effect: rm.eff, items: tom },\n        products: products, removed: removed, batches: batches,\n        data: { rows: D.sales.length, first: days.length ? M.diso(Math.min.apply(null, days)) : null, last: days.length ? M.diso(Math.max.apply(null, days)) : null },\n        summary: btCache.v\n      };\n    }\n    function num(v, lo, hi, integer) {\n      if (v === \"\" || v == null) return null;\n      var x = Number(String(v).replace(\",\", \".\").replace(/\\s/g, \"\"));\n      if (isNaN(x) || (lo != null && x < lo) || (hi != null && x > hi)) throw new Error(\"проверьте число: \" + v);\n      return integer ? Math.round(x) : x;\n    }\n    function name(v, field) { var s = String(v || \"\").replace(/\\s+/g, \" \").trim(); if (!s) throw new Error(\"пустое поле: \" + (field || \"название\")); return s.slice(0, 60); }\n    function act(path, b) {\n      var T = today();\n      try {\n        if (path === \"/api/product\") {\n          var nm = name(b.name), old = b.old_name || null, price = num(b.price, 0, 1e6), cost = num(b.cost, 0, 1e6);\n          if (price != null && cost != null && cost > price) throw new Error(\"себестоимость больше цены — проверьте цифры\");\n          if (old && old !== nm) {\n            D.sales.forEach(function (s) { if (s.product === old) s.product = nm; });\n            Object.keys(D.plans).forEach(function (d) { if (D.plans[d][old]) { D.plans[d][nm] = D.plans[d][old]; delete D.plans[d][old]; } });\n            delete D.products[old]; btCache = null;\n          }\n          D.products[nm] = { name: nm, price: price, cost: cost, shelf_life: num(b.shelf_life, 0, 3650, true), default_qty: num(b.default_qty, 0, 1e5), active: true };\n        } else if (path === \"/api/product/remove\" || path === \"/api/product/restore\") {\n          var n2 = name(b.name), on = path === \"/api/product/restore\";\n          D.products[n2] = D.products[n2] || { name: n2, price: null, cost: null, shelf_life: null, default_qty: null };\n          D.products[n2].active = on;\n          if (!on) [T - 1, T, T + 1].forEach(function (d) { if (D.plans[d]) delete D.plans[d][n2]; });\n        } else if (path === \"/api/batch\") {\n          var it = name(b.item, \"продукт\"), exp = b.expires ? M.dnum(b.expires) : null;\n          if (exp == null) {\n            var p = D.products[it];\n            if (!p || !p.shelf_life) throw new Error(\"укажите дату «годен до» или срок хранения у товара\");\n            exp = M.dnum(b.made || M.diso(T)) + p.shelf_life;\n          }\n          D.batches.push({ id: D.nextId++, item: it, qty: String(b.qty || \"\").trim().slice(0, 20), exp: exp, status: \"active\" });\n        } else if (path === \"/api/batch/close\") {\n          D.batches.forEach(function (x) { if (x.id === +b.id) x.status = b.status; });\n        } else if (path === \"/api/record\") {\n          var pr = name(b.product), so = b.soldout || null;\n          if (so && !/^([01]?\\d|2[0-3]):[0-5]\\d$/.test(so)) throw new Error(\"время в формате 15:30\");\n          var left = so ? null : num(b.left, 0, 1e5, true);\n          if (!so && left == null) throw new Error(\"укажите остаток или время, когда товар закончился\");\n          var baked = (D.plans[T] && D.plans[T][pr]) ? D.plans[T][pr].qty : 0;\n          var s = sale(T, pr), sold = so ? baked : Math.max(0, baked - left);\n          if (s) { s.sold = sold; s.baked = baked; s.soldout = so; } else D.sales.push({ day: T, product: pr, sold: sold, baked: baked, soldout: so, rain: weather(T).rain, temp: weather(T).temp });\n          btCache = null;\n        } else if (path === \"/api/sales\") {\n          var TIME = /^([01]?\\d|2[0-3]):[0-5]\\d$/, ups = (b.upsert || []).map(function (x) {\n            var pr = name(x.product, \"товар\"), sold = num(x.sold, 0, 1e5), bk = num(x.baked, 0, 1e5);\n            if (sold == null) throw new Error(pr + \": не указано, сколько продано\");\n            if (bk != null && sold > bk) throw new Error(pr + \": продано больше, чем испечено\");\n            if (x.soldout && !TIME.test(x.soldout)) throw new Error(\"время в формате 15:30\");\n            return { day: M.dnum(x.day), product: pr, sold: sold, baked: bk, soldout: x.soldout || null, rain: x.rain == null ? null : !!x.rain, temp: num(x.temp, -60, 50) };\n          });\n          var gone = {};\n          (b[\"delete\"] || []).forEach(function (x) { gone[M.dnum(x.day) + \"|\" + x.product] = 1; });\n          D.sales = D.sales.filter(function (x) { return !gone[x.day + \"|\" + x.product]; });\n          var at = {};\n          D.sales.forEach(function (x, i) { at[x.day + \"|\" + x.product] = i; });\n          ups.forEach(function (x) { var k = x.day + \"|\" + x.product; if (k in at) D.sales[at[k]] = x; else { at[k] = D.sales.length; D.sales.push(x); } });\n          btCache = null;\n        } else if (path === \"/api/batch/edit\") {\n          var bi = name(b.item, \"продукт\"), be = b.expires ? M.dnum(b.expires) : null;\n          if (be == null || isNaN(be)) throw new Error(\"годен до: нужна дата\");\n          D.batches.forEach(function (x) { if (x.id === +b.id) { x.item = bi; x.qty = String(b.qty || \"\").trim().slice(0, 20); x.exp = be; } });\n        } else if (path === \"/api/event\") {\n          var f = num(b.factor, 0.3, 3);\n          if (!f || Math.abs(f - 1) < 1e-9) delete D.events[T + 1]; else D.events[T + 1] = f;\n        } else if (path === \"/api/settings\") {\n          if (\"business\" in b) D.business = String(b.business || \"\").trim().slice(0, 80);\n          [\"morning\", \"evening\", \"closing\"].forEach(function (k) {\n            if (b[k]) { if (!/^([01]?\\d|2[0-3]):[0-5]\\d$/.test(b[k])) throw new Error(\"время в формате 06:00\"); D.times[k] = pad(+b[k].split(\":\")[0]) + \":\" + b[k].split(\":\")[1]; }\n          });\n        } else if (path === \"/api/upload\") {\n          var r = M.parseCSV(String(b.csv || \"\"));\n          if (D.example) { D.sales = []; D.products = {}; D.plans = {}; D.events = {}; D.batches = []; D.example = false; D.business = \"\"; }\n          var idx = {};\n          D.sales.forEach(function (x, i) { idx[x.day + \"|\" + x.product] = i; });\n          r.sales.forEach(function (x) { var k = x.day + \"|\" + x.product; if (k in idx) D.sales[idx[k]] = x; else { idx[k] = D.sales.length; D.sales.push(x); } });\n          Object.keys(r.products).forEach(function (k) {\n            var p = r.products[k], cur = D.products[k] || { name: k, shelf_life: null, default_qty: null };\n            cur.price = p.price != null ? p.price : cur.price; cur.cost = p.cost != null ? p.cost : cur.cost; cur.active = true; D.products[k] = cur;\n          });\n          btCache = null; save();\n          return Promise.resolve({ ok: true, rows: r.sales.length, products: Object.keys(r.products).length });\n        } else if (path === \"/api/example\") {\n          example();\n        } else {\n          throw new Error(\"неизвестное действие\");\n        }\n      } catch (e) { return Promise.reject(e); }\n      save();\n      return Promise.resolve({ ok: true });\n    }\n    return { load: load, state: function () { return Promise.resolve(state()); }, sheet: sheet, act: act, reset: example };\n  })();\n  function LoginNeeded() { this.login = true; }\n  var API = {\n    mode: null,\n    init: function () {\n      var self = this;\n      if (location.protocol === \"file:\") { self.mode = \"local\"; Local.load(); return Promise.resolve(); }\n      return fetch(\"api/ping\", { cache: \"no-store\" }).then(function (r) { return r.ok ? r.json() : null; })\n        .catch(function () { return null; })\n        .then(function (j) { if (j && j.app === \"svezhiy\") self.mode = \"server\"; else { self.mode = \"local\"; Local.load(); } });\n    },\n    state: function () {\n      if (this.mode === \"local\") return Local.state();\n      return fetch(\"api/state\", { cache: \"no-store\", credentials: \"same-origin\" }).then(function (r) {\n        if (r.status === 401) throw new LoginNeeded();\n        if (!r.ok) throw new Error(\"Сервер ответил \" + r.status);\n        return r.json();\n      });\n    },\n    sheet: function () {\n      if (this.mode === \"local\") { try { return Promise.resolve(Local.sheet()); } catch (e) { return Promise.reject(e); } }\n      return fetch(\"api/sheet\", { cache: \"no-store\", credentials: \"same-origin\" }).then(function (r) {\n        if (r.status === 401) throw new LoginNeeded();\n        if (!r.ok) throw new Error(\"Сервер ответил \" + r.status);\n        return r.json();\n      });\n    },\n    post: function (path, body) {\n      if (this.mode === \"local\") return Local.act(path, body || {});\n      return fetch(path.slice(1), { method: \"POST\", credentials: \"same-origin\", headers: { \"Content-Type\": \"application/json\" }, body: JSON.stringify(body || {}) })\n        .then(function (r) {\n          return r.json().catch(function () { return {}; }).then(function (j) {\n            if (r.status === 401) throw new LoginNeeded();\n            if (!r.ok) throw new Error(j.error || (\"Ошибка \" + r.status));\n            return j;\n          });\n        });\n    }\n  };\n  var S = { st: null, tab: \"today\", part: null, partManual: false, offset: 0, fetchedAt: 0, edit: null, confirm: null, vitrina: {}, busy: false };\n  function nowMinutes() {\n    var d = new Date(Date.now() + S.offset);\n    return { m: d.getHours() * 60 + d.getMinutes(), label: pad(d.getHours()) + \":\" + pad(d.getMinutes()) };\n  }\n  function serverOffset(nowStr) {\n    var p = nowStr.split(\"T\"), d = p[0].split(\"-\"), t = p[1].split(\":\");\n    var server = new Date(+d[0], +d[1] - 1, +d[2], +t[0], +t[1]).getTime();\n    return server - Date.now();\n  }\n  function renderTop() {\n    var st = S.st, n = nowMinutes();\n    $(\"biz\").textContent = st.business.name ? st.business.name + (st.business.city ? \", \" + st.business.city : \"\") : \"Личный кабинет\";\n    $(\"clock\").textContent = n.label + \" · \" + dayLabel(st.today.date);\n    $(\"top-actions\").innerHTML = API.mode === \"server\"\n      ? '<button class=\"btn btn-ghost btn-sm\" data-act=\"logout\">Выйти</button>'\n      : '<button class=\"btn btn-ghost btn-sm\" data-act=\"reset\">Сбросить пример</button>';\n    $(\"brand\").setAttribute(\"href\", API.mode === \"server\" ? \"#\" : \"index.html\");\n  }\n\n  function renderDay() {\n    var st = S.st, t = st.times, n = nowMinutes(), ph = phaseOf(t, n.m);\n    var a = mins(t.morning), b = mins(t.evening), c = mins(t.closing), end = Math.min(c + 120, 24 * 60);\n    var spans = [Math.max(b - a, 30), Math.max(c - b, 30), Math.max(end - c, 30)];\n    var cur = PART_OF[ph], heads = {\n      night: \"Ночь: план на сегодня уже готов\", morning: \"Утро и день: работаем по плану\",\n      evening: \"Вечер: проверьте витрину\", closing: \"Конец дня: отметьте итоги\" };\n    var parts = [[\"plan\", \"План на день\", t.morning, \"сколько сделать и заказать\"],\n                 [\"vitrina\", \"Витрина и скидки\", t.evening, \"что уценить, чтобы не выбросить\"],\n                 [\"itogi\", \"Итоги дня\", t.closing, \"минута на отметки\"]];\n    var idxNow = { plan: 0, vitrina: 1, itogi: 2 }[cur];\n    var pct = Math.max(0, Math.min(100, (n.m - a) / (end - a) * 100));\n    var h = '<div class=\"day-head\"><h1>' + esc(heads[ph]) + '</h1><span class=\"upd\"><span class=\"pulse\" aria-hidden=\"true\"></span>обновляется автоматически · ' + esc(n.label) + \"</span></div>\";\n    h += '<div class=\"ribbon\" style=\"--cols:' + spans.map(function (s) { return s + \"fr\"; }).join(\" \") + '\">';\n    parts.forEach(function (p, i) {\n      var cls = \"seg\" + (i === idxNow && ph !== \"night\" ? \" now\" : i < idxNow ? \" past\" : \"\");\n      h += '<button class=\"' + cls + '\" data-part=\"' + p[0] + '\" aria-pressed=\"' + (S.part === p[0]) + '\"><span class=\"t\">' + p[2] + \"</span>\" +\n           (i === idxNow && ph !== \"night\" ? '<span class=\"now-tag\">сейчас</span>' : \"\") + \"<b>\" + p[1] + '</b><span class=\"desc\">' + p[3] + \"</span></button>\";\n    });\n    h += '</div><div class=\"track\" aria-hidden=\"true\"><i style=\"width:' + pct.toFixed(1) + '%\"></i><span style=\"left:' + pct.toFixed(1) + '%\"></span></div>';\n    $(\"day\").innerHTML = h;\n  }\n\n  function expiringCount() { return S.st.batches.filter(function (b) { return b.left <= 1; }).length; }\n\n  function renderTabs() {\n    document.querySelectorAll(\".tabs button\").forEach(function (b) {\n      var t = b.getAttribute(\"data-tab\");\n      b.setAttribute(\"aria-selected\", String(S.tab === t));\n      if (t === \"batches\") { var n = expiringCount(); b.innerHTML = \"Сроки годности\" + (n ? '<span class=\"cnt\">' + n + \"</span>\" : \"\"); }\n    });\n  }\n\n  function whyText(it) { return it.reasons.filter(function (x) { return x.indexOf(\"дождь\") !== 0 && x.indexOf(\"событие\") !== 0; }).slice(0, 2).join(\"; \"); }\n\n  function viewPlan() {\n    var st = S.st, items = st.today.items, total = items.reduce(function (a, i) { return a + i.qty; }, 0), exp = st.batches.filter(function (b) { return b.left <= 1; });\n    var h = '<div class=\"kpis\"><div class=\"kpi\"><b class=\"num\">' + total + '</b><span>штук сделать сегодня</span></div>' +\n      '<div class=\"kpi\"><b class=\"num\">' + items.length + \"</b><span>\" + plural(items.length, \"позиция\", \"позиции\", \"позиций\") + \" в плане</span></div>\" +\n      '<div class=\"kpi' + (exp.length ? \" bad\" : \"\") + '\"><b class=\"num\">' + exp.length + \"</b><span>\" + plural(exp.length, \"партия истекает\", \"партии истекают\", \"партий истекают\") + \" сегодня–завтра</span></div>\" +\n      '<div class=\"kpi\"><b>' + (st.today.event !== 1 ? M.signed((st.today.event - 1) * 100) : st.today.rain ? \"дождь\" : \"обычный\") + \"</b><span>\" +\n      (st.today.event !== 1 ? \"событие сегодня\" : st.today.rain ? \"спрос ниже обычного\" : \"день по прогнозу\") + \"</span></div></div>\";\n    if (!items.length) return h + emptyData();\n    h += '<div class=\"card\"><h2>План на ' + dayLabel(st.today.date) + '</h2><p class=\"sub\">Количество зафиксировано утром. Новые товары появляются в плане сразу после добавления.</p><div class=\"table-wrap\"><table class=\"t\"><thead><tr><th>Позиция</th><th class=\"r\">Спрос</th><th class=\"r\">Сделать</th></tr></thead><tbody>';\n    items.forEach(function (it) {\n      var w = whyText(it);\n      h += \"<tr><td><div class=\\\"name\\\">\" + esc(it.product) + (it[\"new\"] ? ' <span class=\"chip new\">новый</span>' : \"\") + \"</div>\" + (w ? '<div class=\"why\">' + esc(w) + \"</div>\" : \"\") +\n        '</td><td class=\"r num\">≈' + Math.round(it.mean) + ' <span class=\"muted\">±' + Math.round(it.sd) + '</span></td><td class=\"r\"><span class=\"big\">' + it.qty + \"</span></td></tr>\";\n    });\n    h += '</tbody><tfoot><tr><td>Всего</td><td></td><td class=\"r\"><span class=\"big\">' + total + \"</span></td></tr></tfoot></table></div></div>\";\n    if (exp.length) {\n      h += '<div class=\"card\"><h2>Пора пустить в работу</h2><p class=\"sub\">Партии, у которых срок заканчивается сегодня или завтра.</p>' + batchRows(exp, true) + \"</div>\";\n    }\n    return h;\n  }\n\n  function viewVitrina() {\n    var items = S.st.today.items;\n    if (!items.length) return emptyData();\n    var h = '<div class=\"card\"><h2>Проверьте витрину</h2><p class=\"sub\">После ' + esc(S.st.times.evening) + \" обычно уходит около пятой части дневных продаж. Если на витрине больше порога — ставьте скидку, иначе остаток уйдёт в списание. Впишите, сколько сейчас лежит, и получите совет.</p>\" +\n      '<div class=\"table-wrap\"><table class=\"t\"><thead><tr><th>Позиция</th><th class=\"r\">Сделали</th><th class=\"r\">Скидка, если больше</th><th>Сейчас на витрине</th><th>Совет</th></tr></thead><tbody>';\n    items.slice().sort(function (a, b) { return a.product < b.product ? -1 : 1; }).forEach(function (it) {\n      var v = S.vitrina[it.product], adv = \"\";\n      if (v !== undefined && v !== \"\") {\n        var a = M.discountAdvice({ mean: it.mean }, +v);\n        adv = a.pct ? '<span class=\"chip warn\">скидка ' + a.pct + \"%</span>\" : '<span class=\"chip ok\">без скидки</span>';\n      }\n      h += \"<tr><td class=\\\"name\\\">\" + esc(it.product) + '</td><td class=\"r num\">' + it.qty + '</td><td class=\"r num\"><b>' + it.threshold + \" шт</b></td>\" +\n        '<td><input class=\"small\" type=\"number\" min=\"0\" inputmode=\"numeric\" data-vitrina=\"' + esc(it.product) + '\" value=\"' + esc(v == null ? \"\" : v) + '\" aria-label=\"Сейчас на витрине: ' + esc(it.product) + '\"></td><td data-adv=\"' + esc(it.product) + '\">' + adv + \"</td></tr>\";\n    });\n    return h + \"</tbody></table></div></div>\";\n  }\n\n  function viewItogi() {\n    var items = S.st.today.items;\n    if (!items.length) return emptyData();\n    var done = items.filter(function (i) { return i.record; }).length;\n    var h = '<div class=\"card\"><h2>Итоги дня · ' + dayLabel(S.st.today.date) + '</h2><p class=\"sub\">Отметьте по каждой позиции, когда она закончилась или сколько осталось. По этим отметкам сервис узнаёт реальный спрос и завтра считает точнее. То же можно сделать в боте.</p>' +\n      '<p class=\"hint\">Отмечено ' + done + \" из \" + items.length + '</p><div class=\"progress\"><i style=\"width:' + (done / items.length * 100).toFixed(0) + '%\"></i></div>' +\n      '<div class=\"table-wrap\" style=\"margin-top:14px\"><table class=\"t\"><thead><tr><th>Позиция</th><th class=\"r\">Сделали</th><th>Закончилась</th><th>Или осталось</th><th>Отметка</th></tr></thead><tbody>';\n    items.slice().sort(function (a, b) { return a.product < b.product ? -1 : 1; }).forEach(function (it) {\n      var r = it.record, mark = !r ? '<span class=\"chip mute\">нет</span>' : r.soldout ? '<span class=\"chip ok\">✓ кончились ' + esc(r.soldout) + \"</span>\" : '<span class=\"chip ok\">✓ осталось ' + r.left + \"</span>\";\n      h += '<tr><td class=\"name\">' + esc(it.product) + '</td><td class=\"r num\">' + it.qty + '</td><td><div class=\"slots\">' +\n        SLOTS.map(function (s) { return '<button class=\"slot\" data-act=\"soldout\" data-p=\"' + esc(it.product) + '\" data-t=\"' + s[1] + '\">' + s[0] + \"</button>\"; }).join(\"\") +\n        '</div></td><td><div class=\"left-in\"><input type=\"number\" min=\"0\" inputmode=\"numeric\" data-left=\"' + esc(it.product) + '\" aria-label=\"Осталось: ' + esc(it.product) + '\"><button class=\"btn btn-quiet btn-sm\" data-act=\"left\" data-p=\"' + esc(it.product) + '\">OK</button></div></td><td>' + mark + \"</td></tr>\";\n    });\n    return h + \"</tbody></table></div></div>\";\n  }\n\n  function emptyData() {\n    return '<div class=\"empty\">Пока нет товаров и продаж. Добавьте товары на вкладке «Товары» или загрузите выгрузку продаж в «Данные и настройки».</div>';\n  }\n\n  function viewTomorrow() {\n    var t = S.st.tomorrow, items = t.items, total = 0, habit = 0, hasH = false;\n    var parts = [];\n    if (t.rain === true) parts.push(\"Ожидается дождь: спрос ниже на \" + Math.abs(Math.round(t.rain_effect * 100)) + \"%\");\n    else if (t.rain === false) parts.push(\"Без осадков\");\n    if (t.temp != null) parts.push(M.deg(t.temp));\n    var weather = parts.length ? parts.join(\" · \") : \"Прогноз погоды пока недоступен\";\n    var opts = [[1, \"Обычный день\"], [1.1, \"Небольшое событие +10%\"], [1.2, \"Праздник, ярмарка +20%\"], [1.3, \"Большое событие +30%\"], [0.8, \"Меньше людей −20%\"]];\n    var h = '<div class=\"card\"><div class=\"day-head\" style=\"margin:0 0 10px\"><div><h2>Завтра, ' + dayLabel(t.date) + '</h2><p class=\"sub\" style=\"margin:0\">' + esc(weather) + '</p></div>' +\n      '<label style=\"display:grid;gap:4px;font-size:12.5px;color:var(--ink-3);min-width:220px\">Событие завтра<select data-act=\"event\">' +\n      opts.map(function (o) { return '<option value=\"' + o[0] + '\"' + (Math.abs(o[0] - t.event) < 1e-9 ? \" selected\" : \"\") + \">\" + o[1] + \"</option>\"; }).join(\"\") + \"</select></label></div>\";\n    if (!items.length) return h + emptyData() + \"</div>\";\n    h += '<div class=\"table-wrap\"><table class=\"t\"><thead><tr><th>Позиция</th><th class=\"r\">Спрос</th><th class=\"r\">Сделать</th><th class=\"r\">Обычно</th></tr></thead><tbody>';\n    items.forEach(function (it) {\n      total += it.qty; if (it.habit != null) { habit += it.habit; hasH = true; } else habit += it.qty;\n      var w = whyText(it);\n      h += \"<tr><td><div class=\\\"name\\\">\" + esc(it.product) + (it[\"new\"] ? ' <span class=\"chip new\">новый</span>' : \"\") + \"</div>\" + (w ? '<div class=\"why\">' + esc(w) + \"</div>\" : \"\") +\n        '</td><td class=\"r num\">≈' + Math.round(it.mean) + '</td><td class=\"r\"><span class=\"big\">' + it.qty + '</span></td><td class=\"r num muted\">' + (it.habit != null ? it.habit : \"—\") + \"</td></tr>\";\n    });\n    h += '</tbody><tfoot><tr><td>Всего</td><td></td><td class=\"r\"><span class=\"big\">' + total + '</span></td><td class=\"r num\">' + (hasH ? Math.round(habit) : \"—\") + \"</td></tr></tfoot></table></div>\" +\n      '<p class=\"hint\" style=\"margin-top:10px\">«Обычно» — сколько в среднем делали в этот день недели за 4 недели. Утром в ' + esc(S.st.times.morning) + \" план зафиксируется и придёт в Telegram.</p></div>\";\n    return h;\n  }\n\n  function productForm(p) {\n    p = p || {};\n    return '<label class=\"wide\">Название<input type=\"text\" name=\"name\" maxlength=\"60\" required value=\"' + esc(p.name || \"\") + '\" placeholder=\"Например, Салат «Оливье»\"></label>' +\n      '<label>Цена, ₽<input type=\"number\" name=\"price\" min=\"0\" step=\"any\" value=\"' + esc(p.price == null ? \"\" : p.price) + '\"></label>' +\n      '<label>Себестоимость, ₽<input type=\"number\" name=\"cost\" min=\"0\" step=\"any\" value=\"' + esc(p.cost == null ? \"\" : p.cost) + '\"></label>' +\n      '<label>Срок хранения, дней<input type=\"number\" name=\"shelf_life\" min=\"0\" step=\"1\" value=\"' + esc(p.shelf_life == null ? \"\" : p.shelf_life) + '\"></label>' +\n      '<label>Обычно в день, шт<input type=\"number\" name=\"default_qty\" min=\"0\" step=\"any\" value=\"' + esc(p.default_qty == null ? \"\" : p.default_qty) + '\"></label>';\n  }\n\n  function viewProducts() {\n    var st = S.st;\n    var h = '<div class=\"card\"><h2>Добавить товар</h2><p class=\"sub\">Цена и себестоимость нужны, чтобы понимать, что дороже: остаток или пустая полка. Срок хранения подставляет дату «годен до» в партиях. «Обычно в день» — для нового товара без истории продаж: план будет таким, пока не накопятся продажи.</p>' +\n      '<form class=\"form\" data-form=\"product-add\">' + productForm() + '<button class=\"btn btn-primary\" type=\"submit\">Добавить</button></form></div>';\n    h += '<div class=\"card\"><h2>Товары · ' + st.products.length + '</h2><p class=\"sub\">Убранный товар исчезает из планов, но его история продаж сохраняется — его можно вернуть.</p>';\n    if (!st.products.length) h += '<div class=\"empty\">Товаров пока нет.</div>';\n    else {\n      h += '<div class=\"table-wrap\"><table class=\"t\"><thead><tr><th>Товар</th><th class=\"r\">Цена</th><th class=\"r\">Себест.</th><th class=\"r\">Маржа</th><th class=\"r\">Срок</th><th>История</th><th></th></tr></thead><tbody>';\n      st.products.forEach(function (p) {\n        if (S.edit === p.name) {\n          h += '<tr><td colspan=\"7\"><form class=\"form\" data-form=\"product-edit\" data-old=\"' + esc(p.name) + '\">' + productForm(p) +\n            '<div class=\"acts\"><button class=\"btn btn-primary btn-sm\" type=\"submit\">Сохранить</button><button class=\"btn btn-quiet btn-sm\" type=\"button\" data-act=\"edit-cancel\">Отмена</button></div></form></td></tr>';\n          return;\n        }\n        var margin = p.price && p.cost != null ? Math.round((p.price - p.cost) / p.price * 100) + \"%\" : \"—\";\n        var acts = S.confirm === p.name\n          ? '<span class=\"hint\">Убрать из планов?</span><button class=\"btn btn-danger btn-sm\" data-act=\"remove\" data-p=\"' + esc(p.name) + '\">Да, убрать</button><button class=\"btn btn-quiet btn-sm\" data-act=\"confirm-cancel\">Нет</button>'\n          : '<button class=\"btn btn-quiet btn-sm\" data-act=\"edit\" data-p=\"' + esc(p.name) + '\">Изменить</button><button class=\"btn btn-quiet btn-sm\" data-act=\"ask-remove\" data-p=\"' + esc(p.name) + '\">Убрать</button>';\n        h += '<tr><td class=\"name\">' + esc(p.name) + '</td><td class=\"r num\">' + (p.price != null ? fmt(p.price) : \"—\") + '</td><td class=\"r num\">' + (p.cost != null ? fmt(p.cost) : \"—\") +\n          '</td><td class=\"r num\">' + margin + '</td><td class=\"r num\">' + (p.shelf_life != null ? p.shelf_life + \" дн.\" : \"—\") + \"</td><td>\" +\n          (p.history_days ? '<span class=\"chip mute\">' + p.history_days + \" дн. продаж</span>\" : '<span class=\"chip new\">новый' + (p.default_qty ? \", \" + fmt(p.default_qty) + \" шт/день\" : \"\") + \"</span>\") +\n          '</td><td><div class=\"acts\">' + acts + \"</div></td></tr>\";\n      });\n      h += \"</tbody></table></div>\";\n    }\n    if (st.removed.length) {\n      h += '<div class=\"removed\" style=\"margin-top:14px\"><span class=\"hint\">Убранные:</span>' + st.removed.map(function (n) {\n        return '<span class=\"chip mute\">' + esc(n) + '</span><button class=\"btn btn-quiet btn-sm\" data-act=\"restore\" data-p=\"' + esc(n) + '\">Вернуть</button>';\n      }).join(\"\") + \"</div>\";\n    }\n    return h + \"</div>\";\n  }\n\n  function batchRows(list, compact) {\n    if (!list.length) return '<div class=\"empty\">Партий нет. Добавьте продукт, количество и срок годности.</div>';\n    var glyph = { ok: \"●\", warn: \"▲\", bad: \"✕\" };\n    var h = '<div class=\"table-wrap\"><table class=\"t\"><thead><tr><th>Продукт</th><th>Количество</th><th>Годен до</th><th>Статус</th>' + (compact ? \"\" : \"<th></th>\") + \"</tr></thead><tbody>\";\n    list.forEach(function (b) {\n      h += '<tr><td><div class=\"name\">' + esc(b.item) + \"</div>\" + (b.tip ? '<div class=\"why\">→ ' + esc(b.tip) + \"</div>\" : \"\") + \"</td><td>\" + esc(b.qty || \"—\") +\n        '</td><td class=\"num\">' + dayLabel(b.expires) + '</td><td><span class=\"chip ' + b.level + '\">' + glyph[b.level] + \" \" + esc(b.label) + \"</span></td>\" +\n        (compact ? \"\" : '<td><div class=\"acts\"><button class=\"btn btn-quiet btn-sm\" data-act=\"batch\" data-id=\"' + b.id + '\" data-s=\"used\">Использовано</button><button class=\"btn btn-quiet btn-sm\" data-act=\"batch\" data-id=\"' + b.id + '\" data-s=\"written_off\">Списано</button><button class=\"btn btn-quiet btn-sm\" data-act=\"batch\" data-id=\"' + b.id + '\" data-s=\"deleted\" aria-label=\"Удалить запись\">Удалить</button></div></td>') + \"</tr>\";\n    });\n    return h + \"</tbody></table></div>\";\n  }\n\n  function viewBatches() {\n    var st = S.st, today = st.today.date;\n    var h = '<div class=\"card\"><h2>Добавить партию</h2><p class=\"sub\">Выберите товар со сроком хранения — дата «годен до» посчитается сама. Для сырья (сливки, творог) укажите дату с упаковки.</p>' +\n      '<form class=\"form\" data-form=\"batch-add\"><label class=\"wide\">Продукт<input type=\"text\" name=\"item\" list=\"prodlist\" maxlength=\"60\" required placeholder=\"Сливки 33% или товар из списка\"></label>' +\n      '<label>Количество<input type=\"text\" name=\"qty\" maxlength=\"20\" placeholder=\"2 л, 20 шт\"></label>' +\n      '<label>Изготовлено / поставлено<input type=\"date\" name=\"made\" value=\"' + today + '\"></label>' +\n      '<label>Годен до<input type=\"date\" name=\"expires\"></label><button class=\"btn btn-primary\" type=\"submit\">Добавить</button></form>' +\n      '<datalist id=\"prodlist\">' + st.products.map(function (p) { return '<option value=\"' + esc(p.name) + '\">' + (p.shelf_life ? \"срок \" + p.shelf_life + \" дн.\" : \"\") + \"</option>\"; }).join(\"\") + \"</datalist></div>\";\n    h += '<div class=\"card\"><h2>Партии и сроки · ' + st.batches.length + '</h2><p class=\"sub\">За день до конца срока сервис напомнит в Telegram и подскажет, куда пустить продукт.</p>' + batchRows(st.batches, false) + \"</div>\";\n    return h;\n  }\n\n  function viewData() {\n    var st = S.st, d = st.data, sm = st.summary;\n    var h = '<div class=\"grid2\"><div class=\"card\"><h2>Заведение и расписание</h2><p class=\"sub\">Время рассылок в Telegram и смены частей дня в кабинете.</p>' +\n      '<form class=\"form\" data-form=\"settings\"><label class=\"wide\">Название<input type=\"text\" name=\"business\" maxlength=\"80\" value=\"' + esc(st.business.name) + '\" placeholder=\"Булочная на Ленина\"></label>' +\n      '<label>План утром<input type=\"time\" name=\"morning\" value=\"' + esc(st.times.morning) + '\"></label><label>Витрина вечером<input type=\"time\" name=\"evening\" value=\"' + esc(st.times.evening) + '\"></label>' +\n      '<label>Итоги дня<input type=\"time\" name=\"closing\" value=\"' + esc(st.times.closing) + '\"></label><button class=\"btn btn-primary\" type=\"submit\">Сохранить</button></form></div>';\n    h += '<div class=\"card\"><h2>Данные продаж</h2><p class=\"sub\">' + (d.rows ? d.rows + \" \" + plural(d.rows, \"строка\", \"строки\", \"строк\") + \" с \" + dayLabel(d.first) + \" по \" + dayLabel(d.last) + (st.business.is_example ? \". Сейчас загружен пример «Пекарня».\" : \".\") : \"Продаж пока нет.\") + \"</p>\" +\n      '<div class=\"form\"><label class=\"wide\">Выгрузка из кассы или Excel (CSV)<input type=\"file\" accept=\".csv,.txt,text/csv\" data-act=\"upload\"></label></div>' +\n      '<p class=\"hint\" style=\"margin-top:8px\">Колонки: <span class=\"mono\">дата;товар;продано</span>, по желанию <span class=\"mono\">испечено (или заказано);закончилось;цена;себестоимость;дождь;температура</span>. Свои данные заменят пример.</p>' +\n      '<div class=\"acts\" style=\"justify-content:flex-start;margin-top:12px\">' + (S.confirm === \"__example\"\n        ? '<span class=\"hint\">Заменить все данные примером?</span><button class=\"btn btn-danger btn-sm\" data-act=\"example\">Да, загрузить пример</button><button class=\"btn btn-quiet btn-sm\" data-act=\"confirm-cancel\">Нет</button>'\n        : '<button class=\"btn btn-quiet btn-sm\" data-act=\"ask-example\">Загрузить пример пекарни</button>') + \"</div></div></div>\";\n    if (sm) {\n      var max = Math.max(sm.waste_now + sm.lost_now, sm.waste_new + sm.lost_new);\n      var row = function (label, a, b) {\n        return '<div style=\"margin-top:14px\"><b>' + label + ' <span class=\"hint\">' + M.signed((b / a - 1) * 100) + '</span></b>' +\n          '<div class=\"cmp\"><span>Как было</span><div class=\"bar\"><i style=\"width:' + (a / max * 100).toFixed(1) + '%;background:var(--ink-3)\"></i></div><span class=\"v\">' + money(a) + \"</span></div>\" +\n          '<div class=\"cmp\"><span>По плану</span><div class=\"bar\"><i style=\"width:' + (b / max * 100).toFixed(1) + '%;background:var(--stamp)\"></i></div><span class=\"v\">' + money(b) + \"</span></div></div>\";\n      };\n      h += '<div class=\"card\"><h2>Проверка на истории</h2><p class=\"sub\">Что было бы за последние ' + sm.days + \" \" + plural(sm.days, \"день\", \"дня\", \"дней\") + \", если бы работали по плану сервиса. Для каждого дня модель видит только продажи до него.\" +\n        (st.business.is_example ? \" Данные примера «Пекарня» смоделированы.\" : \"\") + \"</p>\" +\n        row(\"Списания по себестоимости\", sm.waste_now, sm.waste_new) + row(\"Упущенная прибыль, когда товар закончился\", sm.lost_now, sm.lost_new) +\n        row(\"Итого потери\", sm.waste_now + sm.lost_now, sm.waste_new + sm.lost_new) + \"</div>\";\n    }\n    return h;\n  }\n\n  function viewTables() {\n    var host = $(\"sheets\");\n    if (S.sheets && host && host === S.sheets.el) return;\n    $(\"view\").innerHTML = '<div class=\"card\"><h2>Таблицы</h2><p class=\"sub\">Все данные в одном месте, как в Excel: продажи по дням, товары, сроки годности и план на неделю. Правки сразу учитываются в прогнозе. Можно вставлять прямо из Excel и скачивать обратно.</p><div id=\"sheets\" style=\"margin-top:14px\"></div></div>';\n    S.sheets = new Sheets($(\"sheets\"), sheetAdapter);\n    S.sheets.load();\n  }\n  function touched(r) { S.dirty = true; return r; }\n  function unlogged(e) { if (e && e.login) showLogin(); throw e; }\n  var sheetAdapter = {\n    today: null,\n    load: function () { return API.sheet().then(function (d) { sheetAdapter.today = d.today; return d; }).catch(unlogged); },\n    saveSales: function (up, del) { return API.post(\"/api/sales\", { upsert: up, \"delete\": del }).then(touched).catch(unlogged); },\n    saveProduct: function (p, oldName) { var body = Object.assign({}, p); if (oldName) body.old_name = oldName; return API.post(\"/api/product\", body).then(touched).catch(unlogged); },\n    removeProduct: function (n) { return API.post(\"/api/product/remove\", { name: n }).then(touched).catch(unlogged); },\n    addBatch: function (b) { var body = { item: b.item, qty: b.qty, made: b.made }; if (b.expires) body.expires = b.expires; return API.post(\"/api/batch\", body).then(touched).catch(unlogged); },\n    editBatch: function (b) { return API.post(\"/api/batch/edit\", b).then(touched).catch(unlogged); },\n    deleteBatch: function (id) { return API.post(\"/api/batch/close\", { id: id, status: \"deleted\" }).then(touched).catch(unlogged); },\n    upload: function (text) { return API.post(\"/api/upload\", { csv: text }).then(touched).catch(unlogged); },\n    toast: function (m) { toast(m); }\n  };\n\n  function render() {\n    if (!S.st) return;\n    renderTop(); renderDay(); renderTabs();\n    if (S.tab === \"tables\") { viewTables(); return; }\n    S.sheets = null;\n    var v = \"\";\n    if (S.tab === \"today\") v = S.part === \"vitrina\" ? viewVitrina() : S.part === \"itogi\" ? viewItogi() : viewPlan();\n    else if (S.tab === \"tomorrow\") v = viewTomorrow();\n    else if (S.tab === \"products\") v = viewProducts();\n    else if (S.tab === \"batches\") v = viewBatches();\n    else v = viewData();\n    $(\"view\").innerHTML = v;\n  }\n  function refresh(force) {\n    if (!force && (S.busy || isTyping())) return Promise.resolve();\n    return API.state().then(function (st) {\n      S.st = st; S.offset = serverOffset(st.now); S.fetchedAt = Date.now();\n      var cur = PART_OF[phaseOf(st.times, nowMinutes().m)];\n      if (!S.part || !S.partManual) S.part = cur;\n      $(\"loading\").hidden = true; $(\"login\").hidden = true; $(\"app\").hidden = false;\n      render();\n    }).catch(function (e) {\n      if (e && e.login) return showLogin();\n      $(\"loading\").textContent = \"Не удалось загрузить кабинет: \" + (e && e.message ? e.message : e);\n    });\n  }\n  function isTyping() {\n    var a = document.activeElement;\n    return a && $(\"view\").contains(a) && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.type !== \"file\";\n  }\n  function post(path, body, okMsg) {\n    S.busy = true;\n    return API.post(path, body).then(function (r) {\n      S.busy = false; if (okMsg) toast(okMsg);\n      return refresh(true).then(function () { return r; });\n    }).catch(function (e) {\n      S.busy = false;\n      if (e && e.login) return showLogin();\n      toast(e && e.message ? e.message : \"Не получилось\");\n      throw e;\n    });\n  }\n  function showLogin() {\n    $(\"loading\").hidden = true; $(\"app\").hidden = true; $(\"login\").hidden = false;\n    $(\"top-actions\").innerHTML = \"\"; $(\"clock\").textContent = \"\";\n    var u = \"https://t.me/\" + BOT_USERNAME;\n    $(\"login-bot\").href = u; $(\"login-bot\").textContent = \"@\" + BOT_USERNAME;\n    setTimeout(function () { $(\"code\").focus(); }, 50);\n  }\n  document.addEventListener(\"click\", function (e) {\n    var t = e.target.closest(\"[data-act],[data-part],[data-tab]\");\n    if (!t) return;\n    if (t.hasAttribute(\"data-tab\")) {\n      S.tab = t.getAttribute(\"data-tab\"); S.edit = null; S.confirm = null;\n      if (S.dirty) { S.dirty = false; refresh(true); } else render();\n      return;\n    }\n    if (t.hasAttribute(\"data-part\")) {\n      S.part = t.getAttribute(\"data-part\"); S.partManual = true; S.tab = \"today\";\n      if (S.dirty) { S.dirty = false; refresh(true); } else render();\n      return;\n    }\n    var a = t.getAttribute(\"data-act\"), p = t.getAttribute(\"data-p\");\n    if (a === \"logout\") { API.post(\"/api/logout\").catch(function () {}).then(function () { S.st = null; showLogin(); }); }\n    else if (a === \"reset\") { Local.reset(); S.vitrina = {}; refresh(true).then(function () { toast(\"Пример восстановлен\"); }); }\n    else if (a === \"soldout\") post(\"/api/record\", { product: p, soldout: t.getAttribute(\"data-t\") }, \"Отмечено: \" + p);\n    else if (a === \"left\") {\n      var inp = document.querySelector('[data-left=\"' + CSS.escape(p) + '\"]');\n      if (!inp || inp.value === \"\") { toast(\"Впишите, сколько осталось\"); return; }\n      post(\"/api/record\", { product: p, left: inp.value }, \"Отмечено: \" + p);\n    }\n    else if (a === \"edit\") { S.edit = p; S.confirm = null; render(); }\n    else if (a === \"edit-cancel\") { S.edit = null; render(); }\n    else if (a === \"ask-remove\") { S.confirm = p; S.edit = null; render(); }\n    else if (a === \"ask-example\") { S.confirm = \"__example\"; render(); }\n    else if (a === \"confirm-cancel\") { S.confirm = null; render(); }\n    else if (a === \"remove\") { S.confirm = null; post(\"/api/product/remove\", { name: p }, \"Убрано из планов: \" + p); }\n    else if (a === \"restore\") post(\"/api/product/restore\", { name: p }, \"Вернули: \" + p);\n    else if (a === \"batch\") {\n      var s = t.getAttribute(\"data-s\");\n      post(\"/api/batch/close\", { id: +t.getAttribute(\"data-id\"), status: s }, s === \"used\" ? \"Отмечено: использовано\" : s === \"written_off\" ? \"Списание учтено\" : \"Запись удалена\");\n    }\n    else if (a === \"example\") { S.confirm = null; S.vitrina = {}; post(\"/api/example\", {}, \"Загружен пример пекарни\"); }\n  });\n  document.addEventListener(\"input\", function (e) {\n    var t = e.target;\n    if (t.hasAttribute(\"data-vitrina\")) {\n      var p = t.getAttribute(\"data-vitrina\"); S.vitrina[p] = t.value;\n      var it = S.st.today.items.filter(function (x) { return x.product === p; })[0], cell = document.querySelector('[data-adv=\"' + CSS.escape(p) + '\"]');\n      if (it && cell) {\n        if (t.value === \"\") cell.innerHTML = \"\";\n        else { var a = M.discountAdvice({ mean: it.mean }, +t.value); cell.innerHTML = a.pct ? '<span class=\"chip warn\">скидка ' + a.pct + \"%</span>\" : '<span class=\"chip ok\">без скидки</span>'; }\n      }\n    }\n    if (t.form && t.form.getAttribute(\"data-form\") === \"batch-add\" && (t.name === \"item\" || t.name === \"made\")) {\n      var f = t.form, prod = S.st.products.filter(function (x) { return x.name === f.item.value.trim(); })[0];\n      if (prod && prod.shelf_life && f.made.value) f.expires.value = M.diso(M.dnum(f.made.value) + prod.shelf_life);\n    }\n  });\n  document.addEventListener(\"change\", function (e) {\n    var t = e.target, a = t.getAttribute(\"data-act\");\n    if (a === \"event\") post(\"/api/event\", { factor: +t.value }, \"Прогноз на завтра обновлён\");\n    if (a === \"upload\" && t.files && t.files[0]) {\n      var file = t.files[0], rd = new FileReader();\n      rd.onload = function () {\n        var text;\n        try { text = new TextDecoder(\"utf-8\", { fatal: true }).decode(rd.result); } catch (x) { text = new TextDecoder(\"windows-1251\").decode(rd.result); }\n        post(\"/api/upload\", { csv: text }).then(function (r) { toast(\"Загружено строк: \" + r.rows); }).catch(function () {});\n      };\n      rd.readAsArrayBuffer(file); t.value = \"\";\n    }\n  });\n  document.addEventListener(\"submit\", function (e) {\n    var f = e.target, kind = f.getAttribute(\"data-form\");\n    if (f.id === \"login-form\") {\n      e.preventDefault();\n      var code = $(\"code\").value.replace(/\\D/g, \"\");\n      $(\"login-err\").hidden = true;\n      API.post(\"/api/login\", { code: code }).then(function () { $(\"code\").value = \"\"; return refresh(true); })\n        .catch(function (x) { $(\"login-err\").textContent = x && x.message ? x.message : \"Не получилось войти\"; $(\"login-err\").hidden = false; });\n      return;\n    }\n    if (!kind) return;\n    e.preventDefault();\n    var data = {};\n    Array.prototype.forEach.call(f.elements, function (el) { if (el.name) data[el.name] = el.value; });\n    if (kind === \"product-add\") post(\"/api/product\", data, \"Товар добавлен: \" + data.name).then(function () {}).catch(function () {});\n    else if (kind === \"product-edit\") { data.old_name = f.getAttribute(\"data-old\"); S.edit = null; post(\"/api/product\", data, \"Сохранено\").catch(function () {}); }\n    else if (kind === \"batch-add\") { if (!data.expires) delete data.expires; post(\"/api/batch\", data, \"Партия добавлена\").catch(function () {}); }\n    else if (kind === \"settings\") post(\"/api/settings\", data, \"Настройки сохранены\").catch(function () {});\n  });\n  document.addEventListener(\"visibilitychange\", function () { if (!document.hidden && S.st) refresh(); });\n\n  // часы и смена частей дня\n  var lastPhase = null;\n  setInterval(function () {\n    if (!S.st) return;\n    var ph = phaseOf(S.st.times, nowMinutes().m);\n    if (lastPhase && ph !== lastPhase) { S.partManual = false; refresh(true); }\n    lastPhase = ph;\n    renderTop(); renderDay();\n  }, 15000);\n  setInterval(function () { if (S.st) refresh(); }, 60000);\n  API.init().then(function () {\n    var m = location.search.match(/[?&]code=(\\d{6})/);\n    if (API.mode === \"server\" && m) {\n      history.replaceState(null, \"\", location.pathname);\n      return API.post(\"/api/login\", { code: m[1] }).catch(function () {}).then(function () { return refresh(true); });\n    }\n    return refresh(true);\n  });\n})();\n</script>\n\n</body>\n</html>\n"

# tools/make_demo.py
# Генератор примера «Пекарня»: Челябинск, смоделированные продажи.
#
# Чуть больше года продаж 8 позиций, до середины октября.
# "Испечено" - как пекарь печёт по привычке: одинаково по дням недели, с запасом,
# к сезону подстраивается с опозданием и на погоду не смотрит. Поэтому в дождь
# и в тёплые дни остаётся много, а в пиковые и холодные дни товар заканчивается.
# Температура похожа на челябинскую: зимой около −15°, летом около +19°.
# Горячая выпечка продаётся лучше в холод, сэндвичи и десерты - в тепло;
# летом часть покупателей на дачах, перед Новым годом спрос выше.
#
#     python tools/make_demo.py   -> bot/data/demo_sales.csv
DEMO_END, DEMO_DAYS = date(2026, 10, 17), 400
DEMO_PRODUCTS = [
    # название, цена, себестоимость, спрос в обычный день, профиль недели
    ("Круассан классический", 120, 40, 46, "weekend"),
    ("Багет", 90, 34, 30, "flat"),
    ("Хлеб пшеничный", 70, 32, 38, "flat"),
    ("Синнабон", 160, 55, 18, "weekend"),
    ("Пирожок с капустой", 60, 22, 34, "office"),
    ("Эклер", 110, 45, 22, "weekend"),
    ("Слойка с вишней", 95, 33, 26, "flat"),
    ("Сэндвич с курицей", 220, 110, 20, "office"),
]
DEMO_PROFILES = {
    "weekend": [0.85, 0.85, 0.9, 0.95, 1.1, 1.35, 1.25],
    "flat":    [0.95, 0.95, 1.0, 1.0, 1.08, 1.1, 1.0],
    "office":  [1.1, 1.1, 1.1, 1.1, 1.05, 0.7, 0.6],
}
# насколько меняется спрос на каждый градус холоднее +10° (доля): >0 - в холод берут больше
DEMO_TEMP_SENS = {"Круассан классический": 0.0, "Багет": 0.0, "Хлеб пшеничный": 0.004, "Синнабон": 0.01,
                  "Пирожок с капустой": 0.015, "Эклер": -0.006, "Слойка с вишней": -0.003,
                  "Сэндвич с курицей": -0.012}
# привычка пекаря: план недели и "на глаз" по каждой позиции
DEMO_GUT_WEEK = [1.0, 1.0, 1.0, 1.0, 1.1, 1.2, 1.2]
DEMO_GUT_BIAS = {"Круассан классический": 1.05, "Багет": 1.25, "Хлеб пшеничный": 1.3,
                 "Синнабон": 1.2, "Пирожок с капустой": 1.2, "Эклер": 1.0,
                 "Слойка с вишней": 1.1, "Сэндвич с курицей": 1.25}
# доля дневных продаж по часам с 7:00 до 21:00 (утренний и обеденный пик)
DEMO_HOURLY = [0.04, 0.10, 0.10, 0.07, 0.06, 0.09, 0.10, 0.07, 0.06, 0.06, 0.08, 0.08, 0.06, 0.03]


def demo_climate(d: date) -> float:
    """Обычная температура дня в Челябинске: минимум в конце января, максимум в июле."""
    return 2.8 - 16.7 * math.cos(2 * math.pi * (d.timetuple().tm_yday - 20) / 365.25)


def demo_calendar(d: date) -> float:
    """Поток покупателей по календарю: дачи летом, предновогодние дни, каникулы."""
    if (d.month, d.day) >= (12, 24):
        return 1.2
    if d.month == 1 and d.day <= 8:
        return 0.75
    if d.month in (7, 8) and (d.month, d.day) <= (8, 20):
        return 0.9
    if d.month == 9 and d.day <= 7:
        return 1.08
    return 1.0


def demo_soldout_time(fraction: float) -> str:
    acc = 0.0
    for i, share in enumerate(DEMO_HOURLY):
        if acc + share >= fraction:
            minutes = int(60 * (fraction - acc) / share)
            return f"{7 + i:02d}:{minutes // 10 * 10:02d}"
        acc += share
    return "20:50"


def demo_level(name: str, base: float, d: date, i: int, temp: float) -> float:
    """Средний спрос позиции в этот день без учёта дня недели и дождя."""
    trend = 1 + 0.0004 * i
    return base * trend * demo_calendar(d) * max(0.5, 1 + DEMO_TEMP_SENS[name] * (10 - temp))


def demo_csv_text() -> str:
    rnd = random.Random(42)
    start = DEMO_END - timedelta(days=DEMO_DAYS - 1)
    days = [start + timedelta(days=i) for i in range(DEMO_DAYS)]
    rain, temps, anomaly = {}, {}, 0.0
    for d in days:
        rain[d] = rnd.random() < (0.35 if d.month in (6, 7, 8, 10, 11) else 0.28)
        anomaly = 0.75 * anomaly + rnd.gauss(0, 3.2)   # погода держится несколько дней
        temps[d] = round(demo_climate(d) + anomaly - (1.5 if rain[d] and demo_climate(d) > 5 else 0), 1)
    rows = ["дата;товар;испечено;продано;закончилось;цена;себестоимость;дождь;температура"]
    for i, d in enumerate(days):
        for name, price, cost, base, prof in DEMO_PRODUCTS:
            lam = demo_level(name, base, d, i, temps[d]) * DEMO_PROFILES[prof][d.weekday()] * (0.84 if rain[d] else 1.0)
            demand = max(0, int(round(rnd.gauss(lam, math.sqrt(lam) * 1.25))))  # с лёгким перерассеянием
            # пекарь печёт по привычке: план недели с запасом, к сезону подстраивается
            # с опозданием на три недели, на погоду и дождь не смотрит
            lag = max(0, i - 21)
            habit = demo_level(name, base, days[lag], lag, demo_climate(days[lag]))
            baked = int(round(habit * DEMO_GUT_BIAS[name] * DEMO_GUT_WEEK[d.weekday()] * (1 + rnd.gauss(0, 0.08))))
            sold = min(demand, baked)
            so = demo_soldout_time(baked / demand) if demand > baked else ""
            rows.append(f"{d.isoformat()};{name};{baked};{sold};{so};{price};{cost};{int(rain[d])};{temps[d]}")
    return "\n".join(rows) + "\n"

# bot/bot.py
ROOT = Path(__file__).resolve().parent
log = logging.getLogger("bot")
esc = html.escape

MENU = [["План на завтра", "Итоги дня"], ["Скидка сейчас", "Сроки годности"], ["Добавить партию", "Личный кабинет"],
        ["Помощь"]]
BUTTON_TO_CMD = {"План на завтра": "/plan", "Итоги дня": "/itogi", "Скидка сейчас": "/skidka",
                 "Сроки годности": "/batches", "Добавить партию": "/batch", "Личный кабинет": "/web",
                 "Помощь": "/help"}
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
/web — войти в личный кабинет на сайте
/demo — загрузить пример «Пекарня»
/stop — выключить рассылку, /start — включить

<b>Свои продажи</b> — пришлите CSV-файл (выгрузку из кассы или Excel → «Сохранить как CSV») с колонками:
<code>дата;товар;продано</code> и по желанию <code>испечено;закончилось;цена;себестоимость;дождь;температура</code>"""


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


def deg(t: float) -> str:
    return f"{t:+.0f}°".replace("-", "−").replace("+0°", "0°").replace("−0°", "0°")


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
        self.lock = threading.RLock()   # бот и веб-кабинет работают с одной базой
        self.web_url: str | None = None

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

    def recommendations(self, chat, day: date) -> tuple[list[Recommendation], dict]:
        cid = chat["chat_id"]
        sales = self.db.sales(cid)
        products = self.db.products(cid)
        removed = self.db.inactive_products(cid)
        for s in sales:  # позиции из продаж, у которых нет карточки товара
            if s.product not in removed:
                products.setdefault(s.product, Product(s.product))
        now_ts = time.time()
        if not chat["is_demo"]:  # у старых продаж без погоды дописываем её из архива прогнозов
            filled = self.weather.fill(chat["lat"], chat["lon"], sales, now_ts)
            if filled:
                self.db.save_weather(cid, filled)
        w = self.weather.day(chat["lat"], chat["lon"], day, now_ts)
        event = self.db.event(cid, day)
        eff, learned = rain_effect(sales, chat["open_h"], chat["close_h"])
        recs = [forecast(sales, p, day, bool(w.rain), event, chat["open_h"], chat["close_h"], eff, w.temp)
                for p in products.values()]
        recs = [r for r in recs if r.history_days > 0 or r.qty > 0]
        recs.sort(key=lambda r: -r.qty)
        habit = {}
        for r in recs:
            same = [s.baked for s in sales if s.product == r.product and s.baked is not None
                    and s.day.weekday() == day.weekday() and 0 < (day - s.day).days <= 28]
            if same:
                habit[r.product] = sum(same) / len(same)
        return recs, {"rain": w.rain, "temp": w.temp, "event": event, "rain_eff": eff, "learned": learned,
                      "habit": habit, "products": products}

    def plan_text(self, chat, day: date, title: str) -> str | None:
        recs, info = self.recommendations(chat, day)
        if not recs:
            return None
        self.db.save_plan(chat["chat_id"], day, recs)
        head = [f"<b>{title} · {fmt_day(day)}</b>"]
        weather = []
        if info["rain"]:
            weather.append(f"Ожидается дождь — спрос ниже на {abs(info['rain_eff']) * 100:.0f}%")
        elif info["rain"] is False:
            weather.append("Без осадков")
        if info["temp"] is not None:
            weather.append(deg(info["temp"]))
        if weather:
            head.append(" · ".join(weather))
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
                {"command": "web", "description": "Личный кабинет на сайте"},
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
                    with self.lock:
                        self.handle(u)
                except Exception:
                    log.exception("update failed")
            try:
                with self.lock:
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

    def cmd_start(self, chat, arg: str = ""):
        cid = chat["chat_id"]
        self.db.set_chat(cid, muted=0)
        if arg == "pilot":
            self.state[cid] = {"name": "lead_business", "data": {}}
            return self.tg.send(cid, "Заявка на бесплатный месяц пилота.\n\n<b>1/4.</b> Как называется ваше заведение?",
                                remove_keyboard=True)
        if arg == "demo":  # кнопка "Попробовать в Telegram" на сайте
            self.tg.send(cid, "Привет! Я <b>Свежий расчёт</b>. Покажу, как работаю, на примере «Пекарня».",
                         reply_keyboard=MENU)
            return self.cmd_demo(self.db.chat(cid))
        self.tg.send(cid, "Привет! Я <b>Свежий расчёт</b>. Каждое утро присылаю план: сколько испечь и закупить, "
                          "чтобы меньше выбрасывать и реже оставаться с пустой витриной. "
                          "Вечером подскажу, когда ставить скидку, и напомню о продуктах с истекающим сроком.",
                     reply_keyboard=MENU)
        if not chat["city"]:
            self.state[cid] = {"name": "city"}
            return self.tg.send(cid, "В каком городе ваше заведение? Нужен для погоды и часового пояса. "
                                     "Нажмите «Челябинск» или напишите свой город.", reply_keyboard=[["Челябинск"]])
        self.after_setup(chat)

    def after_setup(self, chat):
        if self.db.products(chat["chat_id"]):
            return self.tg.send(chat["chat_id"], "Данные на месте. План на завтра: /plan")
        self.tg.send(chat["chat_id"],
                     "Чтобы я начал считать, пришлите CSV-файл с продажами (колонки: <code>дата;товар;продано</code>) "
                     "или посмотрите, как всё работает, на примере: /demo")

    def st_city(self, chat, text, st):
        cid = chat["chat_id"]
        g = geocode(text)
        if not g and "челяб" in text.lower():  # без интернета к сервису погоды - знаем Челябинск сами
            g = {"name": "Челябинск", "lat": 55.1644, "lon": 61.4368, "tz": "Asia/Yekaterinburg"}
        if g:
            _, offset = self.weather.daily(g["lat"], g["lon"], time.time())  # запасной вариант для Windows без tzdata
            self.db.set_chat(cid, city=g["name"], lat=g["lat"], lon=g["lon"], tz=g["tz"],
                             utc_offset=offset if offset is not None else chat["utc_offset"])
            self.tg.send(cid, f"Город: <b>{esc(g['name'])}</b>, часовой пояс {esc(g['tz'])}.", reply_keyboard=MENU)
        else:
            self.db.set_chat(cid, city=text[:60], tz="Asia/Yekaterinburg", utc_offset=18000)
            self.tg.send(cid, f"Записал город «{esc(text[:60])}». Не смог узнать часовой пояс, поставил время Челябинска (Москва +2). "
                              "Поменять: /tz +0 (разница с Москвой в часах).", reply_keyboard=MENU)
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
        self.tg.send(cid, "Пример: <code>/tz +2</code> (Челябинск = Москва +2) или <code>/tz Asia/Yekaterinburg</code>")

    def cmd_help(self, chat):
        self.tg.send(chat["chat_id"], HELP.format(morning=chat["morning"], evening=chat["evening"],
                                                  closing=chat["closing"]), reply_keyboard=MENU)

    def cmd_web(self, chat):
        cid = chat["chat_id"]
        if not self.web_url:
            return self.tg.send(cid, "Личный кабинет выключен. Включается настройкой WEB=on при запуске бота.")
        code = self.db.create_code(cid)
        text = (f"<b>Вход в личный кабинет</b>\nКод: <code>{code}</code> — действует 10 минут, один раз.\n"
                f"Адрес: {esc(self.web_url)}\n\nОткройте адрес в браузере и введите код. "
                "В кабинете — план на день, витрина, итоги, товары и сроки годности.")
        if os.environ.get("PUBLIC_URL"):
            try:
                return self.tg.send(cid, text, keyboard=[[{"text": "Открыть кабинет", "url": f"{self.web_url}/?code={code}"}]])
            except TelegramError:
                pass
        self.tg.send(cid, text)

    def cmd_kabinet(self, chat):
        self.cmd_web(chat)

    def cmd_stop(self, chat):
        self.db.set_chat(chat["chat_id"], muted=1)
        self.tg.send(chat["chat_id"], "Рассылка выключена. Включить снова: /start")

    def load_example(self, chat):
        """Пример «Пекарня»: год продаж и несколько партий со сроками годности."""
        cid = chat["chat_id"]
        demo_file = ROOT / "data" / "demo_sales.csv"
        text = demo_file.read_text(encoding="utf-8") if demo_file.exists() else demo_csv_text()
        sales, products = parse_csv(text)
        shift = (self.today(chat) - timedelta(days=1)) - max(s.day for s in sales)
        for s in sales:
            s.day += shift
        for p in products.values():  # срок хранения: выпечка - день, эклеры и сэндвичи - два
            p.shelf_life = 2 if p.name in ("Эклер", "Сэндвич с курицей") else 1
        self.db.clear_sales(cid)
        self.db.import_sales(cid, sales, products)
        self.db.set_chat(cid, is_demo=1)
        today = self.today(chat)
        for item, qty, days in [("Сливки 33%", "2 л", 1), ("Творог 5%", "3 кг", 2),
                                ("Филе куриное", "1,5 кг", 0), ("Вишня замороженная", "2 кг", 20)]:
            self.db.add_batch(cid, item, qty, today + timedelta(days=days))
        self.plan_text(self.db.chat(cid), today, "План")  # сохраняем план на сегодня для вечерних советов
        return sales, products

    def cmd_demo(self, chat, arg: str = ""):
        cid = chat["chat_id"]
        if self.db.sales(cid) and not chat["is_demo"] and arg != "да":
            return self.tg.send(cid, "У вас уже есть свои данные. Заменить их примером?",
                                keyboard=[[{"text": "Да, заменить", "callback_data": "demo:yes"},
                                           {"text": "Отмена", "callback_data": "x"}]])
        sales, products = self.load_example(chat)
        bt = backtest(sales, products)
        lines = [f"Загрузил пример <b>«Пекарня»</b> (Челябинск): {len(products)} позиций, продажи за год с погодой. "
                 "Продажи смоделированы — это пример, чтобы посмотреть, как работает бот."]
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
        w = self.weather.day(chat["lat"], chat["lon"], day, time.time())
        self.db.upsert_sale(cid, Sale(day, product, sold, baked, soldout, w.rain, w.temp))
        self.db.db.commit()

    def st_left(self, chat, text, st):
        cid = chat["chat_id"]
        if not re.fullmatch(r"\d{1,4}", text):
            return self.tg.send(cid, "Напишите число, например 4")
        self.record(chat, st["day"], st["product"], int(text), None)
        self.state.pop(cid, None)
        self.tg.send(cid, self.itogi_text(cid, st["day"]), keyboard=self.itogi_kb(cid, st["day"]))

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
        self.weather.fill(chat["lat"], chat["lon"], sales, time.time(), archive=True)
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
                          "Пока можно посмотреть, как работает бот, на примере: /demo", reply_keyboard=MENU)
        d = st["data"]
        who = f"@{chat['username']}" if chat["username"] else f"id {cid}"
        for admin in self.admins:
            try:
                self.tg.send(admin, f"<b>Новая заявка #{lead_id}</b>\n{esc(d['business'])}, {esc(d['city'])}, "
                                    f"{esc(d.get('kind', ''))}\nКонтакт: {esc(d['contact'] or who)} ({who})")
            except TelegramError:
                pass

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
            time.sleep(0.05)  # лимит Telegram - до ~30 сообщений в секунду
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
    if os.environ.get("WEB", "on") != "off":
        port = int(os.environ.get("WEB_PORT") or 8080)
        if start_web(bot, os.environ.get("WEB_HOST") or "127.0.0.1", port, ROOT):
            bot.web_url = (os.environ.get("PUBLIC_URL") or f"http://localhost:{port}").rstrip("/")
    bot.run()


if __name__ == "__main__":
    main()
