"""
Open-Meteo weather client — multi-model.

Sources (all free, no API key):
  - Open-Meteo Ensemble API  – ICON (40 members) + ECMWF IFS (51 members)
  - Open-Meteo Forecast API  – GFS deterministic (US model, fallback + 3rd opinion)
  - NWS (api.weather.gov)    – US cities only, official NOAA hourly forecast
  - Geocoding API            – city name → (lat, lon)

Probability calculation:
  1. Each model produces a raw probability from its ensemble members.
  2. Model agreement = 1 - normalized_std(model_probs). Low agreement → shrink harder.
  3. Days-out decay multiplier applied on top.
  4. For range markets (threshold_low to threshold_high), fraction of members
     within the exact band is used instead of above/below.

Final confidence = days_decay * agreement_factor
true_probability  = 0.5 + (raw - 0.5) * confidence
"""

from __future__ import annotations

import os
import random
import time
from datetime import date, datetime, timezone
from statistics import mean, stdev
from typing import Optional

import requests
from loguru import logger

from core.models import EnsembleForecast, WeatherCondition, WeatherProbability

# ── Rate-limiting config ──────────────────────────────────────────────────────

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


ENSEMBLE_MIN_INTERVAL_SEC  = _env_float("OPEN_METEO_ENSEMBLE_MIN_INTERVAL", "2.25")
ENSEMBLE_MAX_RETRIES       = max(1, _env_int("OPEN_METEO_ENSEMBLE_MAX_RETRIES", "6"))
GEOCODE_MIN_INTERVAL_SEC   = _env_float("OPEN_METEO_GEOCODE_MIN_INTERVAL", "0.4")
POST_429_COOLDOWN_SEC      = _env_float("OPEN_METEO_POST_429_COOLDOWN_SEC", "22")


def _retry_after_seconds(response: requests.Response, attempt: int) -> float:
    ra = response.headers.get("Retry-After")
    if ra:
        try:
            return min(120.0, float(ra))
        except ValueError:
            pass
    base = min(90.0, 2.0 ** (attempt + 1))
    return base * (1.0 + random.uniform(0.0, 0.2))


# ── API endpoints ─────────────────────────────────────────────────────────────
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
ENSEMBLE_URL  = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL  = "https://api.open-meteo.com/v1/forecast"
NWS_POINTS    = "https://api.weather.gov/points/{lat},{lon}"

# Open-Meteo ensemble models to query (in priority order).
# icon_seamless  = ICON global, ~40 members, German weather service, strong in Europe
# ecmwf_ifs04    = ECMWF IFS,  51 members, European Centre, best global model
ENSEMBLE_MODELS = ["icon_seamless", "ecmwf_ifs04"]

# Days-out base confidence (shrinks raw probability toward 0.5).
# This is the *base* — model disagreement reduces it further.
CONFIDENCE_DECAY = {0: 1.00, 1: 0.92, 2: 0.82, 3: 0.70, 4: 0.60, 5: 0.52, 6: 0.46, 7: 0.40}

RAIN_THRESHOLD_MM = 0.5

# Rough bounding box for NWS coverage (continental US + AK + HI approximation)
def _is_us_location(lat: float, lon: float) -> bool:
    return (24.0 <= lat <= 72.0) and (-180.0 <= lon <= -60.0)


class WeatherClient:
    """Fetches multi-model weather forecasts and converts them to calibrated probabilities."""

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._geo_cache: dict[str, tuple[float, float]] = {}
        # Cache keyed by (lat_r, lon_r, date_iso, model)
        self._forecast_cache: dict[tuple, EnsembleForecast] = {}
        self._last_ensemble_request: float = 0.0
        self._last_geocode_request: float = 0.0
        self._ensemble_cooldown_until: float = 0.0

    # ── Geocoding ────────────────────────────────────────────────────────────

    def geocode(self, location: str) -> Optional[tuple[float, float]]:
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
        results = resp.json().get("results", [])
        if not results:
            logger.warning(f"Could not geocode location: '{location}'")
            return None

        lat = float(results[0]["latitude"])
        lon = float(results[0]["longitude"])
        self._geo_cache[location] = (lat, lon)
        logger.debug(f"Geocoded '{location}' → ({lat:.3f}, {lon:.3f})")
        return lat, lon

    # ── Main public method ────────────────────────────────────────────────────

    def get_ensemble_forecast(
        self,
        latitude: float,
        longitude: float,
        target_date: date,
        location_name: str = "",
    ) -> Optional[EnsembleForecast]:
        """
        Fetch and combine forecasts from all available models.
        Returns a single EnsembleForecast whose member lists contain all
        models' members concatenated (ICON + ECMWF ≈ 91 members).
        """
        days_out = (target_date - date.today()).days
        if days_out == -1:
            days_out = 0
        if days_out < 0 or days_out > 7:
            logger.debug(f"Target date {target_date} is {days_out} days out — outside window (0–7)")
            return None

        forecasts: list[EnsembleForecast] = []
        models_fetched: list[str] = []

        # 1. Fetch each ensemble model
        for model in ENSEMBLE_MODELS:
            fc = self._fetch_ensemble_model(latitude, longitude, target_date, location_name, model)
            if fc and fc.member_count > 0:
                forecasts.append(fc)
                models_fetched.append(model)

        # 2. NWS for US locations (authoritative, single deterministic value but independent)
        if _is_us_location(latitude, longitude):
            nws_fc = self._fetch_nws(latitude, longitude, target_date, location_name)
            if nws_fc and nws_fc.member_count > 0:
                forecasts.append(nws_fc)
                models_fetched.append("nws")

        # 3. GFS deterministic as extra member for Americas
        if not forecasts or (_is_us_location(latitude, longitude) and len(forecasts) < 2):
            gfs_fc = self._fetch_gfs(latitude, longitude, target_date, location_name)
            if gfs_fc and gfs_fc.member_count > 0:
                forecasts.append(gfs_fc)
                models_fetched.append("gfs")

        if not forecasts:
            logger.warning(f"All weather models failed for {location_name} {target_date}")
            return None

        # 4. Combine all members into one EnsembleForecast
        combined = self._combine_forecasts(forecasts, location_name, latitude, longitude, target_date)
        combined_models = ", ".join(models_fetched)
        logger.debug(
            f"[MULTI-MODEL] {location_name} {target_date} | models={combined_models} | "
            f"total_members={combined.member_count} | "
            f"temp_avg={mean(combined.temperature_max_c):.1f}°C" if combined.temperature_max_c else ""
        )
        # Attach model names so calculate_probability can use them
        self._last_models: list[str] = models_fetched
        self._last_per_model_forecasts: list[EnsembleForecast] = forecasts

        return combined

    # ── Per-model fetchers ────────────────────────────────────────────────────

    def _fetch_ensemble_model(
        self,
        latitude: float,
        longitude: float,
        target_date: date,
        location_name: str,
        model: str,
    ) -> Optional[EnsembleForecast]:
        cache_key = (round(latitude, 2), round(longitude, 2), target_date.isoformat(), model)
        if cache_key in self._forecast_cache:
            return self._forecast_cache[cache_key]

        params = {
            "latitude": latitude,
            "longitude": longitude,
            "hourly": ["precipitation", "temperature_2m", "wind_speed_10m"],
            "models": model,
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
                        f"Open-Meteo 429 ({model}) — esperando {wait:.1f}s "
                        f"(intento {attempt + 1}/{ENSEMBLE_MAX_RETRIES})"
                    )
                    time.sleep(wait)
                    continue

                resp.raise_for_status()
                data = resp.json()
                break

            except requests.RequestException as exc:
                logger.warning(f"Ensemble API error ({model}): {exc}")
                return None

        if data is None:
            return None

        hourly = data.get("hourly", {})
        if not hourly:
            return None

        precip   = self._aggregate_members(hourly, "precipitation", "sum")
        temp_max = self._aggregate_members(hourly, "temperature_2m", "max")
        wind_max = self._aggregate_members(hourly, "wind_speed_10m", "max")

        if not precip:
            return None

        fc = EnsembleForecast(
            location=location_name or f"{latitude},{longitude}",
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            fetched_at=datetime.now(timezone.utc),
            precipitation_mm=precip,
            temperature_max_c=temp_max,
            temperature_min_c=temp_max,
            wind_speed_max_kmh=wind_max,
            member_count=len(precip),
        )
        self._forecast_cache[cache_key] = fc
        logger.debug(f"  [{model}] {len(precip)} members | temp_avg={mean(temp_max):.1f}°C")
        return fc

    def _fetch_gfs(
        self,
        latitude: float,
        longitude: float,
        target_date: date,
        location_name: str,
    ) -> Optional[EnsembleForecast]:
        """GFS deterministic forecast via Open-Meteo forecast API."""
        days_out = max(0, (target_date - date.today()).days)
        params = {
            "latitude": latitude,
            "longitude": longitude,
            "daily": ["precipitation_sum", "temperature_2m_max", "wind_speed_10m_max"],
            "models": "gfs_seamless",
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
            logger.debug(f"GFS forecast error: {exc}")
            return None

        daily = data.get("daily", {})
        precip_list = daily.get("precipitation_sum", [])
        idx = min(days_out, len(precip_list) - 1) if precip_list else -1
        if idx < 0:
            return None

        def _get(key: str) -> list[float]:
            v = daily.get(key, [])
            return [float(v[idx])] if idx < len(v) and v[idx] is not None else []

        temp = _get("temperature_2m_max")
        if not temp:
            return None

        logger.debug(f"  [gfs] 1 member | temp={temp[0]:.1f}°C")
        return EnsembleForecast(
            location=location_name or f"{latitude},{longitude}",
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            fetched_at=datetime.now(timezone.utc),
            precipitation_mm=_get("precipitation_sum"),
            temperature_max_c=temp,
            temperature_min_c=temp,
            wind_speed_max_kmh=_get("wind_speed_10m_max"),
            member_count=1,
        )

    def _fetch_nws(
        self,
        latitude: float,
        longitude: float,
        target_date: date,
        location_name: str,
    ) -> Optional[EnsembleForecast]:
        """
        NWS (NOAA) hourly forecast for US locations.
        Returns a single-member EnsembleForecast representing the official NWS forecast.
        """
        try:
            # Step 1: get gridpoint URL
            url = NWS_POINTS.format(lat=round(latitude, 4), lon=round(longitude, 4))
            resp = self._session.get(url, timeout=10, headers={"User-Agent": "betbot/1.0"})
            if resp.status_code != 200:
                return None
            grid_url = resp.json().get("properties", {}).get("forecastHourly")
            if not grid_url:
                return None

            # Step 2: get hourly forecast
            resp2 = self._session.get(grid_url, timeout=15, headers={"User-Agent": "betbot/1.0"})
            if resp2.status_code != 200:
                return None
            periods = resp2.json().get("properties", {}).get("periods", [])

        except Exception as exc:
            logger.debug(f"NWS fetch error: {exc}")
            return None

        # Filter periods that fall on target_date (UTC)
        target_str = target_date.isoformat()
        temps_f: list[float] = []
        precip_vals: list[float] = []

        for p in periods:
            start = p.get("startTime", "")
            if target_str not in start:
                continue
            t = p.get("temperature")
            unit = p.get("temperatureUnit", "F")
            if t is not None:
                t_c = (float(t) - 32) * 5 / 9 if unit == "F" else float(t)
                temps_f.append(t_c)
            prob_precip = p.get("probabilityOfPrecipitation", {})
            if prob_precip and prob_precip.get("value") is not None:
                precip_vals.append(float(prob_precip["value"]) / 100.0)

        if not temps_f:
            return None

        temp_max = max(temps_f)
        # Convert PoP (probability of precipitation) to mm equivalent (rough)
        avg_pop = mean(precip_vals) if precip_vals else 0.0
        # Rough: 50% PoP ≈ 3mm expected precip
        precip_mm = avg_pop * 6.0

        logger.debug(f"  [nws] 1 member | temp_max={temp_max:.1f}°C | precip_est={precip_mm:.1f}mm")
        return EnsembleForecast(
            location=location_name or f"{latitude},{longitude}",
            latitude=latitude,
            longitude=longitude,
            target_date=target_date,
            fetched_at=datetime.now(timezone.utc),
            precipitation_mm=[precip_mm],
            temperature_max_c=[temp_max],
            temperature_min_c=[min(temps_f)],
            wind_speed_max_kmh=[0.0],
            member_count=1,
        )

    # ── Combine forecasts ─────────────────────────────────────────────────────

    def _combine_forecasts(
        self,
        forecasts: list[EnsembleForecast],
        location: str,
        lat: float,
        lon: float,
        target_date: date,
    ) -> EnsembleForecast:
        """Concatenate all members from all models into one EnsembleForecast."""
        all_precip:   list[float] = []
        all_temp_max: list[float] = []
        all_temp_min: list[float] = []
        all_wind:     list[float] = []

        for fc in forecasts:
            all_precip.extend(fc.precipitation_mm)
            all_temp_max.extend(fc.temperature_max_c)
            all_temp_min.extend(fc.temperature_min_c)
            all_wind.extend(fc.wind_speed_max_kmh)

        return EnsembleForecast(
            location=location,
            latitude=lat,
            longitude=lon,
            target_date=target_date,
            fetched_at=datetime.now(timezone.utc),
            precipitation_mm=all_precip,
            temperature_max_c=all_temp_max,
            temperature_min_c=all_temp_min,
            wind_speed_max_kmh=all_wind,
            member_count=len(all_temp_max),
        )

    # ── Probability calculation ───────────────────────────────────────────────

    def calculate_probability(
        self,
        forecast: EnsembleForecast,
        condition: WeatherCondition,
        threshold: Optional[float] = None,
        threshold_high: Optional[float] = None,
    ) -> WeatherProbability:
        """
        Convert multi-model ensemble forecast to a calibrated probability.

        Steps:
          1. Compute raw probability per model (using stored per-model forecasts).
          2. Model agreement = 1 - spread_of_model_probs (normalized).
          3. Composite confidence = days_decay * (0.6 + 0.4 * agreement).
          4. true_prob = 0.5 + (raw - 0.5) * confidence.
        """
        days_out = (forecast.target_date - date.today()).days
        days_out = max(0, min(days_out, 7))
        base_decay = CONFIDENCE_DECAY.get(days_out, 0.40)

        # Per-model raw probabilities (for agreement calculation)
        per_model_forecasts = getattr(self, "_last_per_model_forecasts", [forecast])
        models_used = getattr(self, "_last_models", ["icon_seamless"])

        model_probs: list[float] = []
        for fc in per_model_forecasts:
            p = self._raw_probability(fc, condition, threshold, threshold_high)
            model_probs.append(p)

        raw_prob = self._raw_probability(forecast, condition, threshold, threshold_high)

        # Model agreement: 1.0 = all models agree, 0.0 = max spread (0.5)
        if len(model_probs) >= 2:
            spread = stdev(model_probs)  # std dev of probabilities
            # Normalize: spread of 0.5 (max possible) → agreement=0, spread=0 → agreement=1
            agreement = max(0.0, 1.0 - (spread / 0.25))
        else:
            agreement = 0.85  # single model: moderate agreement assumed

        # Composite confidence: decay * blend(agreement)
        # Even with full disagreement, keep at least 60% of base decay
        agreement_factor = 0.60 + 0.40 * agreement
        confidence = base_decay * agreement_factor

        # Extra penalty for narrow range markets (threshold to threshold_high)
        if threshold_high is not None and threshold is not None:
            range_c = abs(threshold_high - threshold)
            # A 1°F = 0.56°C range gets ~15% extra shrinkage; wider ranges less
            range_penalty = max(0.70, 1.0 - (0.56 / max(range_c, 0.1)) * 0.15)
            confidence = confidence * range_penalty

        confidence = max(0.10, min(1.0, confidence))
        adjusted = 0.5 + (raw_prob - 0.5) * confidence

        logger.debug(
            f"  prob: raw={raw_prob:.2%} | models={models_used} | "
            f"probs={[f'{p:.2%}' for p in model_probs]} | "
            f"agreement={agreement:.2f} | decay={base_decay:.2f} | "
            f"confidence={confidence:.2f} | adjusted={adjusted:.2%}"
        )

        return WeatherProbability(
            condition=condition,
            raw_probability=raw_prob,
            true_probability=adjusted,
            confidence=confidence,
            days_out=float(days_out),
            member_count=forecast.member_count,
            model_agreement=agreement,
            models_used=models_used,
            fetched_at=forecast.fetched_at,
        )

    def _raw_probability(
        self,
        forecast: EnsembleForecast,
        condition: WeatherCondition,
        threshold: Optional[float],
        threshold_high: Optional[float] = None,
    ) -> float:
        """Raw probability from ensemble members (no confidence adjustment)."""
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
            if threshold_high is not None:
                # Range market: fraction of members within [threshold, threshold_high]
                return sum(1 for v in members if threshold <= v <= threshold_high) / len(members)
            else:
                # Exact value: ±0.5°C window
                return sum(1 for v in members if abs(v - threshold) <= 0.5) / len(members)

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

    def _aggregate_members(self, hourly: dict, variable: str, agg: str) -> list[float]:
        members = []
        for i in range(1, 60):
            key = f"{variable}_member{i:02d}"
            if key not in hourly:
                break
            values = [v for v in hourly[key] if v is not None]
            if not values:
                continue
            members.append(sum(values) if agg == "sum" else max(values))

        if not members and variable in hourly:
            values = [v for v in hourly[variable] if v is not None]
            if values:
                members = [sum(values) if agg == "sum" else max(values)]

        return members

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
