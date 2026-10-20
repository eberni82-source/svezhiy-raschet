"""Погода и часовой пояс через Open-Meteo (бесплатно, без ключа)."""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import date

log = logging.getLogger("weather")
RAIN_MM = 1.0  # от 1 мм осадков за день считаем, что был дождь


def _get(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def geocode(city: str) -> dict | None:
    """Город → координаты и часовой пояс. None, если не нашли или нет сети."""
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


class WeatherCache:
    """Осадки по дням: 14 дней назад + 3 дня вперёд. Обновляем раз в 3 часа."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.cache: dict[tuple[float, float], tuple[float, dict[date, float], int]] = {}

    def daily(self, lat: float | None, lon: float | None, now_ts: float) -> tuple[dict[date, float], int | None]:
        if not self.enabled or lat is None or lon is None:
            return {}, None
        key = (round(lat, 2), round(lon, 2))
        hit = self.cache.get(key)
        if hit and now_ts - hit[0] < 3 * 3600:
            return hit[1], hit[2]
        q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "daily": "precipitation_sum",
                                    "past_days": 14, "forecast_days": 3, "timezone": "auto"})
        try:
            data = _get(f"https://api.open-meteo.com/v1/forecast?{q}")
        except Exception as e:
            log.warning("weather failed: %s", e)
            return (hit[1], hit[2]) if hit else ({}, None)
        days = data.get("daily", {})
        out = {date.fromisoformat(d): (p or 0.0) for d, p in zip(days.get("time", []), days.get("precipitation_sum", []))}
        offset = data.get("utc_offset_seconds")
        self.cache[key] = (now_ts, out, offset)
        return out, offset

    def is_rain(self, lat, lon, day: date, now_ts: float) -> bool | None:
        daily, _ = self.daily(lat, lon, now_ts)
        if day not in daily:
            return None
        return daily[day] >= RAIN_MM
