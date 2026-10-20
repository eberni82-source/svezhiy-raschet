"""Личный кабинет заведения: веб-сервер на стандартной библиотеке.

Работает в том же процессе, что и бот, и читает ту же базу: всё, что отмечено
в Telegram, сразу видно в кабинете, и наоборот. Вход — по коду из бота (/web).
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from datetime import date, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .csv_import import ImportErrorRu, parse_csv
from .forecast import Product, Recommendation, backtest, evening_threshold, expiry_status, expiry_tip

log = logging.getLogger("web")
KABINET_HTML: str | None = None   # однофайловая версия бота подставляет сюда страницу кабинета
COOKIE = "sr_session"
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _minutes(hhmm: str) -> int:
    h, m = map(int, (hhmm or "00:00").split(":"))
    return h * 60 + m


def phase_of(chat, now) -> str:
    """Где мы в рабочем дне: ночь → утро (план) → вечер (витрина) → итоги."""
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

    # ---------- состояние ----------
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

        batches = []
        for x in db.batches(cid):
            exp = date.fromisoformat(x["expires"])
            st, left = expiry_status(exp, today)
            level = "bad" if left <= 0 else "warn" if left <= 2 else "ok"
            batches.append({"id": x["id"], "item": x["item"], "qty": x["qty"], "expires": x["expires"],
                            "label": st, "left": left, "level": level,
                            "tip": ("спишите" if left < 0 else expiry_tip(x["item"])) if left <= 1 else None})

        sales = db.sales(cid)
        days = sorted({s.day for s in sales})
        return {
            "mode": "server",
            "business": {"name": chat["business"] or "", "city": chat["city"] or "", "tz": chat["tz"] or "",
                         "is_example": bool(chat["is_demo"])},
            "now": now.strftime("%Y-%m-%dT%H:%M"), "phase": phase_of(chat, now),
            "times": {"morning": chat["morning"], "evening": chat["evening"], "closing": chat["closing"]},
            "today": {"date": today.isoformat(), "rain": info_t["rain"], "event": info_t["event"], "items": items},
            "tomorrow": {"date": tomorrow.isoformat(), "rain": info_m["rain"], "event": info_m["event"],
                         "rain_effect": round(info_m["rain_eff"], 3), "items": tom},
            "products": products,
            "removed": sorted(db.inactive_products(cid)),
            "batches": batches,
            "data": {"rows": len(sales), "first": days[0].isoformat() if days else None,
                     "last": days[-1].isoformat() if days else None},
            "summary": self._summary(cid),
        }

    # ---------- действия ----------
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
