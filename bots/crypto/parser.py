"""Parses Polymarket crypto price market questions into structured CryptoMarketInfo."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from core.models import CryptoMarketInfo, CryptoPriceDirection

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}

_ASSET_PATTERNS = {
    "BTC": re.compile(r"\b(btc|bitcoin)\b", re.I),
    "ETH": re.compile(r"\b(eth|ethereum)\b", re.I),
}

_ABOVE_RE = re.compile(r"\b(above|over|exceed|higher than|close above)\b", re.I)
_BELOW_RE = re.compile(r"\b(below|under|lower than|drop below|close below)\b", re.I)

_THRESHOLD_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_THRESHOLD_FALLBACK_RE = re.compile(r"\b([\d,]{4,}(?:\.\d+)?)\b")

_TIME_FULL_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*UTC\s+on\s+(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?", re.I
)
_TIME_AMPM_RE = re.compile(r"(\d{1,2}:\d{2})\s*(AM|PM)\s*UTC", re.I)
_TIME_UTC_RE  = re.compile(r"(\d{1,2}:\d{2})\s*UTC", re.I)

_MAX_MINUTES = 120.0


class CryptoMarketParser:
    def parse(
        self,
        condition_id: str,
        question: str,
        reference_datetime: Optional[datetime] = None,
    ) -> Optional[CryptoMarketInfo]:
        now = reference_datetime or datetime.now(timezone.utc)
        q = question

        asset = self._parse_asset(q)
        if asset is None:
            return None

        direction = self._parse_direction(q)
        if direction is None:
            return None

        threshold = self._parse_threshold(q)
        if threshold is None:
            return None

        target_dt = self._parse_target_datetime(q, now)
        if target_dt is None:
            return None

        minutes = max(0.0, (target_dt - now).total_seconds() / 60.0)
        if minutes <= 0 or minutes > _MAX_MINUTES:
            return None

        return CryptoMarketInfo(
            condition_id=condition_id,
            question=question,
            asset=asset,
            direction=direction,
            threshold_usd=threshold,
            target_datetime=target_dt,
            minutes_to_resolution=minutes,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _parse_asset(self, q: str) -> Optional[str]:
        for asset, pat in _ASSET_PATTERNS.items():
            if pat.search(q):
                return asset
        return None

    def _parse_direction(self, q: str) -> Optional[CryptoPriceDirection]:
        has_above = bool(_ABOVE_RE.search(q))
        has_below = bool(_BELOW_RE.search(q))
        if has_above and not has_below:
            return CryptoPriceDirection.ABOVE
        if has_below and not has_above:
            return CryptoPriceDirection.BELOW
        return None

    def _parse_threshold(self, q: str) -> Optional[float]:
        m = _THRESHOLD_RE.search(q)
        if m:
            return float(m.group(1).replace(",", ""))
        m = _THRESHOLD_FALLBACK_RE.search(q)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    def _parse_target_datetime(self, q: str, now: datetime) -> Optional[datetime]:
        # Pattern 1: "12:00 UTC on April 18" or "12:00 UTC on April 18, 2025"
        m = _TIME_FULL_RE.search(q)
        if m:
            time_str, month_str, day_str, year_str = m.groups()
            month = _MONTH_MAP.get(month_str.lower())
            if month is None:
                return None
            day = int(day_str)
            year = int(year_str) if year_str else now.year
            hour, minute = map(int, time_str.split(":"))
            try:
                dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
                if dt < now and not year_str:
                    dt = dt.replace(year=year + 1)
                return dt
            except ValueError:
                return None

        # Pattern 2: "12:00 PM UTC"
        m = _TIME_AMPM_RE.search(q)
        if m:
            time_str, ampm = m.groups()
            hour, minute = map(int, time_str.split(":"))
            if ampm.upper() == "PM" and hour != 12:
                hour += 12
            elif ampm.upper() == "AM" and hour == 12:
                hour = 0
            try:
                dt = datetime(now.year, now.month, now.day, hour, minute, tzinfo=timezone.utc)
                if dt < now:
                    from datetime import timedelta
                    dt += timedelta(days=1)
                return dt
            except ValueError:
                return None

        # Pattern 3: "12:00 UTC" — assume today or tomorrow
        m = _TIME_UTC_RE.search(q)
        if m:
            time_str = m.group(1)
            hour, minute = map(int, time_str.split(":"))
            try:
                dt = datetime(now.year, now.month, now.day, hour, minute, tzinfo=timezone.utc)
                if dt < now:
                    from datetime import timedelta
                    dt += timedelta(days=1)
                return dt
            except ValueError:
                return None

        return None
