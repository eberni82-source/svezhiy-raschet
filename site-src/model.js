// модель прогноза, та же что в bot/core/forecast.py
(function (root) {
  "use strict";
  var WD = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"];
  var WDS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];
  var DEFAULT_RAIN = -0.10, SHARE_EVENING = 0.22, DISCOUNT_K = 1.2;

  // даты как номера дней (UTC), чтобы не зависеть от часового пояса
  function dnum(iso) { var p = iso.split("-"); return Math.round(Date.UTC(+p[0], +p[1] - 1, +p[2]) / 864e5); }
  function diso(n) { return new Date(n * 864e5).toISOString().slice(0, 10); }
  function wday(n) { return (new Date(n * 864e5).getUTCDay() + 6) % 7; }
  function ddmm(n) { var s = diso(n); return s.slice(8, 10) + "." + s.slice(5, 7); }

  function parseTime(s) {
    if (!s) return null;
    s = String(s).trim().replace(".", ":");
    if (s.indexOf(":") < 0) return null;
    var p = s.split(":"), h = parseInt(p[0], 10), m = parseInt(p[1], 10);
    if (isNaN(h) || isNaN(m)) return null;
    return h + m / 60;
  }
  function demand(s, oh, ch) {
    oh = oh == null ? 7 : oh; ch = ch == null ? 21 : ch;
    var t = parseTime(s.soldout);
    if (t == null) return s.sold;
    var f = Math.min(1, Math.max(0, (t - oh) / (ch - oh)));
    return s.sold * (1 + 0.5 * (1 - f));
  }
  function rainEffect(sales) {
    var dry = {}, ratios = [];
    sales.forEach(function (s) {
      if (s.rain === false) { var k = s.product + "|" + wday(s.day); (dry[k] = dry[k] || []).push(demand(s)); }
    });
    sales.forEach(function (s) {
      if (s.rain === true) {
        var d = dry[s.product + "|" + wday(s.day)];
        if (d && d.length >= 2) {
          var b = d.reduce(function (a, x) { return a + x; }, 0) / d.length;
          if (b > 0) ratios.push(demand(s) / b);
        }
      }
    });
    if (ratios.length < 3) return { eff: DEFAULT_RAIN, learned: false };
    var e = ratios.reduce(function (a, x) { return a + x; }, 0) / ratios.length - 1;
    return { eff: Math.max(-0.35, Math.min(0.2, e)), learned: true };
  }
  function criticalRatio(price, cost) {
    if (!price || cost == null || price <= 0) return 0.6;
    return Math.max(0.3, Math.min(0.95, (price - cost) / price));
  }
  // обратная функция нормального распределения (Acklam)
  function invNorm(p) {
    var a = [-39.69683028665376, 220.9460984245205, -275.9285104469687, 138.357751867269, -30.66479806614716, 2.506628277459239],
        b = [-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972, -13.28068155288572],
        c = [-0.007784894002430293, -0.3223964580411365, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783],
        d = [0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416], q, r;
    if (p < 0.02425) { q = Math.sqrt(-2 * Math.log(p)); return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1); }
    if (p > 1 - 0.02425) { q = Math.sqrt(-2 * Math.log(1 - p)); return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1); }
    q = p - 0.5; r = q * q;
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1);
  }
  function signed(x) { var v = Math.round(x); return (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v) + "%"; }

  function avg(xs) { return xs.reduce(function (a, x) { return a + x; }, 0) / xs.length; }
  function signedDeg(x) { return Math.abs(Math.round(x)) + "°"; }
  var TEMP_WINDOW = 365, TEMP_NEAR = 15, TEMP_MIN_DAYS = 60, TEMP_MIN_T = 2.5;

  // на сколько (доля) меняется спрос на каждый градус; 0 - если данных мало или связь ненадёжна
  function temperatureSlope(hist, target, cleanValue, wf) {
    var N = TEMP_WINDOW, y = new Array(N + 1).fill(null), tt = new Array(N + 1).fill(null);
    hist.forEach(function (s) {
      var age = target - s.day;
      if (age >= 1 && age <= N) {
        var f = wf(wday(s.day)) || 1;
        y[age] = (y[age] || 0) + cleanValue(s) / f;
        if (s.temp != null) tt[age] = s.temp;
      }
    });
    function prefix(arr) {
      var sums = [0], cnts = [0];
      arr.forEach(function (v) { sums.push(sums[sums.length - 1] + (v != null ? v : 0)); cnts.push(cnts[cnts.length - 1] + (v != null ? 1 : 0)); });
      return [sums, cnts];
    }
    var py = prefix(y), pt = prefix(tt), xs = [], rs = [];
    for (var age = 1; age <= N; age++) {
      if (y[age] == null || tt[age] == null) continue;
      var lo = Math.max(0, age - TEMP_NEAR), hi = Math.min(N, age + TEMP_NEAR) + 1;
      if (py[1][hi] - py[1][lo] === 0 || pt[1][hi] - pt[1][lo] === 0) continue;
      var localY = (py[0][hi] - py[0][lo]) / (py[1][hi] - py[1][lo]);
      var localT = (pt[0][hi] - pt[0][lo]) / (pt[1][hi] - pt[1][lo]);
      if (localY > 0) { xs.push(tt[age] - localT); rs.push(y[age] / localY); }
    }
    var n = xs.length;
    if (n < TEMP_MIN_DAYS) return 0;
    var mx = avg(xs), mr = avg(rs), vx = 0, cov = 0, i;
    for (i = 0; i < n; i++) { vx += (xs[i] - mx) * (xs[i] - mx); cov += (xs[i] - mx) * (rs[i] - mr); }
    vx /= n; cov /= n;
    if (vx < 4) return 0;
    var slope = cov / vx, res = 0;
    for (i = 0; i < n; i++) { var e = rs[i] - mr - slope * (xs[i] - mx); res += e * e; }
    res /= (n - 2);
    var se = res > 0 ? Math.sqrt(res / (n * vx)) : 0;
    if (se === 0 || Math.abs(slope / se) < TEMP_MIN_T) return 0;
    return Math.max(-0.02, Math.min(0.02, slope * n / (n + 30)));
  }

  // во сколько раз спрос точки год назад в неделю с этой даты отличался от двух недель до неё
  function seasonChange(sales, target, rainEff) {
    var total = {}, a, after = [], before = [], pairs = [];
    sales.forEach(function (s) {
      var age = target - s.day;
      if ((age >= 1 && age <= 56) || (age >= 359 && age <= 379)) {
        var d = demand(s);
        total[age] = (total[age] || 0) + (s.rain === true ? d / (1 + rainEff) : d);
      }
    });
    for (a = 359; a <= 379; a++) { if (!(a in total)) return 1; (a <= 365 ? after : before).push(total[a]); }
    // шум: разница одинаковых дней недели через неделю, последние 8 недель
    for (a = 1; a < 50; a++) {
      if (a in total && (a + 7) in total && total[a] + total[a + 7] > 0) {
        var r = (total[a] - total[a + 7]) / ((total[a] + total[a + 7]) / 2);
        pairs.push(r * r / 2);
      }
    }
    if (pairs.length < 7 || avg(before) <= 0) return 1;
    var se = Math.sqrt(avg(pairs) * (1 / 7 + 1 / 14)), ratio = avg(after) / avg(before);
    return Math.abs(ratio - 1) >= Math.max(0.08, 3 * se) ? ratio : 1;
  }

  function forecast(sales, product, target, rainTomorrow, eventFactor, rainEff, tempTomorrow) {
    eventFactor = eventFactor || 1;
    if (rainEff == null) rainEff = rainEffect(sales).eff;
    var hist = sales.filter(function (s) { return s.product === product.name && s.day < target; })
                    .sort(function (x, y) { return y.day - x.day; });
    var cr = criticalRatio(product.price, product.cost), reasons = [];
    if (!hist.length) {
      if (product.default_qty) {
        var dq = +product.default_qty;
        return { product: product.name, mean: dq, sd: Math.sqrt(dq), qty: Math.max(0, Math.floor(dq + 0.5)), ratio: cr, reasons: ["новый товар: истории пока нет, берём ваш обычный объём"], n: 0 };
      }
      return { product: product.name, mean: 0, sd: 0, qty: 0, ratio: cr, reasons: ["нет истории продаж"], n: 0 };
    }
    function cleanValue(s) { var d = demand(s); return s.rain === true ? d / (1 + rainEff) : d; }

    // спрос, очищенный от дождя, за последние 8 недель
    var clean = {}, temps = {}, days = [];
    hist.forEach(function (s) {
      if (target - s.day > 56) return;
      if (!(s.day in clean)) { clean[s.day] = 0; days.push(s.day); }
      clean[s.day] += cleanValue(s);
      if (s.temp != null) temps[s.day] = s.temp;
    });
    // уровень: среднее за последние 14 дней (сам следует за сезоном)
    var recentDays = days.filter(function (k) { return target - k <= 14; });
    if (recentDays.length < 5) recentDays = days.slice();
    var level = avg(recentDays.map(function (k) { return clean[k]; }));
    var overall = avg(days.map(function (k) { return clean[k]; }));
    function wf(w) {
      var v = days.filter(function (k) { return wday(k) === w; }).map(function (k) { return clean[k]; });
      if (v.length < 2 || overall <= 0) return 1;
      return avg(v) / overall;
    }
    var f = wf(wday(target));
    if (days.length < 14) reasons.push("истории меньше 2 недель — прогноз грубый");
    else if (Math.abs(f - 1) >= 0.08) reasons.push(WD[wday(target)] + ": обычно " + signed((f - 1) * 100) + " к среднему дню");

    var resid = [];
    days.forEach(function (k) { if (target - k <= 28) { var g = wf(wday(k)); if (g > 0) resid.push(clean[k] / g - level); } });
    var v = resid.length >= 4 ? resid.reduce(function (a, r) { return a + r * r; }, 0) / resid.length * 1.25 * f * f : 0;
    var base = level * f;

    // температура: связь ищем по истории позиции, день сравниваем с соседними +-15 днями
    var tempF = 1;
    var levelTemps = recentDays.filter(function (k) { return k in temps; }).map(function (k) { return temps[k]; });
    if (tempTomorrow != null && levelTemps.length) {
      var slope = temperatureSlope(hist, target, cleanValue, wf);
      if (slope) {
        var diff = Math.max(-12, Math.min(12, tempTomorrow - avg(levelTemps)));
        tempF = Math.max(0.75, Math.min(1.3, 1 + slope * diff));
        if (Math.abs(tempF - 1) >= 0.03) reasons.push((diff < 0 ? "холоднее" : "теплее") + " на " + signedDeg(diff) + ": " + signed((tempF - 1) * 100) + " к спросу");
      }
    }

    // сезон: как менялся спрос в эти дни год назад
    var yoy = seasonChange(sales, target, rainEff), yoyF = Math.max(0.6, Math.min(1.6, 1 + 0.7 * (yoy - 1)));
    if (yoy !== 1) reasons.push("сезон: год назад в эти дни спрос " + (yoy > 1 ? "вырос" : "снизился") + " на " + Math.round(Math.abs(yoy - 1) * 100) + "%");

    var lastSame = null;
    for (var i = 0; i < hist.length; i++) if (wday(hist[i].day) === wday(target)) { lastSame = hist[i]; break; }
    var mean = base * tempF * yoyF;
    if (rainTomorrow) { mean *= 1 + rainEff; reasons.push("дождь: " + signed(rainEff * 100) + " к спросу"); }
    if (eventFactor !== 1) { mean *= eventFactor; reasons.push("событие: " + signed((eventFactor - 1) * 100)); }
    if (lastSame && lastSame.soldout && target - lastSame.day <= 7)
      reasons.push(WDS[wday(lastSame.day)] + " " + ddmm(lastSame.day) + " закончились в " + lastSame.soldout + " — спрос был выше продаж");
    var scale = base ? mean / base : 1;
    var sd = Math.max(Math.sqrt(v) * scale, Math.sqrt(Math.max(mean, 0)));
    if (days.length < 14) sd *= 1.3;
    var qty = Math.max(0, Math.floor(mean + invNorm(cr) * sd + 0.5));
    if (product.price && product.cost != null) {
      if (cr >= 0.6) reasons.push("маржа " + Math.round(cr * 100) + "% — берём с запасом");
      else if (cr <= 0.45) reasons.push("маржа " + Math.round(cr * 100) + "% — впритык, остаток дорого обходится");
    }
    return { product: product.name, mean: mean, sd: sd, qty: qty, ratio: cr, reasons: reasons, n: days.length };
  }

  function eveningThreshold(rec) { return Math.max(1, Math.ceil(rec.mean * SHARE_EVENING * DISCOUNT_K)); }
  function discountAdvice(rec, left) {
    var exp = rec.mean * SHARE_EVENING;
    if (left <= exp * DISCOUNT_K) return { pct: 0, why: "обычно после 17:00 уходит ~" + Math.round(exp) + " шт — скидка не нужна" };
    var p = left <= exp * 2 ? 30 : 50;
    return { pct: p, why: "обычно после 17:00 уходит ~" + Math.round(exp) + " шт, на витрине " + left + " — скидка " + p + "%" };
  }

  function backtest(sales, products, nDays) {
    nDays = nDays || 28;
    var withBaked = sales.filter(function (s) { return s.baked != null; });
    if (!withBaked.length) return null;
    var allDays = Array.from(new Set(sales.map(function (s) { return s.day; }))).sort(function (a, b) { return a - b; });
    var test = allDays.slice(-nDays), eff = rainEffect(sales).eff;
    var r = { days: test.length, wasteNow: 0, wasteNew: 0, lostNow: 0, lostNew: 0, wasteUnitsNow: 0, wasteUnitsNew: 0, producedNow: 0, producedNew: 0, perDay: [] };
    test.forEach(function (d) {
      var past = sales.filter(function (s) { return s.day < d; }), day = { day: d, wasteNow: 0, wasteNew: 0 };
      withBaked.forEach(function (s) {
        if (s.day !== d) return;
        var p = products[s.product] || { name: s.product }, price = p.price || 0, cost = p.cost != null ? p.cost : price * 0.4;
        var rec = forecast(past, p, d, s.rain === true, 1, eff, s.temp), dem = demand(s), soldNew = Math.min(rec.qty, dem);
        r.wasteNow += (s.baked - s.sold) * cost; r.wasteNew += (rec.qty - soldNew) * cost;
        r.wasteUnitsNow += s.baked - s.sold; r.wasteUnitsNew += rec.qty - soldNew;
        r.lostNow += Math.max(0, dem - s.sold) * (price - cost); r.lostNew += Math.max(0, dem - rec.qty) * (price - cost);
        r.producedNow += s.baked; r.producedNew += rec.qty;
        day.wasteNow += (s.baked - s.sold) * cost; day.wasteNew += (rec.qty - soldNew) * cost;
      });
      r.perDay.push(day);
    });
    return r;
  }

  function expiryStatus(expires, today) {
    var left = expires - today;
    if (left < 0) return { label: "просрочено", left: left, level: "bad" };
    if (left === 0) return { label: "истекает сегодня", left: left, level: "bad" };
    if (left === 1) return { label: "истекает завтра", left: left, level: "warn" };
    if (left <= 2) return { label: "скоро", left: left, level: "warn" };
    return { label: "в порядке", left: left, level: "ok" };
  }
  function expiryTip(item) {
    var n = item.toLowerCase();
    function has(arr) { return arr.some(function (k) { return n.indexOf(k) >= 0; }); }
    if (has(["слив", "молок", "творог", "сметан", "сыр", "кефир", "йогурт", "маскарпоне"])) return "пустите сегодня в крем, начинку или сырники";
    if (has(["ягод", "вишн", "клубник", "яблок", "фрукт", "банан", "груш"])) return "используйте в начинку или соус сегодня";
    if (has(["курин", "куриц", "ветчин", "мяс", "колбас", "рыб", "лосос"])) return "в сэндвичи и пироги сегодня, остаток — списать";
    if (has(["яйц", "яиц"])) return "в тесто или выпечку сегодня";
    return "используйте в первую очередь или продайте со скидкой";
  }

  var ALIASES = {
    date: ["дата", "date", "день"], product: ["товар", "product", "позиция", "наименование", "item"],
    sold: ["продано", "sold", "продажи", "количество", "qty", "кол-во"], baked: ["испечено", "baked", "произведено", "выпечено", "приготовлено", "заказано", "закуплено", "поставлено"],
    soldout: ["закончилось", "soldout", "распродано_в", "закончился"], price: ["цена", "price"],
    cost: ["себестоимость", "cost", "себес"], rain: ["дождь", "rain", "осадки"], temp: ["температура", "temp", "темп", "t"]
  };
  function num(v) { v = String(v == null ? "" : v).replace(/[\s ]/g, "").replace(",", "."); if (!v) return null; var x = Number(v); return isNaN(x) ? null : x; }
  function parseDate(v) {
    v = String(v).trim(); var m;
    if ((m = v.match(/^(\d{4})-(\d{2})-(\d{2})$/))) return dnum(v);
    if ((m = v.match(/^(\d{1,2})[.\/](\d{1,2})[.\/](\d{2,4})$/))) {
      var y = +m[3]; if (y < 100) y += 2000;
      return dnum(y + "-" + ("0" + m[2]).slice(-2) + "-" + ("0" + m[1]).slice(-2));
    }
    throw new Error("не понял дату «" + v + "» — нужен формат 2026-10-17 или 17.10.2026");
  }
  function splitLine(line, delim) {
    var out = [], cur = "", q = false;
    for (var i = 0; i < line.length; i++) {
      var ch = line[i];
      if (ch === '"') { if (q && line[i + 1] === '"') { cur += '"'; i++; } else q = !q; }
      else if (ch === delim && !q) { out.push(cur); cur = ""; }
      else cur += ch;
    }
    out.push(cur);
    return out;
  }
  function parseCSV(text) {
    text = text.replace(/^﻿/, "");
    var lines = text.split(/\r?\n/).filter(function (l) { return l.trim(); });
    if (lines.length < 2) throw new Error("файл пустой — нужна строка заголовков и хотя бы одна строка продаж");
    var first = lines[0], delim = (first.split(";").length >= first.split(",").length) ? ";" : ",";
    var header = splitLine(first, delim).map(function (h) { return h.trim().toLowerCase().replace(/ё/g, "е"); });
    var idx = {};
    Object.keys(ALIASES).forEach(function (k) {
      idx[k] = -1;
      for (var i = 0; i < ALIASES[k].length; i++) { var j = header.indexOf(ALIASES[k][i]); if (j >= 0) { idx[k] = j; break; } }
    });
    ["date", "product", "sold"].forEach(function (k) {
      if (idx[k] < 0) throw new Error("нет обязательной колонки «" + ALIASES[k][0] + "». Нужны как минимум: дата; товар; продано");
    });
    var sales = [], products = {};
    for (var n = 1; n < lines.length; n++) {
      var r = splitLine(lines[n], delim);
      var get = function (k) { return idx[k] >= 0 && idx[k] < r.length ? r[idx[k]] : ""; };
      var day;
      try { day = parseDate(get("date")); } catch (e) { throw new Error("строка " + (n + 1) + ": " + e.message); }
      var name = String(get("product")).trim(), sold = num(get("sold"));
      if (!name || sold == null) continue;
      var rr = String(get("rain")).trim().toLowerCase();
      var rain = rr === "" ? null : ["1", "да", "yes", "true", "дождь"].indexOf(rr) >= 0;
      sales.push({ day: day, product: name, sold: sold, baked: num(get("baked")), soldout: String(get("soldout")).trim() || null, rain: rain, temp: num(get("temp")) });
      var p = products[name] || (products[name] = { name: name, price: null, cost: null });
      var pr = num(get("price")), co = num(get("cost"));
      if (pr != null) p.price = pr;
      if (co != null) p.cost = co;
    }
    if (!sales.length) throw new Error("не нашёл ни одной строки с продажами");
    return { sales: sales, products: products };
  }

  // прогноз Open-Meteo: день -> { rain, temp }
  function loadWeather(lat, lon, days) {
    if (typeof fetch !== "function") return Promise.resolve({});
    var url = "https://api.open-meteo.com/v1/forecast?latitude=" + lat + "&longitude=" + lon +
      "&daily=precipitation_sum,temperature_2m_max,temperature_2m_min&forecast_days=" + (days || 3) + "&timezone=auto";
    return fetch(url).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      var d = j && j.daily, out = {};
      if (d) d.time.forEach(function (iso, i) {
        var hi = d.temperature_2m_max[i], lo = d.temperature_2m_min[i], mm = d.precipitation_sum[i];
        out[dnum(iso)] = { rain: mm == null ? null : mm >= 1, temp: hi == null || lo == null ? null : Math.round((hi + lo) * 5) / 10 };
      });
      return out;
    }).catch(function () { return {}; });
  }
  function deg(t) { var r = Math.round(t); return (r > 0 ? "+" : r < 0 ? "−" : "") + Math.abs(r) + "°"; }
  // средняя температура за 14 дней до дня
  function recentTemp(sales, day) {
    var seen = {}, xs = [];
    sales.forEach(function (s) { if (s.temp != null && day - s.day >= 1 && day - s.day <= 14 && !(s.day in seen)) { seen[s.day] = 1; xs.push(s.temp); } });
    return xs.length ? avg(xs) : null;
  }

  var api = { dnum: dnum, diso: diso, wday: wday, ddmm: ddmm, WD: WD, WDS: WDS, demand: demand, rainEffect: rainEffect,
    criticalRatio: criticalRatio, forecast: forecast, eveningThreshold: eveningThreshold, discountAdvice: discountAdvice,
    backtest: backtest, expiryStatus: expiryStatus, expiryTip: expiryTip, parseCSV: parseCSV, signed: signed, SHARE_EVENING: SHARE_EVENING,
    loadWeather: loadWeather, deg: deg, recentTemp: recentTemp };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.FreshModel = api;
})(this);
