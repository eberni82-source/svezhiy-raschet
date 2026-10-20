"""Минимальный клиент Telegram Bot API на стандартной библиотеке (без pip install)."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("tg")


class TelegramError(Exception):
    def __init__(self, description: str, code: int = 0, retry_after: int | None = None):
        super().__init__(description)
        self.code = code
        self.retry_after = retry_after


class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org"):
        self.token = token
        self.base = f"{api_base.rstrip('/')}/bot{token}"
        self.file_base = f"{api_base.rstrip('/')}/file/bot{token}"

    def call(self, method: str, timeout: float = 30, **params):
        params = {k: v for k, v in params.items() if v is not None}
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{self.base}/{method}", data=data,
                                     headers={"Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                try:
                    body = json.loads(e.read().decode("utf-8"))
                except Exception:
                    raise TelegramError(f"HTTP {e.code}", e.code)
            except (urllib.error.URLError, OSError) as e:  # нет интернета, Telegram недоступен, таймаут
                if attempt < 2:
                    time.sleep(2)
                    continue
                raise TelegramError(f"нет связи с Telegram: {getattr(e, 'reason', e)}", -1)
            if body.get("ok"):
                return body.get("result")
            retry = (body.get("parameters") or {}).get("retry_after")
            if retry and attempt < 2:
                time.sleep(retry)
                continue
            raise TelegramError(body.get("description", "unknown error"), body.get("error_code", 0), retry)
        raise TelegramError("too many retries")

    # --- удобные обёртки ---
    def send(self, chat_id: int, text: str, keyboard=None, reply_keyboard=None, remove_keyboard=False):
        markup = None
        if keyboard is not None:
            markup = {"inline_keyboard": keyboard}
        elif reply_keyboard is not None:
            markup = {"keyboard": reply_keyboard, "resize_keyboard": True, "is_persistent": True}
        elif remove_keyboard:
            markup = {"remove_keyboard": True}
        return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                         reply_markup=markup, disable_web_page_preview=True)

    def edit(self, chat_id: int, message_id: int, text: str, keyboard=None):
        try:
            return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                             parse_mode="HTML", reply_markup={"inline_keyboard": keyboard} if keyboard else None)
        except TelegramError as e:
            if "not modified" in str(e):
                return None
            raise

    def answer(self, callback_id: str, text: str | None = None):
        try:
            self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)
        except TelegramError:
            pass

    def updates(self, offset: int | None, timeout: int = 25):
        """Длинный опрос: ждём новые сообщения до timeout секунд."""
        params = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            params["offset"] = offset
        data = json.dumps(params).encode()
        req = urllib.request.Request(f"{self.base}/getUpdates", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout + 10) as r:
            body = json.loads(r.read().decode("utf-8"))
        if not body.get("ok"):
            raise TelegramError(body.get("description", "getUpdates failed"), body.get("error_code", 0))
        return body["result"]

    def download(self, file_id: str, max_bytes: int = 5_000_000) -> bytes:
        info = self.call("getFile", file_id=file_id)
        if info.get("file_size", 0) > max_bytes:
            raise TelegramError("файл слишком большой (до 5 МБ)")
        with urllib.request.urlopen(f"{self.file_base}/{info['file_path']}", timeout=60) as r:
            return r.read(max_bytes + 1)
