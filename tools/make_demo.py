"""Генератор примера: условная пекарня в Челябинске (смоделированные продажи).

Чуть больше года продаж 8 позиций, до середины октября.
«Испечено» — как пекарь печёт по привычке: одинаково по дням недели, с запасом,
к сезону подстраивается с опозданием и на погоду не смотрит. Поэтому в дождь
и в тёплые дни остаётся много, а в пиковые и холодные дни товар заканчивается.
Температура похожа на челябинскую: зимой около −15°, летом около +19°.
Горячая выпечка продаётся лучше в холод, сэндвичи и десерты — в тепло;
летом часть покупателей на дачах, перед Новым годом спрос выше.

    python tools/make_demo.py   → bot/data/demo_sales.csv
"""
import math
import random
from datetime import date, timedelta
from pathlib import Path

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
# насколько меняется спрос на каждый градус холоднее +10° (доля): >0 — в холод берут больше
DEMO_TEMP_SENS = {"Круассан классический": 0.0, "Багет": 0.0, "Хлеб пшеничный": 0.004, "Синнабон": 0.01,
                  "Пирожок с капустой": 0.015, "Эклер": -0.006, "Слойка с вишней": -0.003,
                  "Сэндвич с курицей": -0.012}
# привычка пекаря: план недели и «на глаз» по каждой позиции
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


def main():
    out = Path(__file__).resolve().parents[1] / "bot" / "data" / "demo_sales.csv"
    text = demo_csv_text()
    out.write_text(text, encoding="utf-8")
    print(f"строк: {text.count(chr(10)) - 1} → {out}")


if __name__ == "__main__":
    main()
