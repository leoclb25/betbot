"""Kraken public REST client for spot price, 24h stats, and order book imbalance.

Uses Kraken instead of Binance — Binance blocks AWS IPs (451).
Kraken public endpoints require no authentication and work from any server.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

import requests
from loguru import logger

from core.env_utils import env_float

_BASE = "https://api.kraken.com/0/public"

# Kraken pair names (also accepts short form like "XBTUSD")
_PAIRS = {"BTC": "XBTUSD", "ETH": "ETHUSD"}
# Kraken returns results under the full pair name
_RESULT_KEYS = {"BTC": "XXBTZUSD", "ETH": "XETHZUSD"}


class BinancePriceClient:
    """
    Price client backed by Kraken public API.
    Named BinancePriceClient for drop-in compatibility — same interface.
    """

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "betbot/1.0", "Accept": "application/json"})
        self._rate_limit: float = env_float("BINANCE_RATE_LIMIT_SECONDS", 1.0)
        self._last_request_at: float = 0.0
        self._price_cache: dict[str, tuple[float, datetime]] = {}
        self._stats_cache: dict[str, tuple[dict, datetime]] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def get_spot_price(self, asset: str) -> float:
        cached, ts = self._price_cache.get(asset, (None, None))
        if cached is not None and ts is not None:
            if (datetime.now(timezone.utc) - ts).total_seconds() < 30:
                return cached

        ticker = self._get_ticker(asset)
        price = float(ticker["c"][0])  # last trade price
        self._price_cache[asset] = (price, datetime.now(timezone.utc))
        return price

    def get_price_stats(self, asset: str) -> dict:
        cached, ts = self._stats_cache.get(asset, (None, None))
        if cached is not None and ts is not None:
            if (datetime.now(timezone.utc) - ts).total_seconds() < 60:
                return cached

        ticker = self._get_ticker(asset)
        last = float(ticker["c"][0])
        open_24h = float(ticker["o"])
        pct_change = ((last - open_24h) / open_24h * 100) if open_24h else 0.0

        stats = {
            "priceChangePercent": str(round(pct_change, 4)),
            "highPrice": ticker["h"][1],   # 24h high
            "lowPrice":  ticker["l"][1],   # 24h low
            "lastPrice": ticker["c"][0],
        }
        self._stats_cache[asset] = (stats, datetime.now(timezone.utc))
        return stats

    def get_order_book_imbalance(self, asset: str, depth: int = 10) -> float:
        pair = self._pair(asset)
        result_key = _RESULT_KEYS.get(asset.upper(), pair)
        try:
            self._throttle()
            resp = self._session.get(
                f"{_BASE}/Depth",
                params={"pair": pair, "count": depth},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("error"):
                raise RuntimeError(data["error"])
            book = data["result"].get(result_key) or next(iter(data["result"].values()))
            sum_bids = sum(float(b[1]) for b in book.get("bids", []))
            sum_asks = sum(float(a[1]) for a in book.get("asks", []))
            total = sum_bids + sum_asks
            if total == 0:
                return 0.0
            return (sum_bids - sum_asks) / total
        except Exception as exc:
            logger.warning(f"[KRAKEN] order book imbalance failed for {asset}: {exc}")
            return 0.0

    # ── Private ───────────────────────────────────────────────────────────────

    def _get_ticker(self, asset: str) -> dict:
        pair = self._pair(asset)
        result_key = _RESULT_KEYS.get(asset.upper(), pair)
        self._throttle()
        resp = self._session.get(f"{_BASE}/Ticker", params={"pair": pair}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Kraken error: {data['error']}")
        return data["result"].get(result_key) or next(iter(data["result"].values()))

    def _pair(self, asset: str) -> str:
        sym = _PAIRS.get(asset.upper())
        if sym is None:
            raise ValueError(f"Unsupported asset: {asset}")
        return sym

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._rate_limit:
            time.sleep(self._rate_limit - elapsed)
        self._last_request_at = time.monotonic()
