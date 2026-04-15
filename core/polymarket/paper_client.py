"""
Paper trading client – same interface as PolymarketClient but all orders
are simulated. Positions and cash are persisted to data/paper_portfolio.json
so state survives restarts.

Market prices are fetched from the real Gamma API (read-only), but no
actual orders are placed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

import requests
from loguru import logger

from core.models import BotMode, Market, OrderSide, Position, PositionStatus, Side, Trade
from core.polymarket.client import FEE_RATE, GAMMA_API, _parse_market, gamma_fetch_market_by_condition_id

STATE_FILE = Path("data/paper_portfolio.json")


def _load_state() -> dict:
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    initial_balance = float(os.getenv("PAPER_INITIAL_BALANCE", "100.0"))
    return {
        "cash_usd": initial_balance,
        "peak_value_usd": initial_balance,
        "positions": {},   # position_id → dict
        "trades": [],
    }


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2, default=str)


class PaperClient:
    """
    Paper trading client.

    Simulates all orders using real market prices from Polymarket's
    public API. No real money is used.
    """

    def __init__(self) -> None:
        self.mode = BotMode.PAPER
        self._state = _load_state()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        logger.info(
            f"[PAPER] Initialized with ${self._state['cash_usd']:.2f} cash | "
            f"{len(self._state['positions'])} open positions"
        )

    # ── Market data (delegates to real API) ──────────────────────────────────

    def get_markets(
        self,
        keywords: Optional[list[str]] = None,
        limit: int = 200,
        active_only: bool = True,
    ) -> list[Market]:
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

        logger.debug(f"[PAPER] Fetched {len(markets)} markets")
        return markets

    def get_market(self, condition_id: str) -> Optional[Market]:
        raw = gamma_fetch_market_by_condition_id(self._session, condition_id)
        return _parse_market(raw) if raw else None

    # ── Account ──────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
        return self._state["cash_usd"]

    def get_positions(self) -> list[Position]:
        positions = []
        for pid, p in self._state["positions"].items():
            positions.append(Position(**p))
        return positions

    def get_peak_value(self) -> float:
        return self._state["peak_value_usd"]

    # ── Trading ──────────────────────────────────────────────────────────────

    def place_order(
        self,
        market: Market,
        side: Side,
        order_side: OrderSide,
        amount_usd: float,
    ) -> Trade:
        """Simulate a market order fill at the current market price."""
        price = market.yes_price if side == Side.YES else market.no_price
        fee_usd = amount_usd * FEE_RATE
        net_amount = amount_usd - fee_usd
        shares = net_amount / price if price > 0 else 0.0

        if order_side == OrderSide.BUY:
            if self._state["cash_usd"] < amount_usd:
                raise ValueError(
                    f"Insufficient paper cash: have ${self._state['cash_usd']:.2f}, "
                    f"need ${amount_usd:.2f}"
                )
            self._state["cash_usd"] -= amount_usd

        trade_id = str(uuid4())
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
            mode=BotMode.PAPER,
        )

        self._state["trades"].append(trade.model_dump())
        _save_state(self._state)

        logger.debug(
            f"[PAPER] {order_side.value} {side.value} | {market.question[:60]} | "
            f"${amount_usd:.2f} @ {price:.3f} | fee=${fee_usd:.2f} | shares={shares:.4f}"
        )
        return trade

    def open_position(
        self,
        market: Market,
        side: Side,
        amount_usd: float,
        true_prob: float,
        edge: float,
    ) -> tuple[Trade, Position]:
        """Convenience: place a BUY order and record the resulting Position."""
        trade = self.place_order(market, side, OrderSide.BUY, amount_usd)
        position_id = str(uuid4())
        position = Position(
            position_id=position_id,
            condition_id=market.condition_id,
            question=market.question,
            side=side,
            status=PositionStatus.OPEN,
            entry_price=trade.price,
            entry_amount_usd=trade.amount_usd,
            entry_fee_usd=trade.fee_usd,
            shares=trade.shares,
            entry_true_prob=true_prob,
            entry_edge=edge,
            opened_at=trade.timestamp,
            market_end_date=market.end_date,
            mode=BotMode.PAPER,
        )
        self._state["positions"][position_id] = position.model_dump()
        _save_state(self._state)
        return trade, position

    def close_position(
        self,
        position: Position,
        market: Market,
        reason: str = "",
    ) -> tuple[Trade, Position]:
        """Simulate selling a position at the current market price."""
        # Sell the YES/NO shares back at current price
        current_price = market.yes_price if position.side == Side.YES else market.no_price
        gross_proceeds = position.shares * current_price
        fee_usd = gross_proceeds * FEE_RATE
        net_proceeds = gross_proceeds - fee_usd

        # Add proceeds back to cash
        self._state["cash_usd"] += net_proceeds

        pnl_usd = net_proceeds - position.entry_amount_usd
        pnl_pct = pnl_usd / position.entry_amount_usd if position.entry_amount_usd else 0.0

        closed = position.model_copy(
            update={
                "status": PositionStatus.CLOSED,
                "exit_price": current_price,
                "exit_fee_usd": fee_usd,
                "closed_at": datetime.now(timezone.utc),
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "exit_reason": reason,
            }
        )

        # Record the SELL trade
        sell_trade = Trade(
            trade_id=str(uuid4()),
            condition_id=market.condition_id,
            question=market.question,
            timestamp=datetime.now(timezone.utc),
            side=position.side,
            order_side=OrderSide.SELL,
            price=current_price,
            amount_usd=gross_proceeds,
            fee_usd=fee_usd,
            shares=position.shares,
            mode=BotMode.PAPER,
            notes=reason,
        )

        self._state["trades"].append(sell_trade.model_dump())
        self._state["positions"][position.position_id] = closed.model_dump()
        _save_state(self._state)

        pnl_sign = "+" if pnl_usd >= 0 else ""
        logger.info(
            f"[PAPER] CLOSE {position.side.value} | {market.question[:60]} | "
            f"P&L={pnl_sign}${pnl_usd:.2f} ({pnl_sign}{pnl_pct*100:.1f}%) | {reason}"
        )
        return sell_trade, closed

    def update_peak(self, total_value: float) -> None:
        if total_value > self._state["peak_value_usd"]:
            self._state["peak_value_usd"] = total_value
            _save_state(self._state)
