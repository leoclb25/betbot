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

from datetime import date, timezone
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
    WeatherCondition,
    WeatherMarketInfo,
)
from core.risk.manager import RiskManager
from core.weather.climatology import ClimatologyClient
from core.weather.client import WeatherClient


def _market_reference_date(market: Market) -> date:
    """Calendar date of market resolution (UTC) for parsing month/day without year."""
    ed = market.end_date
    if ed.tzinfo is not None:
        return ed.astimezone(timezone.utc).date()
    return ed.date()


def _effective_range_c(info: WeatherMarketInfo) -> Optional[float]:
    """
    Ancho efectivo de la banda (°C) para mercados de temperatura.
    None si no aplica (rain, above/below sin banda, etc).
    """
    if info.condition == WeatherCondition.TEMPERATURE_EXACT:
        if info.threshold is None:
            return None
        if info.threshold_high is not None:
            return abs(info.threshold_high - info.threshold)
        return 1.0  # ventana ±0.5°C implícita
    return None


class WeatherStrategy:
    """
    Evaluates weather prediction markets and generates entry/exit signals.
    """

    def __init__(
        self,
        weather_client: WeatherClient,
        parser: WeatherMarketParser,
        risk_manager: RiskManager,
        climatology: Optional[ClimatologyClient] = None,
        arbitrage_cids: Optional[set[str]] = None,
    ) -> None:
        self._weather = weather_client
        self._parser = parser
        self._risk = risk_manager
        self._climatology = climatology
        # Condition IDs marcados por el scanner como parte de un grupo arbitrable
        # (overround entre bins mutuamente excluyentes de la misma ciudad+fecha)
        self._arb_cids: set[str] = arbitrage_cids if arbitrage_cids is not None else set()

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
        info = self._parser.parse(
            market.condition_id,
            market.question,
            reference_date=_market_reference_date(market),
        )
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

        # Step 3a: quick-pass con anchor=0.5 para descartar mercados sin potencial.
        # Evita fetchear climatología (costosa, muchos mercados por ciclo) cuando
        # ya es evidente que ni el mejor anchor cambiaría la decisión.
        quick = self._weather.calculate_probability(
            forecast,
            info.condition,
            info.threshold,
            info.threshold_high,
            climatology_prob=None,
        )
        quick_edge, _ = self._risk.calculate_edge(
            true_prob=quick.true_probability,
            market_price=market.yes_price,
            is_hold_strategy=True,
        )

        # Step 3b: Solo fetcheamos climatología si el mercado muestra potencial
        # (edge actual >= mitad del MIN_EDGE por defecto) o si confidence es baja
        # (donde el anchor marca más diferencia).
        climatology_prob: Optional[float] = None
        worth_climatology = (
            quick_edge >= (self._risk.params.min_edge * 0.5)
            or quick.confidence < 0.6
        )
        if self._climatology is not None and worth_climatology:
            try:
                climatology_prob = self._climatology.probability(
                    latitude=info.latitude,
                    longitude=info.longitude,
                    target_date=info.target_date,
                    condition=info.condition,
                    threshold=info.threshold,
                    threshold_high=info.threshold_high,
                )
            except Exception as exc:
                logger.debug(f"[STRATEGY] climatology fetch failed: {exc}")
                climatology_prob = None

        # Step 3c: probabilidad final con blend (si tenemos climatología)
        if climatology_prob is None:
            weather_prob = quick
        else:
            weather_prob = self._weather.calculate_probability(
                forecast,
                info.condition,
                info.threshold,
                info.threshold_high,
                climatology_prob=climatology_prob,
            )
        true_prob = weather_prob.true_probability

        # Step 4: Calculate edge
        edge, side = self._risk.calculate_edge(
            true_prob=true_prob,
            market_price=market.yes_price,
            is_hold_strategy=True,
        )

        market_price = market.yes_price if side == Side.YES else market.no_price

        # MIN_EDGE efectivo según ancho de banda (asimétrico YES/NO)
        range_c = _effective_range_c(info)
        min_edge = self._risk.effective_min_edge(side, range_c)

        # Arbitraje estructural: si este mercado es parte de un grupo con overround,
        # relajamos MIN_EDGE en el lado NO (edge estructural independiente del forecast)
        is_arb = market.condition_id in self._arb_cids
        if is_arb and side == Side.NO:
            min_edge = min(min_edge, 0.02)

        models_str = ",".join(weather_prob.models_used) if weather_prob.models_used else "?"
        clim_str = f"{climatology_prob:.2%}" if climatology_prob is not None else "n/a"
        range_str = f"{range_c:.2f}°C" if range_c is not None else "n/a"
        logger.debug(
            f"[STRATEGY] {market.question[:70]} | "
            f"raw={weather_prob.raw_probability:.2%} → true={true_prob:.2%} | "
            f"climate={clim_str} range={range_str} | "
            f"market={market.yes_price:.2%} edge={edge:.2%} side={side.value} min_edge={min_edge:.1%}"
            f"{' [ARB]' if is_arb else ''} | "
            f"models={models_str} agreement={weather_prob.model_agreement:.2f} "
            f"confidence={weather_prob.confidence:.0%} members={forecast.member_count}"
        )

        if edge < min_edge:
            return BotSignal(
                action=SignalAction.SKIP,
                condition_id=market.condition_id,
                question=market.question,
                side=side,
                market_price=market_price,
                true_probability=true_prob,
                edge=edge,
                reason=(
                    f"edge {edge:.1%} below minimum {min_edge:.1%} "
                    f"(range={range_str}, side={side.value}) | "
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
                f"edge={edge:.1%} true_prob={true_prob:.1%} raw={weather_prob.raw_probability:.1%} "
                f"market={market.yes_price:.1%} kelly={kelly_pct:.1%} "
                f"models={','.join(weather_prob.models_used)} "
                f"agreement={weather_prob.model_agreement:.2f} "
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
        info = self._parser.parse(
            market.condition_id,
            market.question,
            reference_date=_market_reference_date(market),
        )

        if info is not None:
            forecast = self._weather.get_ensemble_forecast(
                latitude=info.latitude,
                longitude=info.longitude,
                target_date=info.target_date,
                location_name=info.location,
            )
            if forecast and forecast.member_count > 0:
                climatology_prob: Optional[float] = None
                if self._climatology is not None:
                    try:
                        climatology_prob = self._climatology.probability(
                            latitude=info.latitude,
                            longitude=info.longitude,
                            target_date=info.target_date,
                            condition=info.condition,
                            threshold=info.threshold,
                            threshold_high=info.threshold_high,
                        )
                    except Exception:
                        climatology_prob = None
                weather_prob = self._weather.calculate_probability(
                    forecast,
                    info.condition,
                    info.threshold,
                    info.threshold_high,
                    climatology_prob=climatology_prob,
                )
                new_true_prob = weather_prob.true_probability

        # Delegate to risk manager
        should_exit, reason = self._risk.check_exit_signal(
            position=position,
            current_market=market,
            new_true_prob=new_true_prob,
        )

        return should_exit, reason, new_true_prob
