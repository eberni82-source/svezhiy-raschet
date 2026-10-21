"""Погода и часовой пояс через Open-Meteo (бесплатно, без ключа)."""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import NamedTuple

log = logging.getLogger("weather")
RAIN_MM = 1.0  # от 1 мм осадков за день считаем, что был дождь
DAILY = "precipitation_sum,temperature_2m_max,temperature_2m_min"


class Day(NamedTuple):
    rain: bool | None
    temp: float | None   # средняя температура дня, °C


def _get(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _days(data: dict) -> dict[date, Day]:
    d = data.get("daily", {})
    out = {}
    for day, mm, hi, lo in zip(d.get("time", []), d.get("precipitation_sum", []),
                               d.get("temperature_2m_max", []), d.get("temperature_2m_min", [])):
        temp = round((hi + lo) / 2, 1) if hi is not None and lo is not None else None
        out[date.fromisoformat(day)] = Day(None if mm is None else mm >= RAIN_MM, temp)
    return out


def geocode(city: str) -> dict | None:
    """Город -> координаты и часовой пояс. None, если не нашли или нет сети."""
    q = urllib.parse.urlencode({"name": city, "count": 1, "language": "ru", "format": "json"})
    try:
        data = _get(f"https://geocoding-api.open-meteo.com/v1/search?{q}")
    except Exception as e:
        log.warning("geocode failed: %s", e)
        return None
    res = (data.get("results") or [None])[0]
    if not res:
        return None
    return {"name": res.get("name", city), "lat": res["latitude"], "lon": res["longitude"],
            "tz": res.get("timezone") or "Europe/Moscow"}


def history(lat: float | None, lon: float | None, start: date, end: date) -> dict[date, Day]:
    """Архив погоды за прошлые даты - чтобы загруженная история продаж сразу была с погодой."""
    if lat is None or lon is None or start > end:
        return {}
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "daily": DAILY, "timezone": "auto",
                                "start_date": start.isoformat(), "end_date": end.isoformat()})
    try:
        return _days(_get(f"https://archive-api.open-meteo.com/v1/archive?{q}", timeout=20))
    except Exception as e:
        log.warning("weather history failed: %s", e)
        return {}


class WeatherCache:
    """Осадки и температура по дням: 92 дня назад + 3 дня вперёд. Обновляем раз в 3 часа."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.cache: dict[tuple[float, float], tuple[float, dict[date, Day], int | None]] = {}

    def daily(self, lat: float | None, lon: float | None, now_ts: float) -> tuple[dict[date, Day], int | None]:
        if not self.enabled or lat is None or lon is None:
            return {}, None
        key = (round(lat, 2), round(lon, 2))
        hit = self.cache.get(key)
        if hit and now_ts - hit[0] < 3 * 3600:
            return hit[1], hit[2]
        q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "daily": DAILY,
                                    "past_days": 92, "forecast_days": 3, "timezone": "auto"})
        try:
            data = _get(f"https://api.open-meteo.com/v1/forecast?{q}")
        except Exception as e:
            log.warning("weather failed: %s", e)
            return (hit[1], hit[2]) if hit else ({}, None)
        out, offset = _days(data), data.get("utc_offset_seconds")
        self.cache[key] = (now_ts, out, offset)
        return out, offset

    def day(self, lat, lon, day: date, now_ts: float) -> Day:
        return self.daily(lat, lon, now_ts)[0].get(day, Day(None, None))

    def fill(self, lat, lon, sales, now_ts: float, archive: bool = False) -> list:
        """Дописывает дождь и температуру в продажи, где их нет. Возвращает дополненные строки."""
        missing = [s for s in sales if s.rain is None or s.temp is None]
        if not missing or not self.enabled:
            return []
        known = dict(self.daily(lat, lon, now_ts)[0])
        if archive:
            old = [s.day for s in missing if s.day not in known]
            if old:
                # архив отстаёт от сегодняшнего дня на несколько дней, свежие дни берём из прогноза
                known = {**history(lat, lon, min(old), min(max(old), date.today() - timedelta(days=2))), **known}
        filled = []
        for s in missing:
            w = known.get(s.day)
            if w:
                s.rain = w.rain if s.rain is None else s.rain
                s.temp = w.temp if s.temp is None else s.temp
                filled.append(s)
        return filled
