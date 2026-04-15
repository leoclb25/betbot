"""
WeatherBot – main orchestrator for weather prediction market trading.

Wires together:
  - Market scanning (Gamma API, keyword filter)
  - Strategy evaluation (parser + ensemble weather + Kelly)
  - Position management (open/close via paper or live client)
  - Risk gating (RiskManager)
  - Logging (OperationsLogger)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Union

from loguru import logger

from bots.base import BaseBot
from bots.weather.strategy import WeatherStrategy
from core.models import BotMode, BotSignal, Market, OrderSide, PortfolioState, Position, SignalAction
from core.portfolio.logger import OperationsLogger
from core.portfolio.tracker import PortfolioTracker
from core.polymarket.client import PolymarketClient
from core.polymarket.paper_client import PaperClient
from core.risk.manager import RiskManager, load_risk_params
from core.weather.client import WeatherClient
from bots.weather.parser import WeatherMarketParser

# Default keywords to filter weather markets
def _synthetic_market_for_orphan_position(position: Position) -> Market:
    """When Gamma no longer returns the market, close paper at 50¢ (neutral settlement)."""
    return Market(
        condition_id=position.condition_id,
        question=position.question,
        yes_price=0.5,
        no_price=0.5,
        end_date=datetime.now(timezone.utc),
        volume_usd=0.0,
        liquidity_usd=0.0,
        active=False,
        closed=True,
    )


WEATHER_KEYWORDS = [
    "rain", "rainfall", "precipitation", "snow", "snowfall",
    "temperature", "degrees", "hurricane", "storm", "tornado",
    "flood", "drought", "heat", "cold", "frost", "wind",
    "sunny", "cloudy", "weather", "celsius", "fahrenheit",
]


class WeatherBot(BaseBot):
    """
    Polymarket bot focused on weather prediction markets.

    Supports both paper trading (simulation) and live trading.
    """

    name = "weather"

    def __init__(
        self,
        client: Union[PolymarketClient, PaperClient],
        strategy: WeatherStrategy,
        risk_manager: RiskManager,
        tracker: PortfolioTracker,
        ops_logger: OperationsLogger,
        scan_interval_seconds: int = 3600,
        keywords: list[str] | None = None,
    ) -> None:
        super().__init__(client, risk_manager, tracker, ops_logger, scan_interval_seconds)
        self._strategy = strategy
        self._keywords = keywords or WEATHER_KEYWORDS
        # Cache of condition_id → position_id to avoid duplicate entries
        self._open_condition_ids: set[str] = self._load_open_condition_ids()

    # ── Factory ──────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, mode: BotMode) -> "WeatherBot":
        """
        Convenience factory. Reads all config from environment / defaults.
        """
        from dotenv import load_dotenv
        load_dotenv()

        # Client
        if mode == BotMode.PAPER:
            client: Union[PolymarketClient, PaperClient] = PaperClient()
        else:
            client = PolymarketClient()

        # Components
        risk_manager = RiskManager(load_risk_params())
        weather_client = WeatherClient()
        parser = WeatherMarketParser(weather_client)
        strategy = WeatherStrategy(weather_client, parser, risk_manager)
        tracker = PortfolioTracker(client)
        ops_logger = OperationsLogger(mode)

        scan_interval = int(os.getenv("SCAN_INTERVAL_SECONDS", "3600"))

        return cls(
            client=client,
            strategy=strategy,
            risk_manager=risk_manager,
            tracker=tracker,
            ops_logger=ops_logger,
            scan_interval_seconds=scan_interval,
        )

    # ── BaseBot interface ─────────────────────────────────────────────────────

    def scan_markets(self) -> list[Market]:
        """Fetch active weather markets from Polymarket."""
        max_days = int(os.getenv("MAX_DAYS_TO_RESOLUTION", "7"))
        min_days = int(os.getenv("MIN_DAYS_TO_RESOLUTION", "0"))

        all_markets = self.client.get_markets(keywords=self._keywords, limit=200)

        # Filter by resolution window and liquidity
        filtered = [
            m for m in all_markets
            if min_days <= m.days_to_resolution <= max_days
            and m.liquidity_usd >= self.risk.params.min_liquidity_usd
            and m.condition_id not in self._open_condition_ids
        ]

        logger.debug(
            f"[WEATHER] {len(all_markets)} raw → {len(filtered)} after filters "
            f"(resolution {min_days}-{max_days} days, liq≥${self.risk.params.min_liquidity_usd:.0f})"
        )
        return filtered

    def evaluate_market(self, market: Market) -> BotSignal:
        """Evaluate one market using the weather strategy."""
        state = self.tracker.get_state()
        return self._strategy.evaluate_market(market, state)

    def manage_open_positions(self) -> None:
        """Check all open positions for exit signals."""
        positions = self.client.get_positions()
        open_positions = [p for p in positions if p.status.value == "OPEN"]

        if not open_positions:
            return

        for position in open_positions:
            market = self.client.get_market(position.condition_id)
            if market is None:
                if isinstance(self.client, PaperClient):
                    logger.warning(
                        f"[WEATHER] Market {position.condition_id[:16]}… not on Gamma API — "
                        f"closing paper position at 50¢ (resolved/delisted)"
                    )
                    synthetic = _synthetic_market_for_orphan_position(position)
                    self._close_position(position, synthetic, "market not found on Gamma API (closed at 50¢)")
                else:
                    logger.warning(f"[WEATHER] Market {position.condition_id[:16]}… not found – skipping")
                continue

            # Market resolved → always close at final price
            if market.closed or market.days_to_resolution <= 0:
                self._close_position(position, market, "market resolved")
                continue

            # Check early exit (stop-loss / take-profit / thesis flip)
            current_price = market.yes_price if position.side.value == "YES" else market.no_price
            current_value = position.shares * current_price
            pnl = current_value - position.entry_amount_usd
            pnl_sign = "+" if pnl >= 0 else ""
            logger.debug(
                f"[WEATHER] CHECK {position.side.value} '{market.question[:50]}' | "
                f"price={current_price:.3f} (entry={position.entry_price:.3f}) | "
                f"P&L={pnl_sign}${pnl:.2f} ({pnl_sign}{pnl/position.entry_amount_usd*100:.1f}%)"
            )

            should_exit, reason, _ = self._strategy.evaluate_exit(position, market)
            if should_exit:
                self._close_position(position, market, reason)

    # ── Entry / Exit execution ───────────────────────────────────────────────

    def _execute_entry(self, signal: BotSignal, market=None) -> None:
        """Open a new position based on an ENTER signal."""
        # market is passed in from _act_on_signal to avoid a redundant API call
        if market is None:
            logger.error(f"Market {signal.condition_id} not available for entry")
            return

        if isinstance(self.client, PaperClient):
            try:
                trade, position = self.client.open_position(
                    market=market,
                    side=signal.side,
                    amount_usd=signal.position_size_usd,
                    true_prob=signal.true_probability,
                    edge=signal.edge,
                )
                self._open_condition_ids.add(market.condition_id)

                self.logger.log_trade(trade, notes=signal.reason)
                self.logger.log_position_open(
                    condition_id=market.condition_id,
                    question=market.question,
                    side=signal.side.value,
                    entry_price=trade.price,
                    amount_usd=trade.amount_usd,
                    true_prob=signal.true_probability,
                    edge=signal.edge,
                )
                logger.info(
                    f"[PAPER] OPENED {signal.side.value} on '{market.question[:60]}' | "
                    f"${signal.position_size_usd:.2f} | edge={signal.edge:.1%}"
                )
            except ValueError as exc:
                logger.warning(f"Could not open position: {exc}")
        else:
            # Live mode
            trade = self.client.place_order(
                market=market,
                side=signal.side,
                order_side=OrderSide.BUY,
                amount_usd=signal.position_size_usd,
            )
            self._open_condition_ids.add(market.condition_id)
            self.logger.log_trade(trade, notes=signal.reason)

    def _close_position(self, position, market: Market, reason: str) -> None:
        """Close an existing position."""
        if isinstance(self.client, PaperClient):
            try:
                trade, closed = self.client.close_position(
                    position=position,
                    market=market,
                    reason=reason,
                )
                self._open_condition_ids.discard(market.condition_id)
                self.tracker.record_close(closed.pnl_usd or 0.0)

                pnl = closed.pnl_usd or 0.0
                pnl_pct = closed.pnl_pct or 0.0
                pnl_sign = "+" if pnl >= 0 else ""
                pnl_color = "WIN" if pnl >= 0 else "LOSS"
                logger.info(
                    f"[PAPER] ═══ {pnl_color} ═══ CLOSED {position.side.value} | "
                    f"'{market.question[:55]}' | "
                    f"P&L={pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct*100:.1f}%) | "
                    f"reason={reason}"
                )

                self.logger.log_trade(trade, notes=reason)
                self.logger.log_position_close(
                    condition_id=market.condition_id,
                    question=market.question,
                    side=position.side.value,
                    exit_price=closed.exit_price or 0.0,
                    pnl_usd=closed.pnl_usd or 0.0,
                    pnl_pct=closed.pnl_pct or 0.0,
                    reason=reason,
                )
            except Exception as exc:
                logger.error(f"[PAPER] Error closing position {position.position_id}: {exc}")
        else:
            # Live: place SELL order
            trade = self.client.place_order(
                market=market,
                side=position.side,
                order_side=OrderSide.SELL,
                amount_usd=position.shares * (
                    market.yes_price if position.side.value == "YES" else market.no_price
                ),
            )
            self._open_condition_ids.discard(market.condition_id)
            self.logger.log_trade(trade, notes=reason)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _load_open_condition_ids(self) -> set[str]:
        """Populate open condition IDs from persisted positions."""
        try:
            positions = self.client.get_positions()
            return {p.condition_id for p in positions if p.status.value == "OPEN"}
        except Exception:
            return set()
