"""Собирает сайт в один файл: index.src.html + model.js + app.js + демо-данные."""
import json
from pathlib import Path
here = Path(__file__).parent
src = (here / "index.src.html").read_text(encoding="utf-8")
csv = (here.parent / "bot" / "data" / "demo_sales.csv").read_text(encoding="utf-8")
out = (src.replace("/*MODEL*/", (here / "model.js").read_text(encoding="utf-8"))
          .replace("__DEMO_CSV__", json.dumps(csv, ensure_ascii=False))
          .replace("/*APP*/", (here / "app.js").read_text(encoding="utf-8")))
(here / "svezhiy-raschet.html").write_text(out, encoding="utf-8")
print("ok", len(out))
