"""Собирает сайт и кабинет в готовые файлы.

    python site-src/build.py

index.html          - сайт (GitHub Pages)
kabinet.html        - кабинет: на GitHub Pages работает как пример в браузере
bot/web/kabinet.html - тот же кабинет, его раздаёт бот (свои данные, вход по коду)
"""
import json
from urllib.parse import quote
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ICON = ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='8' fill='#2346C8'/>"
        "<text x='16' y='22.5' text-anchor='middle' font-family='Arial,sans-serif' font-size='19' font-weight='700' fill='#fff'>С</text></svg>")


def head() -> str:
    """Начало страницы: кодировка, масштаб, иконка вкладки."""
    links = f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{quote(ICON)}">\n'
    return ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n' + links +
            '<style>:root{padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}'
            'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n</head>\n<body>\n')


TAIL = '\n</body>\n</html>\n'


def read(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


def build(src_name: str, app_name: str, marker: str, example: bool = True) -> str:
    csv = (ROOT / "bot" / "data" / "demo_sales.csv").read_text(encoding="utf-8") if example else ""
    return (read(src_name).replace("/*MODEL*/", read("model.js"))
            .replace("__DEMO_CSV__", json.dumps(csv, ensure_ascii=False))
            .replace(marker, read(app_name)))


def main():
    site = build("index.src.html", "app.js", "/*APP*/")
    kab = build("kabinet.src.html", "kabinet.js", "/*KABINET*/")
    # кабинету в боте пример не нужен: там свои данные с сервера
    kab_bot = build("kabinet.src.html", "kabinet.js", "/*KABINET*/", example=False)
    (ROOT / "index.html").write_text(head() + site + TAIL, encoding="utf-8")
    (ROOT / "kabinet.html").write_text(head() + kab + TAIL, encoding="utf-8")
    (ROOT / "bot" / "web").mkdir(exist_ok=True)
    (ROOT / "bot" / "web" / "kabinet.html").write_text(head() + kab_bot + TAIL, encoding="utf-8")
    print("ok: index.html", len(site), "· kabinet.html", len(kab))


if __name__ == "__main__":
    main()
