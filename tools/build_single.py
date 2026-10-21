"""Собирает бота в один файл bot/svezhiy_bot.py — для тех, кто копирует код вручную."""
import ast
import json
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
    title = f"# {'=' * 70}\n# {path.relative_to(ROOT).as_posix()}\n# {'=' * 70}\n"
    return title + head + body + "\n"


def demo_body() -> str:
    """Генератор примера из tools/make_demo.py: однофайловый бот создаёт пример сам."""
    body = module_body(ROOT / "tools" / "make_demo.py")
    return body[:body.index("\ndef main(")].rstrip() + "\n"


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
for name in ("forecast.py", "csv_import.py", "storage.py", "tg.py", "weather.py", "web.py"):
    parts.append(module_body(BOT / "core" / name))
parts.append("# Страница личного кабинета (встроена, чтобы бот работал одним файлом)\nKABINET_HTML = " +
             json.dumps((BOT / "web" / "kabinet.html").read_text(encoding="utf-8"), ensure_ascii=False))
parts.append(demo_body())
parts.append(bot_body())
out = "\n\n".join(p.strip("\n") for p in parts) + "\n"
# модули склеены в одно пространство имён: функции и классы не должны перекрываться
defs = [n.name for n in ast.parse(out).body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
dup = sorted({n for n in defs if defs.count(n) > 1})
assert not dup, f"имена определены дважды: {dup}"
(BOT / "svezhiy_bot.py").write_text(out, encoding="utf-8")
print("ok", len(out.splitlines()), "строк")
