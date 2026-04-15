"""
Open-Meteo weather client.

Uses:
  - Geocoding API  – city name → (lat, lon)          [free, no key]
  - Ensemble API   – 50-member hourly ensemble        [free, no key]
  - Forecast API   – deterministic forecast (fallback)[free, no key]

The Ensemble API returns HOURLY data per member. We aggregate to daily:
  - precipitation: sum of all hours in the target day
  - temperature_max: max of all hours
  - wind_speed_max: max of all hours

Ensemble members give us empirical probability distributions:
  P(rain) = fraction of members with total_precip > threshold
  P(temp > X) = fraction of members with daily_max_temp > X
  etc.
"""

from __future__ import annotations

import os
import random
import time
from datetime import date, datetime, timezone
from typing import Optional

import requests
from loguru import logger

from core.models import EnsembleForecast, WeatherCondition, WeatherProbability

# Open-Meteo free tier: space requests and retry on 429 (burst limits are strict).
def _env_float(key: str, default: str) -> float:
    try:
        return float(os.getenv(key, default))
    except ValueError:
        return float(default)


def _env_int(key: str, default: str) -> int:
    try:
        return int(os.getenv(key, default))
    except ValueError:
        return int(default)


ENSEMBLE_MIN_INTERVAL_SEC = _env_float("OPEN_METEO_ENSEMBLE_MIN_INTERVAL", "2.25")
ENSEMBLE_MAX_RETRIES = max(1, _env_int("OPEN_METEO_ENSEMBLE_MAX_RETRIES", "6"))
GEOCODE_MIN_INTERVAL_SEC = _env_float("OPEN_METEO_GEOCODE_MIN_INTERVAL", "0.4")
POST_429_COOLDOWN_SEC = _env_float("OPEN_METEO_POST_429_COOLDOWN_SEC", "22")


def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
    """Seconds to wait after HTTP 429 (header Retry-After or exponential backoff + jitter)."""
    ra = response.headers.get("Retry-After")
    if ra:
        try:
            return min(120.0, float(ra))
        except ValueError:
            pass
    base = min(90.0, 2.0 ** (attempt + 1))
    return base * (1.0 + random.uniform(0.0, 0.2))


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Confidence multiplier per day out (shrinks probability toward 0.5)
# adjusted = 0.5 + (raw - 0.5) * confidence
CONFIDENCE_DECAY = {
    0: 1.00,
    1: 0.92,
    2: 0.82,
    3: 0.70,
    4: 0.60,
    5: 0.52,
    6: 0.46,
    7: 0.40,
}

# Default rain threshold (mm/day to count as a rain day)
RAIN_THRESHOLD_MM = 0.5


class WeatherClient:
    """Fetches weather ensemble forecasts and converts them to probabilities."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._geo_cache: dict[str, tuple[float, float]] = {}
        # Cache ensemble forecasts keyed by (lat_rounded, lon_rounded, date_iso)
        # to avoid redundant API calls within the same scan cycle
        self._forecast_cache: dict[tuple, EnsembleForecast] = {}
        self._last_ensemble_request: float = 0.0
        self._last_geocode_request: float = 0.0
        self._ensemble_cooldown_until: float = 0.0

    # ── Geocoding ────────────────────────────────────────────────────────────

    def geocode(self, location: str) -> Optional[tuple[float, float]]:
        """
        Convert a location name to (latitude, longitude).
        Returns None if location not found.
        """
        if location in self._geo_cache:
            return self._geo_cache[location]

        elapsed = time.monotonic() - self._last_geocode_request
        if elapsed < GEOCODE_MIN_INTERVAL_SEC:
            time.sleep(GEOCODE_MIN_INTERVAL_SEC - elapsed)

        resp = self._session.get(
            GEOCODING_URL,
            params={"name": location, "count": 1, "language": "en", "format": "json"},
            timeout=10,
        )
        self._last_geocode_request = time.monotonic()
        resp.raise_for_status()
        data = resp.json()

        results = data.get("results", [])
        if not results:
            logger.warning(f"Could not geocode location: '{location}'")
            return None

        lat = float(results[0]["latitude"])
        lon = float(results[0]["longitude"])
        self._geo_cache[location] = (lat, lon)
        logger.debug(f"Geocoded '{location}' → ({lat}, {lon})")
        return lat, lon

    # ── Ensemble forecast ────────────────────────────────────────────────────

    def get_ensemble_forecast(
        self,
        latitude: float,
        longitude: float,
        target_date: date,
        location_name: str = "",
    ) -> Optional[EnsembleForecast]:
        """
        Fetch hourly ensemble forecast for a specific date and aggregate to daily.

        Open-Meteo ensemble provides ~50 members from the ICON ensemble model.
        The API only supports hourly variables; we aggregate to daily values:
          - precipitation: sum over 24h
          - temperature: max over 24h
          - wind: max over 24h
        """
        days_out = (target_date - date.today()).days
        # days_out=-1 puede pasar cuando el end_date del mercado es medianoche UTC
        # y el target_date se parsea como "ayer". Tratamos -1 como día 0 (hoy).
        if days_out == -1:
            days_out = 0
        if days_out < 0 or days_out > 7:
            logger.debug(
                f"Target date {target_date} is {days_out} days out – outside ensemble window (0–7)"
            )
            return None

        # Check cache (round to 2 decimal places ≈ ~1km resolution)
        cache_key = (round(latitude, 2), round(longitude, 2), target_date.isoformat())
        if cache_key in self._forecast_cache:
            logger.debug(f"Forecast cache hit for {cache_key}")
            return self._forecast_cache[cache_key]

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": [
                "precipitation",
                "temperature_2m",
                "wind_speed_10m",
            ],
            "models": "icon_seamless",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
            "timezone": "UTC",
        }

        data: Optional[dict] = None
        for attempt in range(ENSEMBLE_MAX_RETRIES):
            now = time.monotonic()
            if now < self._ensemble_cooldown_until:
                time.sleep(self._ensemble_cooldown_until - now)
            elapsed = time.monotonic() - self._last_ensemble_request
            if elapsed < ENSEMBLE_MIN_INTERVAL_SEC:
                time.sleep(ENSEMBLE_MIN_INTERVAL_SEC - elapsed)

            try:
                resp = self._session.get(ENSEMBLE_URL, params=params, timeout=20)
                self._last_ensemble_request = time.monotonic()

                if resp.status_code == 429:
                    self._ensemble_cooldown_until = max(
                        self._ensemble_cooldown_until,
                        time.monotonic() + POST_429_COOLDOWN_SEC,
                    )
                    wait = _retry_after_seconds(resp, attempt)
                    logger.warning(
                        f"Open-Meteo ensemble 429 — esperando {wait:.1f}s "
                        f"(intento {attempt + 1}/{ENSEMBLE_MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                break

            except requests.RequestException as exc:
                logger.error(f"Ensemble API error: {exc}")
                return self._fallback_forecast(latitude, longitude, target_date, location_name)

        if data is None:
            logger.warning(
                "Open-Meteo ensemble: agotados reintentos por 429; usando forecast determinístico"
            )
            return self._fallback_forecast(latitude, longitude, target_date, location_name)

        hourly = data.get("hourly", {})
        if not hourly:
            return self._fallback_forecast(latitude, longitude, target_date, location_name)

        # Aggregate per-member hourly → daily
        precip_members = self._aggregate_members(hourly, "precipitation", agg="sum")
        temp_max_members = self._aggregate_members(hourly, "temperature_2m", agg="max")
        wind_max_members = self._aggregate_members(hourly, "wind_speed_10m", agg="max")

        if not precip_members:
            logger.debug("No ensemble members found, using deterministic fallback")
            return self._fallback_forecast(latitude, longitude, target_date, location_name)

        forecast = EnsembleForecast(
            location=location_name or f"{latitude},{longitude}",
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            fetched_at=datetime.now(timezone.utc),
            precipitation_mm=precip_members,
            temperature_max_c=temp_max_members,
            temperature_min_c=temp_max_members,  # use same series; min would need separate var
            wind_speed_max_kmh=wind_max_members,
            member_count=len(precip_members),
        )
        logger.debug(
            f"Ensemble forecast for {location_name or f'({latitude},{longitude})'} "
            f"on {target_date} | {forecast.member_count} members | "
            f"precip avg={sum(precip_members)/len(precip_members):.1f}mm "
            f"temp avg={sum(temp_max_members)/len(temp_max_members):.1f}°C"
        )
        self._forecast_cache[cache_key] = forecast
        return forecast

    def _aggregate_members(
        self, hourly: dict, variable: str, agg: str
    ) -> list[float]:
        """
        Aggregate 24 hourly values per ensemble member into a single daily value.

        agg='sum'  → total (precipitation)
        agg='max'  → maximum (temperature, wind)
        """
        members = []
        for i in range(1, 60):  # up to 59 members
            key = f"{variable}_member{i:02d}"
            if key not in hourly:
                break
            values = [v for v in hourly[key] if v is not None]
            if not values:
                continue
            if agg == "sum":
                members.append(sum(values))
            else:  # max
                members.append(max(values))

        # Fallback to plain (non-member) variable if no member keys found
        if not members and variable in hourly:
            values = [v for v in hourly[variable] if v is not None]
            if values:
                val = sum(values) if agg == "sum" else max(values)
                members = [val]

        return members

    def _fallback_forecast(
        self,
        latitude: float,
        longitude: float,
        target_date: date,
        location_name: str,
    ) -> Optional[EnsembleForecast]:
        """Use deterministic forecast API as fallback (single member)."""
        days_out = (target_date - date.today()).days
        if days_out == -1:
            days_out = 0
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": [
                "precipitation_sum",
                "temperature_2m_max",
                "temperature_2m_min",
                "wind_speed_10m_max",
            ],
            "timezone": "UTC",
            "forecast_days": max(days_out + 1, 1),
        }
        try:
            elapsed = time.monotonic() - self._last_ensemble_request
            if elapsed < ENSEMBLE_MIN_INTERVAL_SEC:
                time.sleep(ENSEMBLE_MIN_INTERVAL_SEC - elapsed)
            resp = self._session.get(FORECAST_URL, params=params, timeout=15)
            self._last_ensemble_request = time.monotonic()
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error(f"Fallback forecast API error: {exc}")
            return None

        daily = data.get("daily", {})
        precip_list = daily.get("precipitation_sum", [])
        idx = min(days_out, len(precip_list) - 1) if precip_list else -1
        if idx < 0:
            return None

        def _get(key: str) -> list[float]:
            v = daily.get(key, [])
            return [float(v[idx])] if idx < len(v) and v[idx] is not None else [0.0]

        logger.debug(f"Using deterministic fallback for {location_name or f'({latitude},{longitude})'}")
        return EnsembleForecast(
            location=location_name or f"{latitude},{longitude}",
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            fetched_at=datetime.now(timezone.utc),
            precipitation_mm=_get("precipitation_sum"),
            temperature_max_c=_get("temperature_2m_max"),
            temperature_min_c=_get("temperature_2m_min"),
            wind_speed_max_kmh=_get("wind_speed_10m_max"),
            member_count=1,
        )

    # ── Probability calculation ──────────────────────────────────────────────

    def calculate_probability(
        self,
        forecast: EnsembleForecast,
        condition: WeatherCondition,
        threshold: Optional[float] = None,
    ) -> WeatherProbability:
        """
        Convert ensemble forecast to a probability for a given condition.

        The raw probability is the empirical fraction of ensemble members
        that satisfy the condition. It is then shrunk toward 0.5 based on
        how many days out the forecast is (epistemic uncertainty).
        """
        days_out = (forecast.target_date - date.today()).days
        days_out = max(0, min(days_out, 7))
        confidence = CONFIDENCE_DECAY.get(days_out, 0.40)

        raw_prob = self._raw_probability(forecast, condition, threshold)

        # Shrink toward 0.5 by confidence factor
        adjusted = 0.5 + (raw_prob - 0.5) * confidence

        return WeatherProbability(
            condition=condition,
            raw_probability=raw_prob,
            true_probability=adjusted,
            confidence=confidence,
            days_out=float(days_out),
            member_count=forecast.member_count,
            fetched_at=forecast.fetched_at,
        )

    def _raw_probability(
        self,
        forecast: EnsembleForecast,
        condition: WeatherCondition,
        threshold: Optional[float],
    ) -> float:
        """Calculate raw (unadjusted) ensemble probability."""
        if condition == WeatherCondition.RAIN:
            return self._fraction_above(
                forecast.precipitation_mm,
                threshold if threshold is not None else RAIN_THRESHOLD_MM,
            )

        elif condition == WeatherCondition.SNOW:
            return self._fraction_above(
                forecast.precipitation_mm,
                threshold if threshold is not None else RAIN_THRESHOLD_MM,
            )

        elif condition == WeatherCondition.TEMPERATURE_ABOVE:
            if threshold is None:
                raise ValueError("threshold required for TEMPERATURE_ABOVE")
            return self._fraction_above(forecast.temperature_max_c, threshold)

        elif condition == WeatherCondition.TEMPERATURE_BELOW:
            if threshold is None:
                raise ValueError("threshold required for TEMPERATURE_BELOW")
            return self._fraction_below(forecast.temperature_min_c, threshold)

        elif condition == WeatherCondition.TEMPERATURE_EXACT:
            if threshold is None:
                return 0.5
            members = forecast.temperature_max_c
            if not members:
                return 0.5
            return sum(1 for v in members if abs(v - threshold) < 0.5) / len(members)

        elif condition == WeatherCondition.WIND_ABOVE:
            if threshold is None:
                raise ValueError("threshold required for WIND_ABOVE")
            return self._fraction_above(forecast.wind_speed_max_kmh, threshold)

        elif condition == WeatherCondition.HURRICANE:
            return self._fraction_above(forecast.wind_speed_max_kmh, 119.0)

        elif condition == WeatherCondition.STORM:
            wind_frac = self._fraction_above(forecast.wind_speed_max_kmh, 62.0)
            rain_frac = self._fraction_above(forecast.precipitation_mm, 20.0)
            return min(1.0, wind_frac + rain_frac - wind_frac * rain_frac)

        elif condition == WeatherCondition.SUNNY:
            rain_prob = self._fraction_above(forecast.precipitation_mm, 1.0)
            return 1.0 - rain_prob

        logger.warning(f"Unknown condition {condition}, defaulting to 0.5")
        return 0.5

    @staticmethod
    def _fraction_above(values: list[float], threshold: float) -> float:
        if not values:
            return 0.5
        return sum(1 for v in values if v > threshold) / len(values)

    @staticmethod
    def _fraction_below(values: list[float], threshold: float) -> float:
        if not values:
            return 0.5
        return sum(1 for v in values if v < threshold) / len(values)
