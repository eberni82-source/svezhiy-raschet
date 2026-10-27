(function () {
  "use strict";
  var M = window.FreshModel;
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function pad(n) { return ("0" + n).slice(-2); }
  function fmt(x) { return Math.round(x).toLocaleString("ru-RU"); }
  function money(x) { return fmt(x) + " ₽"; }
  function nf(x, d) { return (Math.round(x * Math.pow(10, d)) / Math.pow(10, d)).toFixed(d).replace(".", ","); }
  function plural(n, a, b, c) { n = Math.abs(n) % 100; if (n > 10 && n < 15) return c; n %= 10; return n === 1 ? a : n >= 2 && n <= 4 ? b : c; }
  function dayLabel(d) { return M.WDS[M.wday(d)] + ", " + M.ddmm(d); }
  function dateRu(d) { var s = M.diso(d); return s.slice(8, 10) + "." + s.slice(5, 7) + "." + s.slice(0, 4); }
  function byName(a, b) { return a.localeCompare(b, "ru"); }

  var now = new Date();
  var TODAY = M.dnum(now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()));
  var KEY = "svezhiy-mvp-v1";

  // ---------------------------------------------------------------- состояние
  // S сохраняется в браузере: продажи, товары, партии, выбранный день, условия по дням, планы
  function demoState() {
    var r = M.parseCSV(DEMO_CSV);
    var last = Math.max.apply(null, r.sales.map(function (s) { return s.day; })), shift = (TODAY - 1) - last;
    r.sales.forEach(function (s) { s.day += shift; });
    Object.keys(r.products).forEach(function (k) { r.products[k].shelf_life = k === "Эклер" || k === "Сэндвич с курицей" ? 2 : 1; });
    var batches = [["Сливки 33%", "2 л", 1], ["Творог 5%", "3 кг", 2], ["Филе куриное", "1,5 кг", 0], ["Вишня замороженная", "2 кг", 20]]
      .map(function (b, i) { return { id: i + 1, item: b[0], qty: b[1], exp: TODAY + b[2] }; });
    return { v: 1, source: "demo", fileName: "", sales: r.sales, products: r.products, hidden: {}, batches: batches, today: TODAY, cond: {}, plans: {} };
  }
  function fileState(text, name) {
    var r = M.parseCSV(text), last = Math.max.apply(null, r.sales.map(function (s) { return s.day; }));
    return { v: 1, source: "file", fileName: name, sales: r.sales, products: r.products, hidden: {}, batches: [], today: last + 1, cond: {}, plans: {} };
  }
  function restore() {
    try {
      var d = JSON.parse(localStorage.getItem(KEY) || "null");
      if (d && d.v === 1 && Array.isArray(d.sales) && d.sales.length && typeof d.today === "number") return d;
    } catch (e) { /* хранилище недоступно — начинаем с примера */ }
    return demoState();
  }
  var S = restore();
  var U = { open: null, tab: "sheets", btStale: true, err: "", C: null };
  var WX = {};
  var saveT = null, index = null;
  function save() {
    clearTimeout(saveT);
    saveT = setTimeout(function () { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) { /* без хранилища работаем в памяти */ } }, 250);
  }
  function nextId() { return S.batches.reduce(function (m, b) { return Math.max(m, b.id); }, 0) + 1; }

  // ---------------------------------------------------------------- данные
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
  function allProducts() {
    var p = {};
    Object.keys(S.products).forEach(function (k) { p[k] = S.products[k]; });
    S.sales.forEach(function (s) { if (!p[s.product]) p[s.product] = { name: s.product, price: null, cost: null }; });
    Object.keys(S.hidden).forEach(function (k) { delete p[k]; });
    return p;
  }
  function names() { return Object.keys(allProducts()).sort(byName); }

  // погода и событие дня: что указал человек → прогноз Open-Meteo → как было в истории → средняя за 2 недели
  function dayWeather(day) {
    for (var i = 0; i < S.sales.length; i++) {
      var s = S.sales[i];
      if (s.day === day && (s.rain != null || s.temp != null)) return { rain: s.rain, temp: s.temp };
    }
    return null;
  }
  function condFor(day) {
    var c = S.cond[day] || {}, w = WX[day] || {}, h = dayWeather(day) || {}, out = { event: c.event || 1 };
    if (c.rain != null) { out.rain = c.rain; out.rainSrc = "user"; }
    else if (w.rain != null) { out.rain = w.rain; out.rainSrc = "fc"; }
    else if (h.rain != null) { out.rain = h.rain; out.rainSrc = "hist"; }
    else { out.rain = false; out.rainSrc = "none"; }
    if (c.temp != null) { out.temp = c.temp; out.tempSrc = "user"; }
    else if (w.temp != null) { out.temp = w.temp; out.tempSrc = "fc"; }
    else if (h.temp != null) { out.temp = h.temp; out.tempSrc = "hist"; }
    else {
      var avg = M.recentTemp(S.sales, day);
      out.temp = avg == null ? null : Math.round(avg); out.tempSrc = avg == null ? "none" : "avg";
    }
    return out;
  }
  function setCond(day, key, value) {
    var c = S.cond[day] || (S.cond[day] = {});
    if (value == null) delete c[key]; else c[key] = value;
  }

  function compute(day, cond) {
    var eff = M.rainEffect(S.sales), prods = allProducts(), recs = [], habit = {};
    Object.keys(prods).forEach(function (k) {
      var r = M.forecast(S.sales, prods[k], day, cond.rain, cond.event, eff.eff, cond.temp);
      if (r.n > 0 || r.qty > 0) recs.push(r);
    });
    recs.sort(function (a, b) { return b.qty - a.qty || byName(a.product, b.product); });
    recs.forEach(function (r) {
      var same = S.sales.filter(function (s) { return s.product === r.product && s.baked != null && M.wday(s.day) === M.wday(day) && day - s.day > 0 && day - s.day <= 28; });
      if (same.length) habit[r.product] = same.reduce(function (a, s) { return a + s.baked; }, 0) / same.length;
    });
    return { recs: recs, eff: eff, habit: habit };
  }

  var toastT = null;
  function toast(msg) {
    var t = $("toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toastT); toastT = setTimeout(function () { t.hidden = true; }, 2800);
  }

  // ---------------------------------------------------------------- данные: источник
  function renderSource() {
    var np = Object.keys(allProducts()).length, demo = S.source === "demo";
    var h = "<span>Данные: <b>" + (demo ? "Пекарня" : esc(S.fileName)) + "</b> · " + np + " " + plural(np, "позиция", "позиции", "позиций") +
      (S.sales.length ? " · продажи с " + dateRu(firstDay()) + " по " + dateRu(lastDay()) : " · продаж пока нет") + (demo ? " · пример, продажи смоделированы" : "") + "</span>";
    h += '<span class="btn btn-line btn-sm file">Загрузить свой CSV<input type="file" id="file" accept=".csv,.txt,text/csv" aria-label="Загрузить свой CSV"></span>';
    h += '<button type="button" class="btn btn-line btn-sm" id="reset">' + (demo ? "Сбросить пример" : "Вернуть пример") + "</button>";
    if (U.err) h += '<span class="err">Не получилось прочитать файл: ' + esc(U.err) + "</span>";
    $("source").innerHTML = h;
  }

  // ---------------------------------------------------------------- день и условия
  var SRC = { user: "указано вами", fc: "по прогнозу Open-Meteo", hist: "как было в этот день", avg: "средняя за 2 недели", none: "" };
  function renderDay() {
    var d = S.today, c = condFor(d);
    $("today").value = M.diso(d);
    $("today").min = M.diso(firstDay() + 1);
    $("today-wd").textContent = M.WD[M.wday(d)] + (d === TODAY ? " · по календарю сегодня" : "");
    $("day-sub").textContent = S.source === "demo" ? "Челябинск" : "";
    $("w-dry").setAttribute("aria-pressed", String(!c.rain));
    $("w-rain").setAttribute("aria-pressed", String(!!c.rain));
    $("rain-hint").textContent = SRC[c.rainSrc] || "";
    if (document.activeElement !== $("temp")) $("temp").value = c.temp == null ? "" : Math.round(c.temp);
    $("temp-hint").textContent = c.temp == null ? "нет данных — без поправки" : SRC[c.tempSrc];
    $("event").value = String(c.event);
  }

  // ---------------------------------------------------------------- вчерашние продажи
  function yWeather(y) {
    var c = S.cond[y] || {}, h = dayWeather(y) || {}, w = WX[y] || {};
    return { rain: c.rain != null ? c.rain : h.rain != null ? h.rain : w.rain != null ? w.rain : null,
             temp: c.temp != null ? c.temp : h.temp != null ? h.temp : w.temp != null ? w.temp : null };
  }
  function plannedFor(day) {
    if (S.plans[day]) return S.plans[day];
    var out = {};
    compute(day, condFor(day)).recs.forEach(function (r) { out[r.product] = r.qty; });
    return out;
  }
  function val(x) { return x == null ? "" : String(x).replace(".", ","); }
  function renderYesterday() {
    var y = S.today - 1, list = names(), rows = [], plan = null;
    $("y-title").textContent = "Вчера · " + dayLabel(y);
    $("y-sub").textContent = "сколько сделали и продали";
    renderYWeather();
    list.forEach(function (p) {
      var s = saleAt(y, p), baked = s ? s.baked : null;
      if (!s) { plan = plan || plannedFor(y); baked = plan[p] != null ? plan[p] : null; }
      var sold = s ? s.sold : null, left = s && s.soldout ? 0 : baked != null && sold != null ? Math.max(0, baked - sold) : null;
      var lab = esc(p);
      rows.push('<tr data-p="' + esc(p) + '"><td class="name">' + esc(p) + "</td>" +
        '<td class="r"><input class="in cell-in" data-k="baked" inputmode="decimal" autocomplete="off" value="' + esc(val(baked)) + '" aria-label="Сделали: ' + lab + '"></td>' +
        '<td class="r"><input class="in cell-in" data-k="sold" inputmode="decimal" autocomplete="off" value="' + esc(val(sold)) + '" aria-label="Продали: ' + lab + '"></td>' +
        '<td class="r"><input class="in cell-in time" data-k="soldout" inputmode="numeric" autocomplete="off" placeholder="—" value="' + esc(s && s.soldout || "") + '" aria-label="Закончились в: ' + lab + '"></td>' +
        '<td class="r num c-left" data-left>' + (left == null ? "—" : fmt(left)) + "</td></tr>");
    });
    $("y-table").tBodies[0].innerHTML = rows.join("") || '<tr><td colspan="5" class="empty">Нет позиций. Добавьте товары во вкладке «Все данные».</td></tr>';
    renderYesterdayTotals();
  }
  function renderYWeather() {
    var w = yWeather(S.today - 1);
    $("y-rain").checked = w.rain === true;
    if (document.activeElement !== $("y-temp")) $("y-temp").value = w.temp == null ? "" : Math.round(w.temp);
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
      ? "Продажи за вчера внесены — план ниже уже их учитывает."
      : done === 0
        ? "Продаж за вчера нет. Впишите, сколько продали (и во сколько закончилось, если раньше закрытия) — план пересчитается."
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

  // ---------------------------------------------------------------- план
  function whyText(r) {
    return r.reasons.filter(function (x) { return x.indexOf("дождь") !== 0 && x.indexOf("событие") !== 0; }).slice(0, 2).join("; ");
  }
  function condText(c) {
    var t = [c.rain ? "дождь" : "без осадков"];
    if (c.temp != null) t.push(M.deg(c.temp));
    if (c.event !== 1) t.push("событие " + M.signed((c.event - 1) * 100));
    return t.join(", ");
  }
  function renderPlan() {
    var day = S.today, y = day - 1, c = condFor(day), C = compute(day, c), rows = [], tq = 0, tm = 0, ty = 0, th = 0, nh = 0;
    U.C = C;
    $("plan-title").textContent = "План на " + dayLabel(day);
    $("plan-sub").textContent = condText(c);
    C.recs.forEach(function (r) {
      var ys = saleAt(y, r.product), open = U.open === r.product, why = whyText(r);
      tq += r.qty; tm += r.mean; if (ys) ty += ys.sold;
      if (C.habit[r.product] != null) { th += C.habit[r.product]; nh++; } else th += r.qty;
      rows.push('<tr class="row" tabindex="0" data-p="' + esc(r.product) + '" aria-expanded="' + open + '">' +
        '<td><div class="name">' + esc(r.product) + '<span class="chev" aria-hidden="true">' + (open ? "▴" : "▾") + "</span></div>" + (why ? '<div class="why">' + esc(why) + "</div>" : "") + "</td>" +
        '<td class="r num">' + (ys ? fmt(ys.sold) + (ys.soldout ? '<div class="why">до ' + esc(ys.soldout) + "</div>" : "") : '<span class="muted">—</span>') + "</td>" +
        '<td class="r num">≈' + Math.round(r.mean) + ' <span class="sd">±' + Math.round(r.sd) + "</span></td>" +
        '<td class="r"><span class="qty">' + r.qty + "</span></td></tr>");
      if (open) rows.push('<tr class="detail"><td colspan="4">' + breakdown(r, c) + chartBox() + "</td></tr>");
    });
    var tb = $("plan-table").tBodies[0];
    tb.innerHTML = rows.join("") || '<tr><td colspan="4" class="empty">Нет истории продаж для расчёта.</td></tr>';
    $("plan-table").tFoot.innerHTML = C.recs.length
      ? '<tr><td>Всего</td><td class="r num">' + (ty ? fmt(ty) : "—") + '</td><td class="r num">≈' + fmt(tm) + '</td><td class="r"><span class="qty">' + tq + "</span></td></tr>" : "";
    var k = '<span><b>' + tq + "</b>шт сделать</span><span><b>" + C.recs.length + "</b>" + plural(C.recs.length, "позиция", "позиции", "позиций") + "</span>";
    if (nh) {
      var d = Math.round((tq / th - 1) * 100);
      k += "<span>обычно в " + M.WD[M.wday(day)].replace("среда", "среду").replace("пятница", "пятницу").replace("суббота", "субботу") + " делали ~" + fmt(th) +
        (d ? ' <span class="delta ' + (d < 0 ? "down" : "up") + '">' + M.signed(d) + "</span>" : "") + "</span>";
    }
    $("kpis").innerHTML = k;
    if (U.open) renderChart(C.recs.filter(function (r) { return r.product === U.open; })[0]);
  }
  function line(name, value, note, cls) {
    return '<div class="ln' + (cls ? " " + cls : "") + '"><span>' + name + "</span><b>" + value + "</b><span>" + note + "</span></div>";
  }
  function times(v) { return "× " + nf(v, 2); }
  function breakdown(r, c) {
    var p = r.parts, day = S.today, prod = allProducts()[r.product] || {};
    if (!p) return '<p class="muted">Истории продаж нет — берём ваш обычный объём: ' + r.qty + " шт.</p>";
    var h = ['<div class="calc">'];
    h.push(line("Уровень спроса", nf(p.level, 1), "средний день за последние " + p.days + " дн.; " +
      (p.recent ? "вчера весит " + Math.round(p.recent * 100) + "%, каждый день раньше — на 10% меньше" : "за вчера продаж нет — считаем по более ранним дням")));
    h.push(line("День недели", times(p.week), Math.abs(p.week - 1) < 0.02 ? M.WD[M.wday(day)] + " — как средний день" : M.WD[M.wday(day)] + ": обычно " + M.signed((p.week - 1) * 100) + " к среднему дню"));
    h.push(line("Температура", times(p.temp), p.delta != null ? (p.delta < 0 ? "холоднее" : "теплее") + " на " + Math.abs(Math.round(p.delta)) + "°, чем в последние дни" :
      c.temp == null ? "температура не указана" : "надёжной связи с погодой у позиции нет"));
    h.push(line("Сезон", times(p.season), p.season !== 1 ? "год назад в эти дни спрос " + (p.season > 1 ? "рос" : "падал") : "сдвига к прошлому году нет"));
    h.push(line("Дождь", times(p.rain), p.rain !== 1 ? "в дождь спрос ниже на " + Math.round((1 - p.rain) * 100) + "% (по истории)" : "без осадков"));
    h.push(line("Событие", times(p.event), p.event !== 1 ? "указано вами" : "обычный день", "sum"));
    h.push(line("Спрос", "≈ " + nf(r.mean, 1), "разброс по дням ± " + nf(r.sd, 1) + " шт"));
    var reserve = p.z * r.sd, margin = Math.round(r.ratio * 100);
    h.push(line("Запас", (reserve < 0 ? "− " : "+ ") + nf(Math.abs(reserve), 1),
      (prod.price ? "маржа " + margin + "%" : "цена не указана, считаем маржу 60%") + ": " +
      (r.ratio >= 0.5 ? "выгодно, чтобы товара хватало примерно в " + margin + "% дней" : "остаток дороже нехватки — делаем впритык"), "sum"));
    h.push(line("Сделать", r.qty + " шт", "округлено до штуки", "res"));
    h.push("</div>");
    return h.join("");
  }
  function chartBox() {
    return '<div class="mini-chart"><div class="legend"><span><i class="l-sold"></i>Продано</span><span><i class="l-short"></i>Не хватило</span>' +
      '<span><i class="l-fc"></i>Спрос</span><span><i class="l-bake"></i>Сделать</span></div><div class="chart" id="chart"></div></div>';
  }
  function niceStep(max) {
    var raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }
  function renderChart(r) {
    var box = $("chart"); if (!box || !r) return;
    var day = S.today, hist = [];
    for (var d = day - 28; d < day; d++) {
      var s = saleAt(d, r.product);
      hist.push(s ? { day: d, sold: s.sold, short: Math.max(0, M.demand(s) - s.sold) } : { day: d, none: true });
    }
    var W = Math.max(300, Math.round(box.clientWidth || 600)), H = 190, L = 30, R = 8, T = 10, B = 26;
    var slots = hist.length + 2, sw = (W - L - R) / slots, bw = Math.max(3, sw * 0.64), top = Math.max(r.mean + r.sd, r.qty, 1);
    hist.forEach(function (h) { if (!h.none) top = Math.max(top, h.sold + h.short); });
    var step = niceStep(top * 1.05), ymax = Math.ceil(top * 1.05 / step) * step;
    var Y = function (v) { return T + (H - T - B) * (1 - v / ymax); }, g = [];
    g.push('<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(r.product) + ': продажи за 4 недели и прогноз">');
    g.push('<defs><pattern id="hatch" width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line class="hatch-line" x1="0" y1="0" x2="0" y2="5"/></pattern></defs>');
    for (var v = 0; v <= ymax + 1e-9; v += step) {
      g.push('<line class="grid" x1="' + L + '" x2="' + (W - R) + '" y1="' + Y(v).toFixed(1) + '" y2="' + Y(v).toFixed(1) + '"/>');
      g.push('<text class="axis-t" x="' + (L - 5) + '" y="' + (Y(v) + 4).toFixed(1) + '" text-anchor="end">' + Math.round(v) + "</text>");
    }
    hist.forEach(function (h, i) {
      var cx = L + sw * (i + 0.5), x0 = cx - bw / 2;
      if (!h.none) {
        g.push('<rect class="b-sold" x="' + x0.toFixed(1) + '" y="' + Y(h.sold).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + (Y(0) - Y(h.sold)).toFixed(1) + '" rx="2"/>');
        if (h.short > 0.5) g.push('<rect class="b-short" x="' + x0.toFixed(1) + '" y="' + Y(h.sold + h.short).toFixed(1) + '" width="' + bw.toFixed(1) + '" height="' + (Y(h.sold) - Y(h.sold + h.short) - 1).toFixed(1) + '" rx="2"/>');
        if (h.day === day - 1) g.push('<rect class="y-mark" x="' + (x0 - 2).toFixed(1) + '" y="' + (Y(h.sold + h.short) - 2).toFixed(1) + '" width="' + (bw + 4).toFixed(1) + '" height="' + (Y(0) - Y(h.sold + h.short) + 2).toFixed(1) + '" rx="3"/>');
      }
      if (M.wday(h.day) === 0 && day - h.day > 3) g.push('<text class="axis-t" x="' + cx.toFixed(1) + '" y="' + (H - 6) + '" text-anchor="middle">' + M.ddmm(h.day) + "</text>");
    });
    var fx = L + sw * (hist.length + 1.5), fbw = Math.max(bw * 1.3, 8);
    g.push('<rect class="b-fc" x="' + (fx - fbw / 2).toFixed(1) + '" y="' + Y(r.mean).toFixed(1) + '" width="' + fbw.toFixed(1) + '" height="' + (Y(0) - Y(r.mean)).toFixed(1) + '" rx="2"/>');
    g.push('<line class="b-fc-range" x1="' + fx.toFixed(1) + '" x2="' + fx.toFixed(1) + '" y1="' + Y(r.mean + r.sd).toFixed(1) + '" y2="' + Y(Math.max(0, r.mean - r.sd)).toFixed(1) + '"/>');
    g.push('<line class="b-bake" x1="' + (fx - fbw / 2 - 4).toFixed(1) + '" x2="' + (fx + fbw / 2 + 4).toFixed(1) + '" y1="' + Y(r.qty).toFixed(1) + '" y2="' + Y(r.qty).toFixed(1) + '"/>');
    g.push('<text class="axis-t" x="' + fx.toFixed(1) + '" y="' + (H - 6) + '" text-anchor="middle">' + M.WDS[M.wday(day)] + "</text></svg>");
    box.innerHTML = g.join("");
  }

  // ---------------------------------------------------------------- все данные и проверка
  var sheets = null;
  var sheetAdapter = {
    today: null,
    toast: toast,
    load: function () {
      var base = S.today, prods = allProducts(), hist = {}, week = {}, days = [];
      sheetAdapter.today = M.diso(S.today - 1);
      S.sales.forEach(function (s) { (hist[s.product] = hist[s.product] || {})[s.day] = 1; });
      for (var i = 0; i < 7; i++) {
        var d = base + i;
        days.push(M.diso(d));
        compute(d, condFor(d)).recs.forEach(function (r) { (week[r.product] = week[r.product] || [0, 0, 0, 0, 0, 0, 0])[i] = r.qty; });
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
          var st = M.expiryStatus(b.exp, S.today);
          return { id: b.id, item: b.item, qty: b.qty, expires: M.diso(b.exp), label: st.label, level: st.level, tip: st.left <= 1 ? (st.left < 0 ? "спишите" : M.expiryTip(b.item)) : null };
        }),
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
        delete S.products[oldName]; index = null;
      }
      delete S.hidden[p.name];
      S.products[p.name] = { name: p.name, price: p.price, cost: p.cost, shelf_life: p.shelf_life, default_qty: p.default_qty };
      return changed(true);
    },
    removeProduct: function (n) { S.hidden[n] = true; return changed(true); },
    addBatch: function (b) {
      var exp = b.expires ? M.dnum(b.expires) : null, p = allProducts()[b.item];
      if (exp == null) {
        if (!p || !p.shelf_life) return Promise.reject(new Error("Укажите «годен до»"));
        exp = S.today + p.shelf_life;
      }
      S.batches.push({ id: nextId(), item: b.item, qty: b.qty || "", exp: exp });
      return changed(true);
    },
    editBatch: function (b) {
      S.batches.forEach(function (x) { if (x.id === b.id) { x.item = b.item; x.qty = b.qty; x.exp = M.dnum(b.expires); } });
      return changed(true);
    },
    deleteBatch: function (id) { S.batches = S.batches.filter(function (x) { return x.id !== id; }); return changed(true); },
    upload: function (text) {
      try { useFile(text, "Файл из таблицы"); } catch (e) { return Promise.reject(e); }
      return Promise.resolve({ rows: S.sales.length });
    }
  };

  function btBar(label, v, max, cls) {
    return '<div class="bt-bar"><span>' + label + '</span><div class="track"><div class="fill ' + cls + '" style="width:' + (max ? Math.max(1, v / max * 100) : 0).toFixed(1) + '%"></div></div><span class="v">' + money(v) + "</span></div>";
  }
  function renderBacktest() {
    U.btStale = false;
    var b = M.backtest(S.sales, S.products);
    if (!b) {
      $("bt").innerHTML = '<p class="empty">Чтобы сравнить «как было» и «как посоветовала бы модель», нужна колонка «испечено» или «заказано».</p>';
      return;
    }
    var totNow = b.wasteNow + b.lostNow, totNew = b.wasteNew + b.lostNew, max = Math.max(totNow, totNew);
    var row = function (title, a, c) {
      var d = a ? Math.round((c / a - 1) * 100) : 0;
      return '<div class="bt-row"><h4><span>' + title + '</span><span class="delta ' + (c < a ? "down" : "up") + '">' + M.signed(d) + "</span></h4>" +
        btBar("Как было", a, max, "now") + btBar("По плану", c, max, "new") + "</div>";
    };
    var shareNow = b.wasteUnitsNow / b.producedNow * 100, shareNew = b.wasteUnitsNew / b.producedNew * 100;
    $("bt").innerHTML = '<div class="bt-rows">' + row("Списания по себестоимости", b.wasteNow, b.wasteNew) +
      row("Упущенная прибыль, когда товар закончился", b.lostNow, b.lostNew) + row("Итого потери", totNow, totNew) + "</div>" +
      '<p class="hint">Последние ' + b.days + " " + plural(b.days, "день", "дня", "дней") + " истории: на каждый день план построен только по продажам до него и сравнён с тем, что сделали на самом деле. " +
      "Доля списаний " + nf(shareNow, 1) + "% → " + nf(shareNew, 1) + "%." + (S.source === "demo" ? " Продажи примера смоделированы." : "") + "</p>";
  }
  function renderTabs() {
    ["sheets", "bt"].forEach(function (t) {
      $("tab-" + t).setAttribute("aria-selected", String(U.tab === t));
      $("pane-" + t).hidden = U.tab !== t;
    });
    if (U.tab === "bt" && U.btStale) renderBacktest();
    if (U.tab === "sheets") {
      if (!sheets) sheets = new Sheets($("sheets"), sheetAdapter);
      sheets.load();
    }
  }

  // ---------------------------------------------------------------- обновление
  var planT = null, sheetT = null;
  function changed(fromSheets) {
    U.btStale = true;
    save();
    clearTimeout(planT);
    planT = setTimeout(function () {
      renderPlan();
      if (fromSheets) { renderSource(); renderDay(); renderYesterday(); }
    }, 30);
    if (!fromSheets && sheets && U.tab === "sheets") {
      clearTimeout(sheetT);
      sheetT = setTimeout(function () { sheets.load(); }, 400);
    }
    if (U.tab === "bt") renderBacktest();
    return Promise.resolve();
  }
  function renderAll() {
    renderSource(); renderDay(); renderYesterday(); renderPlan();
    if (sheets && U.tab === "sheets") sheets.load();
    if (U.tab === "bt") renderBacktest();
  }
  function goTo(day) {
    var min = firstDay() + 1;
    if (!(day >= min)) day = min;
    S.today = day; U.open = null;
    save(); renderAll();
  }
  function useFile(text, name) {
    S = fileState(text, name); index = null; U.err = ""; U.open = null; U.btStale = true;
    save(); renderAll();
  }

  // ---------------------------------------------------------------- события
  $("source").addEventListener("change", function (e) {
    if (e.target.id !== "file" || !e.target.files || !e.target.files[0]) return;
    var f = e.target.files[0], rd = new FileReader();
    rd.onload = function () {
      var text;
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(rd.result); } catch (x) { text = new TextDecoder("windows-1251").decode(rd.result); }
      try { useFile(text, f.name); toast("Загружено строк: " + S.sales.length); } catch (x) { U.err = x.message; renderSource(); }
    };
    rd.readAsArrayBuffer(f);
    e.target.value = "";
  });
  $("source").addEventListener("click", function (e) {
    if (e.target.id !== "reset") return;
    S = demoState(); index = null; U.err = ""; U.open = null; U.btStale = true;
    save(); renderAll(); toast("Загружен пример «Пекарня»");
  });
  $("today").addEventListener("change", function () { if (this.value) goTo(M.dnum(this.value)); });
  $("day-prev").addEventListener("click", function () { goTo(S.today - 1); });
  $("day-next").addEventListener("click", function () { goTo(S.today + 1); });
  $("w-dry").addEventListener("click", function () { setCond(S.today, "rain", false); save(); renderDay(); renderPlan(); });
  $("w-rain").addEventListener("click", function () { setCond(S.today, "rain", true); save(); renderDay(); renderPlan(); });
  function parseTemp(raw) {
    raw = String(raw).trim().replace(",", ".").replace("−", "-");
    if (raw === "") return null;
    var t = Number(raw);
    return isFinite(t) && t >= -50 && t <= 45 ? t : undefined;
  }
  $("temp").addEventListener("change", function () {
    var t = parseTemp(this.value);
    if (t === undefined) { toast("Температура от −50 до 45 °C"); renderDay(); return; }
    setCond(S.today, "temp", t); save(); renderDay(); renderPlan();
  });
  $("event").addEventListener("change", function () { setCond(S.today, "event", +this.value === 1 ? null : +this.value); save(); renderPlan(); });
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
    if (t === undefined) { toast("Температура от −50 до 45 °C"); renderYesterday(); return; }
    setYesterdayWeather("temp", t);
  });
  function toggleRow(tr) {
    var p = tr.getAttribute("data-p");
    U.open = U.open === p ? null : p;
    renderPlan();
    var again = $("plan-table").querySelector('tr.row[data-p="' + (window.CSS && CSS.escape ? CSS.escape(p) : p) + '"]');
    if (again) again.focus({ preventScroll: true });
  }
  $("plan-table").addEventListener("click", function (e) { var tr = e.target.closest("tr.row"); if (tr) toggleRow(tr); });
  $("plan-table").addEventListener("keydown", function (e) {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr.row")) { e.preventDefault(); toggleRow(e.target); }
  });
  $("go-next").addEventListener("click", function () {
    var plan = {};
    (U.C ? U.C.recs : []).forEach(function (r) { plan[r.product] = r.qty; });
    S.plans[S.today] = plan;
    goTo(S.today + 1);
    toast("Теперь " + dayLabel(S.today) + ". Внесите продажи за вчера");
    $("y-card").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  ["sheets", "bt"].forEach(function (t) { $("tab-" + t).addEventListener("click", function () { U.tab = t; renderTabs(); }); });
  if (window.ResizeObserver) {
    var rt = null;
    new ResizeObserver(function () {
      clearTimeout(rt);
      rt = setTimeout(function () { if (U.open && U.C) renderChart(U.C.recs.filter(function (r) { return r.product === U.open; })[0]); }, 80);
    }).observe($("plan-card"));
  }

  var bot = "https://t.me/" + encodeURIComponent(BOT_USERNAME);
  document.querySelectorAll("[data-bot]").forEach(function (a) {
    a.href = bot + "?" + a.getAttribute("data-bot"); a.target = "_blank"; a.rel = "noopener";
  });
  $("foot").innerHTML = "Пример «Пекарня» построен на смоделированных продажах. Ваши данные и правки хранятся только в этом браузере. " +
    'Бот: <a href="' + bot + '" target="_blank" rel="noopener">@' + esc(BOT_USERNAME) + "</a>";

  renderAll(); renderTabs();
  M.loadWeather(55.16, 61.44, 8).then(function (w) { if (Object.keys(w).length) { WX = w; renderDay(); renderYWeather(); renderPlan(); } });
})();
