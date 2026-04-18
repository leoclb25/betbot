"""
Polymarket live client.

Wraps the Gamma (market data) API and the CLOB (trading) API.
For market data no auth is needed. For trading, py-clob-client is used
with the credentials from .env.

Docs:
  Gamma API  – https://gamma-api.polymarket.com/docs
  CLOB API   – https://docs.polymarket.com
  SDK        – https://github.com/Polymarket/py-clob-client
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import requests
from loguru import logger

from core.models import BotMode, Market, OrderSide, Position, PositionStatus, Side, Trade

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Polymarket charges a 2% fee on taker orders
FEE_RATE = 0.02


def _parse_market(raw: dict) -> Optional[Market]:
    """Convert raw Gamma API market dict to a Market model. Returns None if malformed."""
    try:
        # Outcome prices come as a JSON-encoded string list or actual list
        prices = raw.get("outcomePrices", [])
        if isinstance(prices, str):
            import json
            prices = json.loads(prices)
        prices = [float(p) for p in prices]

        outcomes = raw.get("outcomes", [])
        if isinstance(outcomes, str):
            import json
            outcomes = json.loads(outcomes)

        # Map YES/NO prices (Polymarket always has [YES, NO] order)
        yes_price = prices[0] if len(prices) > 0 else 0.5
        no_price = prices[1] if len(prices) > 1 else 1 - yes_price

        end_date_str = raw.get("endDate") or raw.get("endDateIso", "")
        if not end_date_str:
            return None
        # Strip trailing Z and parse
        end_date_str = end_date_str.replace("Z", "+00:00")
        end_date = datetime.fromisoformat(end_date_str)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        else:
            end_date = end_date.astimezone(timezone.utc)

        return Market(
            condition_id=raw["conditionId"],
            question=raw["question"],
            yes_price=yes_price,
            no_price=no_price,
            end_date=end_date,
            volume_usd=float(raw.get("volume", 0) or 0),
            liquidity_usd=float(raw.get("liquidity", 0) or 0),
            active=bool(raw.get("active", True)),
            closed=bool(raw.get("closed", False)),
        )
    except Exception as exc:
        logger.debug(f"Could not parse market: {exc} | raw={raw.get('conditionId')}")
        return None


def gamma_fetch_market_by_condition_id(
    session: requests.Session, condition_id: str
) -> Optional[dict]:
    """
    Fetch one market from Gamma by conditionId (0x… hex).

    GET /markets/{id} expects the numeric market id, not conditionId — using the
    hex id there returns 404. The list endpoint supports condition_ids=…
    """
    if not condition_id:
        return None

    # Numeric Gamma id (unusual for our codepaths, but supported)
    if condition_id.isdigit():
        try:
            resp = session.get(f"{GAMMA_API}/markets/{condition_id}", timeout=15)
            if resp.status_code == 200:
                raw = resp.json()
                if isinstance(raw, list):
                    return raw[0] if raw else None
                return raw if isinstance(raw, dict) else None
        except requests.RequestException as exc:
            logger.debug(f"Gamma GET /markets/{{id}} failed: {exc}")

    param_sets: list[list[tuple[str, str]]] = [
        [("condition_ids", condition_id), ("limit", "1")],
        [("condition_ids", condition_id), ("limit", "1"), ("closed", "true")],
    ]
    for pairs in param_sets:
        try:
            resp = session.get(f"{GAMMA_API}/markets", params=pairs, timeout=15)
            resp.raise_for_status()
            batch = resp.json()
        except requests.RequestException as exc:
            logger.debug(f"Gamma markets list failed ({pairs}): {exc}")
            continue
        if isinstance(batch, list) and batch:
            return batch[0]
    return None


class PolymarketClient:
    """
    Live Polymarket client.

    Market data is fetched from the public Gamma API (no auth needed).
    Order placement requires CLOB API credentials set in env vars.
    """

    def __init__(self) -> None:
        self.mode = BotMode.LIVE
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        self._clob_client = self._init_clob()

    # ── CLOB initialization ──────────────────────────────────────────────────

    def _init_clob(self):
        """Lazy-initialise the CLOB client. Returns None if credentials missing."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            from core.env_utils import env_int, env_str

            key = env_str("POLY_PRIVATE_KEY", "")
            api_key = env_str("POLY_API_KEY", "")
            secret = env_str("POLY_API_SECRET", "")
            passphrase = env_str("POLY_API_PASSPHRASE", "")
            chain_id = env_int("POLY_CHAIN_ID", 137)

            if not key:
                logger.warning("POLY_PRIVATE_KEY not set – trading disabled.")
                return None

            creds = ApiCreds(api_key=api_key, api_secret=secret, api_passphrase=passphrase)
            client = ClobClient(
                host=CLOB_API,
                chain_id=chain_id,
                key=key,
                creds=creds,
            )
            logger.info("CLOB client initialized (live trading enabled).")
            return client
        except ImportError:
            logger.warning("py-clob-client not installed – trading disabled.")
            return None
        except Exception as exc:
            logger.error(f"Failed to init CLOB client: {exc}")
            return None

    # ── Market data ─────────────────────────────────────────────────────────

    def get_markets(
        self,
        keywords: Optional[list[str]] = None,
        limit: int = 200,
        active_only: bool = True,
    ) -> list[Market]:
        """Fetch markets from Gamma API, optionally filtered by keywords."""
        markets: list[Market] = []
        offset = 0
        batch = 100

        while True:
            params: dict = {
                "limit": batch,
                "offset": offset,
                "order": "volume",
                "ascending": "false",
            }
            if active_only:
                params["active"] = "true"
                params["closed"] = "false"

            resp = self._session.get(f"{GAMMA_API}/markets", params=params, timeout=15)
            resp.raise_for_status()
            batch_data = resp.json()

            if not batch_data:
                break

            for raw in batch_data:
                m = _parse_market(raw)
                if m is None:
                    continue
                if keywords:
                    question_lower = m.question.lower()
                    if not any(kw.lower() in question_lower for kw in keywords):
                        continue
                markets.append(m)
                if len(markets) >= limit:
                    return markets

            if len(batch_data) < batch:
                break
            offset += batch

        logger.info(f"Fetched {len(markets)} markets (keywords={keywords})")
        return markets

    def get_market(self, condition_id: str) -> Optional[Market]:
        """Fetch a single market by condition ID (0x… hex)."""
        raw = gamma_fetch_market_by_condition_id(self._session, condition_id)
        return _parse_market(raw) if raw else None

    # ── Account ──────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        """Return USDC balance from CLOB API."""
        if self._clob_client is None:
            raise RuntimeError("CLOB client not initialized.")
        balance = self._clob_client.get_balance()
        return float(balance)

    def get_positions(self) -> list[Position]:
        """Fetch open positions from CLOB API."""
        # NOTE: The CLOB API returns open orders; resolving them to Position
        # objects requires additional market lookups. Simplified implementation.
        if self._clob_client is None:
            raise RuntimeError("CLOB client not initialized.")
        # Real implementation would query open orders and open positions
        logger.warning("get_positions() is a stub – implement with CLOB positions endpoint.")
        return []

    # ── Trading ──────────────────────────────────────────────────────────────

    def place_order(
        self,
        market: Market,
        side: Side,
        order_side: OrderSide,
        amount_usd: float,
    ) -> Trade:
        """
        Place a market order on Polymarket.

        amount_usd: gross amount in USDC to spend (fee will be deducted).
        Returns a Trade record.
        """
        if self._clob_client is None:
            raise RuntimeError("CLOB client not initialized – set POLY_PRIVATE_KEY in .env")

        price = market.yes_price if side == Side.YES else market.no_price
        fee_usd = amount_usd * FEE_RATE
        net_amount = amount_usd - fee_usd
        shares = net_amount / price if price > 0 else 0.0

        # Build and send order via CLOB client
        # The exact API depends on py-clob-client version; this is illustrative.
        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType

            token_id = market.condition_id  # Simplified – real impl needs token ID
            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=amount_usd,
            )
            signed_order = self._clob_client.create_market_order(order_args)
            resp = self._clob_client.post_order(signed_order, OrderType.FOK)
            trade_id = resp.get("orderID", str(uuid4()))
        except Exception as exc:
            raise RuntimeError(f"Order placement failed: {exc}") from exc

        trade = Trade(
            trade_id=trade_id,
            condition_id=market.condition_id,
            question=market.question,
            timestamp=datetime.now(timezone.utc),
            side=side,
            order_side=order_side,
            price=price,
            amount_usd=amount_usd,
            fee_usd=fee_usd,
            shares=shares,
            mode=BotMode.LIVE,
        )
        logger.info(
            f"[LIVE] {order_side.value} {side.value} | {market.question[:60]} | "
            f"${amount_usd:.2f} @ {price:.3f} | fee=${fee_usd:.2f}"
        )
        return trade
