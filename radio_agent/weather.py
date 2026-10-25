"""The real weather at the station, for its weather report in a contact (Open-Meteo: free, no key).

Wind, sea state, visibility and pressure in shipping-forecast terms, e.g.
"Weather at Rockall, 2145 UTC: wind south-westerly force 4, 17 knots, gusting 21 knots. Drizzle.
Visibility good. Sea very rough, waves around 4.3 metres. Air 9 degrees, sea 11 degrees. Pressure 1001
hectopascals, falling slowly."
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Callable

from .events import log

FORECAST = "https://api.open-meteo.com/v1/forecast"
MARINE = "https://marine-api.open-meteo.com/v1/marine"


def locator_to_latlon(loc: str) -> tuple[float, float] | None:
    """Centre of a Maidenhead square: "IO37do" -> (57.60, -13.71). None if it isn't a locator."""
    loc = (loc or "").strip()
    if len(loc) < 4 or not (loc[:2].isalpha() and loc[2:4].isdigit()):
        return None
    loc = loc[:2].upper() + loc[2:4] + loc[4:6].lower()
    lon = (ord(loc[0]) - 65) * 20 - 180 + int(loc[2]) * 2
    lat = (ord(loc[1]) - 65) * 10 - 90 + int(loc[3])
    if len(loc) >= 6 and loc[4:6].isalpha():
        lon += (ord(loc[4]) - 97) * 2 / 24 + 1 / 24
        lat += (ord(loc[5]) - 97) / 24 + 1 / 48
    else:
        lon, lat = lon + 1, lat + 0.5
    return round(lat, 3), round(lon, 3)


_BEAUFORT = [1, 4, 7, 11, 17, 22, 28, 34, 41, 48, 56, 64]  # knots: upper bound of force 0, 1, 2…
_POINTS = ["northerly", "north-easterly", "easterly", "south-easterly", "southerly", "south-westerly",
           "westerly", "north-westerly"]
_SEA = [(0.1, "calm"), (0.5, "smooth"), (1.25, "slight"), (2.5, "moderate"), (4, "rough"), (6, "very rough"),
        (9, "high"), (14, "very high"), (99, "phenomenal")]
_WMO = {0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
        51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
        61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
        71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers",
        81: "Heavy showers", 82: "Violent showers", 85: "Snow showers", 86: "Heavy snow showers",
        95: "Thunderstorms", 96: "Thunderstorms with hail", 99: "Thunderstorms with hail"}


def beaufort(knots: float) -> int:
    return next((f for f, top in enumerate(_BEAUFORT) if knots < top), 12)


def compass(degrees: float) -> str:
    return _POINTS[int((degrees % 360) / 45 + 0.5) % 8]


def sea_state(metres: float) -> str:
    return next(name for top, name in _SEA if metres < top)


def visibility(metres: float) -> str:
    return "very poor" if metres < 1000 else "poor" if metres < 4000 else "moderate" if metres < 10000 else "good"


def tendency(change: float) -> str:
    """Pressure change over three hours, in Met Office words."""
    a = abs(change)
    if a < 0.1:
        return "steady"
    way = "rising" if change > 0 else "falling"
    if a <= 1.5:
        return f"{way} slowly"
    if a <= 3.5:
        return way
    return f"{way} quickly" if a <= 6 else f"{way} very rapidly"


def _get(url: str, params: dict, timeout: float = 10) -> dict:
    with urllib.request.urlopen(f"{url}?{urllib.parse.urlencode(params)}", timeout=timeout) as r:
        return json.loads(r.read())


class Weather:
    def __init__(self, cfg: dict, get: Callable[[str, dict], dict] = _get, clock: Callable[[], float] = time.time):
        st, w = cfg["station"], cfg.get("weather", {})
        self.enabled = bool(w.get("enabled", True))
        pos = (w.get("latitude"), w.get("longitude"))
        self.pos = pos if None not in pos else locator_to_latlon(st.get("locator", ""))
        self.place = w.get("place") or st.get("qth") or "the station"
        self.get, self.clock = get, clock
        self.max_age = float(w.get("cache_minutes", 10)) * 60
        self._cache: tuple[float, str] | None = None

    def report(self) -> str | None:
        """The weather at the station now, or None if it's off, there's no position, or no internet."""
        if not self.enabled or not self.pos:
            return None
        if self._cache and self.clock() - self._cache[0] < self.max_age:
            return self._cache[1]
        try:
            text = self._fetch()
        except Exception as e:  # no internet: Stephen just won't have a forecast
            log.warning(f"weather unavailable: {e}")
            return self._cache[1] if self._cache else None
        self._cache = (self.clock(), text)
        return text

    def _fetch(self) -> str:
        lat, lon = self.pos
        base = {"latitude": lat, "longitude": lon, "timezone": "UTC"}
        now = self.get(FORECAST, {**base, "wind_speed_unit": "kn", "past_hours": 3, "forecast_hours": 1,
                                  "hourly": "pressure_msl",
                                  "current": "temperature_2m,wind_speed_10m,wind_direction_10m,wind_gusts_10m,"
                                             "pressure_msl,weather_code,visibility"})
        c = now["current"]
        try:
            sea = self.get(MARINE, {**base, "current": "wave_height,sea_surface_temperature"})["current"]
        except Exception:
            sea = {}  # inland stations have no sea
        kn, gust = c["wind_speed_10m"], c.get("wind_gusts_10m")
        parts = [f"Weather at {self.place}, {c['time'][-5:].replace(':', '')} UTC: wind {compass(c['wind_direction_10m'])} "
                 f"force {beaufort(kn)}, {kn:.0f} knots" + (f", gusting {gust:.0f} knots." if gust and gust > kn + 3 else ".")]
        if c.get("weather_code") in _WMO:
            parts.append(_WMO[c["weather_code"]] + ".")
        if c.get("visibility") is not None:
            parts.append(f"Visibility {visibility(c['visibility'])}.")
        if sea.get("wave_height") is not None:
            parts.append(f"Sea {sea_state(sea['wave_height'])}, waves around {sea['wave_height']:.1f} metres.")
        temps = f"Air {c['temperature_2m']:.0f} degrees"
        if sea.get("sea_surface_temperature") is not None:
            temps += f", sea {sea['sea_surface_temperature']:.0f} degrees"
        parts.append(temps + ".")
        pressure = f"Pressure {c['pressure_msl']:.0f} hectopascals"
        hourly = [p for p in now.get("hourly", {}).get("pressure_msl", []) if p is not None]
        if len(hourly) >= 2:
            pressure += f", {tendency(hourly[-1] - hourly[0])}"
        parts.append(pressure + ".")
        return " ".join(parts)
