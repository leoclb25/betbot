"""
Portfolio tracker.

Builds a PortfolioState from the paper or live client's position data.
In paper mode, reads from PaperClient's in-memory state.
In live mode, fetches from Polymarket API.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from core.models import BotMode, PortfolioState, PositionStatus

if TYPE_CHECKING:
    from core.polymarket.paper_client import PaperClient
    from core.polymarket.client import PolymarketClient


class PortfolioTracker:
    """Computes portfolio snapshots from client state."""

    def __init__(self, client: "PaperClient | PolymarketClient") -> None:
        self._client = client
        self._mode = client.mode
        self._trade_count = 0
        self._winning_trades = 0
        self._losing_trades = 0
        self._realized_pnl = 0.0
        self._daily_pnl = 0.0
        self._daily_reset_date = datetime.utcnow().date()

    def get_state(self) -> PortfolioState:
        """Build a full portfolio snapshot."""
        self._refresh_trade_stats()

        cash = self._client.get_balance()
        positions = self._client.get_positions()

        open_positions = [p for p in positions if p.status == PositionStatus.OPEN]
        open_value = sum(p.shares * p.entry_price for p in open_positions)
        total_value = cash + open_value

        unrealized_pnl = sum(
            (p.shares * p.entry_price) - p.entry_amount_usd
            for p in open_positions
        )

        # Peak and drawdown
        peak = self._get_peak(total_value)
        drawdown = (peak - total_value) / peak if peak > 0 else 0.0

        return PortfolioState(
            timestamp=datetime.utcnow(),
            mode=self._mode,
            cash_usd=cash,
            open_positions_value_usd=open_value,
            total_value_usd=total_value,
            realized_pnl_usd=self._realized_pnl,
            unrealized_pnl_usd=unrealized_pnl,
            total_pnl_usd=self._realized_pnl + unrealized_pnl,
            peak_value_usd=peak,
            drawdown_pct=drawdown,
            open_position_count=len(open_positions),
            total_trades=self._trade_count,
            winning_trades=self._winning_trades,
            losing_trades=self._losing_trades,
        )

    def _get_peak(self, total_value: float) -> float:
        if self._mode == BotMode.PAPER:
            self._client.update_peak(total_value)  # type: ignore[attr-defined]
            return self._client.get_peak_value()    # type: ignore[attr-defined]
        return total_value  # live: simplified

    def _refresh_trade_stats(self) -> None:
        """Recompute trade stats from all closed positions."""
        positions = self._client.get_positions()
        closed = [p for p in positions if p.status == PositionStatus.CLOSED]

        self._trade_count = len(closed)
        self._winning_trades = sum(1 for p in closed if (p.pnl_usd or 0) > 0)
        self._losing_trades = sum(1 for p in closed if (p.pnl_usd or 0) < 0)
        self._realized_pnl = sum(p.pnl_usd or 0 for p in closed)

        # Reset daily PnL if new day
        today = datetime.utcnow().date()
        if today != self._daily_reset_date:
            self._daily_pnl = 0.0
            self._daily_reset_date = today

    def record_close(self, pnl_usd: float) -> None:
        """Call after closing a position to update daily P&L."""
        self._daily_pnl += pnl_usd

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl
