// таблицы как в Excel: ячейки, клавиатура, вставка из Excel, выгрузка для Excel
(function (root) {
  "use strict";
  var WDS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];

  var CSS = [
    ".xs-bar{display:flex;flex-wrap:wrap;gap:8px 10px;align-items:center;margin:0 0 10px}",
    ".xs-tabs{display:flex;gap:2px;flex-wrap:wrap;border-bottom:1px solid var(--line);margin:0 0 12px}",
    ".xs-tabs button{border:0;background:none;font:600 14px/1 var(--f-body);color:var(--ink-3);padding:10px 12px 12px;cursor:pointer;border-bottom:2px solid transparent;margin-bottom:-1px}",
    ".xs-tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--stamp)}",
    ".xs-bar select,.xs-bar .xs-btn{font:500 14px/1 var(--f-body);color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:8px 11px;min-height:36px;cursor:pointer}",
    ".xs-bar .xs-btn:hover{border-color:var(--ink-3)}",
    ".xs-bar .xs-btn[disabled]{opacity:.45;cursor:default}",
    ".xs-bar .xs-sp{flex:1}",
    ".xs-bar label{display:flex;gap:6px;align-items:center;font-size:13px;color:var(--ink-3)}",
    ".xs-file{position:relative;overflow:hidden}",
    ".xs-file input{position:absolute;inset:0;opacity:0;cursor:pointer}",
    ".xs-hint{font-size:12.5px;color:var(--ink-3);margin:8px 0 0}",
    ".xg{border:1px solid var(--line);border-radius:10px;background:var(--surface);outline:none;max-width:100%}",
    ".xg:focus-visible{box-shadow:0 0 0 2px var(--stamp-soft)}",
    ".xg-scroll{overflow:auto;max-height:min(68vh,620px);border-radius:10px}",
    ".xg-t{border-collapse:separate;border-spacing:0;font:400 14px/1.3 var(--f-body);font-variant-numeric:tabular-nums;color:var(--ink);table-layout:fixed;min-width:100%}",
    ".xg-t th,.xg-t td{border-right:1px solid var(--line);border-bottom:1px solid var(--line);padding:0 10px;height:34px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
    ".xg-t th:last-child,.xg-t td:last-child{border-right:0}",
    ".xg-t thead th{position:sticky;top:0;z-index:2;background:var(--bg);font:600 12.5px/1.2 var(--f-body);color:var(--ink-2);text-align:left;white-space:normal;height:40px}",
    ".xg-t th.r,.xg-t td.r{text-align:right}",
    ".xg-t .xg-n{position:sticky;left:0;z-index:1;background:var(--bg);color:var(--ink-3);font-size:12px;text-align:right;width:46px;padding:0 8px;user-select:none}",
    ".xg-t thead .xg-n{z-index:3}",
    ".xg-t td{cursor:cell;user-select:none}",
    ".xg-t td.ro{color:var(--ink-2);cursor:default}",
    ".xg-t td.in{background:var(--stamp-soft)}",
    ".xg-t td.cur{box-shadow:inset 0 0 0 2px var(--stamp)}",
    ".xg-t td.bad{background:var(--bad-soft)}",
    ".xg-t tr.draft .xg-n{color:var(--stamp)}",
    ".xg-t tr.add td{color:var(--ink-3)}",
    ".xg-t tfoot td{position:sticky;bottom:0;background:var(--bg);font-weight:600;border-top:1px solid var(--line);border-bottom:0}",
    ".xg-t .mute{color:var(--ink-3)}",
    ".xg-t .tag{font-size:12.5px;font-weight:600}",
    ".xg-t .tag.bad{color:var(--bad)}.xg-t .tag.warn{color:var(--warn)}.xg-t .tag.ok{color:var(--ok)}",
    ".xg-in{width:100%;height:32px;border:0;outline:0;background:transparent;font:inherit;color:inherit;padding:0;margin:0}",
    ".xg-empty{padding:28px 16px;color:var(--ink-3);text-align:center}"
  ].join("\n");

  function injectCss() {
    if (document.getElementById("xg-css")) return;
    var s = document.createElement("style");
    s.id = "xg-css"; s.textContent = CSS;
    document.head.appendChild(s);
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function pad(n) { return ("0" + n).slice(-2); }
  function isoDay(y, m, d) {
    var t = Date.UTC(y, m - 1, d), x = new Date(t);
    if (x.getUTCFullYear() !== y || x.getUTCMonth() !== m - 1 || x.getUTCDate() !== d) return null;
    return y + "-" + pad(m) + "-" + pad(d);
  }
  function weekday(iso) { return WDS[(new Date(iso + "T00:00:00Z").getUTCDay() + 6) % 7]; }
  function nf(x, digits) {
    if (x == null || x === "") return "";
    var v = Math.round(x * Math.pow(10, digits || 0)) / Math.pow(10, digits || 0);
    return String(v).replace(".", ",").replace("-", "−");
  }

  // значение из текста по типу колонки; ошибка — исключение с понятным текстом
  function parse(col, raw) {
    var s = String(raw == null ? "" : raw).replace(/ /g, " ").trim();
    if (s === "") {
      if (col.required) throw new Error("«" + col.title + "» нельзя оставить пустым");
      return null;
    }
    var m;
    if (col.type === "number") {
      var x = Number(s.replace(/\s/g, "").replace(",", ".").replace("−", "-"));
      if (!isFinite(x)) throw new Error("«" + col.title + "»: нужно число, а не «" + s + "»");
      if (col.int && Math.round(x) !== x) throw new Error("«" + col.title + "»: нужно целое число");
      if (col.min != null && x < col.min) throw new Error("«" + col.title + "»: не меньше " + col.min);
      if (col.max != null && x > col.max) throw new Error("«" + col.title + "»: не больше " + col.max);
      return x;
    }
    if (col.type === "date") {
      var iso = null, y = new Date().getFullYear();
      if ((m = s.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/))) iso = isoDay(+m[1], +m[2], +m[3]);
      else if ((m = s.match(/^(\d{1,2})[.\/](\d{1,2})(?:[.\/](\d{2}|\d{4}))?$/))) iso = isoDay(m[3] ? (m[3].length === 2 ? 2000 + +m[3] : +m[3]) : y, +m[2], +m[1]);
      if (!iso) throw new Error("«" + col.title + "»: дата в виде 23.10.2026");
      return iso;
    }
    if (col.type === "time") {
      if (!(m = s.match(/^(\d{1,2})[:.](\d{2})$/)) || +m[1] > 23 || +m[2] > 59) throw new Error("«" + col.title + "»: время в виде 15:40");
      return pad(+m[1]) + ":" + m[2];
    }
    if (col.type === "bool") {
      var l = s.toLowerCase();
      if (["да", "д", "1", "+", "yes", "y", "true", "дождь", "x", "х", "✓"].indexOf(l) >= 0) return true;
      if (["нет", "н", "0", "-", "no", "n", "false"].indexOf(l) >= 0) return false;
      throw new Error("«" + col.title + "»: да или нет");
    }
    return s.slice(0, col.maxLength || 60);
  }
  function text(col, v) {
    if (v == null || v === "") return "";
    if (col.type === "number") return nf(v, col.digits || (col.int ? 0 : 2));
    if (col.type === "date") return v.slice(8, 10) + "." + v.slice(5, 7) + "." + v.slice(0, 4);
    if (col.type === "bool") return v ? "да" : "нет";
    return String(v);
  }

  // буфер обмена: copy и paste приходят на body, поэтому ищем таблицу с фокусом
  var clipboardReady = false;
  function activeGrid() {
    var a = document.activeElement, el = a && a.closest && a.closest(".xg");
    return el && el.__grid && !el.__grid.editing ? el.__grid : null;
  }
  function listenClipboard() {
    if (clipboardReady) return;
    clipboardReady = true;
    document.addEventListener("copy", function (e) { var g = activeGrid(); if (g) g.onCopy(e); });
    document.addEventListener("paste", function (e) { var g = activeGrid(); if (g) g.onPaste(e); });
  }

  // ---------------------------------------------------------------- сетка
  function Grid(el, o) {
    injectCss();
    this.el = el; this.o = o; this.rows = o.rows || [];
    this.sel = { r: 0, c: 0, r2: 0, c2: 0 }; this.editing = null;
    el.className = "xg"; el.tabIndex = 0;
    el.setAttribute("role", "grid"); el.setAttribute("aria-label", o.label || "Таблица");
    var self = this;
    el.addEventListener("mousedown", function (e) { self.onDown(e); });
    el.addEventListener("dblclick", function (e) { var td = e.target.closest("td[data-c]"); if (td) self.startEdit(null); });
    el.addEventListener("keydown", function (e) { self.onKey(e); });
    el.__grid = this;
    listenClipboard();
    this.render();
  }
  Grid.prototype.cols = function () { return this.o.columns; };
  Grid.prototype.count = function () { return this.rows.length + (this.o.allowAdd ? 1 : 0); };
  Grid.prototype.setRows = function (rows) {
    this.rows = rows; this.editing = null;
    var n = Math.max(0, this.count() - 1);
    this.sel.r = Math.min(this.sel.r, n); this.sel.r2 = Math.min(this.sel.r2, n);
    this.render();
  };
  Grid.prototype.render = function () {
    var self = this, cols = this.cols(), h = [];
    h.push('<div class="xg-scroll"><table class="xg-t"><colgroup><col style="width:46px">');
    cols.forEach(function (c) { h.push('<col style="width:' + (c.width || 120) + 'px">'); });
    h.push('</colgroup><thead><tr><th class="xg-n" aria-hidden="true"></th>');
    cols.forEach(function (c) { h.push('<th class="' + (c.type === "number" || c.right ? "r" : "") + '" scope="col">' + esc(c.title) + "</th>"); });
    h.push("</tr></thead><tbody>");
    this.rows.forEach(function (row, r) { h.push(self.rowHtml(row, r)); });
    if (this.o.allowAdd) {
      h.push('<tr class="add"><td class="xg-n">+</td>');
      cols.forEach(function (c, ci) { h.push('<td data-r="' + self.rows.length + '" data-c="' + ci + '" class="' + (c.ro ? "ro " : "") + (c.type === "number" || c.right ? "r" : "") + '">' + (ci === 0 ? '<span class="mute">новая строка</span>' : "") + "</td>"); });
      h.push("</tr>");
    }
    h.push("</tbody>");
    var foot = this.o.footer ? this.o.footer(this.rows) : null;
    if (foot) {
      h.push('<tfoot><tr><td class="xg-n"></td>');
      cols.forEach(function (c, ci) { h.push('<td class="' + (c.type === "number" || c.right ? "r" : "") + '">' + esc(foot[ci] == null ? "" : foot[ci]) + "</td>"); });
      h.push("</tr></tfoot>");
    }
    h.push("</table>");
    if (!this.rows.length && !this.o.allowAdd) h.push('<div class="xg-empty">' + esc(this.o.empty || "Пока пусто") + "</div>");
    h.push("</div>");
    var sc = this.el.querySelector(".xg-scroll"), top = sc ? sc.scrollTop : 0, left = sc ? sc.scrollLeft : 0;
    this.el.innerHTML = h.join("");
    sc = this.el.querySelector(".xg-scroll"); sc.scrollTop = top; sc.scrollLeft = left;
    this.paintSel(false);
  };
  Grid.prototype.rowHtml = function (row, r) {
    var cols = this.cols(), h = '<tr class="' + (row.__draft ? "draft" : "") + '"><td class="xg-n">' + (row.__draft ? "•" : r + 1) + "</td>";
    cols.forEach(function (c, ci) {
      var v = c.value ? c.value(row) : row[c.key];
      var html = c.html ? c.html(v, row) : esc(c.format ? c.format(v, row) : text(c, v));
      h += '<td data-r="' + r + '" data-c="' + ci + '" class="' + (c.ro ? "ro " : "") + (c.type === "number" || c.right ? "r" : "") + '">' + html + "</td>";
    });
    return h + "</tr>";
  };
  Grid.prototype.cell = function (r, c) { return this.el.querySelector('td[data-r="' + r + '"][data-c="' + c + '"]'); };
  Grid.prototype.range = function () {
    var s = this.sel;
    return { r0: Math.min(s.r, s.r2), r1: Math.max(s.r, s.r2), c0: Math.min(s.c, s.c2), c1: Math.max(s.c, s.c2) };
  };
  Grid.prototype.paintSel = function (scroll) {
    this.el.querySelectorAll("td.in,td.cur").forEach(function (td) { td.classList.remove("in", "cur"); });
    var g = this.range(), single = g.r0 === g.r1 && g.c0 === g.c1;
    if (!single) {
      for (var r = g.r0; r <= g.r1; r++) for (var c = g.c0; c <= g.c1; c++) { var td = this.cell(r, c); if (td) td.classList.add("in"); }
    }
    var cur = this.cell(this.sel.r2, this.sel.c2);
    if (cur) {
      cur.classList.add("cur");
      if (scroll) {
        var sc = this.el.querySelector(".xg-scroll"), a = cur.getBoundingClientRect(), b = sc.getBoundingClientRect();
        var headH = 40, numW = 46;
        if (a.top < b.top + headH) sc.scrollTop -= b.top + headH - a.top;
        else if (a.bottom > b.bottom - 34) sc.scrollTop += a.bottom - b.bottom + 34;
        if (a.left < b.left + numW) sc.scrollLeft -= b.left + numW - a.left;
        else if (a.right > b.right) sc.scrollLeft += a.right - b.right;
      }
    }
    if (this.o.onSelect) this.o.onSelect(this.range());
  };
  Grid.prototype.move = function (dr, dc, extend) {
    var n = this.count(), m = this.cols().length, s = this.sel;
    var r = Math.max(0, Math.min(n - 1, s.r2 + dr)), c = Math.max(0, Math.min(m - 1, s.c2 + dc));
    if (extend) { s.r2 = r; s.c2 = c; } else { s.r = s.r2 = r; s.c = s.c2 = c; }
    this.paintSel(true);
  };
  Grid.prototype.onDown = function (e) {
    var td = e.target.closest("td[data-c]");
    if (!td || this.editing && this.editing.td === td) return;
    if (this.editing) this.commit(0, 0);
    e.preventDefault();
    this.el.focus({ preventScroll: true });
    var r = +td.getAttribute("data-r"), c = +td.getAttribute("data-c"), s = this.sel, self = this;
    if (e.shiftKey) { s.r2 = r; s.c2 = c; } else { s.r = s.r2 = r; s.c = s.c2 = c; }
    this.paintSel(false);
    var onMove = function (ev) {
      var t = document.elementFromPoint(ev.clientX, ev.clientY), x = t && t.closest && t.closest("td[data-c]");
      if (x && self.el.contains(x)) { s.r2 = +x.getAttribute("data-r"); s.c2 = +x.getAttribute("data-c"); self.paintSel(false); }
    };
    var onUp = function () { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); };
    document.addEventListener("mousemove", onMove); document.addEventListener("mouseup", onUp);
  };
  Grid.prototype.onKey = function (e) {
    if (this.editing) return;
    var k = e.key, mod = e.ctrlKey || e.metaKey;
    if (mod && (k === "a" || k === "A" || k === "ф" || k === "Ф")) { e.preventDefault(); this.sel = { r: 0, c: 0, r2: Math.max(0, this.rows.length - 1), c2: this.cols().length - 1 }; this.paintSel(false); return; }
    if (mod) return;
    var map = { ArrowUp: [-1, 0], ArrowDown: [1, 0], ArrowLeft: [0, -1], ArrowRight: [0, 1], PageUp: [-10, 0], PageDown: [10, 0] };
    if (map[k]) { e.preventDefault(); this.move(map[k][0], map[k][1], e.shiftKey); return; }
    if (k === "Tab") { e.preventDefault(); this.move(0, e.shiftKey ? -1 : 1, false); return; }
    if (k === "Home") { e.preventDefault(); this.move(0, -99, e.shiftKey); return; }
    if (k === "End") { e.preventDefault(); this.move(0, 99, e.shiftKey); return; }
    if (k === "Enter" || k === "F2") { e.preventDefault(); this.startEdit(null); return; }
    if (k === "Delete" || k === "Backspace") { e.preventDefault(); this.clear(); return; }
    if (k.length === 1 && !e.altKey) { e.preventDefault(); this.startEdit(k); }
  };
  Grid.prototype.startEdit = function (typed) {
    var s = this.sel, col = this.cols()[s.c2], td = this.cell(s.r2, s.c2);
    if (!col || !td) return;
    if (col.ro) { if (this.o.onError) this.o.onError("Эта колонка считается сама"); return; }
    s.r = s.r2; s.c = s.c2; this.paintSel(false);
    var row = this.rows[s.r2], v = row ? row[col.key] : null, self = this;
    var inp = document.createElement("input");
    inp.className = "xg-in"; inp.type = "text"; inp.setAttribute("aria-label", col.title);
    if (col.type === "number") inp.inputMode = "decimal";
    inp.value = typed != null ? typed : text(col, v);
    td.innerHTML = ""; td.appendChild(inp);
    this.editing = { td: td, inp: inp, r: s.r2, c: s.c2, typed: typed != null };
    inp.focus();
    if (typed == null) inp.select(); else inp.setSelectionRange(inp.value.length, inp.value.length);
    inp.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); self.commit(1, 0); }
      else if (e.key === "Tab") { e.preventDefault(); self.commit(0, e.shiftKey ? -1 : 1); }
      else if (e.key === "Escape") { e.preventDefault(); self.cancel(); }
      else if (self.editing && self.editing.typed && (e.key === "ArrowDown" || e.key === "ArrowUp")) { e.preventDefault(); self.commit(e.key === "ArrowDown" ? 1 : -1, 0); }
      e.stopPropagation();
    });
    inp.addEventListener("blur", function () { if (self.editing && self.editing.inp === inp) self.commit(0, 0, true); });
  };
  Grid.prototype.cancel = function () {
    var ed = this.editing; if (!ed) return;
    this.editing = null;
    this.redrawRow(ed.r, this.rows[ed.r]);
    this.el.focus({ preventScroll: true });
    if (this.o.afterEdit) this.o.afterEdit();
  };
  Grid.prototype.redrawRow = function (r, row) {
    var tr = this.cell(r, 0) && this.cell(r, 0).parentNode;
    if (!tr) return this.render();
    if (!row) { this.render(); return; }
    var tmp = document.createElement("tbody"); tmp.innerHTML = this.rowHtml(row, r);
    tr.parentNode.replaceChild(tmp.firstChild, tr);
    this.paintSel(false);
  };
  Grid.prototype.commit = function (dr, dc, blur) {
    var ed = this.editing; if (!ed) return;
    var col = this.cols()[ed.c], val;
    try { val = parse(col, ed.inp.value); }
    catch (err) {
      if (this.o.onError) this.o.onError(err.message);
      if (blur) { this.cancel(); return; }
      ed.td.classList.add("bad"); ed.inp.select(); return;
    }
    this.editing = null;
    var row = this.rows[ed.r], old = row ? row[col.key] : null;
    if (!row && val == null) this.render();
    else if (row && (old === val || (old == null && val == null))) this.redrawRow(ed.r, row);
    else this.apply([{ r: ed.r, key: col.key, value: val }]);
    if (!blur) { this.el.focus({ preventScroll: true }); this.move(dr, dc, false); }
    if (this.o.afterEdit) this.o.afterEdit();
  };
  // изменения: [{ r, key, value }]; новые строки создаются, если r за концом списка
  Grid.prototype.apply = function (changes) {
    var self = this, touched = [];
    changes.forEach(function (ch) {
      var row = self.rows[ch.r];
      if (!row) {
        row = self.o.newRow ? self.o.newRow() : {};
        row.__draft = true;
        self.rows.push(row);
      }
      if (touched.indexOf(row) < 0) touched.push(row);
      row.__old = row.__old || {};
      if (!(ch.key in row.__old)) row.__old[ch.key] = row[ch.key];
      row[ch.key] = ch.value;
    });
    this.render();
    if (this.o.onChange) this.o.onChange(touched);
  };
  Grid.prototype.clear = function () {
    var g = this.range(), cols = this.cols(), ch = [];
    for (var r = g.r0; r <= Math.min(g.r1, this.rows.length - 1); r++) {
      for (var c = g.c0; c <= g.c1; c++) {
        var col = cols[c];
        if (col.ro || col.required) continue;
        if (this.rows[r][col.key] != null) ch.push({ r: r, key: col.key, value: null });
      }
    }
    if (ch.length) this.apply(ch);
  };
  Grid.prototype.tsv = function (g, header) {
    var cols = this.cols(), out = [];
    if (header) { var hd = []; for (var c0 = g.c0; c0 <= g.c1; c0++) hd.push(cols[c0].title); out.push(hd.join("\t")); }
    for (var r = g.r0; r <= g.r1; r++) {
      var row = this.rows[r]; if (!row) continue;
      var line = [];
      for (var c = g.c0; c <= g.c1; c++) {
        var col = cols[c], v = col.value ? col.value(row) : row[col.key];
        line.push((col.format ? col.format(v, row) : text(col, v)).replace(/[\t\n]/g, " "));
      }
      out.push(line.join("\t"));
    }
    return out.join("\r\n");
  };
  Grid.prototype.onCopy = function (e) {
    if (this.editing) return;
    e.preventDefault();
    e.clipboardData.setData("text/plain", this.tsv(this.range(), false));
    if (this.o.onInfo) this.o.onInfo("Скопировано — вставьте в Excel");
  };
  Grid.prototype.onPaste = function (e) {
    if (this.editing) return;
    e.preventDefault();
    var raw = (e.clipboardData || window.clipboardData).getData("text"); if (!raw) return;
    var lines = raw.replace(/\r/g, "").split("\n");
    if (lines.length > 1 && lines[lines.length - 1] === "") lines.pop();
    var cols = this.cols(), g = this.range(), ch = [], errors = [], skipped = 0;
    var start = g.r0, first = lines[0].split("\t");
    // строка заголовков из Excel — пропускаем
    var isTitle = function (x, i) { var c = cols[g.c0 + i], s = x.trim().toLowerCase(); return c && (s === c.title.toLowerCase() || s === String(c.csvTitle || "").toLowerCase()); };
    if (first.some(isTitle)) { lines.shift(); if (!lines.length) return; first = lines[0].split("\t"); }
    var limit = this.o.allowAdd ? Infinity : this.rows.length;
    lines.forEach(function (line, i) {
      var r = start + i; if (r >= limit) { skipped++; return; }
      line.split("\t").forEach(function (cellText, j) {
        var col = cols[g.c0 + j]; if (!col || col.ro) return;
        try { ch.push({ r: r, key: col.key, value: parse(col, cellText) }); }
        catch (err) { errors.push("строка " + (i + 1) + ": " + err.message); }
      });
    });
    if (errors.length && this.o.onError) this.o.onError("Не вставлено " + errors.length + ": " + errors.slice(0, 2).join("; "));
    if (ch.length) {
      this.sel = { r: start, c: g.c0, r2: start + lines.length - 1, c2: Math.min(cols.length - 1, g.c0 + first.length - 1) };
      this.apply(ch);
      if (this.o.onInfo && !errors.length) this.o.onInfo("Вставлено строк: " + lines.length);
    }
  };
  Grid.prototype.selectedRows = function () {
    var g = this.range(), out = [];
    for (var r = g.r0; r <= g.r1; r++) if (this.rows[r]) out.push(this.rows[r]);
    return out;
  };
  Grid.prototype.focusNewRow = function () {
    this.sel = { r: this.rows.length - 1, c: 0, r2: this.rows.length - 1, c2: 0 };
    this.paintSel(true); this.el.focus({ preventScroll: true });
  };

  // ---------------------------------------------------------------- CSV для Excel
  function csv(columns, rows) {
    var cols = columns.filter(function (c) { return c.csv !== false; });
    var q = function (s) { s = String(s == null ? "" : s); return /[;"\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
    var out = [cols.map(function (c) { return q(c.csvTitle || c.title); }).join(";")];
    rows.forEach(function (row) {
      out.push(cols.map(function (c) { var v = c.value ? c.value(row) : row[c.key]; return q(c.format ? c.format(v, row) : text(c, v).replace("−", "-")); }).join(";"));
    });
    return "﻿" + out.join("\r\n") + "\r\n";
  }
  function download(name, content) {
    var blob = new Blob([content], { type: "text/csv;charset=utf-8" }), a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  }

  // ---------------------------------------------------------------- четыре листа
  // adapter: today (ISO), load(), saveSales(upsert, remove), saveProduct(p, oldName), removeProduct(name),
  //          addBatch(b), editBatch(b), deleteBatch(id), upload(text)?, toast(msg)
  function Sheets(el, a) {
    injectCss();
    this.el = el; this.a = a; this.tab = "sales"; this.period = 14; this.product = "";
    this.data = null; this.drafts = { sales: [], products: [], batches: [] }; this.busy = false;
    var self = this;
    el.innerHTML = '<div class="xs-tabs" role="tablist" aria-label="Листы"></div><div class="xs-bar"></div><div class="xs-grid"></div><p class="xs-hint"></p>';
    el.addEventListener("click", function (e) { self.onClick(e); });
    el.addEventListener("change", function (e) { self.onChangeCtl(e); });
  }
  var SHEETS = [["sales", "Продажи по дням"], ["products", "Товары"], ["batches", "Сроки годности"], ["week", "План на неделю"]];
  var HINT = "Кликните ячейку и печатайте. Enter — изменить, стрелки и Tab — переход, Delete — очистить, Ctrl+C / Ctrl+V — копировать и вставить из Excel.";

  Sheets.prototype.load = function () {
    var self = this;
    return this.a.load().then(function (d) {
      self.data = d;
      if (self.grid && self.grid.editing) self.stale = true; else self.show();
    }).catch(function (e) { self.err(e); });
  };
  Sheets.prototype.err = function (e) { this.a.toast(e && e.message ? e.message : "Не получилось сохранить"); };
  Sheets.prototype.productNames = function () { return this.data.products.map(function (p) { return p.name; }).sort(function (x, y) { return x.localeCompare(y, "ru"); }); };

  Sheets.prototype.salesColumns = function () {
    return [
      { key: "day", title: "Дата", type: "date", width: 120, required: true, csvTitle: "дата",
        html: function (v) { return v ? '<span class="mute">' + weekday(v) + "</span> " + esc(text({ type: "date" }, v)) : ""; } },
      { key: "product", title: "Товар", type: "text", width: 200, required: true, csvTitle: "товар" },
      { key: "baked", title: "Испечено / заказано", type: "number", min: 0, max: 100000, width: 120, csvTitle: "испечено" },
      { key: "sold", title: "Продано", type: "number", min: 0, max: 100000, width: 96, required: true, csvTitle: "продано" },
      { key: "left", title: "Осталось", type: "number", ro: true, width: 90, csv: false,
        value: function (r) { return r.baked != null && r.sold != null && !r.soldout ? Math.max(0, r.baked - r.sold) : null; } },
      { key: "soldout", title: "Закончилось в", type: "time", width: 110, csvTitle: "закончилось" },
      { key: "rain", title: "Дождь", type: "bool", width: 76, csvTitle: "дождь" },
      { key: "temp", title: "Темп., °C", type: "number", min: -60, max: 50, digits: 1, width: 92, csvTitle: "температура" }
    ];
  };
  Sheets.prototype.productColumns = function () {
    return [
      { key: "name", title: "Товар", type: "text", width: 210, required: true },
      { key: "price", title: "Цена, ₽", type: "number", min: 0, max: 1e6, width: 96 },
      { key: "cost", title: "Себестоимость, ₽", type: "number", min: 0, max: 1e6, width: 130 },
      { key: "margin", title: "Маржа", type: "number", ro: true, width: 80, csv: false,
        value: function (p) { return p.price && p.cost != null ? Math.round((p.price - p.cost) / p.price * 100) : null; },
        format: function (v) { return v == null ? "" : v + "%"; } },
      { key: "shelf_life", title: "Срок хранения, дней", type: "number", int: true, min: 0, max: 3650, width: 140 },
      { key: "default_qty", title: "Обычно в день, шт", type: "number", min: 0, max: 1e5, width: 130 },
      { key: "history_days", title: "История", type: "number", ro: true, right: true, width: 100, csv: false,
        format: function (v) { return v ? v + " дн." : "новый"; } }
    ];
  };
  Sheets.prototype.batchColumns = function () {
    return [
      { key: "item", title: "Продукт", type: "text", width: 210, required: true },
      { key: "qty", title: "Количество", type: "text", width: 110, maxLength: 20 },
      { key: "expires", title: "Годен до", type: "date", width: 120,
        html: function (v) { return v ? '<span class="mute">' + weekday(v) + "</span> " + esc(text({ type: "date" }, v)) : ""; } },
      { key: "label", title: "Статус", type: "text", ro: true, width: 150,
        html: function (v, b) { return v ? '<span class="tag ' + (b.level || "") + '">' + esc(v) + "</span>" : ""; } },
      { key: "tip", title: "Что сделать", type: "text", ro: true, width: 300 }
    ];
  };
  Sheets.prototype.weekColumns = function () {
    var days = this.data.week.days, cols = [{ key: "product", title: "Товар", type: "text", ro: true, width: 200 }];
    days.forEach(function (d, i) {
      cols.push({ key: "d" + i, title: weekday(d) + " " + d.slice(8, 10) + "." + d.slice(5, 7), type: "number", ro: true, width: 78,
        value: function (r) { return r.qty[i]; } });
    });
    cols.push({ key: "total", title: "За неделю", type: "number", ro: true, width: 100, value: function (r) { return r.qty.reduce(function (x, y) { return x + y; }, 0); } });
    return cols;
  };

  Sheets.prototype.salesRows = function () {
    var d = this.data, a = this.a, self = this, from = null;
    if (this.period !== "all") from = shift(a.today, -(+this.period - 1));
    var rows = d.sales.filter(function (s) { return (!from || s.day >= from) && (!self.product || s.product === self.product); });
    var order = {}; this.productNames().forEach(function (n, i) { order[n] = i; });
    rows.sort(function (x, y) { return x.day < y.day ? -1 : x.day > y.day ? 1 : (order[x.product] || 0) - (order[y.product] || 0); });
    return rows.concat(this.drafts.sales);
  };

  Sheets.prototype.show = function () {
    if (!this.data) return;
    var self = this, a = this.a;
    this.el.querySelector(".xs-tabs").innerHTML = SHEETS.map(function (s) {
      return '<button role="tab" data-sheet="' + s[0] + '" aria-selected="' + (self.tab === s[0]) + '">' + s[1] + "</button>";
    }).join("");
    var bar = [], t = this.tab;
    if (t === "sales") {
      bar.push('<label>Период<select data-ctl="period">' + [[7, "7 дней"], [14, "14 дней"], [30, "30 дней"], [90, "90 дней"], ["all", "Всё время"]].map(function (o) {
        return '<option value="' + o[0] + '"' + (String(self.period) === String(o[0]) ? " selected" : "") + ">" + o[1] + "</option>"; }).join("") + "</select></label>");
      bar.push('<label>Товар<select data-ctl="product"><option value="">Все</option>' + this.productNames().map(function (n) {
        return '<option' + (self.product === n ? " selected" : "") + ">" + esc(n) + "</option>"; }).join("") + "</select></label>");
    }
    bar.push('<span class="xs-sp"></span>');
    if (t !== "week") {
      bar.push('<button class="xs-btn" data-do="add">' + { sales: "+ Строка", products: "+ Товар", batches: "+ Партия" }[t] + "</button>");
      bar.push('<button class="xs-btn" data-do="del" disabled>' + (t === "products" ? "Убрать из планов" : "Удалить строки") + "</button>");
    }
    if (t === "sales" && a.upload) bar.push('<span class="xs-btn xs-file">Загрузить CSV<input type="file" accept=".csv,.txt,text/csv" data-ctl="upload" aria-label="Загрузить CSV"></span>');
    bar.push('<button class="xs-btn" data-do="csv">Скачать для Excel</button>');
    this.el.querySelector(".xs-bar").innerHTML = bar.join("");
    this.el.querySelector(".xs-hint").textContent = t === "week"
      ? "План считается сам по истории продаж, дню недели, погоде и сезону. Выделите ячейки и нажмите Ctrl+C, чтобы вставить в Excel."
      : HINT + (t === "sales" ? " Строка сохраняется, когда заполнены дата, товар и «продано»." : "");
    var o = {
      onError: function (m) { a.toast(m); }, onInfo: function (m) { a.toast(m); },
      onSelect: function (g) { var b = self.el.querySelector('[data-do="del"]'); if (b) b.disabled = !self.grid || g.r0 >= self.grid.rows.length; }
    };
    if (t === "sales") {
      o.label = "Продажи по дням"; o.columns = this.salesColumns(); o.rows = this.salesRows(); o.allowAdd = true;
      o.newRow = function () { return { day: a.today, product: self.product || null }; };
      o.onChange = function (rows) { self.saveSales(rows); };
      o.footer = function (rows) {
        var sum = function (k) { return rows.reduce(function (x, r) { return x + (r[k] || 0); }, 0); };
        var left = rows.reduce(function (x, r) { return x + (r.baked != null && r.sold != null && !r.soldout ? Math.max(0, r.baked - r.sold) : 0); }, 0);
        var n = rows.length, w = n % 10 === 1 && n % 100 !== 11 ? "строка" : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? "строки" : "строк";
        return ["Итого", n + " " + w, nf(sum("baked")), nf(sum("sold")), nf(left), "", "", ""];
      };
    } else if (t === "products") {
      o.label = "Товары"; o.columns = this.productColumns(); o.allowAdd = true;
      o.rows = this.data.products.slice().sort(function (x, y) { return x.name.localeCompare(y.name, "ru"); }).concat(this.drafts.products);
      o.onChange = function (rows) { self.saveProducts(rows); };
    } else if (t === "batches") {
      o.label = "Сроки годности"; o.columns = this.batchColumns(); o.allowAdd = true;
      o.rows = this.data.batches.slice().concat(this.drafts.batches);
      o.onChange = function (rows) { self.saveBatches(rows); };
    } else {
      o.label = "План на неделю"; o.columns = this.weekColumns(); o.rows = this.data.week.rows; o.empty = "Нет товаров для плана";
      o.footer = function (rows) {
        var f = ["Всего"];
        self.data.week.days.forEach(function (d, i) { f.push(nf(rows.reduce(function (x, r) { return x + r.qty[i]; }, 0))); });
        f.push(nf(rows.reduce(function (x, r) { return x + r.qty.reduce(function (p, q) { return p + q; }, 0); }, 0)));
        return f;
      };
    }
    o.afterEdit = function () { if (self.stale) { self.stale = false; self.show(); } };
    var prev = this.grid && this.shownTab === t ? this.grid : null, host = this.el.querySelector(".xs-grid");
    var keep = prev ? { sel: prev.sel, focus: host.contains(document.activeElement), sc: host.querySelector(".xg-scroll") } : null;
    var top = keep && keep.sc ? keep.sc.scrollTop : 0, left = keep && keep.sc ? keep.sc.scrollLeft : 0;
    this.cols = o.columns; this.shownTab = t;
    var gel = document.createElement("div");
    host.innerHTML = ""; host.appendChild(gel);
    this.grid = new Grid(gel, o);
    if (keep) {
      var n = Math.max(0, this.grid.count() - 1), m = o.columns.length - 1, s = keep.sel;
      this.grid.sel = { r: Math.min(s.r, n), c: Math.min(s.c, m), r2: Math.min(s.r2, n), c2: Math.min(s.c2, m) };
      var sc = gel.querySelector(".xg-scroll"); sc.scrollTop = top; sc.scrollLeft = left;
      this.grid.paintSel(false);
      if (keep.focus) this.grid.el.focus({ preventScroll: true });
    }
  };

  Sheets.prototype.onClick = function (e) {
    var b = e.target.closest("[data-sheet],[data-do]"); if (!b) return;
    if (b.hasAttribute("data-sheet")) { this.tab = b.getAttribute("data-sheet"); this.show(); return; }
    var d = b.getAttribute("data-do"), self = this, t = this.tab;
    if (d === "csv") {
      var names = { sales: "prodazhi", products: "tovary", batches: "sroki", week: "plan-na-nedelyu" };
      var rows = t === "sales" ? this.salesRows().filter(function (r) { return !r.__draft; }) : this.grid.rows.filter(function (r) { return !r.__draft; });
      Sheets.saveFile(names[t] + "-" + this.a.today + ".csv", csv(this.cols, rows), this.a.toast);
    } else if (d === "add") {
      var row = t === "sales" ? { day: this.a.today, product: this.product || null } : {};
      row.__draft = true;
      this.drafts[t].push(row);
      this.show();
      this.grid.focusNewRow();
    } else if (d === "del") {
      var sel = this.grid.selectedRows(); if (!sel.length) return;
      var drafts = sel.filter(function (r) { return r.__draft; }), saved = sel.filter(function (r) { return !r.__draft; });
      this.drafts[t] = this.drafts[t].filter(function (r) { return drafts.indexOf(r) < 0; });
      if (!saved.length) { this.show(); return; }
      var job;
      if (t === "sales") job = this.a.saveSales([], saved.map(function (r) { return { day: r.day, product: r.product }; }));
      else if (t === "products") job = chain(saved.map(function (p) { return function () { return self.a.removeProduct(p.name); }; }));
      else job = chain(saved.map(function (x) { return function () { return self.a.deleteBatch(x.id); }; }));
      job.then(function () {
        self.a.toast(t === "products" ? "Убрано из планов: " + saved.length + ". История продаж сохранена." : "Удалено строк: " + saved.length);
        return self.load();
      }).catch(function (x) { self.err(x); self.load(); });
    }
  };
  Sheets.prototype.onChangeCtl = function (e) {
    var c = e.target.getAttribute("data-ctl"), self = this; if (!c) return;
    if (c === "period") { this.period = e.target.value; this.show(); }
    else if (c === "product") { this.product = e.target.value; this.show(); }
    else if (c === "upload" && e.target.files && e.target.files[0]) {
      var rd = new FileReader();
      rd.onload = function () {
        var s;
        try { s = new TextDecoder("utf-8", { fatal: true }).decode(rd.result); } catch (x) { s = new TextDecoder("windows-1251").decode(rd.result); }
        self.a.upload(s).then(function (r) { self.a.toast("Загружено строк: " + (r && r.rows != null ? r.rows : "")); return self.load(); }).catch(function (x) { self.err(x); });
      };
      rd.readAsArrayBuffer(e.target.files[0]); e.target.value = "";
    }
  };

  function chain(jobs) { return jobs.reduce(function (p, f) { return p.then(f); }, Promise.resolve()); }
  function shift(iso, days) { var t = Date.parse(iso + "T00:00:00Z") + days * 864e5; return new Date(t).toISOString().slice(0, 10); }
  function clean(row, keys) { var o = {}; keys.forEach(function (k) { o[k] = row[k] == null ? null : row[k]; }); return o; }

  Sheets.prototype.saveSales = function (rows) {
    var self = this, up = [], del = [], keys = ["day", "product", "baked", "sold", "soldout", "rain", "temp"];
    var fail = null;
    rows.forEach(function (r) {
      var old = r.__old || {}; delete r.__old;
      if (r.day == null || !r.product || r.sold == null) {
        if (!r.__draft) { fail = "Дата, товар и «продано» должны быть заполнены"; return; }
        if (self.drafts.sales.indexOf(r) < 0) self.drafts.sales.push(r);
        return;
      }
      if (r.baked != null && r.sold > r.baked) { fail = r.product + ": продано больше, чем испечено"; return; }
      if (!r.__draft && (("day" in old && old.day !== r.day) || ("product" in old && old.product !== r.product))) {
        del.push({ day: "day" in old ? old.day : r.day, product: "product" in old ? old.product : r.product });
      }
      up.push(clean(r, keys));
    });
    if (fail) { this.a.toast(fail); return this.load(); }
    if (!up.length && !del.length) return;
    this.drafts.sales = this.drafts.sales.filter(function (r) { return rows.indexOf(r) < 0 || r.day == null || !r.product || r.sold == null; });
    return this.a.saveSales(up, del).then(function () { return self.load(); }).catch(function (x) { self.err(x); return self.load(); });
  };
  Sheets.prototype.saveProducts = function (rows) {
    var self = this, jobs = [];
    rows.forEach(function (p) {
      var old = p.__old || {}; delete p.__old;
      if (!p.name) { if (p.__draft && self.drafts.products.indexOf(p) < 0) self.drafts.products.push(p); return; }
      if (p.price != null && p.cost != null && p.cost > p.price) { self.a.toast(p.name + ": себестоимость больше цены"); return; }
      var oldName = !p.__draft && "name" in old && old.name !== p.name ? old.name : null;
      jobs.push(function () { return self.a.saveProduct(clean(p, ["name", "price", "cost", "shelf_life", "default_qty"]), oldName); });
      self.drafts.products = self.drafts.products.filter(function (x) { return x !== p; });
    });
    if (!jobs.length) return this.show();
    return chain(jobs).then(function () { return self.load(); }).catch(function (x) { self.err(x); return self.load(); });
  };
  Sheets.prototype.saveBatches = function (rows) {
    var self = this, jobs = [];
    rows.forEach(function (b) {
      delete b.__old;
      if (!b.item) { if (b.__draft && self.drafts.batches.indexOf(b) < 0) self.drafts.batches.push(b); return; }
      if (b.__draft) {
        var p = self.data.products.filter(function (x) { return x.name === b.item; })[0];
        if (!b.expires && !(p && p.shelf_life)) {
          self.a.toast("Укажите «годен до» — у товара «" + b.item + "» нет срока хранения");
          if (self.drafts.batches.indexOf(b) < 0) self.drafts.batches.push(b);
          return;
        }
        jobs.push(function () { return self.a.addBatch({ item: b.item, qty: b.qty || "", expires: b.expires || null, made: self.a.today }); });
        self.drafts.batches = self.drafts.batches.filter(function (x) { return x !== b; });
      } else {
        if (!b.expires) { self.a.toast("У партии должна быть дата «годен до»"); return; }
        jobs.push(function () { return self.a.editBatch({ id: b.id, item: b.item, qty: b.qty || "", expires: b.expires }); });
      }
    });
    if (!jobs.length) return this.show();
    return chain(jobs).then(function () { return self.load(); }).catch(function (x) { self.err(x); return self.load(); });
  };

  Sheets.saveFile = download;
  root.Sheets = Sheets;
  root.SheetGrid = Grid;
})(this);
