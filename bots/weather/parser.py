"""
Weather market parser.

Parses natural language Polymarket questions into structured WeatherMarketInfo
objects that the strategy can work with.

Polymarket weather questions follow a very consistent format:
  "Will the highest temperature in {CITY} be {X}°C on {DATE}?"
  "Will the highest temperature in {CITY} be {X}°C or higher on {DATE}?"
  "Will the highest temperature in {CITY} be {X}°C or below on {DATE}?"
  "Will the highest temperature in {CITY} be between {X}-{Y}°F on {DATE}?"
  "Will it rain in {CITY} on {DATE}?"
  "Will there be a hurricane ..."

The parser handles all these patterns and maps them to structured data
the strategy can use to fetch the correct weather forecast.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger

from core.models import WeatherCondition, WeatherMarketInfo
from core.weather.client import WeatherClient

# Month name → number
MONTH_MAP = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

# Comprehensive city list – sorted longest-first so multi-word names match first
KNOWN_CITIES = sorted([
    # North America
    "New York City", "New York", "NYC", "Los Angeles", "Chicago", "Houston",
    "Phoenix", "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose",
    "Austin", "Jacksonville", "Fort Worth", "Columbus", "Charlotte", "Indianapolis",
    "San Francisco", "Seattle", "Denver", "Nashville", "Oklahoma City", "El Paso",
    "Washington DC", "Washington", "Las Vegas", "Louisville", "Memphis", "Portland",
    "Baltimore", "Milwaukee", "Albuquerque", "Tucson", "Fresno", "Sacramento",
    "Mesa", "Kansas City", "Atlanta", "Omaha", "Colorado Springs", "Raleigh",
    "Virginia Beach", "Long Beach", "Minneapolis", "Tampa", "New Orleans",
    "Arlington", "Bakersfield", "Honolulu", "Anchorage", "Miami", "Boston",
    "Detroit", "Montreal", "Toronto", "Vancouver", "Calgary", "Ottawa",
    "Mexico City", "Guadalajara", "Monterrey", "Panama City",
    # South America
    "São Paulo", "Sao Paulo", "Rio de Janeiro", "Buenos Aires", "Bogota",
    "Lima", "Santiago", "Caracas", "Medellin", "Cali",
    # Europe
    "London", "Paris", "Berlin", "Madrid", "Rome", "Barcelona", "Vienna",
    "Amsterdam", "Brussels", "Warsaw", "Prague", "Budapest", "Bucharest",
    "Stockholm", "Copenhagen", "Oslo", "Helsinki", "Zurich", "Geneva",
    "Lisbon", "Athens", "Istanbul", "Ankara", "Kyiv", "Kiev", "Minsk",
    "Munich", "Hamburg", "Frankfurt", "Milan", "Naples", "Turin",
    "Marseille", "Lyon", "Rotterdam", "Antwerp", "Dublin", "Edinburgh",
    "Manchester", "Birmingham", "Liverpool", "Vilnius", "Riga", "Tallinn",
    "Ljubljana", "Zagreb", "Belgrade", "Sarajevo", "Skopje", "Sofia",
    "Chisinau", "Tirana", "Podgorica", "Valletta", "Nicosia",
    # Asia
    "Tokyo", "Beijing", "Shanghai", "Shenzhen", "Guangzhou", "Chengdu",
    "Chongqing", "Wuhan", "Xi'an", "Nanjing", "Tianjin", "Hangzhou",
    "Mumbai", "Delhi", "Kolkata", "Chennai", "Bangalore", "Hyderabad",
    "Ahmedabad", "Pune", "Surat", "Lucknow", "Jaipur", "Kanpur",
    "Seoul", "Busan", "Incheon", "Daegu", "Daejeon",
    "Osaka", "Nagoya", "Sapporo", "Fukuoka", "Kobe",
    "Hong Kong", "Taipei", "Kaohsiung", "Taichung",
    "Singapore", "Kuala Lumpur", "Jakarta", "Bangkok", "Ho Chi Minh City",
    "Hanoi", "Manila", "Dhaka", "Karachi", "Lahore", "Islamabad",
    "Colombo", "Yangon", "Phnom Penh", "Vientiane",
    "Ulaanbaatar",
    # Middle East
    "Dubai", "Abu Dhabi", "Riyadh", "Jeddah", "Kuwait City", "Doha",
    "Muscat", "Bahrain", "Tel Aviv", "Jerusalem", "Amman", "Beirut",
    "Baghdad", "Tehran", "Kabul",
    # Africa
    "Cairo", "Alexandria", "Lagos", "Abuja", "Nairobi", "Mombasa",
    "Addis Ababa", "Dar es Salaam", "Kinshasa", "Johannesburg", "Cape Town",
    "Durban", "Pretoria", "Casablanca", "Tunis", "Algiers", "Accra",
    "Kampala", "Kigali", "Lusaka", "Harare", "Maputo", "Luanda",
    # Oceania
    "Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Auckland",
    "Wellington", "Christchurch",
], key=len, reverse=True)


class WeatherMarketParser:
    """Parses Polymarket weather questions into structured data."""

    def __init__(self, weather_client: WeatherClient) -> None:
        self._weather = weather_client

    def parse(self, condition_id: str, question: str) -> Optional[WeatherMarketInfo]:
        """
        Parse a market question.
        Returns None if not parseable (not a weather market or no location/date found).
        """
        q = question.strip()

        location = self._extract_location(q)
        if location is None:
            logger.debug(f"Could not extract location from: '{q[:80]}'")
            return None

        coords = self._weather.geocode(location)
        if coords is None:
            logger.debug(f"Could not geocode location '{location}'")
            return None

        target_date = self._extract_date(q)
        if target_date is None:
            logger.debug(f"Could not extract date from: '{q[:80]}'")
            return None

        condition, threshold, unit = self._extract_condition(q)

        lat, lon = coords
        return WeatherMarketInfo(
            condition_id=condition_id,
            question=q,
            location=location,
            latitude=lat,
            longitude=lon,
            target_date=target_date,
            condition=condition,
            threshold=threshold,
            threshold_unit=unit,
        )

    # ── Location extraction ──────────────────────────────────────────────────

    def _extract_location(self, question: str) -> Optional[str]:
        """Try several heuristics to find the city/location in the question."""
        # Strategy 1: exact match from known city list (catches multi-word names like "New York City")
        q_lower = question.lower()
        for city in KNOWN_CITIES:
            if city.lower() in q_lower:
                return city

        # Strategy 2: "in [City]" pattern – stop at "be", "on", "by", punctuation
        # This handles unknown cities not in the list above
        m = re.search(
            r"\bin\s+([A-Z][a-zA-Z](?:[a-zA-Z\s\']{1,30})?)(?=\s+(?:be|on|by|exceed|drop|below)|[,?])",
            question,
        )
        if m:
            candidate = m.group(1).strip()
            # Filter out false positives like "the" or short noise words
            if len(candidate) >= 3 and candidate.lower() not in {"the", "a", "an"}:
                return candidate

        # Strategy 3: "at [City]" pattern
        m = re.search(
            r"\bat\s+([A-Z][a-zA-Z](?:[a-zA-Z\s\']{1,30})?)(?=\s+(?:be|on|exceed)|[,?])",
            question,
        )
        if m:
            return m.group(1).strip()

        return None

    # ── Date extraction ──────────────────────────────────────────────────────

    def _extract_date(self, question: str) -> Optional[date]:
        """Extract the target date from the question."""
        today = date.today()
        q_lower = question.lower()

        # Pattern: "on April 15", "on April 15, 2025", "on April 15th"
        m = re.search(
            r"on\s+(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?",
            q_lower,
        )
        if m:
            month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
            month = MONTH_MAP.get(month_str)
            if month:
                day = int(day_str)
                year = int(year_str) if year_str else today.year
                try:
                    d = date(year, month, day)
                    if d < today and not year_str:
                        d = date(year + 1, month, day)
                    return d
                except ValueError:
                    pass

        # Pattern: "by [month] [day]"
        m = re.search(
            r"by\s+(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?",
            q_lower,
        )
        if m:
            month_str, day_str, year_str = m.group(1), m.group(2), m.group(3)
            month = MONTH_MAP.get(month_str)
            if month:
                try:
                    return date(int(year_str) if year_str else today.year, month, int(day_str))
                except ValueError:
                    pass

        # Pattern: MM/DD/YYYY
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", question)
        if m:
            try:
                month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if year < 100:
                    year += 2000
                return date(year, month, day)
            except ValueError:
                pass

        if "today" in q_lower:
            return today
        if "tomorrow" in q_lower:
            return today + timedelta(days=1)

        return None

    # ── Condition extraction ─────────────────────────────────────────────────

    def _extract_condition(
        self, question: str
    ) -> tuple[WeatherCondition, Optional[float], Optional[str]]:
        """
        Extract condition type, threshold value, and unit.

        Handles Polymarket-specific formats:
          "be {X}°C or higher"      → TEMPERATURE_ABOVE
          "be {X}°C or below"       → TEMPERATURE_BELOW
          "be {X}°C"                → TEMPERATURE_EXACT (exact temperature match)
          "be between {X}-{Y}°F"    → TEMPERATURE_EXACT (midpoint of range)
          "be {X}°F or higher"      → TEMPERATURE_ABOVE (converted to °C)
          plain rain/snow/wind      → respective conditions
        """
        q_lower = question.lower()

        # ── Hurricane / tropical storm ────────────────────────────────────────
        if "hurricane" in q_lower:
            return WeatherCondition.HURRICANE, None, None
        if any(w in q_lower for w in ["tornado", "cyclone", "typhoon"]):
            return WeatherCondition.STORM, None, None

        # ── Temperature: "between X-Y°F/°C" range ────────────────────────────
        m = re.search(
            r"between\s+(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s*[°]?\s*(f|c|fahrenheit|celsius)",
            q_lower,
        )
        if m:
            low = float(m.group(1))
            high = float(m.group(2))
            unit = "F" if m.group(3).startswith("f") else "C"
            mid = (low + high) / 2.0
            mid_c = (mid - 32) * 5 / 9 if unit == "F" else mid
            half_range = (high - low) / 2.0
            half_range_c = half_range * (5 / 9) if unit == "F" else half_range
            # Store midpoint and half-range encoded as negative threshold (sentinel)
            # Strategy: use TEMPERATURE_EXACT with threshold = midpoint in °C
            return WeatherCondition.TEMPERATURE_EXACT, round(mid_c, 2), "C"

        # ── Temperature: "be X°C/°F [or higher/or below/or above/or lower]" ──
        m = re.search(
            r"be\s+(\d+(?:\.\d+)?)\s*[°]?\s*(f|c|fahrenheit|celsius)"
            r"(?:\s+or\s+(higher|above|lower|below))?",
            q_lower,
        )
        if m:
            val = float(m.group(1))
            unit = "F" if m.group(2).startswith("f") else "C"
            modifier = (m.group(3) or "").lower()
            val_c = (val - 32) * 5 / 9 if unit == "F" else val

            if modifier in ("higher", "above"):
                return WeatherCondition.TEMPERATURE_ABOVE, round(val_c, 2), "C"
            elif modifier in ("lower", "below"):
                return WeatherCondition.TEMPERATURE_BELOW, round(val_c, 2), "C"
            else:
                # Exact match (no modifier) – e.g. "be 29°C"
                return WeatherCondition.TEMPERATURE_EXACT, round(val_c, 2), "C"

        # ── Temperature: "exceed / above / over X°C" ─────────────────────────
        m = re.search(
            r"(?:exceed|above|over|reach|surpass)\s+(\d+(?:\.\d+)?)\s*[°]?\s*(f|c|fahrenheit|celsius)",
            q_lower,
        )
        if m:
            val = float(m.group(1))
            unit = "F" if m.group(2).startswith("f") else "C"
            val_c = (val - 32) * 5 / 9 if unit == "F" else val
            return WeatherCondition.TEMPERATURE_ABOVE, round(val_c, 2), "C"

        # ── Temperature: "below / under / drop below X°C" ────────────────────
        m = re.search(
            r"(?:below|under|drop\s+below|fall\s+below)\s+(\d+(?:\.\d+)?)\s*[°]?\s*(f|c|fahrenheit|celsius)",
            q_lower,
        )
        if m:
            val = float(m.group(1))
            unit = "F" if m.group(2).startswith("f") else "C"
            val_c = (val - 32) * 5 / 9 if unit == "F" else val
            return WeatherCondition.TEMPERATURE_BELOW, round(val_c, 2), "C"

        # ── Generic temperature mention (last resort) ─────────────────────────
        if any(w in q_lower for w in ["temperature", "degrees", "°f", "°c", "fahrenheit", "celsius"]):
            threshold, unit = self._extract_numeric_threshold(question)
            if any(w in q_lower for w in ["exceed", "above", "over", "high", "hot", "warm", "higher"]):
                return WeatherCondition.TEMPERATURE_ABOVE, threshold, unit
            return WeatherCondition.TEMPERATURE_BELOW, threshold, unit

        # ── Snow ──────────────────────────────────────────────────────────────
        if any(w in q_lower for w in ["snow", "blizzard", "snowfall", "snowstorm"]):
            threshold, unit = self._extract_numeric_threshold(question)
            return WeatherCondition.SNOW, threshold, unit

        # ── Rain / precipitation ──────────────────────────────────────────────
        if any(w in q_lower for w in ["rain", "rainfall", "precipitation", "wet"]):
            threshold, unit = self._extract_numeric_threshold(question)
            return WeatherCondition.RAIN, threshold, unit

        # ── Storm ─────────────────────────────────────────────────────────────
        if any(w in q_lower for w in ["storm", "thunder", "lightning"]):
            return WeatherCondition.STORM, None, None

        # ── Wind ──────────────────────────────────────────────────────────────
        if any(w in q_lower for w in ["wind", "gust", "mph", "km/h", "knots"]):
            threshold, unit = self._extract_numeric_threshold(question)
            if unit == "mph" and threshold is not None:
                threshold = threshold * 1.60934
                unit = "km/h"
            return WeatherCondition.WIND_ABOVE, threshold, unit

        # ── Sunny / clear / dry ───────────────────────────────────────────────
        if any(w in q_lower for w in ["sunny", "sun", "clear", "dry", "no rain"]):
            return WeatherCondition.SUNNY, None, None

        return WeatherCondition.UNKNOWN, None, None

    def _extract_numeric_threshold(
        self, question: str
    ) -> tuple[Optional[float], Optional[str]]:
        """Find the first numeric value and its unit in the question."""
        m = re.search(
            r"(\d+(?:\.\d+)?)\s*"
            r"(mm|cm|inches?|in|°?f|°?c|fahrenheit|celsius|mph|km/h|kph|knots?)?",
            question,
            re.IGNORECASE,
        )
        if m:
            val = float(m.group(1))
            unit = (m.group(2) or "").lower().strip("°")
            return val, unit or None
        return None, None
