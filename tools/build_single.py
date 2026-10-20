"""Собирает бота в один файл bot/svezhiy_bot.py — для тех, кто копирует код вручную."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOT = ROOT / "bot"


def module_body(path: Path) -> str:
    src = path.read_text(encoding="utf-8")
    m = re.match(r'\s*"""(.*?)"""\n', src, re.S)
    head = ""
    if m:
        head = "\n".join("# " + line if line.strip() else "#" for line in m.group(1).strip().splitlines()) + "\n"
        src = src[m.end():]
    lines = [l for l in src.splitlines() if not re.match(r"^(from |import )", l)]
    body = "\n".join(lines).strip("\n")
    title = f"# {'=' * 70}\n# {path.relative_to(BOT)}\n# {'=' * 70}\n"
    return title + head + body + "\n"


DEMO = '''
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
    return "\\n".join(rows) + "\\n"
'''

HEADER = '''"""
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
'''


def bot_body() -> str:
    src = (BOT / "bot.py").read_text(encoding="utf-8")
    start = src.index("ROOT = Path(__file__).resolve().parent")
    assert src.index('BOT_TOKEN = ""') < start
    body = src[start:]
    old_demo = 'text = (ROOT / "data" / "demo_sales.csv").read_text(encoding="utf-8")'
    assert old_demo in body
    body = body.replace(old_demo, 'demo_file = ROOT / "data" / "demo_sales.csv"\n'
                        '        text = demo_file.read_text(encoding="utf-8") if demo_file.exists() else demo_csv_text()')
    return f"# {'=' * 70}\n# Бот\n# {'=' * 70}\n" + body


parts = [HEADER]
for name in ("forecast.py", "csv_import.py", "storage.py", "tg.py", "weather.py"):
    parts.append(module_body(BOT / "core" / name))
parts.append(DEMO)
parts.append(bot_body())
out = "\n\n".join(p.strip("\n") for p in parts) + "\n"
(BOT / "svezhiy_bot.py").write_text(out, encoding="utf-8")
print("ok", len(out.splitlines()), "строк")
