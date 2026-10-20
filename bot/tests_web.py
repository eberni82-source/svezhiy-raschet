"""Проверка личного кабинета через HTTP, без интернета: python tests_web.py"""
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from bot import FreshBot  # noqa: E402
from core.web import start_web  # noqa: E402

db = tempfile.mktemp(suffix=".db")
bot = FreshBot("123:TEST", set(), db, "http://127.0.0.1:9", weather_on=False)
srv = start_web(bot, "127.0.0.1", 0, ROOT)
base = f"http://127.0.0.1:{srv.server_address[1]}"
CID = 555
cookie = {"v": ""}


def req(method, path, body=None, expect=200):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(base + path, data=data, method=method,
                               headers={"Content-Type": "application/json", "Cookie": cookie["v"]})
    try:
        with urllib.request.urlopen(r) as resp:
            code, raw, headers = resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        code, raw, headers = e.code, e.read(), e.headers
    sc = headers.get("Set-Cookie")
    if sc:
        cookie["v"] = sc.split(";")[0]
    ok = code == expect
    print(("OK   " if ok else "FAIL ") + f"{method} {path} → {code}")
    if not ok:
        print(raw[:300])
        raise SystemExit(1)
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode()


def check(cond, what):
    print(("OK   " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


page = req("GET", "/")
check("<html" in page.lower() or "<!doctype" in page.lower(), "страница кабинета отдаётся")
req("GET", "/api/ping")
req("GET", "/api/state", expect=401)
bot.db.upsert_chat(CID, "baker", "Пекарь")
req("POST", "/api/login", {"code": "000000"}, expect=403)
code = bot.db.create_code(CID)
req("POST", "/api/login", {"code": code})
req("POST", "/api/login", {"code": code}, expect=403)  # одноразовый
s = req("GET", "/api/state")
check(s["products"] == [] and s["today"]["items"] == [], "пустой кабинет без данных")

req("POST", "/api/example", {})
s = req("GET", "/api/state")
check(len(s["products"]) == 8 and len(s["today"]["items"]) == 8, "пример загружен: 8 товаров, план на сегодня")
check(s["phase"] in ("night", "morning", "evening", "closing"), f"фаза дня: {s['phase']}")
check(s["summary"] and s["summary"]["waste_new"] < s["summary"]["waste_now"], "сводка: списания по плану ниже")
check(all(p["shelf_life"] in (1, 2) for p in s["products"]), "у товаров примера есть срок хранения")

req("POST", "/api/product", {"name": "Салат «Оливье»", "price": 180, "cost": 70, "shelf_life": 2, "default_qty": 15})
s = req("GET", "/api/state")
olivie = [i for i in s["today"]["items"] if i["product"] == "Салат «Оливье»"]
check(olivie and olivie[0]["qty"] == 15 and olivie[0]["new"], "новый товар без истории попал в план с обычным объёмом")
req("POST", "/api/product", {"name": "Салат", "price": 50, "cost": 90}, expect=400)

req("POST", "/api/product/remove", {"name": "Багет"})
s = req("GET", "/api/state")
check(all(i["product"] != "Багет" for i in s["today"]["items"] + s["tomorrow"]["items"]) and "Багет" in s["removed"],
      "убранный товар исчез из планов и лежит в «Убранных»")
req("POST", "/api/product/restore", {"name": "Багет"})
s = req("GET", "/api/state")
check(any(i["product"] == "Багет" for i in s["tomorrow"]["items"]), "товар вернулся")

req("POST", "/api/product", {"name": "Хлеб пшеничный (нарезка)", "old_name": "Хлеб пшеничный", "price": 75, "cost": 32, "shelf_life": 2})
s = req("GET", "/api/state")
renamed = [p for p in s["products"] if p["name"] == "Хлеб пшеничный (нарезка)"]
check(renamed and renamed[0]["history_days"] == 56, "переименование сохранило историю продаж")

req("POST", "/api/batch", {"item": "Салат «Оливье»", "qty": "20 шт", "made": s["today"]["date"]})
s = req("GET", "/api/state")
b = [x for x in s["batches"] if x["item"] == "Салат «Оливье»"]
check(b and b[0]["left"] == 2, "партия: срок посчитан по сроку хранения товара (+2 дня)")
req("POST", "/api/batch", {"item": "Мука", "qty": "10 кг"}, expect=400)
req("POST", "/api/batch", {"item": "Мука", "qty": "10 кг", "expires": "2027-01-01"})
req("POST", "/api/batch/close", {"id": b[0]["id"], "status": "written_off"})
s = req("GET", "/api/state")
check(all(x["item"] != "Салат «Оливье»" for x in s["batches"]), "партия списана")

first = s["today"]["items"][0]["product"]
req("POST", "/api/record", {"product": first, "soldout": "15:30"})
second = s["today"]["items"][1]["product"]
req("POST", "/api/record", {"product": second, "left": 4})
req("POST", "/api/record", {"product": second}, expect=400)
s = req("GET", "/api/state")
rec = {i["product"]: i["record"] for i in s["today"]["items"]}
check(rec[first]["soldout"] == "15:30" and rec[second]["left"] == 4, "итоги дня записаны из кабинета")

req("POST", "/api/event", {"factor": 1.2})
s = req("GET", "/api/state")
check(abs(s["tomorrow"]["event"] - 1.2) < 1e-9, "событие на завтра")
req("POST", "/api/settings", {"business": "Булочная на Ленина", "morning": "05:30", "evening": "16:00", "closing": "20:00"})
s = req("GET", "/api/state")
check(s["business"]["name"] == "Булочная на Ленина" and s["times"]["morning"] == "05:30", "настройки сохранены")
req("POST", "/api/settings", {"morning": "25:00"}, expect=400)

csv = "дата;товар;продано;заказано\n" + "\n".join(f"2026-10-{d:02d};Кефир 1%;{12 + d % 5};18" for d in range(1, 20))
r = req("POST", "/api/upload", {"csv": csv})
s = req("GET", "/api/state")
check(r["rows"] == 19 and [p["name"] for p in s["products"]] == ["Кефир 1%"] and not s["business"]["is_example"],
      "свой CSV заменил пример")

# бот видит то же, что кабинет
bot.db.set_chat(CID, is_demo=0)
recs, _ = bot.recommendations(bot.db.chat(CID), bot.today(bot.db.chat(CID)))
check([r.product for r in recs] == ["Кефир 1%"], "бот и кабинет работают с одной базой")

req("POST", "/api/logout", {})
req("GET", "/api/state", expect=401)
req("POST", "/api/product", {"name": "X"}, expect=401)
print("\nКАБИНЕТ: ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
srv.shutdown()
