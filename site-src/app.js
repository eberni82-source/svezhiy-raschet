(function () {
  "use strict";
  var M = window.FreshModel;
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function pad(n) { return ("0" + n).slice(-2); }
  function fmt(x) { return Math.round(x).toLocaleString("ru-RU"); }
  function money(x) { return fmt(x) + " ₽"; }
  function nf(x, d) { return (Math.round(x * Math.pow(10, d)) / Math.pow(10, d)).toFixed(d).replace(".", ","); }
  function pctDelta(a, b) { return b ? M.signed((a / b - 1) * 100) : "—"; }
  function plural(n, a, b, c) { n = Math.abs(n) % 100; if (n > 10 && n < 15) return c; n %= 10; return n === 1 ? a : n >= 2 && n <= 4 ? b : c; }
  function dayLabel(d) { return M.WDS[M.wday(d)] + ", " + M.ddmm(d); }
  function byName(a, b) { return a.localeCompare(b, "ru"); }

  var now = new Date();
  var TODAY = M.dnum(now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()));
  var KEY = "svezhiy-demo-v3";

  // данные примера и состояние
  // S сохраняется в браузере: продажи, товары, партии, выбранный день, погода и события по дням, планы
  function demoState() {
    var r = M.parseCSV(DEMO_CSV);
    var last = Math.max.apply(null, r.sales.map(function (s) { return s.day; })), shift = (TODAY - 1) - last;
    r.sales.forEach(function (s) { s.day += shift; });
    Object.keys(r.products).forEach(function (k) { r.products[k].shelf_life = k === "Эклер" || k === "Сэндвич с курицей" ? 2 : 1; });
    // сырьё, техкарты и партии примера - из bot/core/materials.py
    var DS = DEMO_STOCK || { materials: [], recipes: [], batches: [] }, mats = {};
    DS.materials.forEach(function (m) { mats[m[0]] = { name: m[0], unit: m[1], delivery: m[2], shelf_life: m[3], cost: m[4] }; });
    var batches = DS.batches.map(function (b, i) {
      var u = mats[b[0]].unit;
      return { id: i + 1, item: b[0], qty: M.fmtAmount(b[1], u), amount: b[1], unit: u, exp: TODAY + b[2], arrived: TODAY };
    });
    return { v: 3, source: "demo", fileName: "", sales: r.sales, products: r.products, hidden: {}, batches: batches, today: TODAY, cond: {}, plans: {}, planAt: {},
             materials: mats, recipes: DS.recipes.map(function (x) { return { product: x[0], material: x[1], norm: x[2] }; }), edited: false };
  }
  function fileState(text, name) {
    var r = M.parseCSV(text), last = Math.max.apply(null, r.sales.map(function (s) { return s.day; }));
    return { v: 3, source: "file", fileName: name, sales: r.sales, products: r.products, hidden: {}, batches: [], today: last + 1, cond: {}, plans: {}, planAt: {},
             materials: {}, recipes: [], edited: false };
  }
  function restore() {
    try {
      var d = JSON.parse(localStorage.getItem(KEY) || "null");
      if (d && d.v === 3 && Array.isArray(d.sales) && d.sales.length && typeof d.today === "number") {
        d.plans = d.plans || {}; d.planAt = d.planAt || {}; d.materials = d.materials || {}; d.recipes = d.recipes || [];
        return d;
      }
    } catch (e) { /* хранилище недоступно — начинаем с примера */ }
    return demoState();
  }
  var TICKET = demoState();   // чек на первом экране всегда по исходному примеру
  var S = restore();
  var U = { tab: "plan", sel: null, msg: "morning", btStale: true, err: "", C: null };
  var WX = {};   // прогноз Open-Meteo для Челябинска по дням
  var saveT = null, index = null;
  function save() {
    U.ver = (U.ver || 0) + 1;
    clearTimeout(saveT);
    saveT = setTimeout(function () { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) { /* без хранилища работаем в памяти */ } }, 250);
  }
  function nextId() { return S.batches.reduce(function (m, b) { return Math.max(m, b.id); }, 0) + 1; }

  function saleAt(day, product) {
    if (!index) { index = {}; S.sales.forEach(function (s) { index[s.day + "|" + s.product] = s; }); }
    return index[day + "|" + product] || null;
  }
  function putSale(s) {
    var old = saleAt(s.day, s.product);
    if (old) S.sales[S.sales.indexOf(old)] = s; else S.sales.push(s);
    index = null;
  }
  function dropSale(day, product) {
    var old = saleAt(day, product);
    if (old) { S.sales.splice(S.sales.indexOf(old), 1); index = null; }
  }
  function firstDay() { return S.sales.length ? Math.min.apply(null, S.sales.map(function (s) { return s.day; })) : S.today - 1; }
  function lastDay() { return S.sales.length ? Math.max.apply(null, S.sales.map(function (s) { return s.day; })) : S.today - 1; }
  function allProducts(data) {
    var p = {};
    Object.keys(data.products).forEach(function (k) { p[k] = data.products[k]; });
    data.sales.forEach(function (s) { if (!p[s.product]) p[s.product] = { name: s.product, price: null, cost: null }; });
    Object.keys(data.hidden || {}).forEach(function (k) { delete p[k]; });
    return p;
  }
  function names() { return Object.keys(allProducts(S)).sort(byName); }

  // погода и событие дня: что указал человек -> прогноз Open-Meteo -> как было в истории -> средняя за 2 недели
  function dayWeather(data, day) {
    for (var i = 0; i < data.sales.length; i++) {
      var s = data.sales[i];
      if (s.day === day && (s.rain != null || s.temp != null)) return { rain: s.rain, temp: s.temp };
    }
    return null;
  }
  function condFor(data, day) {
    var c = (data.cond || {})[day] || {}, w = WX[day] || {}, h = dayWeather(data, day) || {}, out = { event: c.event || 1 };
    if (c.rain != null) { out.rain = c.rain; out.rainSrc = "user"; }
    else if (w.rain != null) { out.rain = w.rain; out.rainSrc = "fc"; }
    else if (h.rain != null) { out.rain = h.rain; out.rainSrc = "hist"; }
    else { out.rain = false; out.rainSrc = "none"; }
    if (c.temp != null) { out.temp = c.temp; out.tempSrc = "user"; }
    else if (w.temp != null) { out.temp = w.temp; out.tempSrc = "fc"; }
    else if (h.temp != null) { out.temp = h.temp; out.tempSrc = "hist"; }
    else {
      var avg = M.recentTemp(data.sales, day);
      out.temp = avg == null ? null : Math.round(avg); out.tempSrc = avg == null ? "none" : "avg";
    }
    return out;
  }
  function setCond(day, key, value) {
    var c = S.cond[day] || (S.cond[day] = {});
    if (value == null) delete c[key]; else c[key] = value;
  }

  function compute(data, day, cond) {
    var eff = M.rainEffect(data.sales), prods = allProducts(data), recs = [], habit = {};
    Object.keys(prods).forEach(function (k) {
      var r = M.forecast(data.sales, prods[k], day, cond.rain, cond.event, eff.eff, cond.temp);
      if (r.n > 0 || r.qty > 0) recs.push(r);
    });
    recs.sort(function (a, b) { return b.qty - a.qty || byName(a.product, b.product); });
    recs.forEach(function (r) {
      var same = data.sales.filter(function (s) { return s.product === r.product && s.baked != null && M.wday(s.day) === M.wday(day) && day - s.day > 0 && day - s.day <= 28; });
      if (same.length) habit[r.product] = same.reduce(function (a, s) { return a + s.baked; }, 0) / same.length;
    });
    return { recs: recs, eff: eff, habit: habit };
  }
  // сырьё: остатки по тому, что произвели, и заказ к ближайшей поставке (M.stockReport)
  function matByName(name) {
    var k = M.mkey(name), hit = null;
    Object.keys(S.materials).forEach(function (n) { if (M.mkey(n) === k) hit = S.materials[n]; });
    return hit;
  }
  var stockMemo = null;
  function stock() {
    if (!Object.keys(S.materials).length) return null;
    var T = S.today, tag = (U.ver || 0) + "|" + T + "|" + Object.keys(WX).length;
    if (stockMemo && stockMemo.tag === tag) return stockMemo.view;
    var made = {}, plan = {}, arr = S.batches.filter(function (b) { return b.amount; }).map(function (b) { return b.arrived == null ? T : b.arrived; });
    for (var d = arr.length ? Math.min.apply(null, arr) : T; d < T; d++) made[d] = Object.assign({}, S.plans[d] || {});
    S.sales.forEach(function (x) { if (made[x.day] && x.baked != null) made[x.day][x.product] = x.baked; });
    for (var i = 0; i < M.HORIZON; i++) plan[T + i] = S.plans[T + i] || qtyMap(compute(S, T + i, condFor(S, T + i)).recs);
    var batches = S.batches.map(function (b) { return { id: b.id, item: b.item, amount: b.amount == null ? null : b.amount, expires: b.exp, arrived: b.arrived == null ? T : b.arrived }; });
    var view = M.stockView(M.stockReport(S.materials, S.recipes, batches, made, plan, T), plan[T]);
    stockMemo = { tag: tag, view: view };
    return view;
  }
  function amountOf(item, qty, amount, unit) {
    var m = matByName(item), r = amount != null && amount !== "" ? M.parseAmount(amount + " " + (unit || ""), m && m.unit) : M.parseAmount(qty, m && m.unit);
    if (m && r.unit && r.unit !== m.unit) throw new Error(m.name + " считается в «" + m.unit + "» — укажите количество в " + m.unit);
    return { qty: r.amount != null ? M.fmtAmount(r.amount, r.unit) : String(qty || "").trim().slice(0, 20), amount: r.amount, unit: r.unit };
  }
  function whyText(r) {
    return r.reasons.filter(function (x) { return x.indexOf("дождь") !== 0 && x.indexOf("событие") !== 0; }).slice(0, 2).join("; ");
  }
  function expiringLines(batches, ref, horizon) {
    return batches.slice().sort(function (a, b) { return a.exp - b.exp; }).filter(function (b) { return b.exp - ref <= horizon; })
      .map(function (b) {
        var st = M.expiryStatus(b.exp, ref);
        return { b: b, st: st, tip: st.left < 0 ? "спишите" : M.expiryTip(b.item) };
      });
  }
  var toastT = null;
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(function () { t.hidden = true; }, 2800);
  }

  // первый экран
  function renderTicket() {
    var day = TODAY + 1, c = condFor(TICKET, day), r = compute(TICKET, day, c), total = 0, habit = 0, h = [];
    h.push('<div class="t-head"><span>СВЕЖИЙ РАСЧЁТ</span><span>06:00</span></div>');
    h.push('<div class="t-sub">План на завтра · ' + dayLabel(day) + (c.tempSrc === "fc" ? " · " + M.deg(c.temp) : "") + "</div>");
    h.push('<hr class="t-cut">');
    r.recs.forEach(function (x) {
      total += x.qty; habit += r.habit[x.product] || x.qty;
      h.push('<div class="t-row"><span class="lbl">' + esc(x.product) + '</span><span class="dots"></span><span class="val">' + x.qty + "</span></div>");
    });
    h.push('<hr class="t-cut">');
    h.push('<div class="t-row t-big"><span class="lbl">ВСЕГО</span><span class="dots"></span><span class="val">' + total + "</span></div>");
    h.push('<div class="t-sub">обычно пекли ~' + Math.round(habit) + ", на " + Math.round(habit - total) + " шт меньше</div>");
    var exp = expiringLines(TICKET.batches, TODAY, 1).filter(function (e) { return e.st.left >= 0; });
    if (exp.length) {
      h.push('<hr class="t-cut"><div class="t-head"><span>СРОКИ</span><span></span></div>');
      exp.forEach(function (e) { h.push('<div class="t-warn">' + esc(e.b.item) + " — " + e.st.label + "</div>"); });
    }
    $("ticket").innerHTML = h.join("");
  }

  // пример: данные, день, погода
  function renderNote() {
    var n = $("data-note"), np = Object.keys(allProducts(S)).length, h;
    var period = S.sales.length ? M.ddmm(firstDay()) + "." + M.diso(firstDay()).slice(0, 4) + " – " + M.ddmm(lastDay()) + "." + M.diso(lastDay()).slice(0, 4) : "продаж пока нет";
    if (S.source === "demo") {
      h = '<span class="tag">Пример</span><span>Пекарня в Челябинске: ' + np + " позиций, продажи и погода за год (смоделированы), " + period + ".</span>";
      if (S.edited) h += '<button type="button" class="mini" id="reset">Сбросить правки</button>';
      h += '<span>Свой файл: колонки <span class="mono">дата;товар;продано</span>, по желанию <span class="mono">испечено (или заказано);закончилось;цена;себестоимость;дождь;температура</span></span>';
    } else {
      h = '<span class="tag own">Ваш файл</span><span>' + esc(S.fileName) + ": " + S.sales.length + " " + plural(S.sales.length, "строка", "строки", "строк") + ", " + np + " " +
        plural(np, "позиция", "позиции", "позиций") + ", " + period + ". Данные остаются в вашем браузере.</span>";
    }
    if (U.err) h += '<span class="err">Не получилось прочитать файл: ' + esc(U.err) + "</span>";
    n.innerHTML = h;
    $("src-demo").setAttribute("aria-pressed", String(S.source === "demo"));
    $("src-file").setAttribute("aria-pressed", String(S.source === "file"));
  }
  function renderDay() {
    var c = condFor(S, S.today);
    $("today").value = M.diso(S.today);
    $("today").min = M.diso(firstDay() + 1);
    $("w-dry").setAttribute("aria-pressed", String(!c.rain));
    $("w-rain").setAttribute("aria-pressed", String(!!c.rain));
    if (document.activeElement !== $("temp")) $("temp").value = c.temp == null ? "" : String(Math.round(c.temp)).replace("-", "−");
    $("event").value = String(c.event);
  }

  // вчерашние продажи
  function yWeather(y) {
    var c = S.cond[y] || {}, h = dayWeather(S, y) || {}, w = WX[y] || {};
    return { rain: c.rain != null ? c.rain : h.rain != null ? h.rain : w.rain != null ? w.rain : null,
             temp: c.temp != null ? c.temp : h.temp != null ? h.temp : w.temp != null ? w.temp : null };
  }
  function val(x) { return x == null ? "" : String(x).replace(".", ","); }
  function renderYWeather() {
    var w = yWeather(S.today - 1);
    $("y-rain").checked = w.rain === true;
    if (document.activeElement !== $("y-temp")) $("y-temp").value = w.temp == null ? "" : String(Math.round(w.temp)).replace("-", "−");
  }
  function renderYesterday() {
    var y = S.today - 1, rows = [], plan = S.plans[y] || {};
    $("y-title").textContent = "Вчера, " + dayLabel(y) + ": сколько сделали и продали";
    renderYWeather();
    names().forEach(function (p) {
      var s = saleAt(y, p), baked = s ? s.baked : plan[p] != null ? plan[p] : null;
      var sold = s ? s.sold : null, left = s && s.soldout ? 0 : baked != null && sold != null ? Math.max(0, baked - sold) : null, lab = esc(p);
      rows.push('<tr data-p="' + esc(p) + '"><td class="name">' + esc(p) + "</td>" +
        '<td class="r"><input type="text" class="cell" data-k="baked" inputmode="decimal" autocomplete="off" value="' + esc(val(baked)) + '" aria-label="Сделали: ' + lab + '"></td>' +
        '<td class="r"><input type="text" class="cell" data-k="sold" inputmode="decimal" autocomplete="off" value="' + esc(val(sold)) + '" aria-label="Продали: ' + lab + '"></td>' +
        '<td class="r"><input type="text" class="cell time" data-k="soldout" inputmode="numeric" autocomplete="off" placeholder="—" value="' + esc(s && s.soldout || "") + '" aria-label="Закончились в: ' + lab + '"></td>' +
        '<td class="r num c-left" data-left>' + (left == null ? "—" : fmt(left)) + "</td></tr>");
    });
    $("y-table").tBodies[0].innerHTML = rows.join("") || '<tr><td colspan="5" class="muted">Нет позиций — добавьте товары во вкладке «Таблицы».</td></tr>';
    renderYesterdayTotals();
  }
  function renderYesterdayTotals() {
    var y = S.today - 1, list = names(), done = 0, baked = 0, sold = 0, left = 0;
    list.forEach(function (p) {
      var s = saleAt(y, p); if (!s) return;
      done++; sold += s.sold;
      if (s.baked != null) { baked += s.baked; if (!s.soldout) left += Math.max(0, s.baked - s.sold); }
    });
    $("y-table").tFoot.innerHTML = done ? '<tr><td>Всего</td><td class="r num">' + fmt(baked) + '</td><td class="r num">' + fmt(sold) + '</td><td></td><td class="r num c-left">' + fmt(left) + "</td></tr>" : "";
    var hint = $("y-hint");
    hint.classList.toggle("warn", done < list.length);
    hint.textContent = !list.length ? "" : done === list.length
      ? "Продажи за вчера внесены — план ниже их учитывает."
      : done === 0
        ? (S.plans[y] ? "«Сделали» — из подтверждённого плана. " : "План на вчера не подтверждали, поэтому он не пошёл в работу. Впишите, сколько сделали на самом деле. ") +
          "Добавьте, сколько продали, и время, если позиция закончилась раньше закрытия, — план пересчитается."
        : "Внесено " + done + " из " + list.length + ". Строки без «Продали» в расчёт не попадают.";
  }
  function parseNum(v) {
    v = String(v).replace(/\s/g, "").replace(",", ".");
    if (v === "") return null;
    var x = Number(v);
    return isFinite(x) && x >= 0 && x <= 100000 ? x : undefined;
  }
  function parseTime(v) {
    v = String(v).trim();
    if (v === "") return null;
    var m = v.match(/^(\d{1,2})[:.]?(\d{2})$/);
    return m && +m[1] <= 23 && +m[2] <= 59 ? pad(+m[1]) + ":" + m[2] : undefined;
  }
  function parseTemp(v) {
    v = String(v).trim().replace(",", ".").replace("−", "-").replace("°", "");
    if (v === "") return null;
    var t = Number(v);
    return isFinite(t) && t >= -50 && t <= 45 ? t : undefined;
  }
  function commitRow(tr) {
    var p = tr.getAttribute("data-p"), y = S.today - 1;
    var get = function (k) { return tr.querySelector('[data-k="' + k + '"]'); };
    var bIn = get("baked"), sIn = get("sold"), tIn = get("soldout");
    [bIn, sIn, tIn].forEach(function (x) { x.classList.remove("bad"); });
    var baked = parseNum(bIn.value), sold = parseNum(sIn.value), so = parseTime(tIn.value);
    if (baked === undefined) { bIn.classList.add("bad"); return toast(p + ": «Сделали» — число от 0"); }
    if (sold === undefined) { sIn.classList.add("bad"); return toast(p + ": «Продали» — число от 0"); }
    if (so === undefined) { tIn.classList.add("bad"); return toast(p + ": время в виде 15:40"); }
    if (so) tIn.value = so;
    if (so && sold == null && baked != null) { sold = baked; sIn.value = val(sold); }
    if (baked != null && sold != null && sold > baked) { sIn.classList.add("bad"); return toast(p + ": продали больше, чем сделали"); }
    var w = yWeather(y);
    if (sold == null) dropSale(y, p);
    else putSale({ day: y, product: p, sold: sold, baked: baked, soldout: so, rain: w.rain, temp: w.temp });
    var left = so ? 0 : baked != null && sold != null ? Math.max(0, baked - sold) : null;
    tr.querySelector("[data-left]").textContent = left == null ? "—" : fmt(left);
    renderYesterdayTotals();
    changed();
  }
  function setYesterdayWeather(key, value) {
    var y = S.today - 1;
    setCond(y, key, value);
    S.sales.forEach(function (s) { if (s.day === y) s[key] = value; });
    changed();
  }

  // план на день
  var SRC = { user: "указано вами", fc: "по прогнозу Open-Meteo", hist: "как было в этот день", avg: "средняя за 2 недели" };
  function renderPlan() {
    var day = S.today, y = day - 1, c = condFor(S, day), C = compute(S, day, c);
    U.C = C;
    if (!U.sel || !C.recs.some(function (r) { return r.product === U.sel; })) U.sel = C.recs.length ? C.recs[0].product : null;
    $("plan-title").textContent = "План на " + dayLabel(day);
    var rows = [], tm = 0, tq = 0, th = 0, ty = 0, hasHabit = Object.keys(C.habit).length > 0, ok = S.plans[day];
    C.recs.forEach(function (r) {
      var hb = C.habit[r.product], ys = saleAt(y, r.product), why = whyText(r), was = ok && ok[r.product] !== r.qty ? ok[r.product] : null;
      tm += r.mean; tq += r.qty; th += hb != null ? hb : r.qty; if (ys) ty += ys.sold;
      rows.push('<tr tabindex="0" data-p="' + esc(r.product) + '" aria-selected="' + (r.product === U.sel) + '">' +
        '<td><div class="name">' + esc(r.product) + "</div>" + (why ? '<div class="why">' + esc(why) + "</div>" : "") + "</td>" +
        '<td class="r num">' + (ys ? fmt(ys.sold) + (ys.soldout ? '<div class="why">до ' + esc(ys.soldout) + "</div>" : "") : '<span class="muted">—</span>') + "</td>" +
        '<td class="r num">≈' + Math.round(r.mean) + ' <span class="muted sdv">±' + Math.round(r.sd) + "</span></td>" +
        '<td class="r"><span class="qty">' + r.qty + "</span>" + (was != null ? '<div class="why">в работе ' + was + "</div>" : "") + "</td>" +
        '<td class="r num muted c-habit">' + (hb != null ? Math.round(hb) : "—") + "</td></tr>");
    });
    $("plan-table").tBodies[0].innerHTML = rows.join("") || '<tr><td colspan="5" class="muted">Нет истории продаж для расчёта.</td></tr>';
    var foot = "";
    if (C.recs.length) {
      var dl = hasHabit ? '<span class="delta ' + (tq < th ? "down" : "up") + '">' + pctDelta(tq, th) + "</span> " : "";
      foot = '<tr><td>Всего</td><td class="r num">' + (ty ? fmt(ty) : "—") + '</td><td class="r num">≈' + Math.round(tm) + '</td><td class="r"><span class="qty">' + tq + "</span></td>" +
        '<td class="r num c-habit">' + dl + (hasHabit ? Math.round(th) : "—") + "</td></tr>";
    }
    $("plan-table").tFoot.innerHTML = foot;
    var e = C.eff, wx = [];
    if (SRC[c.rainSrc]) wx.push("осадки — " + SRC[c.rainSrc]);
    if (c.temp != null) wx.push("температура " + M.deg(c.temp) + " — " + SRC[c.tempSrc]);
    $("rain-note").textContent = (wx.length ? "Погода на " + dayLabel(day) + ": " + wx.join(", ") + ". " : "") +
      "Дождь: " + M.signed(e.eff * 100) + " к спросу" + (e.learned ? " (по истории продаж)." : " (по умолчанию).") + " «Обычно» — сколько делали в этот день недели за 4 недели.";
    renderConfirm(C.recs);
    renderStock();
    renderChart();
  }

  // план идёт в работу только после подтверждения: тогда он становится "Сделали" за этот день
  function qtyMap(recs) {
    var out = {};
    recs.forEach(function (r) { out[r.product] = r.qty; });
    return out;
  }
  function planState(day, recs) {
    var ok = S.plans[day];
    if (!ok) return "draft";
    var same = recs.length === Object.keys(ok).length && recs.every(function (r) { return ok[r.product] === r.qty; });
    return same ? "ok" : "changed";
  }
  function sum(map) { return Object.keys(map).reduce(function (a, k) { return a + map[k]; }, 0); }
  function renderConfirm(recs) {
    var day = S.today, st = planState(day, recs), chip = $("plan-status"), btn = $("confirm-btn"), next = $("go-next"), total = sum(qtyMap(recs)), text;
    if (st === "draft") {
      text = "<b>План не подтверждён.</b> Проверьте цифры и подтвердите: без подтверждения план не уходит в работу и не попадёт во «вчера».";
    } else if (st === "ok") {
      text = "<b>План подтверждён" + (S.planAt[day] ? " в " + S.planAt[day] : "") + ":</b> " + fmt(total) + " шт по " + recs.length + " " +
        plural(recs.length, "позиции", "позициям", "позициям") + ". Вечером внесите продажи — следующий план их учтёт.";
    } else {
      text = "<b>Данные изменились после подтверждения.</b> В работе " + fmt(sum(S.plans[day])) + " шт, новый расчёт — " + fmt(total) + " шт. Подтвердите новый план.";
    }
    chip.className = "chip " + (st === "ok" ? "ok" : "warn");
    chip.textContent = st === "draft" ? "Черновик" : st === "ok" ? "✓ Подтверждён" : "Изменился";
    $("confirm-text").innerHTML = text;
    $("confirm").classList.toggle("ok", st === "ok");
    $("plan-table").classList.toggle("confirmed", st === "ok");
    btn.textContent = st === "draft" ? "Подтвердить план" : st === "ok" ? "Отменить подтверждение" : "Подтвердить новый план";
    btn.className = "btn " + (st === "ok" ? "btn-ghost" : "btn-primary");
    next.className = "btn " + (st === "ok" ? "btn-primary" : "btn-ghost");
    btn.disabled = !recs.length;
  }
  function line(name, value, note, cls) {
    return '<div class="ln' + (cls ? " " + cls : "") + '"><span>' + name + "</span><b>" + value + "</b><span>" + note + "</span></div>";
  }
  function times(v) { return "× " + nf(v, 2); }
  function breakdown(r) {
    var p = r.parts, day = S.today, c = condFor(S, day), prod = allProducts(S)[r.product] || {}, wd = M.WD[M.wday(day)];
    if (!p) return "";
    var h = [];
    h.push(line("Уровень спроса", nf(p.level, 1), "средний день за последние " + p.days + " дн.; " +
      (p.recent ? "вчера весит " + Math.round(p.recent * 100) + "%, каждый день раньше — на 10% меньше" : "за вчера продаж нет — считаем по более ранним дням")));
    h.push(line("День недели", times(p.week), Math.abs(p.week - 1) < 0.02 ? wd + " — как средний день" : wd + ": обычно " + M.signed((p.week - 1) * 100) + " к среднему дню"));
    h.push(line("Температура", times(p.temp), p.delta != null ? (p.delta < 0 ? "холоднее" : "теплее") + " на " + Math.abs(Math.round(p.delta)) + "°, чем в последние дни" :
      c.temp == null ? "температура не указана" : "надёжной связи с погодой у позиции нет"));
    h.push(line("Сезон", times(p.season), p.season !== 1 ? "год назад в эти дни спрос " + (p.season > 1 ? "рос" : "падал") : "сдвига к прошлому году нет"));
    h.push(line("Дождь", times(p.rain), p.rain !== 1 ? "в дождь спрос ниже на " + Math.round((1 - p.rain) * 100) + "%" : "без осадков"));
    h.push(line("Событие", times(p.event), p.event !== 1 ? "указано вами" : "обычный день", "sum"));
    h.push(line("Спрос", "≈ " + nf(r.mean, 1), "разброс по дням ± " + nf(r.sd, 1) + " шт"));
    var reserve = p.z * r.sd, margin = Math.round(r.ratio * 100);
    h.push(line("Запас", (reserve < 0 ? "− " : "+ ") + nf(Math.abs(reserve), 1),
      (prod.price ? "маржа " + margin + "%" : "цена не указана, считаем маржу 60%") + ": " +
      (r.ratio >= 0.5 ? "выгодно, чтобы товара хватало примерно в " + margin + "% дней" : "остаток дороже нехватки — делаем впритык"), "sum"));
    h.push(line("Сделать", r.qty + " шт", "округлено до штуки", "res"));
    return h.join("");
  }

  function niceStep(max) {
    var raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }
  function renderChart() {
    var box = $("chart"), r = U.C && U.C.recs.filter(function (x) { return x.product === U.sel; })[0];
    if (!r) { box.innerHTML = ""; $("breakdown").innerHTML = ""; $("chart-title").textContent = "История позиции"; return; }
    var day = S.today;
    $("chart-title").textContent = r.product + ": как посчитано и 4 недели продаж";
    $("breakdown").innerHTML = breakdown(r);
    var hist = [];
    for (var d = day - 28; d < day; d++) {
      var s = saleAt(d, r.product);
      hist.push(s ? { day: d, sold: s.sold, short: Math.max(0, M.demand(s) - s.sold), s: s } : { day: d, none: true });
    }
    var W = Math.max(300, Math.round(box.clientWidth || 600)), H = 230, L = 34, R = 10, T = 12, B = 30;
    var slots = hist.length + 2, sw = (W - L - R) / slots, bw = Math.max(3, sw * 0.64);
    var ymaxRaw = Math.max(r.mean + r.sd, r.qty, 1);
    hist.forEach(function (h) { if (!h.none) ymaxRaw = Math.max(ymaxRaw, h.sold + h.short); });
    var step = niceStep(ymaxRaw * 1.05), ymax = Math.ceil(ymaxRaw * 1.05 / step) * step;
    var y = function (v) { return T + (H - T - B) * (1 - v / ymax); };
    var g = [];
    g.push('<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="Продажи за 4 недели и прогноз">');
    g.push('<defs><pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line class="hatch-line" x1="0" y1="0" x2="0" y2="5"/></pattern></defs>');
    for (var v = 0; v <= ymax + 1e-9; v += step) {
      g.push('<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v).toFixed(1) + '" y2="' + y(v).toFixed(1) + '"/>');
      g.push('<text class="axis-t" x="' + (L - 6) + '" y="' + (y(v) + 4).toFixed(1) + '" text-anchor="end">' + Math.round(v) + "</text>");
    }
    hist.forEach(function (h, i) {
      var cx = L + sw * (i + 0.5), x = cx - bw / 2;
      if (!h.none) {
        g.push('<rect class="b-sold" x="' + x.toFixed(1) + '" y="' + y(h.sold).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + (y(0) - y(h.sold)).toFixed(1) + '" rx="2"/>');
        if (h.short > 0.5) g.push('<rect class="b-short" x="' + x.toFixed(1) + '" y="' + y(h.sold + h.short).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + (y(h.sold) - y(h.sold + h.short) - 1).toFixed(1) + '" rx="2"/>');
        if (h.s.rain === true) g.push('<circle class="rain-dot" cx="' + cx.toFixed(1) + '" cy="' + (H - B + 7) + '" r="2.6"/>');
        if (h.day === day - 1) g.push('<rect class="y-mark" x="' + (x - 2).toFixed(1) + '" y="' + (y(h.sold + h.short) - 2).toFixed(1) + '" width="' + (bw + 4).toFixed(1) + '" height="' + (y(0) - y(h.sold + h.short) + 2).toFixed(1) + '" rx="3"/>');
      }
      if (M.wday(h.day) === 0 && day - h.day > 3) g.push('<text class="axis-t" x="' + cx.toFixed(1) + '" y="' + (H - 6) + '" text-anchor="middle">' + M.ddmm(h.day) + "</text>");
    });
    var fx = L + sw * (hist.length + 1.5), fbw = Math.max(bw * 1.3, 8);
    g.push('<rect class="b-fc" x="' + (fx - fbw / 2).toFixed(1) + '" y="' + y(r.mean).toFixed(1) + '" width="' + fbw.toFixed(1) + '" height="' + (y(0) - y(r.mean)).toFixed(1) + '" rx="2"/>');
    g.push('<line class="b-fc-range" x1="' + fx.toFixed(1) + '" x2="' + fx.toFixed(1) + '" y1="' + y(r.mean + r.sd).toFixed(1) + '" y2="' + y(Math.max(0, r.mean - r.sd)).toFixed(1) + '"/>');
    g.push('<line class="b-bake" x1="' + (fx - fbw / 2 - 4).toFixed(1) + '" x2="' + (fx + fbw / 2 + 4).toFixed(1) + '" y1="' + y(r.qty).toFixed(1) + '" y2="' + y(r.qty).toFixed(1) + '"/>');
    g.push('<text class="axis-t" x="' + fx.toFixed(1) + '" y="' + (H - 6) + '" text-anchor="middle">' + M.WDS[M.wday(day)] + "</text>");
    hist.forEach(function (h, i) { g.push('<rect class="hit" data-i="' + i + '" x="' + (L + sw * i).toFixed(1) + '" y="' + T + '" width="' + sw.toFixed(1) + '" height="' + (H - T - B) + '"/>'); });
    g.push('<rect class="hit" data-i="fc" x="' + (L + sw * hist.length).toFixed(1) + '" y="' + T + '" width="' + (sw * 2).toFixed(1) + '" height="' + (H - T - B) + '"/>');
    g.push("</svg><div class=\"tip\" hidden></div>");
    box.innerHTML = g.join("");
    var tip = box.querySelector(".tip");
    function show(ev) {
      var i = ev.target.getAttribute("data-i"), html, cx;
      if (i === "fc") {
        cx = fx;
        html = "<b>" + dayLabel(day) + "</b> · прогноз<br>спрос ≈" + Math.round(r.mean) + " ± " + Math.round(r.sd) + "<br>сделать <b>" + r.qty + "</b> шт";
      } else {
        var h = hist[+i]; cx = L + sw * (+i + 0.5);
        if (h.none) html = "<b>" + dayLabel(h.day) + "</b><br>нет данных";
        else html = "<b>" + dayLabel(h.day) + "</b>" + (h.s.rain ? " · дождь" : "") + (h.s.temp != null ? " · " + M.deg(h.s.temp) : "") + "<br>продано " + h.sold + (h.s.baked != null ? " из " + h.s.baked : "") +
          (h.s.soldout ? "<br>закончились в " + esc(h.s.soldout) + ", не хватило ≈" + Math.round(h.short) : (h.s.baked != null ? "<br>осталось " + (h.s.baked - h.sold) : ""));
      }
      tip.innerHTML = html; tip.hidden = false;
      var px = cx * box.clientWidth / W;
      tip.style.left = Math.min(Math.max(px, 70), box.clientWidth - 70) + "px";
      tip.style.top = (T * box.clientWidth / W + 4) + "px";
    }
    box.querySelectorAll(".hit").forEach(function (el) {
      el.addEventListener("pointerenter", show);
      el.addEventListener("pointerdown", show);
    });
    box.querySelector("svg").addEventListener("pointerleave", function () { tip.hidden = true; });
  }

  // сроки годности, проверка, телефон
  function stockAlerts(v) { return M.stockAlerts(v, M.diso(S.today), function (iso) { return dayLabel(M.dnum(iso)); }); }
  function renderStock() {
    var v = stock(), box = $("stock"), note = $("stock-note");
    if (!v) {
      box.innerHTML = '<div class="empty">Сырьё не заведено. Заполните листы «Сырьё» и «Техкарты» во вкладке «Таблицы».</div>';
      note.hidden = true;
      return;
    }
    var al = stockAlerts(v), tom = M.diso(S.today + 1), h = [];
    if (al.length) h.push('<ul class="alerts">' + al.map(function (a) {
      return '<li class="' + a.level + '"><span aria-hidden="true">' + (a.waste ? "🗑" : "⚠️") + "</span><span><b>" + esc(a.name) + ":</b> " + esc(a.text) + "</span></li>";
    }).join("") + "</ul>");
    h.push('<h3 class="plan-h" style="margin-top:18px">Заказ поставщикам</h3><div class="table-wrap"><table class="plan stock-t"><thead><tr><th>Сырьё</th><th class="r">На утро</th>' +
      '<th class="r c-need">Сегодня уйдёт</th><th class="c-del">Поставки</th><th class="c-next">Ближайшая</th><th class="r">Заказать</th></tr></thead><tbody>');
    v.rows.forEach(function (r) {
      var due = r.order > 0 && r.next === tom;
      h.push('<tr class="' + (due ? "due" : "") + '"><td class="name">' + esc(r.name) + '</td><td class="r num">' + esc(r.text.stock) + '</td><td class="r num c-need">' + esc(r.text.need_today) +
        '</td><td class="c-del">' + esc(r.delivery) + '</td><td class="c-next">' + dayLabel(M.dnum(r.next)) + (due ? '<div class="why">заказ сегодня</div>' : "") + '</td><td class="r">' +
        (r.order > 0 ? '<span class="qty">' + esc(r.text.order) + '</span><div class="why"><span class="m-only">' + (due ? "заказ сегодня, " : "к " + M.ddmm(M.dnum(r.next)) + ", ") + "</span>на " +
          r.cover + " " + plural(r.cover, "день", "дня", "дней") + "</div>" : '<span class="muted">не нужно</span>') + "</td></tr>");
    });
    h.push('</tbody></table></div><p class="rain-note">Остаток уменьшается сам: по «Сделали» за прошлые дни, а если не внесено — по подтверждённому плану. ' +
      "Заказ — к ближайшей поставке, чтобы хватило до следующей, с запасом 10% и в пределах срока годности. Расход сырья в примере ориентировочный.</p>");
    box.innerHTML = h.join("");
    var short = al.filter(function (a) { return !a.waste; }).length, waste = al.length - short, due = v.rows.filter(function (r) { return r.order > 0 && r.next === tom; }).length;
    var parts = [];
    if (short) parts.push(short + " " + plural(short, "позиция", "позиции", "позиций") + " не хватит");
    if (waste) parts.push(waste + " спишется, если не пустить в работу");
    if (due) parts.push(due + " заказать сегодня");
    note.hidden = false;
    note.innerHTML = "<b>Сырьё:</b> " + (parts.length ? esc(parts.join(", ")) : "хватает до ближайших поставок") + ' — <button type="button" class="linkbtn" id="go-stock">подробнее</button>';
  }
  function renderBatches() {
    var list = S.batches.slice().sort(function (a, b) { return a.exp - b.exp; }), h = [], v = stock(), now = v ? v.left : {};
    var glyph = { ok: "●", warn: "▲", bad: "✕" };
    renderStock();
    list.forEach(function (b) {
      var st = M.expiryStatus(b.exp, S.today), rest = now[b.id];
      var q = rest != null && b.amount && rest < b.amount - 1e-9 ? "осталось " + M.fmtAmount(rest, b.unit) + " из " + b.qty : b.qty || "";
      h.push('<div class="batch"><div><div class="b-name">' + esc(b.item) + ' <span class="chip ' + st.level + '">' + glyph[st.level] + " " + st.label + "</span></div>" +
        '<div class="b-meta">' + esc(q) + (q ? " · " : "") + "годен до " + dayLabel(b.exp) + "</div></div>" +
        '<div class="b-actions"><button class="mini" data-act="used" data-id="' + b.id + '">Использовано</button><button class="mini" data-act="off" data-id="' + b.id + '">Списано</button></div>' +
        (st.left <= 1 ? '<div class="b-tip">→ ' + (st.left < 0 ? "спишите" : esc(M.expiryTip(b.item))) + "</div>" : "") + "</div>");
    });
    $("batches").innerHTML = h.join("") || '<div class="empty">Партий нет. Добавьте первую: продукт, количество и срок годности.</div>';
  }
  function btBar(label, v, max, cls) {
    return '<div class="bt-bar"><span>' + label + '</span><div class="track"><div class="fill ' + cls + '" style="width:' + (max ? Math.max(1, v / max * 100) : 0).toFixed(1) + '%"></div></div><span class="v">' + money(v) + "</span></div>";
  }
  function renderBacktest() {
    U.btStale = false;
    var b = M.backtest(S.sales, S.products);
    if (!b) {
      $("bt").innerHTML = '<div class="empty">Чтобы сравнить «как было» и «как посоветовала бы модель», нужна колонка <span class="mono">испечено</span> или <span class="mono">заказано</span> в файле.</div>';
      return;
    }
    var totNow = b.wasteNow + b.lostNow, totNew = b.wasteNew + b.lostNew, max = Math.max(totNow, totNew);
    var row = function (title, a, c) {
      return '<div class="bt-row"><h4><span>' + title + '</span><span class="delta ' + (c < a ? "down" : "up") + '">' + pctDelta(c, a) + "</span></h4>" +
        btBar("Как было", a, max, "now") + btBar("По плану", c, max, "new") + "</div>";
    };
    var shareNow = b.wasteUnitsNow / b.producedNow * 100, shareNew = b.wasteUnitsNew / b.producedNew * 100;
    $("bt").innerHTML = '<div class="bt-rows">' +
      row("Списания по себестоимости", b.wasteNow, b.wasteNew) +
      row("Упущенная прибыль, когда товар закончился", b.lostNow, b.lostNew) +
      row("Итого потери", totNow, totNew) + "</div>" +
      '<p class="note">За последние ' + b.days + " " + plural(b.days, "день", "дня", "дней") + ": доля списаний от выпечки " + nf(shareNow, 1) + "% → <b>" + nf(shareNew, 1) + "%</b>. " +
      "Для каждого дня модель видела только продажи до него." + (S.source === "demo" ? " Данные смоделированы." : "") + "</p>";
  }
  function renderPhone() {
    var day = S.today, c = condFor(S, day), out = [], kb = [], time = "06:00";
    var cap = { morning: "Утреннее сообщение в Telegram.", evening: "Вечернее сообщение в Telegram.", closing: "Сообщение перед закрытием." };
    $("msg-caption").textContent = cap[U.msg];
    var recs = U.C ? U.C.recs : [], habitMap = U.C ? U.C.habit : {};
    var byN = recs.slice().sort(function (a, b) { return byName(a.product, b.product); });
    if (U.msg === "morning") {
      var title = day === TODAY ? "План на сегодня" : day === TODAY + 1 ? "План на завтра" : "План";
      out.push("<b>" + title + " · " + dayLabel(day) + "</b>");
      out.push((c.rain ? "Ожидается дождь — спрос ниже на " + Math.abs(Math.round(U.C.eff.eff * 100)) + "%" : "Без осадков") + (c.temp != null ? " · " + M.deg(c.temp) : ""));
      if (c.event !== 1) out.push("Событие: " + M.signed((c.event - 1) * 100) + " к спросу");
      out.push("", "<b>Испечь</b>");
      var total = 0, habit = 0, hasH = Object.keys(habitMap).length > 0;
      recs.forEach(function (r) {
        total += r.qty; habit += habitMap[r.product] != null ? habitMap[r.product] : r.qty;
        var w = whyText(r);
        out.push("• " + esc(r.product) + " — <b>" + r.qty + "</b> шт" + (w ? "\n   <i>" + esc(w) + "</i>" : ""));
      });
      out.push("\nВсего: <b>" + total + "</b> шт");
      if (hasH && habit > total * 1.03) out.push("Обычно в этот день пекли ~" + Math.round(habit) + " шт — план на " + Math.round(habit - total) + " шт меньше");
      var sv = stock(), mk = {};
      if (sv) sv.rows.forEach(function (r) { mk[M.mkey(r.name)] = 1; });
      var exp = expiringLines(S.batches.filter(function (b) { return !(b.amount != null && mk[M.mkey(b.item)]); }), day, 1);
      if (exp.length) {
        out.push("", "<b>Сроки годности</b>");
        exp.forEach(function (e) { out.push("• " + esc(e.b.item) + " (" + esc(e.b.qty) + ") — " + e.st.label + " → " + e.tip); });
      }
      if (sv) {
        var al = stockAlerts(sv), tom = M.diso(day + 1), due = sv.rows.filter(function (r) { return r.order > 0 && r.next === tom; });
        out.push("", "<b>Сырьё</b>");
        if (!al.length && !due.length) out.push("Сырья хватает до ближайших поставок.");
        al.forEach(function (a) { out.push((a.waste ? "🗑 " : "⚠️ ") + esc(a.name) + ": " + esc(a.text)); });
        if (due.length) {
          out.push("", "<b>Заказать сегодня — поставка завтра</b>");
          due.forEach(function (r) { out.push("• " + esc(r.name) + " — <b>" + esc(r.text.order) + "</b>"); });
        }
        out.push("Все остатки и заказы: /syrye");
      }
      var ps = planState(day, recs);
      if (ps === "ok") out.push("\n✅ План подтверждён" + (S.planAt[day] ? " в " + S.planAt[day] : "") + ".");
      else out.push("\n<b>Подтвердите план</b> кнопкой ниже — без подтверждения он не идёт в работу.");
      kb = ps === "ok" ? [] : [ps === "draft" ? "✅ Подтвердить план" : "✅ Подтвердить новый план"];
      if (day > TODAY) kb.push("Завтра событие / праздник");
    } else if (!S.plans[day]) {
      time = U.msg === "evening" ? "17:00" : "21:00";
      out.push("План на сегодня не подтверждён, поэтому в работу не пошёл — " + (U.msg === "evening" ? "советов по витрине не будет" : "итоги по нему не отметить") + ". Подтвердить можно и сейчас:");
      kb = ["✅ Подтвердить план"];
    } else if (U.msg === "evening") {
      time = "17:00";
      out.push("<b>17:00 · проверьте витрину</b>", "Если сейчас на витрине больше, чем указано, — ставьте скидку 30%:");
      byN.forEach(function (r) { out.push("• " + esc(r.product) + " — больше " + M.eveningThreshold(r) + " шт"); });
      out.push("\nТочный совет по позиции: /skidka");
      var ex2 = expiringLines(S.batches, day, 0);
      if (ex2.length) { out.push("", "<b>Истекает сегодня</b>"); ex2.forEach(function (e) { out.push("• " + esc(e.b.item) + " (" + esc(e.b.qty) + ") — " + e.st.label + " → " + e.tip); }); }
    } else {
      time = "21:00";
      out.push("<b>Итоги дня · " + dayLabel(day) + "</b>", "Отметьте по каждой позиции: закончилась (и когда) или сколько осталось. Так прогноз станет точнее.", "", "Отмечено: 0 из " + byN.length);
      kb = byN.map(function (r) { return r.product; });
    }
    $("bubble-wrap").innerHTML = '<div class="bubble">' + out.join("\n") + '<span class="tm">' + time + "</span></div>" +
      (kb.length ? '<div class="tg-kb">' + kb.map(function (k) { return "<span>" + esc(k) + "</span>"; }).join("") + "</div>" : "");
    ["morning", "evening", "closing"].forEach(function (m) { $("m-" + m).setAttribute("aria-pressed", String(U.msg === m)); });
  }
  function renderTabs() {
    ["plan", "exp", "bt", "sheets"].forEach(function (t) {
      $("tab-" + t).setAttribute("aria-selected", String(U.tab === t));
      $("pane-" + t).hidden = U.tab !== t;
    });
    document.querySelector(".console-body").classList.toggle("wide", U.tab === "sheets");
    if (U.tab === "plan") renderChart();
    if (U.tab === "bt" && U.btStale) renderBacktest();
    if (U.tab === "sheets") {
      if (!U.sheets) U.sheets = new Sheets($("sheets"), sheetAdapter);
      U.sheets.load();
    }
  }

  // таблицы
  var sheetAdapter = {
    today: null,
    toast: toast,
    load: function () {
      var base = S.today, prods = allProducts(S), hist = {}, week = {}, days = [], sv = stock();
      sheetAdapter.today = M.diso(S.today - 1);
      S.sales.forEach(function (s) { (hist[s.product] = hist[s.product] || {})[s.day] = 1; });
      for (var i = 0; i < 7; i++) {
        var d = base + i;
        days.push(M.diso(d));
        compute(S, d, condFor(S, d)).recs.forEach(function (r) { (week[r.product] = week[r.product] || [0, 0, 0, 0, 0, 0, 0])[i] = r.qty; });
      }
      var total = function (q) { return q.reduce(function (a, b) { return a + b; }, 0); };
      return Promise.resolve({
        today: sheetAdapter.today,
        sales: S.sales.map(function (s) {
          return { day: M.diso(s.day), product: s.product, baked: s.baked == null ? null : s.baked, sold: s.sold, soldout: s.soldout || null,
                   rain: s.rain == null ? null : s.rain, temp: s.temp == null ? null : s.temp };
        }),
        products: Object.keys(prods).map(function (k) {
          var p = prods[k];
          return { name: k, price: p.price == null ? null : p.price, cost: p.cost == null ? null : p.cost, shelf_life: p.shelf_life == null ? null : p.shelf_life,
                   default_qty: p.default_qty == null ? null : p.default_qty, history_days: hist[k] ? Object.keys(hist[k]).length : 0 };
        }),
        batches: S.batches.slice().sort(function (a, b) { return a.exp - b.exp || a.id - b.id; }).map(function (b) {
          var st = M.expiryStatus(b.exp, S.today), rest = sv ? sv.left[b.id] : null;
          return { id: b.id, item: b.item, qty: b.qty, rest_text: rest == null ? null : M.fmtAmount(rest, b.unit), expires: M.diso(b.exp), label: st.label, level: st.level,
                   tip: st.left <= 1 ? (st.left < 0 ? "спишите" : M.expiryTip(b.item)) : null };
        }),
        materials: Object.keys(S.materials).sort().map(function (k) {
          var m = S.materials[k], r = sv ? sv.rows.filter(function (x) { return x.name === m.name; })[0] : null;
          return { name: m.name, unit: m.unit, delivery: m.delivery, shelf_life: m.shelf_life == null ? null : m.shelf_life, cost: m.cost == null ? null : m.cost,
                   stock_text: r ? r.text.stock : null, order_text: r ? (r.order > 0 ? r.text.order + " к " + M.ddmm(M.dnum(r.next)) : "не нужно") : null };
        }),
        recipes: S.recipes.map(function (r) { var m = matByName(r.material); return { product: r.product, material: r.material, norm: r.norm, unit: m ? m.unit : null }; })
          .sort(function (a, b) { return a.product < b.product ? -1 : a.product > b.product ? 1 : a.material < b.material ? -1 : 1; }),
        week: { days: days, rows: Object.keys(week).map(function (k) { return { product: k, qty: week[k] }; }).sort(function (a, b) { return total(b.qty) - total(a.qty); }) }
      });
    },
    saveSales: function (up, del) {
      del.forEach(function (x) { dropSale(M.dnum(x.day), x.product); });
      up.forEach(function (x) { putSale({ day: M.dnum(x.day), product: x.product, sold: x.sold, baked: x.baked, soldout: x.soldout, rain: x.rain, temp: x.temp }); });
      return changed(true);
    },
    saveProduct: function (p, oldName) {
      if (oldName && oldName !== p.name) {
        S.sales.forEach(function (s) { if (s.product === oldName) s.product = p.name; });
        S.recipes.forEach(function (r) { if (r.product === oldName) r.product = p.name; });
        delete S.products[oldName]; index = null;
      }
      delete S.hidden[p.name];
      S.products[p.name] = { name: p.name, price: p.price, cost: p.cost, shelf_life: p.shelf_life, default_qty: p.default_qty };
      return changed(true);
    },
    removeProduct: function (n) { S.hidden[n] = true; return changed(true); },
    addBatch: function (b) {
      var exp = b.expires ? M.dnum(b.expires) : null, p = allProducts(S)[b.item], m = matByName(b.item), am;
      try { am = amountOf(b.item, b.qty); } catch (e) { return Promise.reject(e); }
      if (exp == null) {
        var life = (p && p.shelf_life) || (m && m.shelf_life);
        if (!life) return Promise.reject(new Error("Укажите «годен до»"));
        exp = S.today + life;
      }
      S.batches.push({ id: nextId(), item: b.item, qty: am.qty, amount: am.amount, unit: am.unit, exp: exp, arrived: S.today });
      return changed(true);
    },
    editBatch: function (b) {
      var am;
      try { am = amountOf(b.item, b.qty); } catch (e) { return Promise.reject(e); }
      S.batches.forEach(function (x) { if (x.id === b.id) { x.item = b.item; x.qty = am.qty; x.amount = am.amount; x.unit = am.unit; x.exp = M.dnum(b.expires); } });
      return changed(true);
    },
    saveMaterial: function (m, oldName) {
      var days, unit;
      try { days = M.parseDelivery(m.delivery); unit = M.normUnit(m.unit); } catch (e) { return Promise.reject(new Error(m.name + ": " + e.message)); }
      if (oldName && oldName !== m.name) {
        delete S.materials[oldName];
        S.recipes.forEach(function (r) { if (r.material === oldName) r.material = m.name; });
        S.batches.forEach(function (x) { if (x.item === oldName) x.item = m.name; });
      }
      S.materials[m.name] = { name: m.name, unit: unit, delivery: M.deliveryText(days), shelf_life: m.shelf_life, cost: m.cost };
      return changed(true);
    },
    removeMaterial: function (n) {
      delete S.materials[n];
      S.recipes = S.recipes.filter(function (r) { return r.material !== n; });
      return changed(true);
    },
    saveRecipe: function (r, old) {
      var m = matByName(r.material);
      if (!m) return Promise.reject(new Error("Сырья «" + r.material + "» нет в списке — добавьте его на листе «Сырьё»"));
      var drop = function (p, mat) { S.recipes = S.recipes.filter(function (x) { return !(x.product === p && x.material === mat); }); };
      if (old) drop(old.product, old.material);
      drop(r.product, m.name);
      S.recipes.push({ product: r.product, material: m.name, norm: r.norm });
      return changed(true);
    },
    removeRecipe: function (r) {
      S.recipes = S.recipes.filter(function (x) { return !(x.product === r.product && x.material === r.material); });
      return changed(true);
    },
    deleteBatch: function (id) { S.batches = S.batches.filter(function (x) { return x.id !== id; }); return changed(true); },
    upload: function (text) {
      try { useFile(text, "Файл из таблицы"); } catch (e) { return Promise.reject(e); }
      return Promise.resolve({ rows: S.sales.length });
    }
  };

  // обновление
  var planT = null;
  function changed(fromSheets) {
    U.btStale = true;
    S.edited = true;
    save();
    clearTimeout(planT);
    planT = setTimeout(function () {
      renderPlan(); renderBatches(); renderPhone(); renderNote();
      if (fromSheets) { renderDay(); renderYesterday(); }
      if (U.tab === "bt") renderBacktest();
    }, 30);
    return Promise.resolve();
  }
  function renderAll() {
    renderNote(); renderDay(); renderYesterday(); renderPlan(); renderBatches(); renderPhone(); renderTabs();
    if (U.tab !== "bt") U.btStale = true;
  }
  function goTo(day) {
    var min = firstDay() + 1;
    S.today = day >= min ? day : min;
    save(); renderAll();
  }
  function useFile(text, name) {
    S = fileState(text, name); index = null; U.err = ""; U.sel = null; U.btStale = true;
    save(); renderAll();
  }
  function useDemo() {
    S = demoState(); index = null; U.err = ""; U.sel = null; U.btStale = true;
    save(); renderAll();
  }

  // калькулятор и бот
  var SUBSCRIPTION = 5900;
  function renderCalc() {
    var rev = +$("c-rev").value, w = +$("c-waste").value / 100, cost = +$("c-cost").value / 100, cut = +$("c-cut").value / 100, days = 30;
    $("o-rev").textContent = money(rev); $("o-waste").textContent = Math.round(w * 100) + "%";
    $("o-cost").textContent = Math.round(cost * 100) + "%"; $("o-cut").textContent = "−" + Math.round(cut * 100) + "%";
    var loss = rev / (1 - w) * w * cost * days, gain = loss * cut, net = gain - SUBSCRIPTION;
    $("r-loss").textContent = money(loss); $("r-save").textContent = money(gain);
    $("r-net").textContent = (net < 0 ? "−" : "") + money(Math.abs(net));
    $("r-roi").textContent = net > 0 ? "Подписка окупается в " + (gain / SUBSCRIPTION).toFixed(1).replace(".", ",") + " раза" : "При таких цифрах подписка не окупается.";
  }
  function renderPilot() {
    var box = $("pilot-cta"), url = "https://t.me/" + encodeURIComponent(BOT_USERNAME);
    document.querySelectorAll("[data-bot]").forEach(function (a) {
      var q = a.getAttribute("data-bot");
      a.href = url + (q ? "?" + q : "");
      a.target = "_blank"; a.rel = "noopener"; a.hidden = false;
    });
    box.innerHTML = '<a class="btn btn-primary" href="' + url + '?start=pilot" target="_blank" rel="noopener">Открыть бота в Telegram</a>' +
      '<a class="btn btn-ghost" href="' + url + '?start=demo" target="_blank" rel="noopener">Посмотреть пример в боте</a>' +
      '<span class="bot-addr">@' + esc(BOT_USERNAME) + "</span>";
  }

  // события
  $("today").addEventListener("change", function () { if (this.value) goTo(M.dnum(this.value)); });
  $("day-prev").addEventListener("click", function () { goTo(S.today - 1); });
  $("day-next").addEventListener("click", function () { goTo(S.today + 1); });
  $("w-dry").addEventListener("click", function () { setCond(S.today, "rain", false); save(); renderDay(); renderPlan(); renderPhone(); });
  $("w-rain").addEventListener("click", function () { setCond(S.today, "rain", true); save(); renderDay(); renderPlan(); renderPhone(); });
  $("temp").addEventListener("change", function () {
    var t = parseTemp(this.value);
    if (t === undefined) { toast("Температура от −50 до 45 °C"); renderDay(); return; }
    setCond(S.today, "temp", t); save(); renderDay(); renderPlan(); renderPhone();
  });
  $("event").addEventListener("change", function () { setCond(S.today, "event", +this.value === 1 ? null : +this.value); save(); renderPlan(); renderPhone(); });
  $("src-demo").addEventListener("click", function () { if (S.source !== "demo") useDemo(); });
  $("data-note").addEventListener("click", function (e) { if (e.target.id === "reset") { useDemo(); toast("Пример «Пекарня» загружен заново"); } });
  $("file").addEventListener("change", function () {
    var f = this.files && this.files[0];
    if (!f) return;
    var rd = new FileReader();
    rd.onload = function () {
      var buf = rd.result, text;
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); } catch (e) { text = new TextDecoder("windows-1251").decode(buf); }
      try { useFile(text, f.name); } catch (e) { U.err = e.message; renderNote(); }
    };
    rd.readAsArrayBuffer(f);
    this.value = "";
  });
  $("y-table").addEventListener("change", function (e) { var tr = e.target.closest("tr[data-p]"); if (tr && e.target.matches("[data-k]")) commitRow(tr); });
  $("y-table").addEventListener("input", function (e) {
    var tr = e.target.closest("tr[data-p]"); if (!tr) return;
    var b = parseNum(tr.querySelector('[data-k="baked"]').value), s = parseNum(tr.querySelector('[data-k="sold"]').value);
    var so = tr.querySelector('[data-k="soldout"]').value.trim();
    tr.querySelector("[data-left]").textContent = so ? "0" : b != null && b !== undefined && s != null && s !== undefined ? fmt(Math.max(0, b - s)) : "—";
  });
  $("y-table").addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || !e.target.matches("[data-k]")) return;
    e.preventDefault();
    var all = Array.prototype.slice.call(this.querySelectorAll("[data-k]")), down = all[all.indexOf(e.target) + 3];
    if (down) down.focus(); else e.target.blur();
  });
  $("y-rain").addEventListener("change", function () { setYesterdayWeather("rain", this.checked); });
  $("y-temp").addEventListener("change", function () {
    var t = parseTemp(this.value);
    if (t === undefined) { toast("Температура от −50 до 45 °C"); renderYWeather(); return; }
    setYesterdayWeather("temp", t);
  });
  $("confirm-btn").addEventListener("click", function () {
    var day = S.today, recs = U.C ? U.C.recs : [];
    if (planState(day, recs) === "ok") {
      delete S.plans[day]; delete S.planAt[day];
      toast("Подтверждение снято — план снова черновик");
    } else {
      var d = new Date();
      S.plans[day] = qtyMap(recs); S.planAt[day] = pad(d.getHours()) + ":" + pad(d.getMinutes());
      toast("План на " + dayLabel(day) + " подтверждён и пошёл в работу");
    }
    S.edited = true;
    save(); renderPlan(); renderBatches(); renderPhone(); renderNote();
  });
  $("go-next").addEventListener("click", function () {
    var day = S.today, inWork = !!S.plans[day];
    goTo(day + 1);
    toast(inWork ? "Теперь " + dayLabel(S.today) + ". Внесите продажи за вчера" : "План на " + dayLabel(day) + " не подтвердили — в работу он не пошёл");
    $("y-title").scrollIntoView({ behavior: "smooth", block: "center" });
  });
  ["plan", "exp", "bt", "sheets"].forEach(function (t) { $("tab-" + t).addEventListener("click", function () { U.tab = t; renderTabs(); }); });
  ["morning", "evening", "closing"].forEach(function (m) { $("m-" + m).addEventListener("click", function () { U.msg = m; renderPhone(); }); });
  $("stock-note").addEventListener("click", function (e) {
    if (e.target.id !== "go-stock") return;
    U.tab = "exp"; renderTabs();
    $("tab-exp").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("plan-table").addEventListener("click", function (e) {
    var tr = e.target.closest("tr[data-p]"); if (!tr) return;
    U.sel = tr.getAttribute("data-p");
    this.querySelectorAll("tbody tr").forEach(function (x) { x.setAttribute("aria-selected", String(x === tr)); });
    renderChart();
  });
  $("plan-table").addEventListener("keydown", function (e) {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr[data-p]")) { e.preventDefault(); e.target.click(); }
  });
  $("batches").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-id]"); if (!b) return;
    var id = +b.getAttribute("data-id");
    S.batches = S.batches.filter(function (x) { return x.id !== id; });
    changed();
  });
  $("b-date").value = M.diso(S.today + 2);
  $("add-batch").addEventListener("submit", function (e) {
    e.preventDefault();
    var item = $("b-item").value.trim(), dt = $("b-date").value;
    if (!item || !dt) return;
    var am;
    try { am = amountOf(item, $("b-qty").value); } catch (x) { toast(x.message); return; }
    S.batches.push({ id: nextId(), item: item.slice(0, 60), qty: am.qty, amount: am.amount, unit: am.unit, exp: M.dnum(dt), arrived: S.today });
    $("b-item").value = ""; $("b-qty").value = "";
    changed();
  });
  ["c-rev", "c-waste", "c-cost", "c-cut"].forEach(function (id) { $(id).addEventListener("input", renderCalc); });
  if (window.ResizeObserver) {
    var rt = null;
    new ResizeObserver(function () { clearTimeout(rt); rt = setTimeout(function () { if (U.tab === "plan") renderChart(); }, 80); }).observe($("chart"));
  }
  renderTicket(); renderAll(); renderCalc(); renderPilot();
  M.loadWeather(55.16, 61.44, 8).then(function (w) {
    if (!Object.keys(w).length) return;
    WX = w; renderTicket(); renderDay(); renderYWeather(); renderPlan(); renderPhone();
  });
})();
