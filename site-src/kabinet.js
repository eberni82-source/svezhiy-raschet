(function () {
  "use strict";
  var M = window.FreshModel;
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function pad(n) { return ("0" + n).slice(-2); }
  function fmt(x) { return Math.round(x).toLocaleString("ru-RU"); }
  function money(x) { return fmt(x) + " ₽"; }
  function dayLabel(iso) { var n = M.dnum(iso); return M.WDS[M.wday(n)] + ", " + M.ddmm(n); }
  function mins(hhmm) { var p = String(hhmm || "0:0").split(":"); return (+p[0]) * 60 + (+p[1]); }
  function plural(n, a, b, c) { n = Math.abs(n) % 100; if (n > 10 && n < 15) return c; n %= 10; return n === 1 ? a : n >= 2 && n <= 4 ? b : c; }
  function phaseOf(times, m) {
    if (m < mins(times.morning)) return "night";
    if (m < mins(times.evening)) return "morning";
    if (m < mins(times.closing)) return "evening";
    return "closing";
  }
  var PART_OF = { night: "plan", morning: "plan", evening: "vitrina", closing: "itogi" };
  var SLOTS = [["до 12", "11:00"], ["12–15", "13:30"], ["15–18", "16:30"], ["после 18", "19:30"]];
  var toastT = null;
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(function () { t.hidden = true; }, 2600);
  }
  var Local = (function () {
    var KEY = "svezhiy-kabinet-v2", D = null, btCache = null, W = {};
    function today() { var n = new Date(); return M.dnum(n.getFullYear() + "-" + pad(n.getMonth() + 1) + "-" + pad(n.getDate())); }
    function save() { try { localStorage.setItem(KEY, JSON.stringify(D)); } catch (e) { /* без localStorage работаем в памяти */ } }
    function example() {
      var r = DEMO_CSV ? M.parseCSV(DEMO_CSV) : { sales: [], products: {} }, T = today();
      var last = Math.max.apply(null, r.sales.map(function (s) { return s.day; }));
      r.sales.forEach(function (s) { s.day += (T - 1) - last; });
      var products = {};
      Object.keys(r.products).forEach(function (k) {
        var p = r.products[k];
        products[k] = { name: k, price: p.price, cost: p.cost, shelf_life: (k === "Эклер" || k === "Сэндвич с курицей") ? 2 : 1, default_qty: null, active: true };
      });
      D = { v: 2, business: "Условная пекарня", example: true, times: { morning: "06:00", evening: "17:00", closing: "21:00" },
            products: products, sales: r.sales, plans: {}, events: {}, nextId: 5,
            batches: [[1, "Сливки 33%", "2 л", 1], [2, "Творог 5%", "3 кг", 2], [3, "Филе куриное", "1,5 кг", 0], [4, "Вишня замороженная", "2 кг", 20]]
              .map(function (b) { return { id: b[0], item: b[1], qty: b[2], exp: T + b[3], status: "active" }; }) };
      btCache = null; save();
    }
    function load() {
      try { var raw = localStorage.getItem(KEY); if (raw) { var d = JSON.parse(raw); if (d && d.v === 2) D = d; } } catch (e) { /* нет доступа */ }
      if (!D) example();
      // пример в Челябинске, берём реальный прогноз
      M.loadWeather(55.16, 61.44, 3).then(function (w) { W = w; if (Object.keys(w).length && S.st) refresh(true); });
    }
    function weather(day) { return (D.example && W[day]) || { rain: null, temp: null }; }
    function prodMap(includeSalesOnly) {
      var out = {}, removed = {};
      Object.keys(D.products).forEach(function (k) { var p = D.products[k]; if (p.active) out[k] = p; else removed[k] = 1; });
      if (includeSalesOnly) D.sales.forEach(function (s) { if (!out[s.product] && !removed[s.product]) out[s.product] = { name: s.product, price: null, cost: null }; });
      return out;
    }
    function recs(day) {
      var prods = prodMap(true), eff = M.rainEffect(D.sales).eff, ev = D.events[day] || 1, w = weather(day), list = [], habit = {};
      Object.keys(prods).forEach(function (k) { var r = M.forecast(D.sales, prods[k], day, w.rain === true, ev, eff, w.temp); if (r.n > 0 || r.qty > 0) list.push(r); });
      list.sort(function (a, b) { return b.qty - a.qty; });
      list.forEach(function (r) {
        var same = D.sales.filter(function (s) { return s.product === r.product && s.baked != null && M.wday(s.day) === M.wday(day) && day - s.day > 0 && day - s.day <= 28; });
        if (same.length) habit[r.product] = same.reduce(function (a, s) { return a + s.baked; }, 0) / same.length;
      });
      return { recs: list, eff: eff, event: ev, habit: habit };
    }
    function item(r, qty, mean) {
      var q = qty == null ? r.qty : qty, m = mean == null ? r.mean : mean;
      return { product: r.product, qty: q, mean: m, sd: r.sd, reasons: r.reasons, "new": r.n === 0, threshold: M.eveningThreshold({ mean: m }) };
    }
    function sale(day, product) { for (var i = 0; i < D.sales.length; i++) if (D.sales[i].day === day && D.sales[i].product === product) return D.sales[i]; return null; }
    function state() {
      var now = new Date(), T = today(), m = now.getHours() * 60 + now.getMinutes();
      Object.keys(D.plans).forEach(function (k) { if (+k < T - 14) delete D.plans[k]; });
      var rt = recs(T), plan = D.plans[T] || (D.plans[T] = {}), changed = false;
      rt.recs.forEach(function (r) { if (!plan[r.product]) { plan[r.product] = { qty: r.qty, mean: r.mean }; changed = true; } });
      if (changed) save();
      var items = rt.recs.map(function (r) {
        var p = plan[r.product], it = item(r, p.qty, p.mean), s = sale(T, r.product);
        it.record = s ? { soldout: s.soldout, baked: s.baked, left: s.soldout ? null : Math.max(0, Math.round((s.baked || 0) - s.sold)) } : null;
        return it;
      }).sort(function (a, b) { return b.qty - a.qty; });
      var rm = recs(T + 1);
      var tom = rm.recs.map(function (r) { var it = item(r); it.habit = rm.habit[r.product] != null ? Math.round(rm.habit[r.product]) : null; return it; });
      var hist = {};
      D.sales.forEach(function (s) { (hist[s.product] = hist[s.product] || {})[s.day] = 1; });
      var pm = prodMap(true);
      var products = Object.keys(pm).map(function (k) {
        var p = pm[k];
        return { name: k, price: p.price, cost: p.cost, shelf_life: p.shelf_life == null ? null : p.shelf_life, default_qty: p.default_qty == null ? null : p.default_qty, history_days: hist[k] ? Object.keys(hist[k]).length : 0 };
      }).sort(function (a, b) { return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1; });
      var removed = Object.keys(D.products).filter(function (k) { return !D.products[k].active; }).sort();
      var batches = D.batches.filter(function (b) { return b.status === "active"; }).sort(function (a, b) { return a.exp - b.exp || a.id - b.id; }).map(function (b) {
        var st = M.expiryStatus(b.exp, T);
        return { id: b.id, item: b.item, qty: b.qty, expires: M.diso(b.exp), label: st.label, left: st.left, level: st.left <= 0 ? "bad" : st.left <= 2 ? "warn" : "ok",
                 tip: st.left <= 1 ? (st.left < 0 ? "спишите" : M.expiryTip(b.item)) : null };
      });
      var days = D.sales.map(function (s) { return s.day; });
      if (!btCache || btCache.n !== D.sales.length) {
        var bt = D.sales.length ? M.backtest(D.sales, prodMap(false)) : null;
        btCache = { n: D.sales.length, v: bt && bt.wasteNow > 0 ? { days: bt.days, waste_now: bt.wasteNow, waste_new: bt.wasteNew, lost_now: bt.lostNow, lost_new: bt.lostNew,
          share_now: bt.wasteUnitsNow / bt.producedNow * 100, share_new: bt.wasteUnitsNew / bt.producedNew * 100 } : null };
      }
      return {
        mode: "local", business: { name: D.business, city: D.example ? "Челябинск" : "", is_example: D.example },
        now: M.diso(T) + "T" + pad(now.getHours()) + ":" + pad(now.getMinutes()), phase: phaseOf(D.times, m), times: D.times,
        today: { date: M.diso(T), rain: weather(T).rain, temp: weather(T).temp, event: D.events[T] || 1, items: items },
        tomorrow: { date: M.diso(T + 1), rain: weather(T + 1).rain, temp: weather(T + 1).temp, event: D.events[T + 1] || 1, rain_effect: rm.eff, items: tom },
        products: products, removed: removed, batches: batches,
        data: { rows: D.sales.length, first: days.length ? M.diso(Math.min.apply(null, days)) : null, last: days.length ? M.diso(Math.max.apply(null, days)) : null },
        summary: btCache.v
      };
    }
    function num(v, lo, hi, integer) {
      if (v === "" || v == null) return null;
      var x = Number(String(v).replace(",", ".").replace(/\s/g, ""));
      if (isNaN(x) || (lo != null && x < lo) || (hi != null && x > hi)) throw new Error("проверьте число: " + v);
      return integer ? Math.round(x) : x;
    }
    function name(v, field) { var s = String(v || "").replace(/\s+/g, " ").trim(); if (!s) throw new Error("пустое поле: " + (field || "название")); return s.slice(0, 60); }
    function act(path, b) {
      var T = today();
      try {
        if (path === "/api/product") {
          var nm = name(b.name), old = b.old_name || null, price = num(b.price, 0, 1e6), cost = num(b.cost, 0, 1e6);
          if (price != null && cost != null && cost > price) throw new Error("себестоимость больше цены — проверьте цифры");
          if (old && old !== nm) {
            D.sales.forEach(function (s) { if (s.product === old) s.product = nm; });
            Object.keys(D.plans).forEach(function (d) { if (D.plans[d][old]) { D.plans[d][nm] = D.plans[d][old]; delete D.plans[d][old]; } });
            delete D.products[old]; btCache = null;
          }
          D.products[nm] = { name: nm, price: price, cost: cost, shelf_life: num(b.shelf_life, 0, 3650, true), default_qty: num(b.default_qty, 0, 1e5), active: true };
        } else if (path === "/api/product/remove" || path === "/api/product/restore") {
          var n2 = name(b.name), on = path === "/api/product/restore";
          D.products[n2] = D.products[n2] || { name: n2, price: null, cost: null, shelf_life: null, default_qty: null };
          D.products[n2].active = on;
          if (!on) [T - 1, T, T + 1].forEach(function (d) { if (D.plans[d]) delete D.plans[d][n2]; });
        } else if (path === "/api/batch") {
          var it = name(b.item, "продукт"), exp = b.expires ? M.dnum(b.expires) : null;
          if (exp == null) {
            var p = D.products[it];
            if (!p || !p.shelf_life) throw new Error("укажите дату «годен до» или срок хранения у товара");
            exp = M.dnum(b.made || M.diso(T)) + p.shelf_life;
          }
          D.batches.push({ id: D.nextId++, item: it, qty: String(b.qty || "").trim().slice(0, 20), exp: exp, status: "active" });
        } else if (path === "/api/batch/close") {
          D.batches.forEach(function (x) { if (x.id === +b.id) x.status = b.status; });
        } else if (path === "/api/record") {
          var pr = name(b.product), so = b.soldout || null;
          if (so && !/^([01]?\d|2[0-3]):[0-5]\d$/.test(so)) throw new Error("время в формате 15:30");
          var left = so ? null : num(b.left, 0, 1e5, true);
          if (!so && left == null) throw new Error("укажите остаток или время, когда товар закончился");
          var baked = (D.plans[T] && D.plans[T][pr]) ? D.plans[T][pr].qty : 0;
          var s = sale(T, pr), sold = so ? baked : Math.max(0, baked - left);
          if (s) { s.sold = sold; s.baked = baked; s.soldout = so; } else D.sales.push({ day: T, product: pr, sold: sold, baked: baked, soldout: so, rain: weather(T).rain, temp: weather(T).temp });
          btCache = null;
        } else if (path === "/api/event") {
          var f = num(b.factor, 0.3, 3);
          if (!f || Math.abs(f - 1) < 1e-9) delete D.events[T + 1]; else D.events[T + 1] = f;
        } else if (path === "/api/settings") {
          if ("business" in b) D.business = String(b.business || "").trim().slice(0, 80);
          ["morning", "evening", "closing"].forEach(function (k) {
            if (b[k]) { if (!/^([01]?\d|2[0-3]):[0-5]\d$/.test(b[k])) throw new Error("время в формате 06:00"); D.times[k] = pad(+b[k].split(":")[0]) + ":" + b[k].split(":")[1]; }
          });
        } else if (path === "/api/upload") {
          var r = M.parseCSV(String(b.csv || ""));
          if (D.example) { D.sales = []; D.products = {}; D.plans = {}; D.events = {}; D.batches = []; D.example = false; D.business = ""; }
          var idx = {};
          D.sales.forEach(function (x, i) { idx[x.day + "|" + x.product] = i; });
          r.sales.forEach(function (x) { var k = x.day + "|" + x.product; if (k in idx) D.sales[idx[k]] = x; else { idx[k] = D.sales.length; D.sales.push(x); } });
          Object.keys(r.products).forEach(function (k) {
            var p = r.products[k], cur = D.products[k] || { name: k, shelf_life: null, default_qty: null };
            cur.price = p.price != null ? p.price : cur.price; cur.cost = p.cost != null ? p.cost : cur.cost; cur.active = true; D.products[k] = cur;
          });
          btCache = null; save();
          return Promise.resolve({ ok: true, rows: r.sales.length, products: Object.keys(r.products).length });
        } else if (path === "/api/example") {
          example();
        } else {
          throw new Error("неизвестное действие");
        }
      } catch (e) { return Promise.reject(e); }
      save();
      return Promise.resolve({ ok: true });
    }
    return { load: load, state: function () { return Promise.resolve(state()); }, act: act, reset: example };
  })();
  function LoginNeeded() { this.login = true; }
  var API = {
    mode: null,
    init: function () {
      var self = this;
      if (location.protocol === "file:") { self.mode = "local"; Local.load(); return Promise.resolve(); }
      return fetch("api/ping", { cache: "no-store" }).then(function (r) { return r.ok ? r.json() : null; })
        .catch(function () { return null; })
        .then(function (j) { if (j && j.app === "svezhiy") self.mode = "server"; else { self.mode = "local"; Local.load(); } });
    },
    state: function () {
      if (this.mode === "local") return Local.state();
      return fetch("api/state", { cache: "no-store", credentials: "same-origin" }).then(function (r) {
        if (r.status === 401) throw new LoginNeeded();
        if (!r.ok) throw new Error("Сервер ответил " + r.status);
        return r.json();
      });
    },
    post: function (path, body) {
      if (this.mode === "local") return Local.act(path, body || {});
      return fetch(path.slice(1), { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) })
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            if (r.status === 401) throw new LoginNeeded();
            if (!r.ok) throw new Error(j.error || ("Ошибка " + r.status));
            return j;
          });
        });
    }
  };
  var S = { st: null, tab: "today", part: null, partManual: false, offset: 0, fetchedAt: 0, edit: null, confirm: null, vitrina: {}, busy: false };
  function nowMinutes() {
    var d = new Date(Date.now() + S.offset);
    return { m: d.getHours() * 60 + d.getMinutes(), label: pad(d.getHours()) + ":" + pad(d.getMinutes()) };
  }
  function serverOffset(nowStr) {
    var p = nowStr.split("T"), d = p[0].split("-"), t = p[1].split(":");
    var server = new Date(+d[0], +d[1] - 1, +d[2], +t[0], +t[1]).getTime();
    return server - Date.now();
  }
  function renderTop() {
    var st = S.st, n = nowMinutes();
    $("biz").textContent = st.business.name ? st.business.name + (st.business.city ? ", " + st.business.city : "") : "Личный кабинет";
    $("clock").textContent = n.label + " · " + dayLabel(st.today.date);
    $("top-actions").innerHTML = API.mode === "server"
      ? '<button class="btn btn-ghost btn-sm" data-act="logout">Выйти</button>'
      : '<button class="btn btn-ghost btn-sm" data-act="reset">Сбросить пример</button>';
    $("brand").setAttribute("href", API.mode === "server" ? "#" : "index.html");
  }

  function renderDay() {
    var st = S.st, t = st.times, n = nowMinutes(), ph = phaseOf(t, n.m);
    var a = mins(t.morning), b = mins(t.evening), c = mins(t.closing), end = Math.min(c + 120, 24 * 60);
    var spans = [Math.max(b - a, 30), Math.max(c - b, 30), Math.max(end - c, 30)];
    var cur = PART_OF[ph], heads = {
      night: "Ночь: план на сегодня уже готов", morning: "Утро и день: работаем по плану",
      evening: "Вечер: проверьте витрину", closing: "Конец дня: отметьте итоги" };
    var parts = [["plan", "План на день", t.morning, "сколько сделать и заказать"],
                 ["vitrina", "Витрина и скидки", t.evening, "что уценить, чтобы не выбросить"],
                 ["itogi", "Итоги дня", t.closing, "минута на отметки"]];
    var idxNow = { plan: 0, vitrina: 1, itogi: 2 }[cur];
    var pct = Math.max(0, Math.min(100, (n.m - a) / (end - a) * 100));
    var h = '<div class="day-head"><h1>' + esc(heads[ph]) + '</h1><span class="upd"><span class="pulse" aria-hidden="true"></span>обновляется автоматически · ' + esc(n.label) + "</span></div>";
    h += '<div class="ribbon" style="--cols:' + spans.map(function (s) { return s + "fr"; }).join(" ") + '">';
    parts.forEach(function (p, i) {
      var cls = "seg" + (i === idxNow && ph !== "night" ? " now" : i < idxNow ? " past" : "");
      h += '<button class="' + cls + '" data-part="' + p[0] + '" aria-pressed="' + (S.part === p[0]) + '"><span class="t">' + p[2] + "</span>" +
           (i === idxNow && ph !== "night" ? '<span class="now-tag">сейчас</span>' : "") + "<b>" + p[1] + '</b><span class="desc">' + p[3] + "</span></button>";
    });
    h += '</div><div class="track" aria-hidden="true"><i style="width:' + pct.toFixed(1) + '%"></i><span style="left:' + pct.toFixed(1) + '%"></span></div>';
    $("day").innerHTML = h;
  }

  function expiringCount() { return S.st.batches.filter(function (b) { return b.left <= 1; }).length; }

  function renderTabs() {
    document.querySelectorAll(".tabs button").forEach(function (b) {
      var t = b.getAttribute("data-tab");
      b.setAttribute("aria-selected", String(S.tab === t));
      if (t === "batches") { var n = expiringCount(); b.innerHTML = "Сроки годности" + (n ? '<span class="cnt">' + n + "</span>" : ""); }
    });
  }

  function whyText(it) { return it.reasons.filter(function (x) { return x.indexOf("дождь") !== 0 && x.indexOf("событие") !== 0; }).slice(0, 2).join("; "); }

  function viewPlan() {
    var st = S.st, items = st.today.items, total = items.reduce(function (a, i) { return a + i.qty; }, 0), exp = st.batches.filter(function (b) { return b.left <= 1; });
    var h = '<div class="kpis"><div class="kpi"><b class="num">' + total + '</b><span>штук сделать сегодня</span></div>' +
      '<div class="kpi"><b class="num">' + items.length + "</b><span>" + plural(items.length, "позиция", "позиции", "позиций") + " в плане</span></div>" +
      '<div class="kpi' + (exp.length ? " bad" : "") + '"><b class="num">' + exp.length + "</b><span>" + plural(exp.length, "партия истекает", "партии истекают", "партий истекают") + " сегодня–завтра</span></div>" +
      '<div class="kpi"><b>' + (st.today.event !== 1 ? M.signed((st.today.event - 1) * 100) : st.today.rain ? "дождь" : "обычный") + "</b><span>" +
      (st.today.event !== 1 ? "событие сегодня" : st.today.rain ? "спрос ниже обычного" : "день по прогнозу") + "</span></div></div>";
    if (!items.length) return h + emptyData();
    h += '<div class="card"><h2>План на ' + dayLabel(st.today.date) + '</h2><p class="sub">Количество зафиксировано утром. Новые товары появляются в плане сразу после добавления.</p><div class="table-wrap"><table class="t"><thead><tr><th>Позиция</th><th class="r">Спрос</th><th class="r">Сделать</th></tr></thead><tbody>';
    items.forEach(function (it) {
      var w = whyText(it);
      h += "<tr><td><div class=\"name\">" + esc(it.product) + (it["new"] ? ' <span class="chip new">новый</span>' : "") + "</div>" + (w ? '<div class="why">' + esc(w) + "</div>" : "") +
        '</td><td class="r num">≈' + Math.round(it.mean) + ' <span class="muted">±' + Math.round(it.sd) + '</span></td><td class="r"><span class="big">' + it.qty + "</span></td></tr>";
    });
    h += '</tbody><tfoot><tr><td>Всего</td><td></td><td class="r"><span class="big">' + total + "</span></td></tr></tfoot></table></div></div>";
    if (exp.length) {
      h += '<div class="card"><h2>Пора пустить в работу</h2><p class="sub">Партии, у которых срок заканчивается сегодня или завтра.</p>' + batchRows(exp, true) + "</div>";
    }
    return h;
  }

  function viewVitrina() {
    var items = S.st.today.items;
    if (!items.length) return emptyData();
    var h = '<div class="card"><h2>Проверьте витрину</h2><p class="sub">После ' + esc(S.st.times.evening) + " обычно уходит около пятой части дневных продаж. Если на витрине больше порога — ставьте скидку, иначе остаток уйдёт в списание. Впишите, сколько сейчас лежит, и получите совет.</p>" +
      '<div class="table-wrap"><table class="t"><thead><tr><th>Позиция</th><th class="r">Сделали</th><th class="r">Скидка, если больше</th><th>Сейчас на витрине</th><th>Совет</th></tr></thead><tbody>';
    items.slice().sort(function (a, b) { return a.product < b.product ? -1 : 1; }).forEach(function (it) {
      var v = S.vitrina[it.product], adv = "";
      if (v !== undefined && v !== "") {
        var a = M.discountAdvice({ mean: it.mean }, +v);
        adv = a.pct ? '<span class="chip warn">скидка ' + a.pct + "%</span>" : '<span class="chip ok">без скидки</span>';
      }
      h += "<tr><td class=\"name\">" + esc(it.product) + '</td><td class="r num">' + it.qty + '</td><td class="r num"><b>' + it.threshold + " шт</b></td>" +
        '<td><input class="small" type="number" min="0" inputmode="numeric" data-vitrina="' + esc(it.product) + '" value="' + esc(v == null ? "" : v) + '" aria-label="Сейчас на витрине: ' + esc(it.product) + '"></td><td data-adv="' + esc(it.product) + '">' + adv + "</td></tr>";
    });
    return h + "</tbody></table></div></div>";
  }

  function viewItogi() {
    var items = S.st.today.items;
    if (!items.length) return emptyData();
    var done = items.filter(function (i) { return i.record; }).length;
    var h = '<div class="card"><h2>Итоги дня · ' + dayLabel(S.st.today.date) + '</h2><p class="sub">Отметьте по каждой позиции, когда она закончилась или сколько осталось. По этим отметкам сервис узнаёт реальный спрос и завтра считает точнее. То же можно сделать в боте.</p>' +
      '<p class="hint">Отмечено ' + done + " из " + items.length + '</p><div class="progress"><i style="width:' + (done / items.length * 100).toFixed(0) + '%"></i></div>' +
      '<div class="table-wrap" style="margin-top:14px"><table class="t"><thead><tr><th>Позиция</th><th class="r">Сделали</th><th>Закончилась</th><th>Или осталось</th><th>Отметка</th></tr></thead><tbody>';
    items.slice().sort(function (a, b) { return a.product < b.product ? -1 : 1; }).forEach(function (it) {
      var r = it.record, mark = !r ? '<span class="chip mute">нет</span>' : r.soldout ? '<span class="chip ok">✓ кончились ' + esc(r.soldout) + "</span>" : '<span class="chip ok">✓ осталось ' + r.left + "</span>";
      h += '<tr><td class="name">' + esc(it.product) + '</td><td class="r num">' + it.qty + '</td><td><div class="slots">' +
        SLOTS.map(function (s) { return '<button class="slot" data-act="soldout" data-p="' + esc(it.product) + '" data-t="' + s[1] + '">' + s[0] + "</button>"; }).join("") +
        '</div></td><td><div class="left-in"><input type="number" min="0" inputmode="numeric" data-left="' + esc(it.product) + '" aria-label="Осталось: ' + esc(it.product) + '"><button class="btn btn-quiet btn-sm" data-act="left" data-p="' + esc(it.product) + '">OK</button></div></td><td>' + mark + "</td></tr>";
    });
    return h + "</tbody></table></div></div>";
  }

  function emptyData() {
    return '<div class="empty">Пока нет товаров и продаж. Добавьте товары на вкладке «Товары» или загрузите выгрузку продаж в «Данные и настройки».</div>';
  }

  function viewTomorrow() {
    var t = S.st.tomorrow, items = t.items, total = 0, habit = 0, hasH = false;
    var parts = [];
    if (t.rain === true) parts.push("Ожидается дождь: спрос ниже на " + Math.abs(Math.round(t.rain_effect * 100)) + "%");
    else if (t.rain === false) parts.push("Без осадков");
    if (t.temp != null) parts.push(M.deg(t.temp));
    var weather = parts.length ? parts.join(" · ") : "Прогноз погоды пока недоступен";
    var opts = [[1, "Обычный день"], [1.1, "Небольшое событие +10%"], [1.2, "Праздник, ярмарка +20%"], [1.3, "Большое событие +30%"], [0.8, "Меньше людей −20%"]];
    var h = '<div class="card"><div class="day-head" style="margin:0 0 10px"><div><h2>Завтра, ' + dayLabel(t.date) + '</h2><p class="sub" style="margin:0">' + esc(weather) + '</p></div>' +
      '<label style="display:grid;gap:4px;font-size:12.5px;color:var(--ink-3);min-width:220px">Событие завтра<select data-act="event">' +
      opts.map(function (o) { return '<option value="' + o[0] + '"' + (Math.abs(o[0] - t.event) < 1e-9 ? " selected" : "") + ">" + o[1] + "</option>"; }).join("") + "</select></label></div>";
    if (!items.length) return h + emptyData() + "</div>";
    h += '<div class="table-wrap"><table class="t"><thead><tr><th>Позиция</th><th class="r">Спрос</th><th class="r">Сделать</th><th class="r">Обычно</th></tr></thead><tbody>';
    items.forEach(function (it) {
      total += it.qty; if (it.habit != null) { habit += it.habit; hasH = true; } else habit += it.qty;
      var w = whyText(it);
      h += "<tr><td><div class=\"name\">" + esc(it.product) + (it["new"] ? ' <span class="chip new">новый</span>' : "") + "</div>" + (w ? '<div class="why">' + esc(w) + "</div>" : "") +
        '</td><td class="r num">≈' + Math.round(it.mean) + '</td><td class="r"><span class="big">' + it.qty + '</span></td><td class="r num muted">' + (it.habit != null ? it.habit : "—") + "</td></tr>";
    });
    h += '</tbody><tfoot><tr><td>Всего</td><td></td><td class="r"><span class="big">' + total + '</span></td><td class="r num">' + (hasH ? Math.round(habit) : "—") + "</td></tr></tfoot></table></div>" +
      '<p class="hint" style="margin-top:10px">«Обычно» — сколько в среднем делали в этот день недели за 4 недели. Утром в ' + esc(S.st.times.morning) + " план зафиксируется и придёт в Telegram.</p></div>";
    return h;
  }

  function productForm(p) {
    p = p || {};
    return '<label class="wide">Название<input type="text" name="name" maxlength="60" required value="' + esc(p.name || "") + '" placeholder="Например, Салат «Оливье»"></label>' +
      '<label>Цена, ₽<input type="number" name="price" min="0" step="any" value="' + esc(p.price == null ? "" : p.price) + '"></label>' +
      '<label>Себестоимость, ₽<input type="number" name="cost" min="0" step="any" value="' + esc(p.cost == null ? "" : p.cost) + '"></label>' +
      '<label>Срок хранения, дней<input type="number" name="shelf_life" min="0" step="1" value="' + esc(p.shelf_life == null ? "" : p.shelf_life) + '"></label>' +
      '<label>Обычно в день, шт<input type="number" name="default_qty" min="0" step="any" value="' + esc(p.default_qty == null ? "" : p.default_qty) + '"></label>';
  }

  function viewProducts() {
    var st = S.st;
    var h = '<div class="card"><h2>Добавить товар</h2><p class="sub">Цена и себестоимость нужны, чтобы понимать, что дороже: остаток или пустая полка. Срок хранения подставляет дату «годен до» в партиях. «Обычно в день» — для нового товара без истории продаж: план будет таким, пока не накопятся продажи.</p>' +
      '<form class="form" data-form="product-add">' + productForm() + '<button class="btn btn-primary" type="submit">Добавить</button></form></div>';
    h += '<div class="card"><h2>Товары · ' + st.products.length + '</h2><p class="sub">Убранный товар исчезает из планов, но его история продаж сохраняется — его можно вернуть.</p>';
    if (!st.products.length) h += '<div class="empty">Товаров пока нет.</div>';
    else {
      h += '<div class="table-wrap"><table class="t"><thead><tr><th>Товар</th><th class="r">Цена</th><th class="r">Себест.</th><th class="r">Маржа</th><th class="r">Срок</th><th>История</th><th></th></tr></thead><tbody>';
      st.products.forEach(function (p) {
        if (S.edit === p.name) {
          h += '<tr><td colspan="7"><form class="form" data-form="product-edit" data-old="' + esc(p.name) + '">' + productForm(p) +
            '<div class="acts"><button class="btn btn-primary btn-sm" type="submit">Сохранить</button><button class="btn btn-quiet btn-sm" type="button" data-act="edit-cancel">Отмена</button></div></form></td></tr>';
          return;
        }
        var margin = p.price && p.cost != null ? Math.round((p.price - p.cost) / p.price * 100) + "%" : "—";
        var acts = S.confirm === p.name
          ? '<span class="hint">Убрать из планов?</span><button class="btn btn-danger btn-sm" data-act="remove" data-p="' + esc(p.name) + '">Да, убрать</button><button class="btn btn-quiet btn-sm" data-act="confirm-cancel">Нет</button>'
          : '<button class="btn btn-quiet btn-sm" data-act="edit" data-p="' + esc(p.name) + '">Изменить</button><button class="btn btn-quiet btn-sm" data-act="ask-remove" data-p="' + esc(p.name) + '">Убрать</button>';
        h += '<tr><td class="name">' + esc(p.name) + '</td><td class="r num">' + (p.price != null ? fmt(p.price) : "—") + '</td><td class="r num">' + (p.cost != null ? fmt(p.cost) : "—") +
          '</td><td class="r num">' + margin + '</td><td class="r num">' + (p.shelf_life != null ? p.shelf_life + " дн." : "—") + "</td><td>" +
          (p.history_days ? '<span class="chip mute">' + p.history_days + " дн. продаж</span>" : '<span class="chip new">новый' + (p.default_qty ? ", " + fmt(p.default_qty) + " шт/день" : "") + "</span>") +
          '</td><td><div class="acts">' + acts + "</div></td></tr>";
      });
      h += "</tbody></table></div>";
    }
    if (st.removed.length) {
      h += '<div class="removed" style="margin-top:14px"><span class="hint">Убранные:</span>' + st.removed.map(function (n) {
        return '<span class="chip mute">' + esc(n) + '</span><button class="btn btn-quiet btn-sm" data-act="restore" data-p="' + esc(n) + '">Вернуть</button>';
      }).join("") + "</div>";
    }
    return h + "</div>";
  }

  function batchRows(list, compact) {
    if (!list.length) return '<div class="empty">Партий нет. Добавьте продукт, количество и срок годности.</div>';
    var glyph = { ok: "●", warn: "▲", bad: "✕" };
    var h = '<div class="table-wrap"><table class="t"><thead><tr><th>Продукт</th><th>Количество</th><th>Годен до</th><th>Статус</th>' + (compact ? "" : "<th></th>") + "</tr></thead><tbody>";
    list.forEach(function (b) {
      h += '<tr><td><div class="name">' + esc(b.item) + "</div>" + (b.tip ? '<div class="why">→ ' + esc(b.tip) + "</div>" : "") + "</td><td>" + esc(b.qty || "—") +
        '</td><td class="num">' + dayLabel(b.expires) + '</td><td><span class="chip ' + b.level + '">' + glyph[b.level] + " " + esc(b.label) + "</span></td>" +
        (compact ? "" : '<td><div class="acts"><button class="btn btn-quiet btn-sm" data-act="batch" data-id="' + b.id + '" data-s="used">Использовано</button><button class="btn btn-quiet btn-sm" data-act="batch" data-id="' + b.id + '" data-s="written_off">Списано</button><button class="btn btn-quiet btn-sm" data-act="batch" data-id="' + b.id + '" data-s="deleted" aria-label="Удалить запись">Удалить</button></div></td>') + "</tr>";
    });
    return h + "</tbody></table></div>";
  }

  function viewBatches() {
    var st = S.st, today = st.today.date;
    var h = '<div class="card"><h2>Добавить партию</h2><p class="sub">Выберите товар со сроком хранения — дата «годен до» посчитается сама. Для сырья (сливки, творог) укажите дату с упаковки.</p>' +
      '<form class="form" data-form="batch-add"><label class="wide">Продукт<input type="text" name="item" list="prodlist" maxlength="60" required placeholder="Сливки 33% или товар из списка"></label>' +
      '<label>Количество<input type="text" name="qty" maxlength="20" placeholder="2 л, 20 шт"></label>' +
      '<label>Изготовлено / поставлено<input type="date" name="made" value="' + today + '"></label>' +
      '<label>Годен до<input type="date" name="expires"></label><button class="btn btn-primary" type="submit">Добавить</button></form>' +
      '<datalist id="prodlist">' + st.products.map(function (p) { return '<option value="' + esc(p.name) + '">' + (p.shelf_life ? "срок " + p.shelf_life + " дн." : "") + "</option>"; }).join("") + "</datalist></div>";
    h += '<div class="card"><h2>Партии и сроки · ' + st.batches.length + '</h2><p class="sub">За день до конца срока сервис напомнит в Telegram и подскажет, куда пустить продукт.</p>' + batchRows(st.batches, false) + "</div>";
    return h;
  }

  function viewData() {
    var st = S.st, d = st.data, sm = st.summary;
    var h = '<div class="grid2"><div class="card"><h2>Заведение и расписание</h2><p class="sub">Время рассылок в Telegram и смены частей дня в кабинете.</p>' +
      '<form class="form" data-form="settings"><label class="wide">Название<input type="text" name="business" maxlength="80" value="' + esc(st.business.name) + '" placeholder="Булочная на Ленина"></label>' +
      '<label>План утром<input type="time" name="morning" value="' + esc(st.times.morning) + '"></label><label>Витрина вечером<input type="time" name="evening" value="' + esc(st.times.evening) + '"></label>' +
      '<label>Итоги дня<input type="time" name="closing" value="' + esc(st.times.closing) + '"></label><button class="btn btn-primary" type="submit">Сохранить</button></form></div>';
    h += '<div class="card"><h2>Данные продаж</h2><p class="sub">' + (d.rows ? d.rows + " " + plural(d.rows, "строка", "строки", "строк") + " с " + dayLabel(d.first) + " по " + dayLabel(d.last) + (st.business.is_example ? ". Сейчас загружен пример — условная пекарня." : ".") : "Продаж пока нет.") + "</p>" +
      '<div class="form"><label class="wide">Выгрузка из кассы или Excel (CSV)<input type="file" accept=".csv,.txt,text/csv" data-act="upload"></label></div>' +
      '<p class="hint" style="margin-top:8px">Колонки: <span class="mono">дата;товар;продано</span>, по желанию <span class="mono">испечено (или заказано);закончилось;цена;себестоимость;дождь;температура</span>. Свои данные заменят пример.</p>' +
      '<div class="acts" style="justify-content:flex-start;margin-top:12px">' + (S.confirm === "__example"
        ? '<span class="hint">Заменить все данные примером?</span><button class="btn btn-danger btn-sm" data-act="example">Да, загрузить пример</button><button class="btn btn-quiet btn-sm" data-act="confirm-cancel">Нет</button>'
        : '<button class="btn btn-quiet btn-sm" data-act="ask-example">Загрузить пример пекарни</button>') + "</div></div></div>";
    if (sm) {
      var max = Math.max(sm.waste_now + sm.lost_now, sm.waste_new + sm.lost_new);
      var row = function (label, a, b) {
        return '<div style="margin-top:14px"><b>' + label + ' <span class="hint">' + M.signed((b / a - 1) * 100) + '</span></b>' +
          '<div class="cmp"><span>Как было</span><div class="bar"><i style="width:' + (a / max * 100).toFixed(1) + '%;background:var(--ink-3)"></i></div><span class="v">' + money(a) + "</span></div>" +
          '<div class="cmp"><span>По плану</span><div class="bar"><i style="width:' + (b / max * 100).toFixed(1) + '%;background:var(--stamp)"></i></div><span class="v">' + money(b) + "</span></div></div>";
      };
      h += '<div class="card"><h2>Проверка на истории</h2><p class="sub">Что было бы за последние ' + sm.days + " " + plural(sm.days, "день", "дня", "дней") + ", если бы работали по плану сервиса. Для каждого дня модель видит только продажи до него." +
        (st.business.is_example ? " Данные условной пекарни смоделированы." : "") + "</p>" +
        row("Списания по себестоимости", sm.waste_now, sm.waste_new) + row("Упущенная прибыль, когда товар закончился", sm.lost_now, sm.lost_new) +
        row("Итого потери", sm.waste_now + sm.lost_now, sm.waste_new + sm.lost_new) + "</div>";
    }
    return h;
  }

  function render() {
    if (!S.st) return;
    renderTop(); renderDay(); renderTabs();
    var v = "";
    if (S.tab === "today") v = S.part === "vitrina" ? viewVitrina() : S.part === "itogi" ? viewItogi() : viewPlan();
    else if (S.tab === "tomorrow") v = viewTomorrow();
    else if (S.tab === "products") v = viewProducts();
    else if (S.tab === "batches") v = viewBatches();
    else v = viewData();
    $("view").innerHTML = v;
  }
  function refresh(force) {
    if (!force && (S.busy || isTyping())) return Promise.resolve();
    return API.state().then(function (st) {
      S.st = st; S.offset = serverOffset(st.now); S.fetchedAt = Date.now();
      var cur = PART_OF[phaseOf(st.times, nowMinutes().m)];
      if (!S.part || !S.partManual) S.part = cur;
      $("loading").hidden = true; $("login").hidden = true; $("app").hidden = false;
      render();
    }).catch(function (e) {
      if (e && e.login) return showLogin();
      $("loading").textContent = "Не удалось загрузить кабинет: " + (e && e.message ? e.message : e);
    });
  }
  function isTyping() {
    var a = document.activeElement;
    return a && $("view").contains(a) && /INPUT|SELECT|TEXTAREA/.test(a.tagName) && a.type !== "file";
  }
  function post(path, body, okMsg) {
    S.busy = true;
    return API.post(path, body).then(function (r) {
      S.busy = false; if (okMsg) toast(okMsg);
      return refresh(true).then(function () { return r; });
    }).catch(function (e) {
      S.busy = false;
      if (e && e.login) return showLogin();
      toast(e && e.message ? e.message : "Не получилось");
      throw e;
    });
  }
  function showLogin() {
    $("loading").hidden = true; $("app").hidden = true; $("login").hidden = false;
    $("top-actions").innerHTML = ""; $("clock").textContent = "";
    var u = "https://t.me/" + BOT_USERNAME;
    $("login-bot").href = u; $("login-bot").textContent = "@" + BOT_USERNAME;
    setTimeout(function () { $("code").focus(); }, 50);
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest("[data-act],[data-part],[data-tab]");
    if (!t) return;
    if (t.hasAttribute("data-tab")) { S.tab = t.getAttribute("data-tab"); S.edit = null; S.confirm = null; render(); return; }
    if (t.hasAttribute("data-part")) { S.part = t.getAttribute("data-part"); S.partManual = true; S.tab = "today"; render(); return; }
    var a = t.getAttribute("data-act"), p = t.getAttribute("data-p");
    if (a === "logout") { API.post("/api/logout").catch(function () {}).then(function () { S.st = null; showLogin(); }); }
    else if (a === "reset") { Local.reset(); S.vitrina = {}; refresh(true).then(function () { toast("Пример восстановлен"); }); }
    else if (a === "soldout") post("/api/record", { product: p, soldout: t.getAttribute("data-t") }, "Отмечено: " + p);
    else if (a === "left") {
      var inp = document.querySelector('[data-left="' + CSS.escape(p) + '"]');
      if (!inp || inp.value === "") { toast("Впишите, сколько осталось"); return; }
      post("/api/record", { product: p, left: inp.value }, "Отмечено: " + p);
    }
    else if (a === "edit") { S.edit = p; S.confirm = null; render(); }
    else if (a === "edit-cancel") { S.edit = null; render(); }
    else if (a === "ask-remove") { S.confirm = p; S.edit = null; render(); }
    else if (a === "ask-example") { S.confirm = "__example"; render(); }
    else if (a === "confirm-cancel") { S.confirm = null; render(); }
    else if (a === "remove") { S.confirm = null; post("/api/product/remove", { name: p }, "Убрано из планов: " + p); }
    else if (a === "restore") post("/api/product/restore", { name: p }, "Вернули: " + p);
    else if (a === "batch") {
      var s = t.getAttribute("data-s");
      post("/api/batch/close", { id: +t.getAttribute("data-id"), status: s }, s === "used" ? "Отмечено: использовано" : s === "written_off" ? "Списание учтено" : "Запись удалена");
    }
    else if (a === "example") { S.confirm = null; S.vitrina = {}; post("/api/example", {}, "Загружен пример пекарни"); }
  });
  document.addEventListener("input", function (e) {
    var t = e.target;
    if (t.hasAttribute("data-vitrina")) {
      var p = t.getAttribute("data-vitrina"); S.vitrina[p] = t.value;
      var it = S.st.today.items.filter(function (x) { return x.product === p; })[0], cell = document.querySelector('[data-adv="' + CSS.escape(p) + '"]');
      if (it && cell) {
        if (t.value === "") cell.innerHTML = "";
        else { var a = M.discountAdvice({ mean: it.mean }, +t.value); cell.innerHTML = a.pct ? '<span class="chip warn">скидка ' + a.pct + "%</span>" : '<span class="chip ok">без скидки</span>'; }
      }
    }
    if (t.form && t.form.getAttribute("data-form") === "batch-add" && (t.name === "item" || t.name === "made")) {
      var f = t.form, prod = S.st.products.filter(function (x) { return x.name === f.item.value.trim(); })[0];
      if (prod && prod.shelf_life && f.made.value) f.expires.value = M.diso(M.dnum(f.made.value) + prod.shelf_life);
    }
  });
  document.addEventListener("change", function (e) {
    var t = e.target, a = t.getAttribute("data-act");
    if (a === "event") post("/api/event", { factor: +t.value }, "Прогноз на завтра обновлён");
    if (a === "upload" && t.files && t.files[0]) {
      var file = t.files[0], rd = new FileReader();
      rd.onload = function () {
        var text;
        try { text = new TextDecoder("utf-8", { fatal: true }).decode(rd.result); } catch (x) { text = new TextDecoder("windows-1251").decode(rd.result); }
        post("/api/upload", { csv: text }).then(function (r) { toast("Загружено строк: " + r.rows); }).catch(function () {});
      };
      rd.readAsArrayBuffer(file); t.value = "";
    }
  });
  document.addEventListener("submit", function (e) {
    var f = e.target, kind = f.getAttribute("data-form");
    if (f.id === "login-form") {
      e.preventDefault();
      var code = $("code").value.replace(/\D/g, "");
      $("login-err").hidden = true;
      API.post("/api/login", { code: code }).then(function () { $("code").value = ""; return refresh(true); })
        .catch(function (x) { $("login-err").textContent = x && x.message ? x.message : "Не получилось войти"; $("login-err").hidden = false; });
      return;
    }
    if (!kind) return;
    e.preventDefault();
    var data = {};
    Array.prototype.forEach.call(f.elements, function (el) { if (el.name) data[el.name] = el.value; });
    if (kind === "product-add") post("/api/product", data, "Товар добавлен: " + data.name).then(function () {}).catch(function () {});
    else if (kind === "product-edit") { data.old_name = f.getAttribute("data-old"); S.edit = null; post("/api/product", data, "Сохранено").catch(function () {}); }
    else if (kind === "batch-add") { if (!data.expires) delete data.expires; post("/api/batch", data, "Партия добавлена").catch(function () {}); }
    else if (kind === "settings") post("/api/settings", data, "Настройки сохранены").catch(function () {});
  });
  document.addEventListener("visibilitychange", function () { if (!document.hidden && S.st) refresh(); });

  // часы и смена частей дня
  var lastPhase = null;
  setInterval(function () {
    if (!S.st) return;
    var ph = phaseOf(S.st.times, nowMinutes().m);
    if (lastPhase && ph !== lastPhase) { S.partManual = false; refresh(true); }
    lastPhase = ph;
    renderTop(); renderDay();
  }, 15000);
  setInterval(function () { if (S.st) refresh(); }, 60000);
  API.init().then(function () {
    var m = location.search.match(/[?&]code=(\d{6})/);
    if (API.mode === "server" && m) {
      history.replaceState(null, "", location.pathname);
      return API.post("/api/login", { code: m[1] }).catch(function () {}).then(function () { return refresh(true); });
    }
    return refresh(true);
  });
})();
