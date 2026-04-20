"""
Climatology client — prior histórico para priorizar forecasts anómalos.

Usa el endpoint gratuito Archive API de Open-Meteo:
  https://archive-api.open-meteo.com/v1/archive

Para cada (ciudad, fecha_objetivo, condición, umbral) calcula la frecuencia
histórica del evento en los últimos CLIMATOLOGY_YEARS años, en una ventana de
±CLIMATOLOGY_WINDOW_DAYS días alrededor de la fecha objetivo.

El resultado se usa como prior bayesiano: el forecast del ensemble se blendea
hacia la climatología según la confianza del modelo.
  true_prob = climatology + (forecast_raw - climatology) * confidence

Cuando confidence=1 el modelo domina; cuando confidence=0 cae al anchor climático.
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import json
import requests
from loguru import logger

from core.env_utils import env_float, env_int
from core.models import EnsembleForecast, WeatherCondition

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
CACHE_FILE = Path("data/weather_climatology_cache.json")
CACHE_TTL_DAYS = 30  # la climatología mensual no cambia rápido

RAIN_THRESHOLD_MM = 0.5


class ClimatologyClient:
    """
    Fetches and caches multi-year historical climatology for weather events.
    """

    def __init__(self) -> None:
        self._years = max(3, env_int("CLIMATOLOGY_YEARS", 10))
        self._window_days = max(3, env_int("CLIMATOLOGY_WINDOW_DAYS", 7))
        self._min_interval = env_float("CLIMATOLOGY_MIN_INTERVAL_SEC", 1.5)
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._last_request = 0.0
        self._cache: dict[str, dict] = self._load_cache()

    # ── Cache I/O ────────────────────────────────────────────────────────────

    def _load_cache(self) -> dict:
        if not CACHE_FILE.exists():
            return {}
        try:
            with CACHE_FILE.open() as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with CACHE_FILE.open("w") as f:
                json.dump(self._cache, f)
        except OSError as exc:
            logger.debug(f"[CLIMATOLOGY] could not save cache: {exc}")

    # ── Archive fetch ────────────────────────────────────────────────────────

    def _fetch_archive(self, latitude: float, longitude: float, target_date: date) -> Optional[dict]:
        """
        Fetch historical daily data for ±window_days around target_date over the last N years.
        Results are cached by (lat_r, lon_r, month, day) because climatology varies slowly.
        """
        cache_key = f"{round(latitude, 2)},{round(longitude, 2)},{target_date.month:02d}-{target_date.day:02d}"
        cached = self._cache.get(cache_key)
        if cached and cached.get("stored_at"):
            stored = date.fromisoformat(cached["stored_at"])
            # TTL corto para entradas marcadas como None (429 / error transitorio)
            if cached.get("data") is None:
                if (date.today() - stored).days < 1:
                    return None
            elif (date.today() - stored).days < CACHE_TTL_DAYS:
                return cached["data"]

        # Build year ranges: for the last N years, take ±window_days around (same month, same day)
        today = date.today()
        start_year = today.year - self._years
        # Archive API can accept a single continuous range; we fetch the full span
        # then filter by month/day client-side.
        start = date(start_year, 1, 1)
        end = today - timedelta(days=5)  # archive needs ~5 day lag
        if end <= start:
            return None

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
            "timezone": "UTC",
        }
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        try:
            resp = self._session.get(ARCHIVE_URL, params=params, timeout=12)
            self._last_request = time.monotonic()
            if resp.status_code == 429:
                logger.debug(f"[CLIMATOLOGY] 429 fetching {cache_key} — backing off")
                # Marcamos el cache key como "unavailable" con TTL corto para no
                # reintentar inmediatamente en el mismo ciclo.
                self._cache[cache_key] = {
                    "stored_at": date.today().isoformat(),
                    "data": None,
                }
                return None
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            logger.debug(f"[CLIMATOLOGY] fetch error: {exc}")
            return None

        daily = data.get("daily", {})
        times = daily.get("time", [])
        if not times:
            return None

        # Filter to ±window_days around (target_date.month, target_date.day) for each year
        filtered_temps_max: list[float] = []
        filtered_temps_min: list[float] = []
        filtered_precip: list[float] = []
        filtered_wind: list[float] = []
        for i, t in enumerate(times):
            try:
                d = date.fromisoformat(t)
            except ValueError:
                continue
            # Handle year-wrap window: compute the closest (same-year, prev-year, next-year) anchor
            anchors = [
                date(d.year, target_date.month, target_date.day)
                if (target_date.month, target_date.day) != (2, 29) or d.year % 4 == 0
                else date(d.year, 2, 28),
            ]
            in_window = any(
                abs((d - a).days) <= self._window_days for a in anchors
            )
            if not in_window:
                continue
            tmx = daily.get("temperature_2m_max", [])
            tmn = daily.get("temperature_2m_min", [])
            prc = daily.get("precipitation_sum", [])
            wnd = daily.get("wind_speed_10m_max", [])
            if i < len(tmx) and tmx[i] is not None:
                filtered_temps_max.append(float(tmx[i]))
            if i < len(tmn) and tmn[i] is not None:
                filtered_temps_min.append(float(tmn[i]))
            if i < len(prc) and prc[i] is not None:
                filtered_precip.append(float(prc[i]))
            if i < len(wnd) and wnd[i] is not None:
                filtered_wind.append(float(wnd[i]))

        if not filtered_temps_max:
            return None

        aggregated = {
            "temperature_max_c": filtered_temps_max,
            "temperature_min_c": filtered_temps_min,
            "precipitation_mm": filtered_precip,
            "wind_speed_max_kmh": filtered_wind,
            "sample_size": len(filtered_temps_max),
        }
        self._cache[cache_key] = {
            "stored_at": date.today().isoformat(),
            "data": aggregated,
        }
        self._save_cache()
        logger.debug(
            f"[CLIMATOLOGY] fetched {cache_key} | samples={len(filtered_temps_max)} | "
            f"temp_mean={sum(filtered_temps_max)/len(filtered_temps_max):.1f}°C"
        )
        return aggregated

    # ── Public API ───────────────────────────────────────────────────────────

    def probability(
        self,
        latitude: float,
        longitude: float,
        target_date: date,
        condition: WeatherCondition,
        threshold: Optional[float],
        threshold_high: Optional[float] = None,
    ) -> Optional[float]:
        """
        Fracción histórica de días (en ventana ±window_days, últimos N años) que
        satisficieron la condición del mercado. Devuelve None si no hay datos.
        """
        data = self._fetch_archive(latitude, longitude, target_date)
        if data is None:
            return None

        if condition == WeatherCondition.RAIN or condition == WeatherCondition.SNOW:
            values = data.get("precipitation_mm", [])
            thr = threshold if threshold is not None else RAIN_THRESHOLD_MM
            return _frac_above(values, thr)

        if condition == WeatherCondition.TEMPERATURE_ABOVE:
            if threshold is None:
                return None
            return _frac_above(data.get("temperature_max_c", []), threshold)

        if condition == WeatherCondition.TEMPERATURE_BELOW:
            if threshold is None:
                return None
            return _frac_below(data.get("temperature_min_c", []), threshold)

        if condition == WeatherCondition.TEMPERATURE_EXACT:
            values = data.get("temperature_max_c", [])
            if not values or threshold is None:
                return None
            if threshold_high is not None:
                return sum(1 for v in values if threshold <= v <= threshold_high) / len(values)
            return sum(1 for v in values if abs(v - threshold) <= 0.5) / len(values)

        if condition == WeatherCondition.WIND_ABOVE:
            if threshold is None:
                return None
            return _frac_above(data.get("wind_speed_max_kmh", []), threshold)

        if condition == WeatherCondition.HURRICANE:
            return _frac_above(data.get("wind_speed_max_kmh", []), 119.0)

        if condition == WeatherCondition.STORM:
            wnd = data.get("wind_speed_max_kmh", [])
            prc = data.get("precipitation_mm", [])
            wind_frac = _frac_above(wnd, 62.0)
            rain_frac = _frac_above(prc, 20.0)
            return min(1.0, wind_frac + rain_frac - wind_frac * rain_frac)

        if condition == WeatherCondition.SUNNY:
            return 1.0 - _frac_above(data.get("precipitation_mm", []), 1.0)

        return None


def _frac_above(values: list[float], threshold: float) -> float:
    if not values:
        return 0.5
    return sum(1 for v in values if v > threshold) / len(values)


def _frac_below(values: list[float], threshold: float) -> float:
    if not values:
        return 0.5
    return sum(1 for v in values if v < threshold) / len(values)
