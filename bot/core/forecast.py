"""
Модель «Свежий расчёт»: прогноз спроса + оптимальный объём выпечки.

Логика (одинаковая в боте и на сайте):
1. Спрос дня. Если товар закончился раньше закрытия, реальный спрос был больше
   продаж: добавляем поправку sold * (1 + 0.5 * доля_дня_после_распродажи).
2. Влияние дождя считается по истории всех товаров точки (если есть отметки
   погоды); иначе берётся -10%.
3. Базовый прогноз = уровень (средний спрос за 14 дней) × профиль недели
   (во сколько раз этот день недели выше среднего за 8 недель). Всё очищено
   от влияния дождя.
4. Объём выпечки по модели «газетчика»: печём столько, чтобы вероятность
   продать всё была равна доле (цена - себестоимость) / цена.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import NormalDist

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
    shelf_life: int | None = None      # срок хранения, дней
    default_qty: float | None = None   # обычный объём в день — для нового товара без истории


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
        cr = critical_ratio(product.price, product.cost)
        if product.default_qty:
            dq = float(product.default_qty)
            return Recommendation(product.name, dq, math.sqrt(dq), max(0, int(math.floor(dq + 0.5))), cr,
                                  ["новый товар: истории пока нет, берём ваш обычный объём"], 0)
        return Recommendation(product.name, 0, 0, 0, cr, ["нет истории продаж"], 0)

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
