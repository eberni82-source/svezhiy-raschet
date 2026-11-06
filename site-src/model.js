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
  var LEVEL_DAYS = 14, LEVEL_DECAY = 0.9;
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
    // профиль недели: во сколько раз этот день недели выше среднего
    var overall = avg(days.map(function (k) { return clean[k]; }));
    function wf(w) {
      var v = days.filter(function (k) { return wday(k) === w; }).map(function (k) { return clean[k]; });
      if (v.length < 2 || overall <= 0) return 1;
      return avg(v) / overall;
    }
    // уровень: последние 14 дней, каждый приведён к среднему дню недели;
    // чем ближе день, тем больше вес: вчера - 1, позавчера - 0.9, и так далее
    var recentDays = days.filter(function (k) { return target - k <= LEVEL_DAYS; });
    if (recentDays.length < 5) recentDays = days.slice();
    var weights = {}, wsum = 0, level = 0;
    recentDays.forEach(function (k) {
      var g = wf(wday(k));
      if (g <= 0) return;
      weights[k] = Math.pow(LEVEL_DECAY, target - k - 1);
      wsum += weights[k]; level += weights[k] * clean[k] / g;
    });
    level = wsum ? level / wsum : 0;
    var f = wf(wday(target));
    if (days.length < 14) reasons.push("истории меньше 2 недель — прогноз грубый");
    else if (Math.abs(f - 1) >= 0.08) reasons.push(WD[wday(target)] + ": обычно " + signed((f - 1) * 100) + " к среднему дню");

    var resid = [];
    days.forEach(function (k) { if (target - k <= 28) { var g = wf(wday(k)); if (g > 0) resid.push(clean[k] / g - level); } });
    var v = resid.length >= 4 ? resid.reduce(function (a, r) { return a + r * r; }, 0) / resid.length * 1.25 * f * f : 0;
    var base = level * f;

    // температура: связь ищем по истории позиции, день сравниваем с соседними +-15 днями
    var tempF = 1, diff = null;
    var tempDays = recentDays.filter(function (k) { return k in weights && k in temps; });
    if (tempTomorrow != null && tempDays.length) {
      var slope = temperatureSlope(hist, target, cleanValue, wf);
      if (slope) {
        var tw = 0, tsum = 0;
        tempDays.forEach(function (k) { tw += weights[k]; tsum += weights[k] * temps[k]; });
        diff = Math.max(-12, Math.min(12, tempTomorrow - tsum / tw));
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
    var z = invNorm(cr), qty = Math.max(0, Math.floor(mean + z * sd + 0.5));
    if (product.price && product.cost != null) {
      if (cr >= 0.6) reasons.push("маржа " + Math.round(cr * 100) + "% — берём с запасом");
      else if (cr <= 0.45) reasons.push("маржа " + Math.round(cr * 100) + "% — впритык, остаток дорого обходится");
    }
    // из чего сложился прогноз: уровень × день недели × температура × сезон × дождь × событие
    var parts = { level: level, days: Object.keys(weights).length, recent: (target - 1) in weights ? weights[target - 1] / wsum : 0,
                  week: f, temp: tempF, delta: diff, season: yoyF, rain: rainTomorrow ? 1 + rainEff : 1, event: eventFactor, z: z };
    return { product: product.name, mean: mean, sd: sd, qty: qty, ratio: cr, reasons: reasons, n: days.length, parts: parts };
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

  // ---------------------------------------------------------------- сырьё: техкарты, остатки, заказ
  // та же логика, что в bot/core/materials.py
  var UNITS = { "кг": ["г", 1000], "л": ["мл", 1000], "шт": ["шт", 1] };
  var UNIT_WORDS = { "кг": "кг", "kg": "кг", "килограмм": "кг", "г": "г", "гр": "г", "грамм": "г", "g": "г",
    "л": "л", "l": "л", "литр": "л", "литра": "л", "литров": "л", "мл": "мл", "ml": "мл",
    "шт": "шт", "штук": "шт", "штуки": "шт", "штука": "шт", "pcs": "шт", "уп": "шт" };
  var DAY_WORDS = ["понед", "вторн", "сред", "четв", "пятн", "субб", "воскр"];
  var RESERVE = 0.10, HORIZON = 21, EPS = 1e-9;
  function mkey(name) { return String(name || "").toLowerCase().split(/\s+/).filter(Boolean).join(" "); }
  function rnd(x, digits) { var p = Math.pow(10, digits || 0); return Math.floor(x * p + 0.5) / p; }
  function normUnit(text) {
    var u = UNIT_WORDS[String(text || "").trim().toLowerCase().replace(/\.$/, "")];
    if (!UNITS[u]) throw new Error("единица сырья: кг, л или шт");
    return u;
  }
  function smallUnit(unit) { return (UNITS[unit] || ["шт", 1])[0]; }
  function unitFactor(unit) { return (UNITS[unit] || ["шт", 1])[1]; }
  function parseDelivery(text) {
    var s = String(text || "").toLowerCase().replace(/ё/g, "е"), found = {};
    if (!s.trim() || s.indexOf("ежеднев") >= 0 || s.indexOf("каждый день") >= 0 || s.indexOf("все дни") >= 0) return [0, 1, 2, 3, 4, 5, 6];
    if (s.indexOf("будн") >= 0) [0, 1, 2, 3, 4].forEach(function (i) { found[i] = 1; });
    WDS.forEach(function (short, i) {
      if (s.indexOf(DAY_WORDS[i]) >= 0 || new RegExp("(^|[^а-я])" + short + "([^а-я]|$)").test(s)) found[i] = 1;
    });
    var out = Object.keys(found).map(Number).sort(function (a, b) { return a - b; });
    if (!out.length) throw new Error("дни поставок: например «пн, чт» или «ежедневно»");
    return out;
  }
  function deliveryText(days) { return days.length === 7 ? "ежедневно" : days.map(function (i) { return WDS[i]; }).join(", "); }
  function nextDelivery(days, after) { var d = after + 1; while (days.indexOf(wday(d)) < 0) d++; return d; }
  function parseAmount(text, unit) {
    var m = String(text == null ? "" : text).trim().toLowerCase().replace(",", ".").match(/^(\d+(?:\.\d+)?)\s*([a-zа-я.]*)/);
    if (!m) return { amount: null, unit: null };
    var x = parseFloat(m[1]), w = UNIT_WORDS[m[2].replace(/\.$/, "")];
    if (w === "г") return { amount: x / 1000, unit: "кг" };
    if (w === "мл") return { amount: x / 1000, unit: "л" };
    return { amount: x, unit: w || unit || null };
  }
  function fmtAmount(x, unit) {
    if (!UNITS[unit]) return String(rnd(x, 1)).replace(".", ",") + (unit ? " " + unit : "");
    if (unit === "шт") return rnd(x) + " шт";
    if (x > 0 && x < 1) return rnd(x * 1000) + " " + smallUnit(unit);
    return String(rnd(x, 1)).replace(".", ",") + " " + unit;
  }
  function roundUp(x, unit) {
    if (x <= EPS) return 0;
    return unit === "шт" ? Math.ceil(x - EPS) : Math.ceil(x * 10 - EPS) / 10;
  }
  function perPiece(materials, recipes) {
    var mats = {}, out = {};
    Object.keys(materials).forEach(function (k) { mats[mkey(materials[k].name)] = materials[k]; });
    recipes.forEach(function (r) {
      var mk = mkey(r.material);
      if (mats[mk] && r.norm > 0) (out[r.product] = out[r.product] || []).push([mk, r.norm / unitFactor(mats[mk].unit)]);
    });
    return out;
  }
  function needsOf(per, dayPlan) {
    var out = {};
    Object.keys(dayPlan || {}).forEach(function (p) {
      (per[p] || []).forEach(function (x) { out[x[0]] = (out[x[0]] || 0) + dayPlan[p] * x[1]; });
    });
    return out;
  }
  function takeFrom(batches, day, amount) {
    for (var i = 0; i < batches.length && amount > EPS; i++) {
      var b = batches[i];
      if (b.left <= EPS || b.arrived > day || b.expires < day) continue;
      var t = Math.min(b.left, amount); b.left -= t; amount -= t;
    }
    return amount > EPS ? amount : 0;
  }
  // materials: { имя: { name, unit, delivery, shelf_life, cost } }, recipes: [{ product, material, norm }]
  // batches: [{ id, item, amount, expires, arrived }] — дни числами, made и plan: { день: { товар: штук } }
  function stockReport(materials, recipes, batches, made, plan, today) {
    var mats = {}, per = perPiece(materials, recipes), pool = {};
    Object.keys(materials).forEach(function (k) { mats[mkey(materials[k].name)] = materials[k]; });
    batches.forEach(function (b) {
      if (b.amount == null || b.amount <= 0) return;
      var k = mkey(b.item);
      (pool[k] = pool[k] || []).push({ id: b.id, left: +b.amount, expires: b.expires, arrived: b.arrived });
    });
    var arrived = [];
    Object.keys(pool).forEach(function (k) {
      pool[k].sort(function (a, b) { return a.expires - b.expires || a.id - b.id; });
      pool[k].forEach(function (b) { arrived.push(b.arrived); });
    });
    for (var d = arrived.length ? Math.min.apply(null, arrived) : today; d < today; d++) {
      var nd = needsOf(per, made[d]);
      Object.keys(nd).forEach(function (mk) { if (pool[mk]) takeFrom(pool[mk], d, nd[mk]); });
    }
    var leftNow = {}, uses = {};
    Object.keys(pool).forEach(function (k) { pool[k].forEach(function (b) { leftNow[b.id] = b.left; }); });
    Object.keys(per).forEach(function (p) { per[p].forEach(function (x) { (uses[x[0]] = uses[x[0]] || []).push([p, x[1]]); }); });
    var rows = Object.keys(mats).sort().map(function (mk) {
      var m = mats[mk], days = parseDelivery(m.delivery), first = nextDelivery(days, today);
      var cover = nextDelivery(days, first) - first;
      if (m.shelf_life) cover = Math.min(cover, Math.max(1, Math.floor(m.shelf_life)));
      var end = Math.min(first + cover, today + HORIZON);
      var bs = (pool[mk] || []).map(function (b) { return { id: b.id, left: b.left, expires: b.expires, arrived: b.arrived }; });
      var stock = bs.reduce(function (a, b) { return a + (b.expires >= today && b.arrived <= today ? b.left : 0); }, 0);
      var needToday = needsOf(per, plan[today])[mk] || 0;
      var shortToday = 0, shortBefore = 0, unmetCover = 0, needCover = 0, shortFirst = null;
      for (var dd = today; dd < end; dd++) {
        var n = needsOf(per, plan[dd])[mk] || 0;
        if (dd >= first) needCover += n;
        var u = takeFrom(bs, dd, n);
        if (u > 0) {
          if (dd === today) shortToday = u;
          if (dd < first) { shortBefore += u; if (shortFirst == null) shortFirst = dd; } else unmetCover += u;
        }
      }
      var waste = bs.filter(function (b) { return b.left > EPS && b.expires < end; }).map(function (b) { return { id: b.id, left: b.left, expires: b.expires }; });
      var later = bs.reduce(function (a, b) { return a + (b.expires >= end ? b.left : 0); }, 0);
      var us = (uses[mk] || []).slice().sort(function (a, b) { return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] - b[1]; });
      return { name: m.name, unit: m.unit, cost: m.cost == null ? null : m.cost, delivery: deliveryText(days),
        stock: stock, need_today: needToday, short_today: shortToday, short_first: shortFirst, short_before: shortBefore,
        next: first, cover: cover, order: roundUp(unmetCover + RESERVE * needCover - later, m.unit), need_cover: needCover,
        waste: waste, waste_total: waste.reduce(function (a, w) { return a + w.left; }, 0), uses: us };
    });
    return { rows: rows, left: leftNow };
  }
  function wasteHint(row, planToday) {
    var extra = [];
    row.uses.forEach(function (u) { var n = u[1] > 0 ? Math.floor(row.waste_total / u[1] + EPS) : 0; if (n > 0) extra.push([n, u[0]]); });
    if (!extra.length) return "";
    var q = function (p) { return (planToday || {})[p] || 0; };
    extra.sort(function (a, b) { return q(b[1]) - q(a[1]) || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0); });
    return "можно сделать больше: " + extra.slice(0, 2).map(function (x) { return x[1] + " +" + x[0]; }).join(" или ");
  }

  // отчёт по сырью в виде для экрана — тот же, что отдаёт сервер бота (web.py, Cabinet._stock)
  function stockView(rep, planToday) {
    var iso = function (d) { return d == null ? null : diso(d); };
    return { rows: rep.rows.map(function (r) {
      var first = r.waste.length ? Math.min.apply(null, r.waste.map(function (w) { return w.expires; })) : null;
      var rest = roundUp(r.short_before - r.short_today, r.unit), f = function (x) { return fmtAmount(x, r.unit); };
      return { name: r.name, unit: r.unit, delivery: r.delivery, cost: r.cost, stock: r.stock, need_today: r.need_today,
        short_today: r.short_today, short_before: r.short_before, short_rest: rest, cover: r.cover, order: r.order, waste_total: r.waste_total,
        short_first: iso(r.short_first), next: iso(r.next), waste_first: iso(first), uses: r.uses.map(function (u) { return u[0]; }),
        hint: r.waste.length ? (wasteHint(r, planToday) || expiryTip(r.name)) : null,
        text: { stock: f(r.stock), need_today: f(r.need_today), short_today: f(roundUp(r.short_today, r.unit)),
                short_before: f(roundUp(r.short_before, r.unit)), short_rest: f(rest), order: f(r.order), waste_total: f(r.waste_total) } };
    }), left: rep.left };
  }
  // что важно по сырью сегодня: нехватка и будущее списание. label(iso) — подпись дня, «пн, 09.11»
  function stockAlerts(view, todayIso, label) {
    var out = [], T = dnum(todayIso);
    view.rows.forEach(function (r) {
      if (r.short_today > 0) {
        out.push({ level: "bad", name: r.name, text: "на сегодня нужно " + r.text.need_today + ", есть " + r.text.stock + " — не хватит " + r.text.short_today +
          (r.short_rest > 0 ? ", до поставки " + label(r.next) + " — ещё " + r.text.short_rest : "") });
      } else if (r.short_before > 0) {
        out.push({ level: "warn", name: r.name, text: "хватит до " + label(diso(dnum(r.short_first) - 1)) + ", поставка " + label(r.next) + " — докупите " + r.text.short_before });
      }
      if (r.waste_total > 0) {
        var st = expiryStatus(dnum(r.waste_first), T);
        out.push({ level: st.left < 0 ? "bad" : "warn", name: r.name, waste: true, text: st.left < 0 ? "просрочено — спишите " + r.text.waste_total
          : st.label + " — по плану не уйдёт " + r.text.waste_total + (r.cost ? " (≈ " + Math.round(r.waste_total * r.cost).toLocaleString("ru-RU") + " ₽)" : "") + " → " + r.hint });
      }
    });
    return out;
  }

  var api = { dnum: dnum, diso: diso, wday: wday, ddmm: ddmm, WD: WD, WDS: WDS, demand: demand, rainEffect: rainEffect,
    stockReport: stockReport, stockView: stockView, stockAlerts: stockAlerts, roundUp: roundUp, wasteHint: wasteHint, parseDelivery: parseDelivery, deliveryText: deliveryText, parseAmount: parseAmount,
    fmtAmount: fmtAmount, normUnit: normUnit, smallUnit: smallUnit, mkey: mkey, HORIZON: HORIZON,
    criticalRatio: criticalRatio, forecast: forecast, eveningThreshold: eveningThreshold, discountAdvice: discountAdvice,
    backtest: backtest, expiryStatus: expiryStatus, expiryTip: expiryTip, parseCSV: parseCSV, signed: signed, SHARE_EVENING: SHARE_EVENING,
    loadWeather: loadWeather, deg: deg, recentTemp: recentTemp };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.FreshModel = api;
})(this);
