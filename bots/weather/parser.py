"""
Weather market parser.

Parses natural language Polymarket questions into structured WeatherMarketInfo
objects that the strategy can work with.

Examples of questions it can parse:
  "Will it rain in New York City on April 15, 2025?"
  "Will the high temperature in Miami exceed 95°F on July 4?"
  "Will there be a hurricane in the Gulf of Mexico by October 31?"
  "Will London receive more than 10mm of rain on March 22?"
  "Will temperatures in Chicago drop below 0°F on January 5?"
  "Will wind speeds in Boston exceed 60 mph on December 3?"
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from loguru import logger

from core.models import WeatherCondition, WeatherMarketInfo
from core.weather.client import WeatherClient

# Months for parsing
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

# Known major cities for geocoding (fallback pattern matching)
KNOWN_CITIES = [
    "New York", "New York City", "NYC", "Los Angeles", "Chicago", "Houston",
    "Phoenix", "Philadelphia", "San Antonio", "San Diego", "Dallas", "San Jose",
    "Austin", "Jacksonville", "Fort Worth", "Columbus", "Charlotte", "Indianapolis",
    "San Francisco", "Seattle", "Denver", "Nashville", "Oklahoma City", "El Paso",
    "Washington DC", "Las Vegas", "Louisville", "Memphis", "Portland", "Baltimore",
    "Milwaukee", "Albuquerque", "Tucson", "Fresno", "Sacramento", "Mesa",
    "Kansas City", "Atlanta", "Omaha", "Colorado Springs", "Raleigh", "Virginia Beach",
    "Long Beach", "Minneapolis", "Tampa", "New Orleans", "Arlington", "Bakersfield",
    "Honolulu", "Anchorage", "Miami", "Boston", "Detroit",
    # International
    "London", "Paris", "Berlin", "Tokyo", "Beijing", "Sydney", "Melbourne",
    "Toronto", "Vancouver", "Mexico City", "São Paulo", "Buenos Aires",
    "Mumbai", "Delhi", "Shanghai", "Dubai", "Moscow", "Cairo",
    "Lagos", "Nairobi", "Cape Town", "Johannesburg",
]

# Sort by length descending so longer names match first (e.g. "New York City" before "New York")
KNOWN_CITIES.sort(key=len, reverse=True)


class WeatherMarketParser:
    """Parses Polymarket weather questions into structured data."""

    def __init__(self, weather_client: WeatherClient) -> None:
        self._weather = weather_client

    def parse(self, condition_id: str, question: str) -> Optional[WeatherMarketInfo]:
        """
        Parse a market question.
        Returns None if this doesn't look like a parseable weather market.
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
        # Strategy 1: known city list
        q_lower = question.lower()
        for city in KNOWN_CITIES:
            if city.lower() in q_lower:
                return city

        # Strategy 2: "in [City]" pattern
        m = re.search(r"\bin\s+([A-Z][a-zA-Z\s]{2,25})(?:\s+on|\s+by|\s+exceed|\s+drop|[,?])", question)
        if m:
            candidate = m.group(1).strip()
            if len(candidate) > 2:
                return candidate

        # Strategy 3: "at [City]" pattern
        m = re.search(r"\bat\s+([A-Z][a-zA-Z\s]{2,20})(?:\s+on|\s+exceed|\s+[,?])", question)
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
            r"on\s+(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?"
            r"(?:,?\s+(\d{4}))?",
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
                    # If date is in the past, try next year
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
                day = int(day_str)
                year = int(year_str) if year_str else today.year
                try:
                    return date(year, month, day)
                except ValueError:
                    pass

        # Pattern: MM/DD/YYYY or MM/DD/YY
        m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", question)
        if m:
            try:
                month, day = int(m.group(1)), int(m.group(2))
                year = int(m.group(3))
                if year < 100:
                    year += 2000
                return date(year, month, day)
            except ValueError:
                pass

        # Pattern: "today", "tomorrow", "this week"
        if "today" in q_lower:
            return today
        if "tomorrow" in q_lower:
            return today + timedelta(days=1)

        return None

    # ── Condition extraction ─────────────────────────────────────────────────

    def _extract_condition(
        self, question: str
    ) -> tuple[WeatherCondition, Optional[float], Optional[str]]:
        """Extract condition type, threshold value, and unit."""
        q_lower = question.lower()

        # Hurricane/storm
        if "hurricane" in q_lower:
            return WeatherCondition.HURRICANE, None, None
        if any(w in q_lower for w in ["storm", "tornado", "cyclone", "typhoon"]):
            return WeatherCondition.STORM, None, None

        # Snow
        if any(w in q_lower for w in ["snow", "blizzard", "snowfall", "snowstorm"]):
            threshold, unit = self._extract_numeric_threshold(question)
            return WeatherCondition.SNOW, threshold, unit

        # Rain / precipitation
        if any(w in q_lower for w in ["rain", "rainfall", "precipitation", "wet"]):
            threshold, unit = self._extract_numeric_threshold(question)
            return WeatherCondition.RAIN, threshold, unit

        # Temperature above threshold
        temp_above = re.search(
            r"(?:exceed|above|over|reach|surpass|high.*above|high.*over)\s+"
            r"(\d+(?:\.\d+)?)\s*[°]?\s*(f|c|fahrenheit|celsius)",
            q_lower,
        )
        if temp_above:
            val = float(temp_above.group(1))
            unit = "F" if temp_above.group(2).startswith("f") else "C"
            val_c = (val - 32) * 5 / 9 if unit == "F" else val
            return WeatherCondition.TEMPERATURE_ABOVE, val_c, "C"

        # Temperature below threshold
        temp_below = re.search(
            r"(?:below|under|drop.*below|fall.*below)\s+"
            r"(\d+(?:\.\d+)?)\s*[°]?\s*(f|c|fahrenheit|celsius)",
            q_lower,
        )
        if temp_below:
            val = float(temp_below.group(1))
            unit = "F" if temp_below.group(2).startswith("f") else "C"
            val_c = (val - 32) * 5 / 9 if unit == "F" else val
            return WeatherCondition.TEMPERATURE_BELOW, val_c, "C"

        # Generic temperature mention
        if any(w in q_lower for w in ["temperature", "degrees", "°f", "°c", "fahrenheit", "celsius"]):
            # Try to figure out above/below from context
            if any(w in q_lower for w in ["exceed", "above", "over", "high", "hot", "warm"]):
                threshold, unit = self._extract_numeric_threshold(question)
                return WeatherCondition.TEMPERATURE_ABOVE, threshold, unit
            threshold, unit = self._extract_numeric_threshold(question)
            return WeatherCondition.TEMPERATURE_BELOW, threshold, unit

        # Wind
        if any(w in q_lower for w in ["wind", "gust", "mph", "km/h", "knots"]):
            threshold, unit = self._extract_numeric_threshold(question)
            # Convert mph to km/h if needed
            if unit == "mph" and threshold is not None:
                threshold = threshold * 1.60934
                unit = "km/h"
            return WeatherCondition.WIND_ABOVE, threshold, unit

        # Sunny / clear
        if any(w in q_lower for w in ["sunny", "sun", "clear", "dry", "no rain"]):
            return WeatherCondition.SUNNY, None, None

        return WeatherCondition.UNKNOWN, None, None

    def _extract_numeric_threshold(
        self, question: str
    ) -> tuple[Optional[float], Optional[str]]:
        """Find the first numeric value and its unit in the question."""
        # Look for: number + optional unit
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
