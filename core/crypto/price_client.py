"""Binance public REST client for spot price, 24h stats, and order book imbalance."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests
from loguru import logger

from core.env_utils import env_float

_BASE = "https://api.binance.com/api/v3"
_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


class BinancePriceClient:
    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "betbot/1.0", "Accept": "application/json"})
        self._rate_limit: float = env_float("BINANCE_RATE_LIMIT_SECONDS", 1.0)
        self._last_request_at: float = 0.0
        self._price_cache: dict[str, tuple[float, datetime]] = {}
        self._stats_cache: dict[str, tuple[dict, datetime]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get_spot_price(self, asset: str) -> float:
        sym = self._symbol(asset)
        cached, ts = self._price_cache.get(asset, (None, None))
        if cached is not None and ts is not None:
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < 30:
                return cached

        self._throttle()
        resp = self._session.get(f"{_BASE}/ticker/price", params={"symbol": sym}, timeout=10)
        resp.raise_for_status()
        price = float(resp.json()["price"])
        self._price_cache[asset] = (price, datetime.now(timezone.utc))
        return price

    def get_price_stats(self, asset: str) -> dict:
        sym = self._symbol(asset)
        cached, ts = self._stats_cache.get(asset, (None, None))
        if cached is not None and ts is not None:
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age < 60:
                return cached

        self._throttle()
        resp = self._session.get(f"{_BASE}/ticker/24hr", params={"symbol": sym}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        self._stats_cache[asset] = (data, datetime.now(timezone.utc))
        return data

    def get_order_book_imbalance(self, asset: str, depth: int = 10) -> float:
        sym = self._symbol(asset)
        try:
            self._throttle()
            resp = self._session.get(
                f"{_BASE}/depth", params={"symbol": sym, "limit": depth}, timeout=10
            )
            resp.raise_for_status()
            data = resp.json()
            sum_bids = sum(float(b[1]) for b in data.get("bids", []))
            sum_asks = sum(float(a[1]) for a in data.get("asks", []))
            total = sum_bids + sum_asks
            if total == 0:
                return 0.0
            return (sum_bids - sum_asks) / total
        except Exception as exc:
            logger.warning(f"[BINANCE] order book imbalance failed for {asset}: {exc}")
            return 0.0

    # ── Private ───────────────────────────────────────────────────────────────

    def _symbol(self, asset: str) -> str:
        sym = _SYMBOLS.get(asset.upper())
        if sym is None:
            raise ValueError(f"Unsupported asset: {asset}")
        return sym

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)
        self._last_request_at = time.monotonic()
