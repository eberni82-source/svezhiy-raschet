"""Собирает сайт и кабинет в готовые файлы.

    python site-src/build.py

index.html          — сайт (GitHub Pages)
kabinet.html        — кабинет: на GitHub Pages работает как пример в браузере
bot/web/kabinet.html — тот же кабинет, его раздаёт бот (свои данные, вход по коду)
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HEAD = ('<!doctype html>\n<html lang="ru">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n'
        '<style>:root{padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}'
        'body{margin:0}img{max-width:100%}[hidden]{display:none!important}</style>\n</head>\n<body>\n')
TAIL = '\n</body>\n</html>\n'


def read(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8")


def build(src_name: str, app_name: str, marker: str) -> str:
    csv = (ROOT / "bot" / "data" / "demo_sales.csv").read_text(encoding="utf-8")
    return (read(src_name).replace("/*MODEL*/", read("model.js"))
            .replace("__DEMO_CSV__", json.dumps(csv, ensure_ascii=False))
            .replace(marker, read(app_name)))


def main():
    site = build("index.src.html", "app.js", "/*APP*/")
    kab = build("kabinet.src.html", "kabinet.js", "/*KABINET*/")
    (ROOT / "index.html").write_text(HEAD + site + TAIL, encoding="utf-8")
    (ROOT / "kabinet.html").write_text(HEAD + kab + TAIL, encoding="utf-8")
    (ROOT / "bot" / "web").mkdir(exist_ok=True)
    (ROOT / "bot" / "web" / "kabinet.html").write_text(HEAD + kab + TAIL, encoding="utf-8")
    print("ok: index.html", len(site), "· kabinet.html", len(kab))


if __name__ == "__main__":
    main()
