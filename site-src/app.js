(function () {
  "use strict";
  var M = window.FreshModel;
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s).replace(/[&<>"]/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]; }); }
  function pad(n) { return ("0" + n).slice(-2); }
  function fmt(x) { return Math.round(x).toLocaleString("ru-RU"); }
  function money(x) { return fmt(x) + " ₽"; }
  function pctDelta(a, b) { return b ? M.signed((a / b - 1) * 100) : "—"; }
  function plural(n, a, b, c) { n = Math.abs(n) % 100; if (n > 10 && n < 15) return c; n %= 10; return n === 1 ? a : n >= 2 && n <= 4 ? b : c; }
  function dayLabel(d) { return M.WDS[M.wday(d)] + ", " + M.ddmm(d); }

  var now = new Date();
  var TODAY = M.dnum(now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate()));

  function loadDemo() {
    var r = M.parseCSV(DEMO_CSV);
    var last = Math.max.apply(null, r.sales.map(function (s) { return s.day; }));
    var shift = (TODAY - 1) - last;
    r.sales.forEach(function (s) { s.day += shift; });
    return r;
  }
  var uid = 0;
  function demoBatches() {
    return [["Сливки 33%", "2 л", 1], ["Творог 5%", "3 кг", 2], ["Филе куриное", "1,5 кг", 0], ["Вишня замороженная", "2 кг", 20]]
      .map(function (b) { return { id: ++uid, item: b[0], qty: b[1], exp: TODAY + b[2] }; });
  }

  var DEMO = loadDemo();
  var S = { data: DEMO, source: "demo", fileName: "", dayIdx: 1, rain: false, event: 1, tab: "plan", sel: null,
            msg: "morning", batches: demoBatches(), err: "" };

  function lastDay(data) { return Math.max.apply(null, data.sales.map(function (s) { return s.day; })); }
  function baseDay() { return S.source === "demo" ? TODAY : lastDay(S.data) + 1; }
  function target() { return baseDay() + S.dayIdx; }

  function allProducts(data) {
    var p = {};
    Object.keys(data.products).forEach(function (k) { p[k] = data.products[k]; });
    data.sales.forEach(function (s) { if (!p[s.product]) p[s.product] = { name: s.product, price: null, cost: null }; });
    return p;
  }
  function compute(data, day, rain, event) {
    var eff = M.rainEffect(data.sales), prods = allProducts(data), recs = [], habit = {};
    Object.keys(prods).forEach(function (k) {
      var r = M.forecast(data.sales, prods[k], day, rain, event, eff.eff);
      if (r.n > 0) recs.push(r);
    });
    recs.sort(function (a, b) { return b.qty - a.qty; });
    recs.forEach(function (r) {
      var same = data.sales.filter(function (s) { return s.product === r.product && s.baked != null && M.wday(s.day) === M.wday(day) && day - s.day > 0 && day - s.day <= 28; });
      if (same.length) habit[r.product] = same.reduce(function (a, s) { return a + s.baked; }, 0) / same.length;
    });
    return { recs: recs, eff: eff, habit: habit };
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

  /* ---------------- чек на первом экране ---------------- */
  function renderTicket() {
    var day = TODAY + 1, c = compute(DEMO, day, false, 1), total = 0, habit = 0, h = [];
    h.push('<div class="t-head"><span>СВЕЖИЙ РАСЧЁТ</span><span>06:00</span></div>');
    h.push('<div class="t-sub">План на завтра · ' + dayLabel(day) + '</div>');
    h.push('<hr class="t-cut">');
    c.recs.forEach(function (r) {
      total += r.qty; habit += c.habit[r.product] || r.qty;
      h.push('<div class="t-row"><span class="lbl">' + esc(r.product) + '</span><span class="dots"></span><span class="val">' + r.qty + '</span></div>');
    });
    h.push('<hr class="t-cut">');
    h.push('<div class="t-row t-big"><span class="lbl">ВСЕГО</span><span class="dots"></span><span class="val">' + total + '</span></div>');
    h.push('<div class="t-sub">обычно пекли ~' + Math.round(habit) + ', на ' + Math.round(habit - total) + ' шт меньше</div>');
    var exp = expiringLines(S.batches, TODAY, 1).filter(function (e) { return e.st.left >= 0; });
    if (exp.length) {
      h.push('<hr class="t-cut"><div class="t-head"><span>СРОКИ</span><span></span></div>');
      exp.forEach(function (e) { h.push('<div class="t-warn">' + esc(e.b.item) + ' — ' + e.st.label + '</div>'); });
    }
    $("ticket").innerHTML = h.join("");
  }

  /* ---------------- пульт ---------------- */
  function renderDays() {
    var sel = $("day"), b = baseDay(), opts = [];
    for (var i = 0; i < 7; i++) {
      var d = b + i, pre = d === TODAY ? "Сегодня, " : d === TODAY + 1 ? "Завтра, " : "";
      opts.push('<option value="' + i + '"' + (i === S.dayIdx ? " selected" : "") + ">" + pre + dayLabel(d) + "</option>");
    }
    sel.innerHTML = opts.join("");
  }
  function renderNote() {
    var n = $("data-note"), d = S.data, days = d.sales.map(function (s) { return s.day; });
    var first = Math.min.apply(null, days), last = Math.max.apply(null, days), np = Object.keys(allProducts(d)).length;
    var h = S.source === "demo"
      ? '<span class="tag">Пример</span><span>Пекарня в Челябинске: ' + np + " позиций, продажи за 8 недель (смоделированы).</span>"
      : '<span class="tag own">Ваш файл</span><span>' + esc(S.fileName) + ": " + d.sales.length + " " + plural(d.sales.length, "строка", "строки", "строк") + ", " + np + " " + plural(np, "позиция", "позиции", "позиций") + ", " + M.ddmm(first) + "–" + M.ddmm(last) + ". Данные остаются в вашем браузере.</span>";
    if (S.err) h += '<span class="err">Не получилось прочитать файл: ' + esc(S.err) + "</span>";
    if (S.source === "demo") h += '<span>Свой файл: колонки <span class="mono">дата;товар;продано</span>, по желанию <span class="mono">испечено (или заказано);закончилось;цена;себестоимость;дождь</span></span>';
    n.innerHTML = h;
  }

  var C = null;
  function renderPlan() {
    var day = target();
    C = compute(S.data, day, S.rain, S.event);
    if (!S.sel || !C.recs.some(function (r) { return r.product === S.sel; })) S.sel = C.recs.length ? C.recs[0].product : null;
    var rows = [], tm = 0, tq = 0, th = 0, hasHabit = Object.keys(C.habit).length > 0;
    C.recs.forEach(function (r) {
      var hb = C.habit[r.product];
      tm += r.mean; tq += r.qty; th += hb != null ? hb : r.qty;
      var why = whyText(r);
      rows.push('<tr tabindex="0" data-p="' + esc(r.product) + '" aria-selected="' + (r.product === S.sel) + '">' +
        '<td><div class="name">' + esc(r.product) + "</div>" + (why ? '<div class="why">' + esc(why) + "</div>" : "") + "</td>" +
        '<td class="r num">≈' + Math.round(r.mean) + ' <span class="muted sdv">±' + Math.round(r.sd) + "</span></td>" +
        '<td class="r"><span class="qty">' + r.qty + "</span></td>" +
        '<td class="r num muted">' + (hb != null ? Math.round(hb) : "—") + "</td></tr>");
    });
    var tb = $("plan-table").tBodies[0];
    tb.innerHTML = rows.join("") || '<tr><td colspan="4" class="muted">Нет истории продаж для расчёта.</td></tr>';
    var foot = "";
    if (C.recs.length) {
      var dl = hasHabit ? '<span class="delta ' + (tq < th ? "down" : "up") + '">' + pctDelta(tq, th) + "</span> " : "";
      foot = '<tr><td>Всего</td><td class="r num">≈' + Math.round(tm) + '</td><td class="r"><span class="qty">' + tq + "</span></td>" +
        '<td class="r num">' + dl + (hasHabit ? Math.round(th) : "—") + "</td></tr>";
    }
    $("plan-table").tFoot.innerHTML = foot;
    var e = C.eff;
    $("rain-note").textContent = "Дождь: " + M.signed(e.eff * 100) + " к спросу" + (e.learned ? " (по истории продаж)." : " (по умолчанию).") + " «Обычно» — среднее за 4 недели в этот день недели.";
    renderChart();
  }

  function niceStep(max) {
    var raw = max / 4, p = Math.pow(10, Math.floor(Math.log10(raw))), n = raw / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }
  function renderChart() {
    var box = $("chart"), r = C && C.recs.filter(function (x) { return x.product === S.sel; })[0];
    if (!r) { box.innerHTML = ""; $("chart-title").textContent = "История позиции"; return; }
    var day = target();
    $("chart-title").textContent = r.product + ": 4 недели продаж и прогноз на " + dayLabel(day);
    var hist = [];
    for (var d = day - 28; d < day; d++) {
      var s = S.data.sales.filter(function (x) { return x.product === r.product && x.day === d; })[0];
      if (s && d <= lastDay(S.data)) hist.push({ day: d, sold: s.sold, short: Math.max(0, M.demand(s) - s.sold), s: s });
      else hist.push({ day: d, none: true });
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
      }
      if (M.wday(h.day) === 0) g.push('<text class="axis-t" x="' + cx.toFixed(1) + '" y="' + (H - 6) + '" text-anchor="middle">' + M.ddmm(h.day) + "</text>");
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
        html = "<b>" + dayLabel(day) + "</b> · прогноз<br>спрос ≈" + Math.round(r.mean) + " ± " + Math.round(r.sd) + "<br>испечь <b>" + r.qty + "</b> шт";
      } else {
        var h = hist[+i]; cx = L + sw * (+i + 0.5);
        if (h.none) html = "<b>" + dayLabel(h.day) + "</b><br>нет данных";
        else html = "<b>" + dayLabel(h.day) + "</b>" + (h.s.rain ? " · дождь" : "") + "<br>продано " + h.sold + (h.s.baked != null ? " из " + h.s.baked : "") +
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

  function renderBatches() {
    var list = S.batches.slice().sort(function (a, b) { return a.exp - b.exp; }), h = [];
    var glyph = { ok: "●", warn: "▲", bad: "✕" };
    list.forEach(function (b) {
      var st = M.expiryStatus(b.exp, TODAY);
      h.push('<div class="batch"><div><div class="b-name">' + esc(b.item) + ' <span class="chip ' + st.level + '">' + glyph[st.level] + " " + st.label + "</span></div>" +
        '<div class="b-meta">' + esc(b.qty || "") + (b.qty ? " · " : "") + "годен до " + dayLabel(b.exp) + "</div></div>" +
        '<div class="b-actions"><button class="mini" data-act="used" data-id="' + b.id + '">Использовано</button><button class="mini" data-act="off" data-id="' + b.id + '">Списано</button></div>' +
        (st.left <= 1 ? '<div class="b-tip">→ ' + (st.left < 0 ? "спишите" : esc(M.expiryTip(b.item))) + "</div>" : "") + "</div>");
    });
    $("batches").innerHTML = h.join("") || '<div class="empty">Партий нет. Добавьте первую: продукт, количество и срок годности.</div>';
  }

  function btBar(label, v, max, cls) {
    return '<div class="bt-bar"><span>' + label + '</span><div class="track"><div class="fill ' + cls + '" style="width:' + (max ? Math.max(1, v / max * 100) : 0).toFixed(1) + '%"></div></div><span class="v">' + money(v) + "</span></div>";
  }
  function renderBacktest() {
    var b = M.backtest(S.data.sales, S.data.products);
    if (!b) {
      $("bt").innerHTML = '<div class="empty">Чтобы сравнить «как было» и «как посоветовала бы модель», нужна колонка <span class="mono">испечено</span> или <span class="mono">заказано</span> в файле.</div>';
      return;
    }
    var totNow = b.wasteNow + b.lostNow, totNew = b.wasteNew + b.lostNew, max = Math.max(totNow, totNew);
    var row = function (title, a, c, good) {
      var down = c < a;
      return '<div class="bt-row"><h4><span>' + title + '</span><span class="delta ' + (down ? "down" : "up") + '">' + pctDelta(c, a) + "</span></h4>" +
        btBar("Как было", a, max, "now") + btBar("По плану", c, max, "new") + "</div>";
    };
    var shareNow = b.wasteUnitsNow / b.producedNow * 100, shareNew = b.wasteUnitsNew / b.producedNew * 100;
    $("bt").innerHTML = '<div class="bt-rows">' +
      row("Списания по себестоимости", b.wasteNow, b.wasteNew) +
      row("Упущенная прибыль, когда товар закончился", b.lostNow, b.lostNew) +
      row("Итого потери", totNow, totNew) + "</div>" +
      '<p class="note">За последние ' + b.days + " " + plural(b.days, "день", "дня", "дней") + ": доля списаний от выпечки " + shareNow.toFixed(1).replace(".", ",") + "% → <b>" + shareNew.toFixed(1).replace(".", ",") + "%</b>. " +
      "Для каждого дня модель видела только продажи до него." + (S.source === "demo" ? " Данные смоделированы." : "") + "</p>";
  }

  /* ---------------- сообщение в Telegram ---------------- */
  function renderPhone() {
    var day = target(), out = [], kb = [], time = "06:00";
    var cap = { morning: "Утреннее сообщение в Telegram.", evening: "Вечернее сообщение в Telegram.", closing: "Сообщение перед закрытием." };
    $("msg-caption").textContent = cap[S.msg];
    var byName = C.recs.slice().sort(function (a, b) { return a.product < b.product ? -1 : 1; });
    if (S.msg === "morning") {
      var title = day === TODAY ? "План на сегодня" : day === TODAY + 1 ? "План на завтра" : "План";
      out.push("<b>" + title + " · " + dayLabel(day) + "</b>");
      if (S.rain) out.push("Ожидается дождь — спрос ниже на " + Math.abs(Math.round(C.eff.eff * 100)) + "%");
      else out.push("Без осадков");
      if (S.event !== 1) out.push("Событие: " + M.signed((S.event - 1) * 100) + " к спросу");
      out.push("", "<b>Испечь</b>");
      var total = 0, habit = 0, hasH = Object.keys(C.habit).length > 0;
      C.recs.forEach(function (r) {
        total += r.qty; habit += C.habit[r.product] != null ? C.habit[r.product] : r.qty;
        var w = whyText(r);
        out.push("• " + esc(r.product) + " — <b>" + r.qty + "</b> шт" + (w ? "\n   <i>" + esc(w) + "</i>" : ""));
      });
      out.push("\nВсего: <b>" + total + "</b> шт");
      if (hasH && habit > total * 1.03) out.push("Обычно в этот день пекли ~" + Math.round(habit) + " шт — план на " + Math.round(habit - total) + " шт меньше");
      var exp = expiringLines(S.batches, TODAY, Math.max(1, day - TODAY + 1));
      if (exp.length) {
        out.push("", "<b>Сроки годности</b>");
        exp.forEach(function (e) { out.push("• " + esc(e.b.item) + " (" + esc(e.b.qty) + ") — " + e.st.label + " → " + e.tip); });
      }
      kb = ["Завтра событие / праздник"];
    } else if (S.msg === "evening") {
      time = "17:00";
      out.push("<b>17:00 · проверьте витрину</b>", "Если сейчас на витрине больше, чем указано, — ставьте скидку 30%:");
      byName.forEach(function (r) { out.push("• " + esc(r.product) + " — больше " + M.eveningThreshold(r) + " шт"); });
      out.push("\nТочный совет по позиции: /skidka");
      var ex2 = expiringLines(S.batches, TODAY, 0);
      if (ex2.length) { out.push("", "<b>Истекает сегодня</b>"); ex2.forEach(function (e) { out.push("• " + esc(e.b.item) + " (" + esc(e.b.qty) + ") — " + e.st.label + " → " + e.tip); }); }
    } else {
      time = "21:00";
      out.push("<b>Итоги дня · " + dayLabel(day) + "</b>", "Отметьте по каждой позиции: закончилась (и когда) или сколько осталось. Так прогноз станет точнее.", "", "Отмечено: 0 из " + byName.length);
      kb = byName.map(function (r) { return r.product; });
    }
    $("bubble-wrap").innerHTML = '<div class="bubble">' + out.join("\n") + '<span class="tm">' + time + "</span></div>" +
      (kb.length ? '<div class="tg-kb">' + kb.map(function (k) { return "<span>" + esc(k) + "</span>"; }).join("") + "</div>" : "");
  }

  function renderTabs() {
    ["plan", "exp", "bt"].forEach(function (t) {
      $("tab-" + t).setAttribute("aria-selected", String(S.tab === t));
      $("pane-" + t).hidden = S.tab !== t;
    });
    if (S.tab === "plan") renderChart();
  }
  function renderAll() {
    renderNote(); renderDays(); renderPlan(); renderBatches(); renderBacktest(); renderPhone(); renderTabs();
    $("w-dry").setAttribute("aria-pressed", String(!S.rain)); $("w-rain").setAttribute("aria-pressed", String(S.rain));
    $("src-demo").setAttribute("aria-pressed", String(S.source === "demo")); $("src-file").setAttribute("aria-pressed", String(S.source === "file"));
    ["morning", "evening", "closing"].forEach(function (m) { $("m-" + m).setAttribute("aria-pressed", String(S.msg === m)); });
  }

  /* ---------------- калькулятор ---------------- */
  var DEMO_CUT = null;
  var SUBSCRIPTION = 5999; // ₽ в месяц за точку
  function renderCalc() {
    var rev = +$("c-rev").value, w = +$("c-waste").value / 100, cost = +$("c-cost").value / 100, cut = +$("c-cut").value / 100, days = 30;
    $("o-rev").textContent = money(rev); $("o-waste").textContent = Math.round(w * 100) + "%";
    $("o-cost").textContent = Math.round(cost * 100) + "%"; $("o-cut").textContent = "−" + Math.round(cut * 100) + "%";
    var loss = rev / (1 - w) * w * cost * days, save = loss * cut, net = save - SUBSCRIPTION;
    $("r-loss").textContent = money(loss); $("r-save").textContent = money(save);
    $("r-net").textContent = (net < 0 ? "−" : "") + money(Math.abs(net));
    $("r-roi").textContent = net > 0 ? "Подписка окупается в " + (save / SUBSCRIPTION).toFixed(1).replace(".", ",") + " раза" : "При таких цифрах подписка не окупается.";
    if ($("r-formula")) $("r-formula").textContent = "товар на полке = выручка ÷ (1 − " + Math.round(w * 100) + "%); потери на просрочке = товар × " + Math.round(w * 100) + "% × себестоимость " + Math.round(cost * 100) + "% × 30 дней";
  }

  /* ---------------- подключение ---------------- */
  function renderPilot() {
    var box = $("pilot-cta");
    if (BOT_USERNAME) {
      var base = "https://t.me/" + encodeURIComponent(BOT_USERNAME);
      document.querySelectorAll("[data-bot]").forEach(function (a) {
        var q = a.getAttribute("data-bot");
        a.href = base + (q ? "?" + q : "");
        a.target = "_blank"; a.rel = "noopener"; a.hidden = false;
      });
    }
    if (BOT_USERNAME) {
      var url = "https://t.me/" + encodeURIComponent(BOT_USERNAME);
      box.innerHTML = '<a class="btn btn-primary" href="' + url + '?start=pilot" target="_blank" rel="noopener">Открыть бота в Telegram</a>' +
        '<a class="btn btn-ghost" href="' + url + '?start=demo" target="_blank" rel="noopener">Посмотреть пример в боте</a>' +
        '<span class="bot-addr">@' + esc(BOT_USERNAME) + "</span>";
    } else {
      box.innerHTML = '<span class="btn btn-primary" aria-disabled="true">Бот запускается</span><span>Ссылка на Telegram-бота появится здесь в ближайшее время.</span>';
    }
  }

  /* ---------------- события ---------------- */
  $("day").addEventListener("change", function () { S.dayIdx = +this.value; renderPlan(); renderPhone(); });
  $("event").addEventListener("change", function () { S.event = +this.value; renderPlan(); renderPhone(); });
  $("w-dry").addEventListener("click", function () { S.rain = false; renderAll(); });
  $("w-rain").addEventListener("click", function () { S.rain = true; renderAll(); });
  $("src-demo").addEventListener("click", function () { S.data = DEMO; S.source = "demo"; S.err = ""; S.dayIdx = 1; S.sel = null; renderAll(); });
  $("file").addEventListener("change", function () {
    var f = this.files && this.files[0];
    if (!f) return;
    var rd = new FileReader();
    rd.onload = function () {
      var buf = rd.result, text;
      try { text = new TextDecoder("utf-8", { fatal: true }).decode(buf); } catch (e) { text = new TextDecoder("windows-1251").decode(buf); }
      try {
        var d = M.parseCSV(text);
        S.data = d; S.source = "file"; S.fileName = f.name; S.err = ""; S.dayIdx = 0; S.sel = null;
      } catch (e) { S.err = e.message; }
      renderAll();
    };
    rd.readAsArrayBuffer(f);
    this.value = "";
  });
  ["plan", "exp", "bt"].forEach(function (t) { $("tab-" + t).addEventListener("click", function () { S.tab = t; renderTabs(); }); });
  ["morning", "evening", "closing"].forEach(function (m) { $("m-" + m).addEventListener("click", function () { S.msg = m; renderAll(); }); });
  $("plan-table").addEventListener("click", function (e) {
    var tr = e.target.closest("tr[data-p]"); if (!tr) return;
    S.sel = tr.getAttribute("data-p");
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
    renderBatches(); renderPhone(); renderTicket();
  });
  $("b-date").value = M.diso(TODAY + 2);
  $("add-batch").addEventListener("submit", function (e) {
    e.preventDefault();
    var item = $("b-item").value.trim(), dt = $("b-date").value;
    if (!item || !dt) return;
    S.batches.push({ id: ++uid, item: item.slice(0, 60), qty: $("b-qty").value.trim().slice(0, 20), exp: M.dnum(dt) });
    $("b-item").value = ""; $("b-qty").value = "";
    renderBatches(); renderPhone();
  });
  ["c-rev", "c-waste", "c-cost", "c-cut"].forEach(function (id) { $(id).addEventListener("input", renderCalc); });
  if (window.ResizeObserver) {
    var rt = null;
    new ResizeObserver(function () { clearTimeout(rt); rt = setTimeout(function () { if (S.tab === "plan") renderChart(); }, 80); }).observe($("chart"));
  }

  /* ---------------- старт ---------------- */
  var bt0 = M.backtest(DEMO.sales, DEMO.products);
  if (bt0) {
    var cutPct = Math.round((1 - bt0.wasteNew / bt0.wasteNow) * 100);
    if ($("hf-waste")) $("hf-waste").textContent = "−" + cutPct + "%";
  }
  renderTicket(); renderAll(); renderCalc(); renderPilot();
})();
