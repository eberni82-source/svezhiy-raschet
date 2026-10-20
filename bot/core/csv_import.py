"""Разбор CSV/выгрузки из кассы: русские или английские заголовки, разделитель ; или ,"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime

from .forecast import Product, Sale

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
