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
    var KEY = "svezhiy-kabinet-v3", D = null, btCache = null, W = {}, ver = 0, stockCache = null;
    function today() { var n = new Date(); return M.dnum(n.getFullYear() + "-" + pad(n.getMonth() + 1) + "-" + pad(n.getDate())); }
    function save() { ver++; try { localStorage.setItem(KEY, JSON.stringify(D)); } catch (e) { /* без localStorage работаем в памяти */ } }
    function example() {
      var r = DEMO_CSV ? M.parseCSV(DEMO_CSV) : { sales: [], products: {} }, T = today();
      var last = Math.max.apply(null, r.sales.map(function (s) { return s.day; }));
      r.sales.forEach(function (s) { s.day += (T - 1) - last; });
      var products = {};
      Object.keys(r.products).forEach(function (k) {
        var p = r.products[k];
        products[k] = { name: k, price: p.price, cost: p.cost, shelf_life: (k === "Эклер" || k === "Сэндвич с курицей") ? 2 : 1, default_qty: null, active: true };
      });
      // сырьё, техкарты и партии примера - из bot/core/materials.py
      var DS = DEMO_STOCK || { materials: [], recipes: [], batches: [] }, mats = {};
      DS.materials.forEach(function (m) { mats[m[0]] = { name: m[0], unit: m[1], delivery: m[2], shelf_life: m[3], cost: m[4] }; });
      D = { v: 3, business: "Пекарня", example: true, times: { morning: "06:00", evening: "17:00", closing: "21:00" },
            products: products, sales: r.sales, plans: {}, ok: {}, events: {}, materials: mats,
            recipes: DS.recipes.map(function (x) { return { product: x[0], material: x[1], norm: x[2] }; }),
            batches: DS.batches.map(function (b, i) {
              var u = mats[b[0]].unit;
              return { id: i + 1, item: b[0], qty: M.fmtAmount(b[1], u), amount: b[1], unit: u, exp: T + b[2], arrived: T, status: "active" };
            }) };
      D.nextId = D.batches.length + 1;
      btCache = null; save();
    }
    function load() {
      try { var raw = localStorage.getItem(KEY); if (raw) { var d = JSON.parse(raw); if (d && d.v === 3) D = d; } } catch (e) { /* нет доступа */ }
      if (!D) example();
      D.ok = D.ok || {}; D.materials = D.materials || {}; D.recipes = D.recipes || [];
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
    // план в работе - только подтверждённый: { at, items: { товар: { qty, mean } } }
    function planInfo(ok, current) {
      if (!ok) return { status: "draft", at: null, total_ok: null };
      var keys = Object.keys(ok.items), total = keys.reduce(function (a, k) { return a + ok.items[k].qty; }, 0);
      var same = keys.length === Object.keys(current).length && keys.every(function (k) { return current[k] === ok.items[k].qty; });
      return { status: same ? "ok" : "changed", at: ok.at, total_ok: total };
    }
    function qtyOf(list) { var m = {}; list.forEach(function (r) { m[r.product] = r.qty; }); return m; }
    function sale(day, product) { for (var i = 0; i < D.sales.length; i++) if (D.sales[i].day === day && D.sales[i].product === product) return D.sales[i]; return null; }
    function productList() {
      var hist = {}, pm = prodMap(true);
      D.sales.forEach(function (s) { (hist[s.product] = hist[s.product] || {})[s.day] = 1; });
      return Object.keys(pm).map(function (k) {
        var p = pm[k];
        return { name: k, price: p.price, cost: p.cost, shelf_life: p.shelf_life == null ? null : p.shelf_life, default_qty: p.default_qty == null ? null : p.default_qty, history_days: hist[k] ? Object.keys(hist[k]).length : 0 };
      }).sort(function (a, b) { return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1; });
    }
    function batchList(T, view) {
      var now = view ? view.left : {};
      return D.batches.filter(function (b) { return b.status === "active"; }).sort(function (a, b) { return a.exp - b.exp || a.id - b.id; }).map(function (b) {
        var st = M.expiryStatus(b.exp, T), rest = now[b.id] != null ? now[b.id] : null;
        return { id: b.id, item: b.item, qty: b.qty, amount: b.amount == null ? null : b.amount, unit: b.unit || null, rest: rest,
                 rest_text: rest == null ? null : M.fmtAmount(rest, b.unit), expires: M.diso(b.exp), label: st.label, left: st.left,
                 level: st.left <= 0 ? "bad" : st.left <= 2 ? "warn" : "ok", tip: st.left <= 1 ? (st.left < 0 ? "спишите" : M.expiryTip(b.item)) : null };
      });
    }
    // сырьё: остатки по тому, что произвели, и заказ к ближайшей поставке (M.stockReport)
    function matByName(name) {
      var k = M.mkey(name), hit = null;
      Object.keys(D.materials).forEach(function (n) { if (M.mkey(n) === k) hit = D.materials[n]; });
      return hit;
    }
    function dayPlan(day, T) {
      var ok = D.ok[day], out = {};
      if (ok) { Object.keys(ok.items).forEach(function (k) { out[k] = ok.items[k].qty; }); return out; }
      if (day === T && D.plans[T]) { Object.keys(D.plans[T]).forEach(function (k) { out[k] = D.plans[T][k].qty; }); return out; }
      return qtyOf(recs(day).recs);
    }
    function stock() {
      if (!Object.keys(D.materials).length) return null;
      var T = today(), tag = ver + "|" + T + "|" + JSON.stringify(W);
      if (stockCache && stockCache.tag === tag) return stockCache.view;
      var active = D.batches.filter(function (b) { return b.status === "active"; }).map(function (b) {
        return { id: b.id, item: b.item, amount: b.amount == null ? null : b.amount, expires: b.exp, arrived: b.arrived == null ? T : b.arrived };
      });
      var arr = active.filter(function (b) { return b.amount; }).map(function (b) { return b.arrived; });
      var made = {}, plan = {};
      for (var d = arr.length ? Math.min.apply(null, arr) : T; d < T; d++) {
        made[d] = {};
        if (D.ok[d]) Object.keys(D.ok[d].items).forEach(function (k) { made[d][k] = D.ok[d].items[k].qty; });
      }
      D.sales.forEach(function (s) { if (made[s.day] && s.baked != null) made[s.day][s.product] = s.baked; });
      for (var i = 0; i < M.HORIZON; i++) plan[T + i] = dayPlan(T + i, T);
      var view = M.stockView(M.stockReport(D.materials, D.recipes, active, made, plan, T), plan[T]);
      stockCache = { tag: tag, view: view };
      return view;
    }
    function materialList(view) {
      var rows = {};
      (view ? view.rows : []).forEach(function (r) { rows[r.name] = r; });
      return Object.keys(D.materials).sort().map(function (k) {
        var m = D.materials[k], r = rows[m.name];
        return { name: m.name, unit: m.unit, delivery: m.delivery, shelf_life: m.shelf_life == null ? null : m.shelf_life, cost: m.cost == null ? null : m.cost,
                 stock_text: r ? r.text.stock : null, order_text: r && r.order > 0 ? r.text.order + " к " + M.ddmm(M.dnum(r.next)) : r ? "не нужно" : null };
      });
    }
    function recipeList() {
      return D.recipes.map(function (r) { var m = matByName(r.material); return { product: r.product, material: r.material, norm: r.norm, unit: m ? m.unit : null }; })
        .sort(function (a, b) { return a.product < b.product ? -1 : a.product > b.product ? 1 : a.material < b.material ? -1 : 1; });
    }
    function amountOf(item, b) {
      var m = matByName(item), mu = m ? m.unit : null, r;
      if (b.amount != null && b.amount !== "") r = M.parseAmount(num(b.amount, 0, 1e6) + " " + (b.unit || ""), mu);
      else r = M.parseAmount(b.qty, mu);
      if (m && r.unit && r.unit !== m.unit) throw new Error(m.name + " считается в «" + m.unit + "» — укажите количество в " + m.unit);
      return { qty: r.amount != null ? M.fmtAmount(r.amount, r.unit) : String(b.qty || "").trim().slice(0, 20), amount: r.amount, unit: r.unit };
    }
    function sheet() {
      var T = today(), days = [], week = {}, sum = function (q) { return q.reduce(function (x, y) { return x + y; }, 0); };
      for (var i = 0; i < 7; i++) {
        days.push(M.diso(T + i));
        var fix = D.ok[T + i] ? D.ok[T + i].items : i === 0 ? D.plans[T] : null;
        recs(T + i).recs.forEach(function (r) {
          var fixed = fix && fix[r.product];
          (week[r.product] = week[r.product] || [0, 0, 0, 0, 0, 0, 0])[i] = fixed ? fixed.qty : r.qty;
        });
      }
      var view = stock();
      return {
        today: M.diso(T), products: productList(), batches: batchList(T, view), materials: materialList(view), recipes: recipeList(),
        sales: D.sales.map(function (s) {
          return { day: M.diso(s.day), product: s.product, baked: s.baked == null ? null : s.baked, sold: s.sold, soldout: s.soldout || null,
                   rain: s.rain == null ? null : s.rain, temp: s.temp == null ? null : s.temp };
        }),
        week: { days: days, rows: Object.keys(week).map(function (k) { return { product: k, qty: week[k] }; }).sort(function (a, b) { return sum(b.qty) - sum(a.qty); }) }
      };
    }
    function state() {
      var now = new Date(), T = today(), m = now.getHours() * 60 + now.getMinutes();
      [D.plans, D.ok].forEach(function (m) { Object.keys(m).forEach(function (k) { if (+k < T - 14) delete m[k]; }); });
      var rt = recs(T), plan = D.plans[T] || (D.plans[T] = {}), changed = false, okT = D.ok[T], okM = D.ok[T + 1];
      rt.recs.forEach(function (r) { if (!plan[r.product]) { plan[r.product] = { qty: r.qty, mean: r.mean }; changed = true; } });
      if (changed) save();
      var items = rt.recs.map(function (r) {
        var p = plan[r.product], it = item(r, p.qty, p.mean), s = sale(T, r.product), w = okT && okT.items[r.product];
        it.ok_qty = w ? w.qty : null;
        if (w) it.threshold = M.eveningThreshold({ mean: w.mean });
        it.record = s ? { soldout: s.soldout, baked: s.baked, left: s.soldout ? null : Math.max(0, Math.round((s.baked || 0) - s.sold)) } : null;
        return it;
      }).sort(function (a, b) { return b.qty - a.qty; });
      var draft = {};
      Object.keys(plan).forEach(function (k) { draft[k] = plan[k].qty; });
      var rm = recs(T + 1);
      var tom = rm.recs.map(function (r) {
        var it = item(r); it.habit = rm.habit[r.product] != null ? Math.round(rm.habit[r.product]) : null;
        it.ok_qty = okM && okM.items[r.product] ? okM.items[r.product].qty : null;
        return it;
      });
      var products = productList(), view = stock(), batches = batchList(T, view);
      var removed = Object.keys(D.products).filter(function (k) { return !D.products[k].active; }).sort();
      var days = D.sales.map(function (s) { return s.day; });
      if (!btCache || btCache.n !== D.sales.length) {
        var bt = D.sales.length ? M.backtest(D.sales, prodMap(false)) : null;
        btCache = { n: D.sales.length, v: bt && bt.wasteNow > 0 ? { days: bt.days, waste_now: bt.wasteNow, waste_new: bt.wasteNew, lost_now: bt.lostNow, lost_new: bt.lostNew,
          share_now: bt.wasteUnitsNow / bt.producedNow * 100, share_new: bt.wasteUnitsNew / bt.producedNew * 100 } : null };
      }
      return {
        mode: "local", business: { name: D.business, city: D.example ? "Челябинск" : "", is_example: D.example },
        now: M.diso(T) + "T" + pad(now.getHours()) + ":" + pad(now.getMinutes()), phase: phaseOf(D.times, m), times: D.times,
        today: { date: M.diso(T), rain: weather(T).rain, temp: weather(T).temp, event: D.events[T] || 1, items: items, plan: planInfo(okT, draft) },
        tomorrow: { date: M.diso(T + 1), rain: weather(T + 1).rain, temp: weather(T + 1).temp, event: D.events[T + 1] || 1, rain_effect: rm.eff, items: tom,
                    plan: planInfo(okM, qtyOf(rm.recs)) },
        products: products, removed: removed, batches: batches, stock: view,
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
            Object.keys(D.ok).forEach(function (d) { var x = D.ok[d].items; if (x[old]) { x[nm] = x[old]; delete x[old]; } });
            D.recipes.forEach(function (r) { if (r.product === old) r.product = nm; });
            delete D.products[old]; btCache = null;
          }
          D.products[nm] = { name: nm, price: price, cost: cost, shelf_life: num(b.shelf_life, 0, 3650, true), default_qty: num(b.default_qty, 0, 1e5), active: true };
        } else if (path === "/api/product/remove" || path === "/api/product/restore") {
          var n2 = name(b.name), on = path === "/api/product/restore";
          D.products[n2] = D.products[n2] || { name: n2, price: null, cost: null, shelf_life: null, default_qty: null };
          D.products[n2].active = on;
          if (!on) [T - 1, T, T + 1].forEach(function (d) { if (D.plans[d]) delete D.plans[d][n2]; if (D.ok[d]) delete D.ok[d].items[n2]; });
        } else if (path === "/api/batch") {
          var it = name(b.item, "продукт"), exp = b.expires ? M.dnum(b.expires) : null, am = amountOf(it, b);
          if (exp == null) {
            var p = D.products[it], mm = matByName(it), life = (p && p.shelf_life) || (mm && mm.shelf_life);
            if (!life) throw new Error("укажите дату «годен до» или срок хранения у товара или сырья");
            exp = M.dnum(b.made || M.diso(T)) + life;
          }
          D.batches.push({ id: D.nextId++, item: it, qty: am.qty, amount: am.amount, unit: am.unit, exp: exp, arrived: T, status: "active" });
        } else if (path === "/api/batch/close") {
          D.batches.forEach(function (x) { if (x.id === +b.id) x.status = b.status; });
        } else if (path === "/api/record") {
          var pr = name(b.product), so = b.soldout || null;
          if (so && !/^([01]?\d|2[0-3]):[0-5]\d$/.test(so)) throw new Error("время в формате 15:30");
          var left = so ? null : num(b.left, 0, 1e5, true);
          if (!so && left == null) throw new Error("укажите остаток или время, когда товар закончился");
          var w = D.ok[T] && D.ok[T].items[pr];
          if (!w) throw new Error("план на сегодня не подтверждён — итоги отмечаются по подтверждённому плану");
          var baked = w.qty, s = sale(T, pr), sold = so ? baked : Math.max(0, baked - left);
          if (s) { s.sold = sold; s.baked = baked; s.soldout = so; } else D.sales.push({ day: T, product: pr, sold: sold, baked: baked, soldout: so, rain: weather(T).rain, temp: weather(T).temp });
          btCache = null;
        } else if (path === "/api/sales") {
          var TIME = /^([01]?\d|2[0-3]):[0-5]\d$/, ups = (b.upsert || []).map(function (x) {
            var pr = name(x.product, "товар"), sold = num(x.sold, 0, 1e5), bk = num(x.baked, 0, 1e5);
            if (sold == null) throw new Error(pr + ": не указано, сколько продано");
            if (bk != null && sold > bk) throw new Error(pr + ": продано больше, чем испечено");
            if (x.soldout && !TIME.test(x.soldout)) throw new Error("время в формате 15:30");
            return { day: M.dnum(x.day), product: pr, sold: sold, baked: bk, soldout: x.soldout || null, rain: x.rain == null ? null : !!x.rain, temp: num(x.temp, -60, 50) };
          });
          var gone = {};
          (b["delete"] || []).forEach(function (x) { gone[M.dnum(x.day) + "|" + x.product] = 1; });
          D.sales = D.sales.filter(function (x) { return !gone[x.day + "|" + x.product]; });
          var at = {};
          D.sales.forEach(function (x, i) { at[x.day + "|" + x.product] = i; });
          ups.forEach(function (x) { var k = x.day + "|" + x.product; if (k in at) D.sales[at[k]] = x; else { at[k] = D.sales.length; D.sales.push(x); } });
          btCache = null;
        } else if (path === "/api/batch/edit") {
          var bi = name(b.item, "продукт"), be = b.expires ? M.dnum(b.expires) : null, ba = amountOf(bi, b);
          if (be == null || isNaN(be)) throw new Error("годен до: нужна дата");
          D.batches.forEach(function (x) { if (x.id === +b.id) { x.item = bi; x.qty = ba.qty; x.amount = ba.amount; x.unit = ba.unit; x.exp = be; } });
        } else if (path === "/api/material") {
          var mn = name(b.name, "сырьё"), days;
          try { days = M.parseDelivery(b.delivery); } catch (x) { throw new Error(mn + ": " + x.message); }
          var mat = { name: mn, unit: M.normUnit(b.unit || "кг"), delivery: M.deliveryText(days), shelf_life: num(b.shelf_life, 1, 3650, true), cost: num(b.cost, 0, 1e6) };
          if (b.old_name && b.old_name !== mn) {
            delete D.materials[b.old_name];
            D.recipes.forEach(function (r) { if (r.material === b.old_name) r.material = mn; });
            D.batches.forEach(function (x) { if (x.item === b.old_name && x.status === "active") x.item = mn; });
          }
          D.materials[mn] = mat;
        } else if (path === "/api/material/remove") {
          var rn = name(b.name, "сырьё");
          delete D.materials[rn];
          D.recipes = D.recipes.filter(function (r) { return r.material !== rn; });
        } else if (path === "/api/recipe") {
          var rp = name(b.product, "товар"), rm0 = matByName(b.material), norm = num(b.norm, 0, 1e5);
          if (!rm0) throw new Error("сырья «" + String(b.material || "") + "» нет в списке — добавьте его на листе «Сырьё»");
          if (!norm) throw new Error(rp + ": укажите расход сырья на 1 шт");
          var drop = function (pr, ma) { D.recipes = D.recipes.filter(function (r) { return !(r.product === pr && r.material === ma); }); };
          if (b.old_product) drop(b.old_product, b.old_material);
          drop(rp, rm0.name);
          D.recipes.push({ product: rp, material: rm0.name, norm: norm });
        } else if (path === "/api/recipe/remove") {
          D.recipes = D.recipes.filter(function (r) { return !(r.product === b.product && r.material === b.material); });
        } else if (path === "/api/plan/confirm" || path === "/api/plan/unconfirm") {
          var day = M.dnum(String(b.day || ""));
          if (day !== T && day !== T + 1) throw new Error("подтвердить можно план на сегодня или на завтра");
          if (path === "/api/plan/unconfirm") delete D.ok[day];
          else {
            var src = {}, now = new Date();
            if (day === T) src = D.plans[T] || {};
            else recs(day).recs.forEach(function (r) { src[r.product] = { qty: r.qty, mean: r.mean }; });
            if (!Object.keys(src).length) throw new Error("плана на этот день нет");
            var copy = {};
            Object.keys(src).forEach(function (k) { copy[k] = { qty: src[k].qty, mean: src[k].mean }; });
            D.ok[day] = { at: M.diso(T) + "T" + pad(now.getHours()) + ":" + pad(now.getMinutes()), items: copy };
          }
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
          if (D.example) { D.sales = []; D.products = {}; D.plans = {}; D.ok = {}; D.events = {}; D.batches = []; D.materials = {}; D.recipes = []; D.example = false; D.business = ""; }
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
    return { load: load, state: function () { return Promise.resolve(state()); }, sheet: sheet, act: act, reset: example };
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
    sheet: function () {
      if (this.mode === "local") { try { return Promise.resolve(Local.sheet()); } catch (e) { return Promise.reject(e); } }
      return fetch("api/sheet", { cache: "no-store", credentials: "same-origin" }).then(function (r) {
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
    var parts = [["plan", "План на день", t.morning, st.today.plan.status === "ok" ? "✓ подтверждён" : "ждёт подтверждения"],
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

  // подтверждение плана: в работу идёт только подтверждённый план
  function when(at) {
    var p = String(at || "").split("T"), t = (p[1] || "").slice(0, 5);
    return p[0] === S.st.today.date ? "в " + t : M.ddmm(M.dnum(p[0])) + " в " + t;
  }
  function planChip(pl) {
    return '<span class="chip ' + (pl.status === "ok" ? "ok" : "warn") + '">' + (pl.status === "draft" ? "Черновик" : pl.status === "ok" ? "✓ Подтверждён" : "Изменился") + "</span>";
  }
  function confirmBlock(d) {
    var pl = d.plan, t = S.st.times, total = d.items.reduce(function (a, i) { return a + i.qty; }, 0), text, btn;
    if (pl.status === "draft") {
      text = "<b>План не подтверждён.</b> Проверьте цифры и подтвердите: без подтверждения план не идёт в работу — не будет советов по витрине и итогов дня.";
      btn = '<button class="btn btn-primary" data-act="plan-ok" data-day="' + d.date + '">Подтвердить план</button>';
    } else if (pl.status === "ok") {
      text = "<b>План подтверждён " + esc(when(pl.at)) + ":</b> " + fmt(pl.total_ok) + " шт. По нему — витрина в " + esc(t.evening) + " и итоги в " + esc(t.closing) + ".";
      btn = '<button class="btn btn-quiet" data-act="plan-undo" data-day="' + d.date + '">Отменить подтверждение</button>';
    } else {
      text = "<b>Расчёт изменился после подтверждения.</b> В работе " + fmt(pl.total_ok) + " шт, новый расчёт — " + fmt(total) + " шт. Подтвердите новый план или оставьте прежний.";
      btn = '<button class="btn btn-primary" data-act="plan-ok" data-day="' + d.date + '">Подтвердить новый план</button>';
    }
    return '<div class="confirm' + (pl.status === "ok" ? " ok" : "") + '"><p>' + text + "</p>" + btn + "</div>";
  }
  function inWork(it) { return it.ok_qty != null && it.ok_qty !== it.qty ? '<div class="why">в работе ' + it.ok_qty + "</div>" : ""; }
  function notConfirmed(title, what) {
    return '<div class="card"><h2>' + title + '</h2><div class="confirm"><p><b>План на сегодня не подтверждён</b>, поэтому в работу не пошёл — ' + what +
      ' появятся после подтверждения.</p><button class="btn btn-primary" data-act="plan-ok" data-day="' + S.st.today.date + '">Подтвердить план</button></div></div>';
  }
  function working() {
    return S.st.today.items.filter(function (i) { return i.ok_qty != null; }).sort(function (a, b) { return a.product < b.product ? -1 : 1; });
  }

  // ---------------------------------------------------------------- сырьё
  function alertList(list) {
    return '<ul class="alerts">' + list.map(function (a) {
      return '<li class="' + a.level + '"><span class="ic" aria-hidden="true">' + (a.waste ? "🗑" : "⚠️") + "</span><span><b>" + esc(a.name) + ":</b> " + esc(a.text) + "</span></li>";
    }).join("") + "</ul>";
  }
  function stockCard() {
    var st = S.st, v = st.stock;
    if (!v) return "";
    var al = M.stockAlerts(v, st.today.date, dayLabel), tom = st.tomorrow.date;
    var due = v.rows.filter(function (r) { return r.order > 0 && r.next === tom; });
    var h = '<div class="card"><h2>Сырьё на сегодня</h2>';
    if (!al.length && !due.length) return h + '<p class="sub" style="margin:0">Сырья хватает до ближайших поставок.</p></div>';
    if (al.length) h += alertList(al);
    if (due.length) h += '<p class="hint" style="margin-top:12px"><b>Заказать сегодня — поставка завтра:</b> ' +
      due.map(function (r) { return esc(r.name) + " — " + esc(r.text.order); }).join(", ") + "</p>";
    return h + '<div class="acts" style="justify-content:flex-start;margin-top:12px"><button class="btn btn-quiet btn-sm" data-tab="stock">Все остатки и заказ</button></div></div>';
  }
  function viewStock() {
    var st = S.st, v = st.stock;
    if (!v) return '<div class="card"><h2>Сырьё и заказ</h2><div class="empty">Сырьё пока не заведено. Откройте «Таблицы» и заполните листы «Сырьё» (единица, дни поставок, срок) и «Техкарты» (сколько сырья уходит на 1 шт товара). Потом добавьте партии с количеством во вкладке «Сроки годности».</div></div>';
    var al = M.stockAlerts(v, st.today.date, dayLabel), tom = st.tomorrow.date, orders = v.rows.filter(function (r) { return r.order > 0; });
    var h = '<div class="kpis"><div class="kpi' + (al.some(function (a) { return !a.waste; }) ? " bad" : "") + '"><b class="num">' + al.filter(function (a) { return !a.waste; }).length +
      '</b><span>не хватит до поставки</span></div><div class="kpi' + (al.some(function (a) { return a.waste; }) ? " warn" : "") + '"><b class="num">' + al.filter(function (a) { return a.waste; }).length +
      '</b><span>спишется, если не пустить в работу</span></div><div class="kpi"><b class="num">' + orders.length + "</b><span>" + plural(orders.length, "позиция", "позиции", "позиций") + " к заказу</span></div></div>";
    if (al.length) h += '<div class="card"><h2>Что важно</h2><p class="sub">По плану на сегодня и прогнозу до следующих поставок.</p>' + alertList(al) + "</div>";
    h += '<div class="card"><h2>Заказ поставщикам</h2><p class="sub">Остаток на утро уменьшается сам: по тому, сколько испекли, а если это не отмечено — по подтверждённому плану. Заказ — к ближайшей поставке, чтобы хватило до следующей, с запасом 10% и не больше, чем успеете использовать до конца срока.</p>' +
      '<div class="table-wrap"><table class="t stock-t"><thead><tr><th>Сырьё</th><th class="r">На утро</th><th class="r c-need">Сегодня уйдёт</th><th class="c-del">Поставки</th><th class="c-next">Ближайшая</th><th class="r">Заказать</th></tr></thead><tbody>';
    v.rows.forEach(function (r) {
      var isDue = r.order > 0 && r.next === tom;
      h += '<tr class="' + (isDue ? "due" : "") + '"><td class="name">' + esc(r.name) + '</td><td class="r num">' + esc(r.text.stock) + '</td><td class="r num c-need">' + esc(r.text.need_today) +
        '</td><td class="c-del">' + esc(r.delivery) + '</td><td class="c-next">' + dayLabel(r.next) + (isDue ? ' <span class="chip new">заказ сегодня</span>' : "") +
        '</td><td class="r">' + (r.order > 0 ? '<span class="order">' + esc(r.text.order) + '</span><div class="why"><span class="m-only">' + (isDue ? "заказ сегодня, " : "к " + dayLabel(r.next) + ", ") +
          "</span>на " + r.cover + " " + plural(r.cover, "день", "дня", "дней") + "</div>" : '<span class="muted">не нужно</span>') + "</td></tr>";
    });
    h += '</tbody></table></div><p class="hint" style="margin-top:10px">Сырьё, дни поставок и техкарты меняются во вкладке «Таблицы» — листы «Сырьё» и «Техкарты».' +
      (st.business.is_example ? " В примере расход сырья ориентировочный." : "") + "</p></div>";
    return h;
  }

  function viewPlan() {
    var st = S.st, items = st.today.items, total = items.reduce(function (a, i) { return a + i.qty; }, 0), exp = st.batches.filter(function (b) { return b.left <= 1; });
    var h = '<div class="kpis"><div class="kpi"><b class="num">' + total + '</b><span>штук сделать сегодня</span></div>' +
      '<div class="kpi"><b class="num">' + items.length + "</b><span>" + plural(items.length, "позиция", "позиции", "позиций") + " в плане</span></div>" +
      '<div class="kpi' + (exp.length ? " bad" : "") + '"><b class="num">' + exp.length + "</b><span>" + plural(exp.length, "партия истекает", "партии истекают", "партий истекают") + " сегодня–завтра</span></div>" +
      '<div class="kpi"><b>' + (st.today.event !== 1 ? M.signed((st.today.event - 1) * 100) : st.today.rain ? "дождь" : "обычный") + "</b><span>" +
      (st.today.event !== 1 ? "событие сегодня" : st.today.rain ? "спрос ниже обычного" : "день по прогнозу") + "</span></div></div>";
    if (!items.length) return h + emptyData();
    h += '<div class="card"><h2>План на ' + dayLabel(st.today.date) + " " + planChip(st.today.plan) + '</h2><p class="sub">Количество зафиксировано утром, новые товары добавляются сразу. В работу идёт только подтверждённый план.</p>' + confirmBlock(st.today) +
      '<div class="table-wrap"><table class="t' + (st.today.plan.status === "ok" ? " confirmed" : "") + '"><thead><tr><th>Позиция</th><th class="r">Спрос</th><th class="r">Сделать</th></tr></thead><tbody>';
    items.forEach(function (it) {
      var w = whyText(it);
      h += "<tr><td><div class=\"name\">" + esc(it.product) + (it["new"] ? ' <span class="chip new">новый</span>' : "") + "</div>" + (w ? '<div class="why">' + esc(w) + "</div>" : "") +
        '</td><td class="r num">≈' + Math.round(it.mean) + ' <span class="muted">±' + Math.round(it.sd) + '</span></td><td class="r"><span class="big">' + it.qty + "</span>" + inWork(it) + "</td></tr>";
    });
    h += '</tbody><tfoot><tr><td>Всего</td><td></td><td class="r"><span class="big">' + total + "</span></td></tr></tfoot></table></div></div>";
    h += stockCard();
    if (st.stock) exp = exp.filter(function (b) { return !st.stock.rows.some(function (r) { return M.mkey(r.name) === M.mkey(b.item); }); });
    if (exp.length) {
      h += '<div class="card"><h2>Пора пустить в работу</h2><p class="sub">Партии, у которых срок заканчивается сегодня или завтра.</p>' + batchRows(exp, true) + "</div>";
    }
    return h;
  }

  function viewVitrina() {
    if (!S.st.today.items.length) return emptyData();
    if (S.st.today.plan.status === "draft") return notConfirmed("Проверьте витрину", "советы по витрине и скидкам");
    var items = working();
    var h = '<div class="card"><h2>Проверьте витрину</h2><p class="sub">После ' + esc(S.st.times.evening) + " обычно уходит около пятой части дневных продаж. Если на витрине больше порога — ставьте скидку, иначе остаток уйдёт в списание. Впишите, сколько сейчас лежит, и получите совет.</p>" +
      '<div class="table-wrap"><table class="t"><thead><tr><th>Позиция</th><th class="r">Сделали</th><th class="r">Скидка, если больше</th><th>Сейчас на витрине</th><th>Совет</th></tr></thead><tbody>';
    items.forEach(function (it) {
      var v = S.vitrina[it.product], adv = "";
      if (v !== undefined && v !== "") {
        var a = M.discountAdvice({ mean: it.mean }, +v);
        adv = a.pct ? '<span class="chip warn">скидка ' + a.pct + "%</span>" : '<span class="chip ok">без скидки</span>';
      }
      h += "<tr><td class=\"name\">" + esc(it.product) + '</td><td class="r num">' + it.ok_qty + '</td><td class="r num"><b>' + it.threshold + " шт</b></td>" +
        '<td><input class="small" type="number" min="0" inputmode="numeric" data-vitrina="' + esc(it.product) + '" value="' + esc(v == null ? "" : v) + '" aria-label="Сейчас на витрине: ' + esc(it.product) + '"></td><td data-adv="' + esc(it.product) + '">' + adv + "</td></tr>";
    });
    return h + "</tbody></table></div></div>";
  }

  function viewItogi() {
    if (!S.st.today.items.length) return emptyData();
    if (S.st.today.plan.status === "draft") return notConfirmed("Итоги дня · " + dayLabel(S.st.today.date), "итоги дня");
    var items = working(), done = items.filter(function (i) { return i.record; }).length;
    var h = '<div class="card"><h2>Итоги дня · ' + dayLabel(S.st.today.date) + '</h2><p class="sub">Отметьте по каждой позиции, когда она закончилась или сколько осталось. По этим отметкам сервис узнаёт реальный спрос и завтра считает точнее. То же можно сделать в боте.</p>' +
      '<p class="hint">Отмечено ' + done + " из " + items.length + '</p><div class="progress"><i style="width:' + (done / items.length * 100).toFixed(0) + '%"></i></div>' +
      '<div class="table-wrap" style="margin-top:14px"><table class="t"><thead><tr><th>Позиция</th><th class="r">Сделали</th><th>Закончилась</th><th>Или осталось</th><th>Отметка</th></tr></thead><tbody>';
    items.forEach(function (it) {
      var r = it.record, mark = !r ? '<span class="chip mute">нет</span>' : r.soldout ? '<span class="chip ok">✓ кончились ' + esc(r.soldout) + "</span>" : '<span class="chip ok">✓ осталось ' + r.left + "</span>";
      h += '<tr><td class="name">' + esc(it.product) + '</td><td class="r num">' + it.ok_qty + '</td><td><div class="slots">' +
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
    var h = '<div class="card"><div class="day-head" style="margin:0 0 10px"><div><h2>Завтра, ' + dayLabel(t.date) + (items.length ? " " + planChip(t.plan) : "") + '</h2><p class="sub" style="margin:0">' + esc(weather) + '</p></div>' +
      '<label style="display:grid;gap:4px;font-size:12.5px;color:var(--ink-3);min-width:220px">Событие завтра<select data-act="event">' +
      opts.map(function (o) { return '<option value="' + o[0] + '"' + (Math.abs(o[0] - t.event) < 1e-9 ? " selected" : "") + ">" + o[1] + "</option>"; }).join("") + "</select></label></div>";
    if (!items.length) return h + emptyData() + "</div>";
    h += confirmBlock(t) + '<div class="table-wrap"><table class="t' + (t.plan.status === "ok" ? " confirmed" : "") + '"><thead><tr><th>Позиция</th><th class="r">Спрос</th><th class="r">Сделать</th><th class="r">Обычно</th></tr></thead><tbody>';
    items.forEach(function (it) {
      total += it.qty; if (it.habit != null) { habit += it.habit; hasH = true; } else habit += it.qty;
      var w = whyText(it);
      h += "<tr><td><div class=\"name\">" + esc(it.product) + (it["new"] ? ' <span class="chip new">новый</span>' : "") + "</div>" + (w ? '<div class="why">' + esc(w) + "</div>" : "") +
        '</td><td class="r num">≈' + Math.round(it.mean) + '</td><td class="r"><span class="big">' + it.qty + "</span>" + inWork(it) + '</td><td class="r num muted">' + (it.habit != null ? it.habit : "—") + "</td></tr>";
    });
    h += '</tbody><tfoot><tr><td>Всего</td><td></td><td class="r"><span class="big">' + total + '</span></td><td class="r num">' + (hasH ? Math.round(habit) : "—") + "</td></tr></tfoot></table></div>" +
      '<p class="hint" style="margin-top:10px">«Обычно» — сколько в среднем делали в этот день недели за 4 недели. Подтвердить план можно заранее здесь или утром в Telegram, когда он придёт в ' + esc(S.st.times.morning) + ".</p></div>";
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
      var q = b.rest != null && b.amount && b.rest < b.amount - 1e-9 ? esc(b.rest_text) + ' <span class="muted">из ' + esc(b.qty) + "</span>" : esc(b.qty || "—");
      h += '<tr><td><div class="name">' + esc(b.item) + "</div>" + (b.tip ? '<div class="why">→ ' + esc(b.tip) + "</div>" : "") + "</td><td>" + q +
        '</td><td class="num">' + dayLabel(b.expires) + '</td><td><span class="chip ' + b.level + '">' + glyph[b.level] + " " + esc(b.label) + "</span></td>" +
        (compact ? "" : '<td><div class="acts"><button class="btn btn-quiet btn-sm" data-act="batch" data-id="' + b.id + '" data-s="used">Использовано</button><button class="btn btn-quiet btn-sm" data-act="batch" data-id="' + b.id + '" data-s="written_off">Списано</button><button class="btn btn-quiet btn-sm" data-act="batch" data-id="' + b.id + '" data-s="deleted" aria-label="Удалить запись">Удалить</button></div></td>') + "</tr>";
    });
    return h + "</tbody></table></div>";
  }

  function viewBatches() {
    var st = S.st, today = st.today.date;
    var h = '<div class="card"><h2>Добавить партию</h2><p class="sub">Выберите товар со сроком хранения — дата «годен до» посчитается сама. Для сырья (сливки, творог) укажите дату с упаковки.</p>' +
      '<form class="form" data-form="batch-add"><label class="wide">Продукт<input type="text" name="item" list="prodlist" maxlength="60" required placeholder="Сливки 33% или товар из списка"></label>' +
      '<label>Количество<input type="text" name="qty" maxlength="20" placeholder="2 л, 800 г, 30 шт"></label>' +
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
    h += '<div class="card"><h2>Данные продаж</h2><p class="sub">' + (d.rows ? d.rows + " " + plural(d.rows, "строка", "строки", "строк") + " с " + dayLabel(d.first) + " по " + dayLabel(d.last) + (st.business.is_example ? ". Сейчас загружен пример «Пекарня»." : ".") : "Продаж пока нет.") + "</p>" +
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
        (st.business.is_example ? " Данные примера «Пекарня» смоделированы." : "") + "</p>" +
        row("Списания по себестоимости", sm.waste_now, sm.waste_new) + row("Упущенная прибыль, когда товар закончился", sm.lost_now, sm.lost_new) +
        row("Итого потери", sm.waste_now + sm.lost_now, sm.waste_new + sm.lost_new) + "</div>";
    }
    return h;
  }

  function viewTables() {
    var host = $("sheets");
    if (S.sheets && host && host === S.sheets.el) return;
    $("view").innerHTML = '<div class="card"><h2>Таблицы</h2><p class="sub">Все данные в одном месте, как в Excel: продажи по дням, товары, сроки годности и план на неделю. Правки сразу учитываются в прогнозе. Можно вставлять прямо из Excel и скачивать обратно.</p><div id="sheets" style="margin-top:14px"></div></div>';
    S.sheets = new Sheets($("sheets"), sheetAdapter);
    S.sheets.load();
  }
  function touched(r) { S.dirty = true; return r; }
  function unlogged(e) { if (e && e.login) showLogin(); throw e; }
  var sheetAdapter = {
    today: null,
    load: function () { return API.sheet().then(function (d) { sheetAdapter.today = d.today; return d; }).catch(unlogged); },
    saveSales: function (up, del) { return API.post("/api/sales", { upsert: up, "delete": del }).then(touched).catch(unlogged); },
    saveProduct: function (p, oldName) { var body = Object.assign({}, p); if (oldName) body.old_name = oldName; return API.post("/api/product", body).then(touched).catch(unlogged); },
    removeProduct: function (n) { return API.post("/api/product/remove", { name: n }).then(touched).catch(unlogged); },
    addBatch: function (b) { var body = { item: b.item, qty: b.qty, made: b.made }; if (b.expires) body.expires = b.expires; return API.post("/api/batch", body).then(touched).catch(unlogged); },
    editBatch: function (b) { return API.post("/api/batch/edit", b).then(touched).catch(unlogged); },
    deleteBatch: function (id) { return API.post("/api/batch/close", { id: id, status: "deleted" }).then(touched).catch(unlogged); },
    saveMaterial: function (m, oldName) { var body = Object.assign({}, m); if (oldName) body.old_name = oldName; return API.post("/api/material", body).then(touched).catch(unlogged); },
    removeMaterial: function (n) { return API.post("/api/material/remove", { name: n }).then(touched).catch(unlogged); },
    saveRecipe: function (r, old) { var body = Object.assign({}, r); if (old) { body.old_product = old.product; body.old_material = old.material; } return API.post("/api/recipe", body).then(touched).catch(unlogged); },
    removeRecipe: function (r) { return API.post("/api/recipe/remove", { product: r.product, material: r.material }).then(touched).catch(unlogged); },
    upload: function (text) { return API.post("/api/upload", { csv: text }).then(touched).catch(unlogged); },
    toast: function (m) { toast(m); }
  };

  function render() {
    if (!S.st) return;
    renderTop(); renderDay(); renderTabs();
    if (S.tab === "tables") { viewTables(); return; }
    S.sheets = null;
    var v = "";
    if (S.tab === "today") v = S.part === "vitrina" ? viewVitrina() : S.part === "itogi" ? viewItogi() : viewPlan();
    else if (S.tab === "tomorrow") v = viewTomorrow();
    else if (S.tab === "products") v = viewProducts();
    else if (S.tab === "batches") v = viewBatches();
    else if (S.tab === "stock") v = viewStock();
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
    if (t.hasAttribute("data-tab")) {
      S.tab = t.getAttribute("data-tab"); S.edit = null; S.confirm = null;
      if (S.dirty) { S.dirty = false; refresh(true); } else render();
      return;
    }
    if (t.hasAttribute("data-part")) {
      S.part = t.getAttribute("data-part"); S.partManual = true; S.tab = "today";
      if (S.dirty) { S.dirty = false; refresh(true); } else render();
      return;
    }
    var a = t.getAttribute("data-act"), p = t.getAttribute("data-p");
    if (a === "logout") { API.post("/api/logout").catch(function () {}).then(function () { S.st = null; showLogin(); }); }
    else if (a === "reset") { Local.reset(); S.vitrina = {}; refresh(true).then(function () { toast("Пример восстановлен"); }); }
    else if (a === "plan-ok") post("/api/plan/confirm", { day: t.getAttribute("data-day") }, "План подтверждён и пошёл в работу").catch(function () {});
    else if (a === "plan-undo") post("/api/plan/unconfirm", { day: t.getAttribute("data-day") }, "Подтверждение снято — план снова черновик").catch(function () {});
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
    document.body.classList.toggle("server", API.mode === "server");   // в кабинете из бота нижняя панель не нужна
    var m = location.search.match(/[?&]code=(\d{6})/);
    if (API.mode === "server" && m) {
      history.replaceState(null, "", location.pathname);
      return API.post("/api/login", { code: m[1] }).catch(function () {}).then(function () { return refresh(true); });
    }
    return refresh(true);
  });
})();
