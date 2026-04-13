"""
Weather betting strategy.

Given a parsed WeatherMarketInfo and its ensemble forecast, determines:
  1. True probability of the outcome
  2. Edge vs. market price
  3. Whether to enter (and on which side)
  4. Position sizing via fractional Kelly
  5. Exit decisions for open positions

Strategy summary:
  - DEFAULT: hold to resolution (1 fee only)
  - EARLY EXIT if:
      a) Take-profit: captured ≥ TAKE_PROFIT_PCT of theoretical max gain
      b) Stop-loss: position lost ≥ STOP_LOSS_PCT of entry value
      c) Thesis flip: weather model now supports opposite side with edge
"""

from __future__ import annotations

from typing import Optional

from loguru import logger

from bots.weather.parser import WeatherMarketParser
from core.models import (
    BotSignal,
    Market,
    Position,
    PortfolioState,
    Side,
    SignalAction,
    WeatherMarketInfo,
)
from core.risk.manager import RiskManager
from core.weather.client import WeatherClient


class WeatherStrategy:
    """
    Evaluates weather prediction markets and generates entry/exit signals.
    """

    def __init__(
        self,
        weather_client: WeatherClient,
        parser: WeatherMarketParser,
        risk_manager: RiskManager,
    ) -> None:
        self._weather = weather_client
        self._parser = parser
        self._risk = risk_manager

    # ── Entry evaluation ─────────────────────────────────────────────────────

    def evaluate_market(
        self,
        market: Market,
        portfolio: PortfolioState,
    ) -> BotSignal:
        """
        Full evaluation of a market. Returns a BotSignal with action
        ENTER, SKIP, or HOLD.
        """
        # Step 1: Parse the market question
        info = self._parser.parse(market.condition_id, market.question)
        if info is None or info.condition.value == "unknown":
            return BotSignal(
                action=SignalAction.SKIP,
                condition_id=market.condition_id,
                question=market.question,
                reason="could not parse market question or unknown condition",
            )

        # Step 2: Fetch ensemble forecast
        forecast = self._weather.get_ensemble_forecast(
            latitude=info.latitude,
            longitude=info.longitude,
            target_date=info.target_date,
            location_name=info.location,
        )
        if forecast is None or forecast.member_count == 0:
            return BotSignal(
                action=SignalAction.SKIP,
                condition_id=market.condition_id,
                question=market.question,
                reason="could not fetch weather forecast",
            )

        # Step 3: Calculate true probability
        weather_prob = self._weather.calculate_probability(
            forecast, info.condition, info.threshold
        )
        true_prob = weather_prob.true_probability

        # Step 4: Calculate edge
        edge, side = self._risk.calculate_edge(
            true_prob=true_prob,
            market_price=market.yes_price,
            is_hold_strategy=True,  # default: hold to resolution
        )

        market_price = market.yes_price if side == Side.YES else market.no_price

        logger.debug(
            f"[STRATEGY] {market.question[:70]} | "
            f"true_prob={true_prob:.2%} market={market.yes_price:.2%} "
            f"edge={edge:.2%} side={side.value} "
            f"confidence={weather_prob.confidence:.0%} members={forecast.member_count}"
        )

        if edge < self._risk.params.min_edge:
            return BotSignal(
                action=SignalAction.SKIP,
                condition_id=market.condition_id,
                question=market.question,
                side=side,
                market_price=market_price,
                true_probability=true_prob,
                edge=edge,
                reason=(
                    f"edge {edge:.1%} below minimum {self._risk.params.min_edge:.1%} | "
                    f"true_prob={true_prob:.1%} market={market.yes_price:.1%}"
                ),
            )

        # Step 5: Position sizing
        position_usd, kelly_pct = self._risk.calculate_position_size(
            portfolio_value=portfolio.total_value_usd,
            true_prob=true_prob if side == Side.YES else 1 - true_prob,
            price=market_price,
            open_positions_value=portfolio.open_positions_value_usd,
        )

        return BotSignal(
            action=SignalAction.ENTER,
            condition_id=market.condition_id,
            question=market.question,
            side=side,
            market_price=market_price,
            true_probability=true_prob,
            edge=edge,
            kelly_fraction=kelly_pct,
            position_size_usd=position_usd,
            reason=(
                f"edge={edge:.1%} true_prob={true_prob:.1%} "
                f"market={market.yes_price:.1%} kelly={kelly_pct:.1%} "
                f"confidence={weather_prob.confidence:.0%} members={forecast.member_count}"
            ),
        )

    # ── Exit evaluation ──────────────────────────────────────────────────────

    def evaluate_exit(
        self,
        position: Position,
        market: Market,
    ) -> tuple[bool, str, Optional[float]]:
        """
        Check whether an open position should be exited early.

        Returns (should_exit, reason, new_true_prob).
        new_true_prob is the refreshed weather probability (may be None if unavailable).
        """
        # Re-fetch weather forecast to check thesis
        new_true_prob: Optional[float] = None
        info = self._parser.parse(market.condition_id, market.question)

        if info is not None:
            forecast = self._weather.get_ensemble_forecast(
                latitude=info.latitude,
                longitude=info.longitude,
                target_date=info.target_date,
                location_name=info.location,
            )
            if forecast and forecast.member_count > 0:
                weather_prob = self._weather.calculate_probability(
                    forecast, info.condition, info.threshold
                )
                new_true_prob = weather_prob.true_probability

        # Delegate to risk manager
        should_exit, reason = self._risk.check_exit_signal(
            position=position,
            current_market=market,
            new_true_prob=new_true_prob,
        )

        return should_exit, reason, new_true_prob
