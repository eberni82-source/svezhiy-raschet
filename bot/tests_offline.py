"""Сквозной тест бота на поддельном сервере Telegram (без интернета)."""
import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from bot import FreshBot  # noqa: E402

SENT = []
FILES = {}
MID = [100]


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("/file/bot")[1].split("/", 1)[1]
        data = FILES[path]
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        method = self.path.rsplit("/", 1)[1]
        n = int(self.headers.get("Content-Length", 0))
        params = json.loads(self.rfile.read(n) or b"{}")
        if method in ("sendMessage", "editMessageText"):
            SENT.append((method, params))
            MID[0] += 1
            return self._json({"ok": True, "result": {"message_id": MID[0], "chat": {"id": params["chat_id"]}, "text": params["text"]}})
        if method == "getFile":
            fid = params["file_id"]
            return self._json({"ok": True, "result": {"file_id": fid, "file_path": f"documents/{fid}.csv", "file_size": len(FILES[f'documents/{fid}.csv'])}})
        if method == "getUpdates":
            return self._json({"ok": True, "result": []})
        return self._json({"ok": True, "result": True})


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
db = tempfile.mktemp(suffix=".db")
ADMIN, USER = 111, 222
bot = FreshBot("123:TEST", {ADMIN}, db, base, weather_on=False)
uid = [0]


def msg(chat, text=None, doc=None, username="baker"):
    uid[0] += 1
    m = {"message_id": uid[0], "chat": {"id": chat, "type": "private"}, "from": {"id": chat, "username": username}}
    if text is not None:
        m["text"] = text
    if doc:
        m["document"] = doc
    SENT.clear()
    bot.handle({"update_id": uid[0], "message": m})
    return list(SENT)


def cb(chat, data):
    uid[0] += 1
    SENT.clear()
    bot.handle({"update_id": uid[0], "callback_query": {"id": str(uid[0]), "data": data,
                "message": {"message_id": 5, "chat": {"id": chat}}}})
    return list(SENT)


def show(title, out):
    print(f"\n===== {title} =====")
    for method, p in out:
        print(f"[{method}] {p['text']}")
        kb = (p.get("reply_markup") or {}).get("inline_keyboard")
        if kb:
            print("   кнопки:", [b["text"] for row in kb for b in row])


def check(cond, what):
    print(("OK   " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


out = msg(USER, "/start"); show("/start", out)
check(any("каком городе" in p["text"] for _, p in out), "спрашивает город")
out = msg(USER, "Екатеринбург"); show("город", out)
check(bot.db.chat(USER)["city"], "город сохранён")
out = msg(999, "/start") + msg(999, "Челябинск"); show("город Челябинск", out)
check(bot.db.chat(999)["tz"] == "Asia/Yekaterinburg" and bot.db.chat(999)["city"] == "Челябинск", "Челябинск: часовой пояс +5 без интернета")
out = msg(USER, "/demo"); show("/demo", out)
check(any("Списания" in p["text"] for _, p in out) and any("План на завтра" in p["text"] for _, p in out), "демо + бэктест + план")
out = msg(USER, "План на завтра"); show("кнопка План на завтра", out)
out = cb(USER, "e:1.2"); show("событие +20%", out)
check(any("+20%" in p["text"] for _, p in out), "событие учтено в плане")
out = msg(USER, "/itogi"); show("/itogi", out)
out = cb(USER, "d:0"); show("итоги: позиция 0", out)
out = cb(USER, "o:0:13:30"); show("кончились 12–15", out)
check(any("✓ кончились 13:30" in b["text"] for _, p in out for row in p["reply_markup"]["inline_keyboard"] for b in row), "отметка сохранена")
out = cb(USER, "l:1"); out += msg(USER, "4"); show("осталось 4", out)
check(any("✓ осталось 4" in b["text"] for _, p in out if p.get("reply_markup") for row in p["reply_markup"].get("inline_keyboard", []) for b in row), "остаток сохранён")
out = msg(USER, "/skidka"); out += cb(USER, "s:0"); out += msg(USER, "30"); show("скидка", out)
check(any("скидка" in p["text"].lower() for _, p in out), "совет по скидке")
out = msg(USER, "/batch") + msg(USER, "Сметана 20%") + msg(USER, "1 кг") + cb(USER, "b:1"); show("партия", out)
out = msg(USER, "/batches"); show("/batches", out)
check(any("Сметана" in p["text"] for _, p in out), "партия в списке")

csv = "дата;товар;продано;испечено\n" + "\n".join(
    f"2026-10-{d:02d};Пончик;{20 + d % 7};{28}" for d in range(1, 18))
FILES["documents/F1.csv"] = csv.encode("cp1251")
out = msg(USER, doc={"file_id": "F1", "file_name": "sales.csv"}); show("загрузка CSV (cp1251)", out)
check(any("Загружено" in p["text"] for _, p in out), "CSV импортирован")
out = msg(USER, "/plan"); show("план по своим данным", out)

out = msg(444, "/start demo"); show("/start demo с сайта", out)
check(any("демо-данные" in p["text"] for _, p in out) and any("План на завтра" in p["text"] for _, p in out), "кнопка с сайта сразу показывает демо")

out = msg(333, "/start pilot", username="newbakery") + msg(333, "Булочная на Ленина") + msg(333, "Тюмень") + cb(333, "k:0") + msg(333, "+7 900 000-00-00")
show("заявка на пилот", out)
check(any(p["chat_id"] == ADMIN and "Новая заявка" in p["text"] for _, p in out), "админ получил заявку")

msg(ADMIN, "/start")
out = msg(ADMIN, "/broadcast Завтра добавим прогноз для кофеен") + cb(ADMIN, "bc:go"); show("рассылка", out)
check(sum(1 for _, p in out if p["text"].startswith("Завтра добавим")) >= 3, "рассылка ушла всем")
out = msg(USER, "/broadcast взлом"); check(any("только для администратора" in p["text"] for _, p in out), "рассылка закрыта для не-админа")
out = msg(ADMIN, "/stats"); show("/stats", out)

# планировщик: делаем утренний пост «пора»
chat = bot.db.chat(USER)
now = bot.now(chat)
bot.db.set_chat(USER, morning=f"{now.hour:02d}:{now.minute:02d}", last_morning=None, muted=0)
SENT.clear(); bot.last_tick = 0; bot.tick(); show("утренняя рассылка", SENT)
check(any(p["chat_id"] == USER and "План на сегодня" in p["text"] for _, p in SENT), "утренний план отправлен по расписанию")
SENT.clear(); bot.last_tick = 0; bot.tick()
check(not any(p["chat_id"] == USER and "План на сегодня" in p["text"] for _, p in SENT), "второй раз за день не шлёт")
print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
