"""Сырьё: техкарты, остатки по партиям и заказ к поставке. На сайте - stockReport в model.js."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, timedelta

DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAY_WORDS = ["понед", "вторн", "сред", "четв", "пятн", "субб", "воскр"]
UNITS = {"кг": ("г", 1000), "л": ("мл", 1000), "шт": ("шт", 1)}
UNIT_WORDS = {"кг": "кг", "kg": "кг", "килограмм": "кг", "г": "г", "гр": "г", "грамм": "г", "g": "г",
              "л": "л", "l": "л", "литр": "л", "литра": "л", "литров": "л", "мл": "мл", "ml": "мл",
              "шт": "шт", "штук": "шт", "штуки": "шт", "штука": "шт", "pcs": "шт", "уп": "шт"}
RESERVE = 0.10   # запас к заказу
HORIZON = 21     # дальше трёх недель вперёд не считаем
EPS = 1e-9


@dataclass
class Material:
    name: str
    unit: str = "кг"
    delivery: str = ""                # "пн, чт", "ежедневно"; пусто - каждый день
    shelf_life: int | None = None     # срок годности новой поставки, дней
    cost: float | None = None         # ₽ за единицу (кг, л, шт)


def mat_key(name: str) -> str:
    return " ".join(str(name or "").lower().split())


def rnd(x: float, digits: int = 0) -> float:
    """Округление как Math.round в JS: половинки - вверх."""
    p = 10 ** digits
    return math.floor(x * p + 0.5) / p


def norm_unit(text: str | None) -> str:
    u = UNIT_WORDS.get(str(text or "").strip().lower().rstrip("."))
    if u not in UNITS:
        raise ValueError("единица сырья: кг, л или шт")
    return u


def small_unit(unit: str) -> str:
    return UNITS.get(unit, ("шт", 1))[0]


def factor(unit: str) -> int:
    return UNITS.get(unit, ("шт", 1))[1]


def parse_delivery(text: str | None) -> list[int]:
    """Дни поставок: "пн, чт" -> [0, 3]; "ежедневно" или пусто -> все дни недели."""
    s = str(text or "").lower().replace("ё", "е")
    if not s.strip() or "ежеднев" in s or "каждый день" in s or "все дни" in s:
        return list(range(7))
    found = set(range(5)) if "будн" in s else set()
    for i, (short, word) in enumerate(zip(DAYS, DAY_WORDS)):
        if word in s or re.search(r"(^|[^а-я])" + short + r"([^а-я]|$)", s):
            found.add(i)
    if not found:
        raise ValueError("дни поставок: например «пн, чт» или «ежедневно»")
    return sorted(found)


def delivery_text(days: list[int]) -> str:
    return "ежедневно" if len(days) == 7 else ", ".join(DAYS[i] for i in days)


def next_delivery(days: list[int], after: date) -> date:
    d = after + timedelta(days=1)
    while d.weekday() not in days:
        d += timedelta(days=1)
    return d


def parse_amount(text: str | None, unit: str | None = None) -> tuple[float | None, str | None]:
    """Количество: "1,5 кг" -> (1.5, "кг"), "300 мл" -> (0.3, "л"). Без числа -> (None, None)."""
    s = str(text or "").strip().lower().replace(",", ".")
    m = re.match(r"^(\d+(?:\.\d+)?)\s*([a-zа-я.]*)", s)
    if not m:
        return None, None
    x, word = float(m.group(1)), UNIT_WORDS.get(m.group(2).rstrip("."))
    if word == "г":
        return x / 1000, "кг"
    if word == "мл":
        return x / 1000, "л"
    return x, word or unit


def fmt_amount(x: float, unit: str) -> str:
    if unit not in UNITS:
        return f"{rnd(x, 1):g}".replace(".", ",") + (f" {unit}" if unit else "")
    if unit == "шт":
        return f"{rnd(x):.0f} шт"
    if 0 < x < 1:
        return f"{rnd(x * 1000):.0f} {small_unit(unit)}"
    return f"{rnd(x, 1):g}".replace(".", ",") + f" {unit}"


def round_up(x: float, unit: str) -> float:
    if x <= EPS:
        return 0.0
    if unit == "шт":
        return float(math.ceil(x - EPS))
    return math.ceil(x * 10 - EPS) / 10


def per_piece(materials: dict[str, Material], recipes) -> dict[str, list[tuple[str, float]]]:
    """Товар -> [(сырьё, расход на штуку в единицах сырья)]."""
    mats = {mat_key(m.name): m for m in materials.values()}
    out: dict[str, list[tuple[str, float]]] = {}
    for product, material, norm in recipes:
        mk = mat_key(material)
        if mk in mats and norm and norm > 0:
            out.setdefault(product, []).append((mk, norm / factor(mats[mk].unit)))
    return out


def needs(per: dict, day_plan: dict[str, float] | None) -> dict[str, float]:
    out: dict[str, float] = {}
    for product, q in (day_plan or {}).items():
        for mk, a in per.get(product, []):
            out[mk] = out.get(mk, 0.0) + q * a
    return out


def _take(batches: list[dict], day: date, amount: float) -> float:
    """Списать amount с партий, которые на этот день уже пришли и ещё годны. Вернёт нехватку."""
    for b in batches:
        if amount <= EPS:
            break
        if b["left"] <= EPS or b["arrived"] > day or b["expires"] < day:
            continue
        t = min(b["left"], amount)
        b["left"] -= t
        amount -= t
    return amount if amount > EPS else 0.0


def stock_report(materials: dict[str, Material], recipes, batches: list[dict],
                 made: dict[date, dict[str, float]], plan: dict[date, dict[str, float]], today: date) -> dict:
    """
    batches - активные партии: {"id", "item", "amount", "expires": date, "arrived": date}
    made    - что произвели в прошлые дни: {день: {товар: штук}}
    plan    - план на сегодня и прогноз дальше: {день: {товар: штук}}
    """
    mats = {mat_key(m.name): m for m in materials.values()}
    per = per_piece(materials, recipes)
    pool: dict[str, list[dict]] = {}
    for b in batches:
        if b.get("amount") is None or b["amount"] <= 0:
            continue
        pool.setdefault(mat_key(b["item"]), []).append(
            {"id": b["id"], "left": float(b["amount"]), "expires": b["expires"], "arrived": b["arrived"]})
    for lst in pool.values():
        lst.sort(key=lambda b: (b["expires"], b["id"]))

    # прошлое: остаток уменьшается на то, что уже произвели
    arrived = [b["arrived"] for lst in pool.values() for b in lst]
    d = min(arrived) if arrived else today
    while d < today:
        for mk, a in needs(per, made.get(d)).items():
            if mk in pool:
                _take(pool[mk], d, a)
        d += timedelta(days=1)
    left_now = {b["id"]: b["left"] for lst in pool.values() for b in lst}

    uses: dict[str, list[tuple[str, float]]] = {}
    for product, lst in per.items():
        for mk, a in lst:
            uses.setdefault(mk, []).append((product, a))

    rows = []
    for mk in sorted(mats):
        m = mats[mk]
        days = parse_delivery(m.delivery)
        first = next_delivery(days, today)
        cover = (next_delivery(days, first) - first).days
        if m.shelf_life:
            cover = min(cover, max(1, int(m.shelf_life)))
        end = min(first + timedelta(days=cover), today + timedelta(days=HORIZON))
        bs = [dict(b) for b in pool.get(mk, [])]
        stock = sum(b["left"] for b in bs if b["expires"] >= today and b["arrived"] <= today)
        need_today = needs(per, plan.get(today)).get(mk, 0.0)
        short_today = short_before = unmet_cover = need_cover = 0.0
        short_first = None
        d = today
        while d < end:
            n = needs(per, plan.get(d)).get(mk, 0.0)
            if d >= first:
                need_cover += n
            u = _take(bs, d, n)
            if u > 0:
                if d == today:
                    short_today = u
                if d < first:
                    short_before += u
                    short_first = short_first or d
                else:
                    unmet_cover += u
            d += timedelta(days=1)
        waste = [{"id": b["id"], "left": b["left"], "expires": b["expires"]}
                 for b in bs if b["left"] > EPS and b["expires"] < end]
        later = sum(b["left"] for b in bs if b["expires"] >= end)
        order = round_up(unmet_cover + RESERVE * need_cover - later, m.unit)
        rows.append({
            "name": m.name, "unit": m.unit, "cost": m.cost, "delivery": delivery_text(days),
            "stock": stock, "need_today": need_today, "short_today": short_today,
            "short_first": short_first, "short_before": short_before,
            "next": first, "cover": cover, "order": order, "need_cover": need_cover,
            "waste": waste, "waste_total": sum(w["left"] for w in waste),
            "uses": sorted(uses.get(mk, [])),
        })
    return {"rows": rows, "left": left_now}


def waste_hint(row: dict, plan_today: dict[str, float] | None = None) -> str:
    """Во что пустить остаток, который иначе спишется: "можно сделать больше: Эклер +20"."""
    total = row["waste_total"]
    extra = []
    for product, a in row["uses"]:
        n = int(total / a + EPS) if a > 0 else 0
        if n > 0:
            extra.append((n, product))
    if not extra:
        return ""
    extra.sort(key=lambda x: (-((plan_today or {}).get(x[1], 0)), x[1]))
    return "можно сделать больше: " + " или ".join(f"{p} +{n}" for n, p in extra[:2])


# пример "Пекарня": сырьё, техкарты и партии. Цифры расхода ориентировочные.
DEMO_MATERIALS = [
    # название, ед., поставки, срок годности поставки, ₽ за ед.
    ("Мука пшеничная", "кг", "пн, чт", 180, 45),
    ("Масло сливочное 82%", "кг", "пн, чт", 30, 780),
    ("Сахар", "кг", "пн", 365, 75),
    ("Дрожжи прессованные", "кг", "пн, чт", 12, 220),
    ("Капуста белокочанная", "кг", "пн, ср, пт", 10, 35),
    ("Вишня замороженная", "кг", "пн", 90, 450),
    ("Яйца", "шт", "пн, чт", 21, 12),
    ("Сливки 33%", "л", "пн, ср, пт", 7, 420),
    ("Филе куриное", "кг", "ежедневно", 3, 380),
    ("Творог 5%", "кг", "пн, ср, пт", 5, 320),
]
DEMO_RECIPES = [
    # товар, сырьё, расход на 1 шт (г, мл или шт)
    ("Круассан классический", "Мука пшеничная", 60), ("Круассан классический", "Масло сливочное 82%", 35),
    ("Круассан классический", "Сахар", 8), ("Круассан классический", "Дрожжи прессованные", 3),
    ("Багет", "Мука пшеничная", 250), ("Багет", "Дрожжи прессованные", 3),
    ("Хлеб пшеничный", "Мука пшеничная", 350), ("Хлеб пшеничный", "Дрожжи прессованные", 5), ("Хлеб пшеничный", "Сахар", 5),
    ("Синнабон", "Мука пшеничная", 70), ("Синнабон", "Масло сливочное 82%", 20), ("Синнабон", "Сахар", 20),
    ("Синнабон", "Дрожжи прессованные", 3), ("Синнабон", "Сливки 33%", 15),
    ("Пирожок с капустой", "Мука пшеничная", 50), ("Пирожок с капустой", "Капуста белокочанная", 60),
    ("Пирожок с капустой", "Масло сливочное 82%", 5), ("Пирожок с капустой", "Дрожжи прессованные", 2),
    ("Эклер", "Мука пшеничная", 15), ("Эклер", "Масло сливочное 82%", 12), ("Эклер", "Яйца", 0.5), ("Эклер", "Сливки 33%", 30),
    ("Слойка с вишней", "Мука пшеничная", 45), ("Слойка с вишней", "Масло сливочное 82%", 30),
    ("Слойка с вишней", "Вишня замороженная", 30), ("Слойка с вишней", "Сахар", 10),
    ("Сэндвич с курицей", "Филе куриное", 60),
]
DEMO_BATCHES = [
    # сырьё, количество, годен ещё дней (партии пришли сегодня)
    ("Мука пшеничная", 200, 120), ("Масло сливочное 82%", 15, 20), ("Сахар", 25, 300),
    ("Дрожжи прессованные", 2, 10), ("Капуста белокочанная", 8, 6), ("Вишня замороженная", 2, 20),
    ("Яйца", 60, 14), ("Сливки 33%", 3, 1), ("Филе куриное", 1.5, 0), ("Творог 5%", 3, 2),
]
