"""
«Свежий расчёт» — Telegram-бот: план выпечки, вечерние скидки, сроки годности.

Запуск:  python bot.py   (нужен Python 3.10+, никаких pip install)
Настройки в файле .env рядом (см. .env.example).
"""
from __future__ import annotations

import html
import logging
import math
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if sys.version_info < (3, 9):
    raise SystemExit(f"Нужен Python 3.9 или новее, сейчас {sys.version.split()[0]}. Скачайте свежий с python.org")

# Папка core лежит рядом с bot.py. Добавляем её в путь поиска, чтобы бот запускался
# откуда угодно: из IDE, из другой папки, двойным кликом.
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from core.csv_import import ImportErrorRu, parse_csv
    from core.forecast import (WEEKDAYS_SHORT, Product, Sale, backtest, discount_advice, evening_threshold,
                               expiry_status, expiry_tip, forecast, rain_effect, Recommendation)
    from core.storage import Storage
    from core.tg import Telegram, TelegramError
    from core.weather import WeatherCache, geocode
except ModuleNotFoundError as e:
    if (e.name or "").startswith("core"):
        raise SystemExit(f"Не нашёл папку core рядом с bot.py ({Path(__file__).resolve().parent}).\n"
                         "Скачайте папку bot целиком (bot.py, core/, data/) и запускайте bot.py из неё.")
    raise

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# ----------------------------------------------------------------------
# НАСТРОЙКИ. Можно вписать прямо сюда или в файл .env рядом с bot.py.
# Если вписали токен сюда — не выкладывайте файл в открытый доступ.
# ----------------------------------------------------------------------
BOT_TOKEN = ""   # токен от @BotFather, например "1234567890:AAH..."
ADMIN_IDS = ""   # ваш Telegram ID от @userinfobot, несколько — через запятую

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
        text = (ROOT / "data" / "demo_sales.csv").read_text(encoding="utf-8")
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
