"""
Модель "Свежий расчёт": прогноз спроса + оптимальный объём выпечки.

Логика (одинаковая в боте и на сайте):
1. Спрос дня. Если товар закончился раньше закрытия, реальный спрос был больше
   продаж: добавляем поправку sold * (1 + 0.5 * доля_дня_после_распродажи).
2. Влияние дождя считается по истории всех товаров точки (если есть отметки
   погоды); иначе берётся -10%.
3. Базовый прогноз = уровень * профиль недели (во сколько раз этот день
   недели выше среднего за 8 недель). Уровень - взвешенное среднее за 14 дней,
   каждый день приведён к среднему дню недели; вчера весит 1, позавчера 0.9
   и так далее, поэтому свежие продажи влияют сильнее. Всё очищено от дождя.
4. Сезон. Уровень по двум неделям сам идёт за сезоном. Сверху две поправки:
   температура - по истории модель узнаёт, как холод и жара меняют спрос на
   каждую позицию (нужно от двух месяцев данных с температурой);
   прошлый год - как менялся спрос всей точки в эти же дни год назад
   (праздники, дачный сезон, каникулы). Обе включаются, только если эффект
   заметно больше случайного разброса.
5. Объём выпечки по модели "газетчика": печём столько, чтобы вероятность
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
