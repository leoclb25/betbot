"""
Open-Meteo weather client.

Uses:
  - Geocoding API  – city name → (lat, lon)          [free, no key]
  - Ensemble API   – 50-member ensemble forecast      [free, no key]
  - Forecast API   – deterministic forecast (fallback)[free, no key]

Ensemble members give us empirical probability distributions:
  P(rain) = fraction of members with precipitation > threshold
  P(temp > X) = fraction of members with max_temp > X
  etc.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Optional

import requests
from loguru import logger

from core.models import EnsembleForecast, WeatherCondition, WeatherProbability

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

    # ── Geocoding ────────────────────────────────────────────────────────────

    def geocode(self, location: str) -> Optional[tuple[float, float]]:
        """
        Convert a location name to (latitude, longitude).
        Returns None if location not found.
        """
        if location in self._geo_cache:
            return self._geo_cache[location]

        resp = self._session.get(
            GEOCODING_URL,
            params={"name": location, "count": 1, "language": "en", "format": "json"},
            timeout=10,
        )
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
        Fetch ensemble forecast for a specific date.

        Open-Meteo ensemble provides ~50 members from the ICON ensemble model.
        Each member represents one possible state of the atmosphere.
        """
        # Ensemble is available for ~7 days out
        days_out = (target_date - date.today()).days
        if days_out < 0 or days_out > 7:
            logger.warning(f"Target date {target_date} is {days_out} days out – outside range")
            return None

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": [
                "precipitation_sum",
                "temperature_2m_max",
                "temperature_2m_min",
                "wind_speed_10m_max",
            ],
            "models": "icon_seamless",  # ICON ensemble (~50 members)
            "timezone": "UTC",
            "start_date": target_date.isoformat(),
            "end_date": target_date.isoformat(),
            "forecast_days": days_out + 1,
        }

        try:
            resp = self._session.get(ENSEMBLE_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error(f"Ensemble API error: {exc}")
            return self._fallback_forecast(latitude, longitude, target_date, location_name)

        # Extract ensemble member data
        # Open-Meteo ensemble returns variables like:
        # "precipitation_sum_member01", "precipitation_sum_member02", etc.
        daily = data.get("daily", {})
        precip_members = self._extract_members(daily, "precipitation_sum", 0)
        temp_max_members = self._extract_members(daily, "temperature_2m_max", 0)
        temp_min_members = self._extract_members(daily, "temperature_2m_min", 0)
        wind_members = self._extract_members(daily, "wind_speed_10m_max", 0)

        # If ensemble keys not found, try single-model fallback
        if not precip_members:
            logger.debug("No ensemble members found in response, using fallback")
            return self._fallback_forecast(latitude, longitude, target_date, location_name)

        forecast = EnsembleForecast(
            location=location_name or f"{latitude},{longitude}",
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            fetched_at=datetime.utcnow(),
            precipitation_mm=precip_members,
            temperature_max_c=temp_max_members,
            temperature_min_c=temp_min_members,
            wind_speed_max_kmh=wind_members,
            member_count=len(precip_members),
        )
        logger.info(
            f"Fetched ensemble forecast for {location_name or f'({latitude},{longitude})'} "
            f"on {target_date} | {forecast.member_count} members"
        )
        return forecast

    def _extract_members(self, daily: dict, variable_prefix: str, day_index: int) -> list[float]:
        """
        Extract ensemble member values from Open-Meteo daily response dict.
        Handles both member-suffixed keys and plain arrays.
        """
        members = []
        # Try numbered member keys: variable_member01, variable_member02, ...
        for i in range(1, 51):
            key = f"{variable_prefix}_member{i:02d}"
            if key in daily:
                values = daily[key]
                if values and day_index < len(values) and values[day_index] is not None:
                    members.append(float(values[day_index]))

        if members:
            return members

        # Fallback: single value array (no ensemble)
        if variable_prefix in daily:
            values = daily[variable_prefix]
            if values and day_index < len(values) and values[day_index] is not None:
                return [float(values[day_index])]

        return []

    def _fallback_forecast(
        self,
        latitude: float,
        longitude: float,
        target_date: date,
        location_name: str,
    ) -> Optional[EnsembleForecast]:
        """Use deterministic forecast API as fallback (returns single member)."""
        days_out = (target_date - date.today()).days
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
            resp = self._session.get(FORECAST_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.error(f"Fallback forecast API error: {exc}")
            return None

        daily = data.get("daily", {})
        idx = min(days_out, len(daily.get("precipitation_sum", [None])) - 1)
        if idx < 0:
            return None

        def _get(key: str) -> list[float]:
            v = daily.get(key, [])
            return [float(v[idx])] if idx < len(v) and v[idx] is not None else []

        return EnsembleForecast(
            location=location_name or f"{latitude},{longitude}",
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            fetched_at=datetime.utcnow(),
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
            # Treat as rain (Open-Meteo precipitation_sum includes snow)
            # A better impl would use snowfall_sum specifically
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

        elif condition == WeatherCondition.WIND_ABOVE:
            if threshold is None:
                raise ValueError("threshold required for WIND_ABOVE")
            return self._fraction_above(forecast.wind_speed_max_kmh, threshold)

        elif condition == WeatherCondition.HURRICANE:
            # Sustained winds > 119 km/h (74 mph = Category 1)
            return self._fraction_above(forecast.wind_speed_max_kmh, 119.0)

        elif condition == WeatherCondition.STORM:
            # Strong storm: winds > 62 km/h or heavy rain > 20mm
            wind_frac = self._fraction_above(forecast.wind_speed_max_kmh, 62.0)
            rain_frac = self._fraction_above(forecast.precipitation_mm, 20.0)
            # Union probability (P(A or B) = P(A) + P(B) - P(A and B), approximate)
            return min(1.0, wind_frac + rain_frac - wind_frac * rain_frac)

        elif condition == WeatherCondition.SUNNY:
            # Sunny: less than 1mm rain
            rain_prob = self._fraction_above(forecast.precipitation_mm, 1.0)
            return 1.0 - rain_prob

        logger.warning(f"Unknown condition {condition}, defaulting to 0.5")
        return 0.5

    @staticmethod
    def _fraction_above(values: list[float], threshold: float) -> float:
        if not values:
            return 0.5  # no data → 50% uncertainty
        return sum(1 for v in values if v > threshold) / len(values)

    @staticmethod
    def _fraction_below(values: list[float], threshold: float) -> float:
        if not values:
            return 0.5
        return sum(1 for v in values if v < threshold) / len(values)
