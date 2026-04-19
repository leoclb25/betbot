"""Parses Polymarket crypto price market questions into structured CryptoMarketInfo."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

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

# "Up or Down" market: "Bitcoin Up or Down - April 18, 11:10PM-11:15PM ET"
_UP_OR_DOWN_RE = re.compile(
    r"\b(up or down)\b.*?(\w+)\s+(\d{1,2}),\s*(\d{1,2}:\d{2}(?:AM|PM))-(\d{1,2}:\d{2}(?:AM|PM))\s*(ET|EST|EDT|UTC)",
    re.I,
)

# Classic "above/below $X at HH:MM UTC on Month Day"
_ABOVE_RE = re.compile(r"\b(above|over|exceed|higher than|close above)\b", re.I)
_BELOW_RE = re.compile(r"\b(below|under|lower than|drop below|close below)\b", re.I)
_THRESHOLD_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_THRESHOLD_FALLBACK_RE = re.compile(r"\b([\d,]{4,}(?:\.\d+)?)\b")

_TIME_FULL_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*UTC\s+on\s+(\w+)\s+(\d{1,2})(?:,?\s*(\d{4}))?", re.I
)
_TIME_AMPM_UTC_RE = re.compile(r"(\d{1,2}:\d{2})\s*(AM|PM)\s*UTC", re.I)
_TIME_UTC_RE = re.compile(r"(\d{1,2}:\d{2})\s*UTC", re.I)

_MAX_MINUTES = 120.0
_ET = ZoneInfo("America/New_York")


class CryptoMarketParser:
    def parse(
        self,
        condition_id: str,
        question: str,
        reference_datetime: Optional[datetime] = None,
    ) -> Optional[CryptoMarketInfo]:
        now = reference_datetime or datetime.now(timezone.utc)

        asset = self._parse_asset(question)
        if asset is None:
            return None

        # Try "Up or Down" format first
        info = self._parse_up_or_down(condition_id, question, asset, now)
        if info is not None:
            return info

        # Fall back to classic "above/below $X" format
        return self._parse_price_level(condition_id, question, asset, now)

    # ── Up or Down ────────────────────────────────────────────────────────────

    def _parse_up_or_down(
        self, condition_id: str, question: str, asset: str, now: datetime
    ) -> Optional[CryptoMarketInfo]:
        if not re.search(r"\bup or down\b", question, re.I):
            return None

        m = _UP_OR_DOWN_RE.search(question)
        if not m:
            # Try simpler fallback: just grab end time
            target_dt = self._parse_et_window_end(question, now)
            if target_dt is None:
                return None
        else:
            _, month_str, day_str, _start_time, end_time_str, tz_str = m.groups()
            month = _MONTH_MAP.get(month_str.lower())
            if month is None:
                return None
            day = int(day_str)
            target_dt = self._parse_ampm_time(end_time_str, month, day, now, tz_str)
            if target_dt is None:
                return None

        minutes = max(0.0, (target_dt - now).total_seconds() / 60.0)
        if minutes <= 0 or minutes > _MAX_MINUTES:
            return None

        # YES = price goes UP in this window
        return CryptoMarketInfo(
            condition_id=condition_id,
            question=question,
            asset=asset,
            direction=CryptoPriceDirection.UP,
            threshold_usd=None,
            target_datetime=target_dt,
            minutes_to_resolution=minutes,
        )

    def _parse_et_window_end(self, question: str, now: datetime) -> Optional[datetime]:
        """Extract end time from 'HH:MMPM-HH:MMPM ET' patterns."""
        pat = re.compile(r"(\d{1,2}:\d{2}(?:AM|PM))-(\d{1,2}:\d{2}(?:AM|PM))\s*(ET|EST|EDT|UTC)", re.I)
        m = pat.search(question)
        if not m:
            return None
        end_time_str = m.group(2)
        tz_str = m.group(3)

        # Try to find date in question
        date_pat = re.compile(r"(\w+)\s+(\d{1,2}),", re.I)
        dm = date_pat.search(question)
        if dm:
            month = _MONTH_MAP.get(dm.group(1).lower())
            day = int(dm.group(2))
            if month:
                return self._parse_ampm_time(end_time_str, month, day, now, tz_str)

        # No date found — use today/tomorrow
        return self._parse_ampm_time_today(end_time_str, now, tz_str)

    def _parse_ampm_time(
        self, time_str: str, month: int, day: int, now: datetime, tz_str: str
    ) -> Optional[datetime]:
        pat = re.compile(r"(\d{1,2}):(\d{2})(AM|PM)", re.I)
        m = pat.match(time_str.strip())
        if not m:
            return None
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        year = now.year
        try:
            if tz_str.upper() in ("ET", "EST", "EDT"):
                dt_local = datetime(year, month, day, hour, minute, tzinfo=_ET)
                return dt_local.astimezone(timezone.utc)
            else:
                return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            return None

    def _parse_ampm_time_today(self, time_str: str, now: datetime, tz_str: str) -> Optional[datetime]:
        pat = re.compile(r"(\d{1,2}):(\d{2})(AM|PM)", re.I)
        m = pat.match(time_str.strip())
        if not m:
            return None
        hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
        if ampm == "PM" and hour != 12:
            hour += 12
        elif ampm == "AM" and hour == 12:
            hour = 0

        try:
            if tz_str.upper() in ("ET", "EST", "EDT"):
                now_et = now.astimezone(_ET)
                dt_local = now_et.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if dt_local <= now_et:
                    dt_local += timedelta(days=1)
                return dt_local.astimezone(timezone.utc)
            else:
                dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if dt <= now:
                    dt += timedelta(days=1)
                return dt
        except ValueError:
            return None

    # ── Classic price-level format ────────────────────────────────────────────

    def _parse_price_level(
        self, condition_id: str, question: str, asset: str, now: datetime
    ) -> Optional[CryptoMarketInfo]:
        direction = self._parse_direction(question)
        if direction is None:
            return None

        threshold = self._parse_threshold(question)
        if threshold is None:
            return None

        target_dt = self._parse_target_datetime(question, now)
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

        m = _TIME_AMPM_UTC_RE.search(q)
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
                    dt += timedelta(days=1)
                return dt
            except ValueError:
                return None

        m = _TIME_UTC_RE.search(q)
        if m:
            time_str = m.group(1)
            hour, minute = map(int, time_str.split(":"))
            try:
                dt = datetime(now.year, now.month, now.day, hour, minute, tzinfo=timezone.utc)
                if dt < now:
                    dt += timedelta(days=1)
                return dt
            except ValueError:
                return None

        return None
