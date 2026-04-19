"""Crypto strategy: signal-based for Up/Down markets, lognormal for price-level markets."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from bots.crypto.parser import CryptoMarketParser
from core.crypto.price_client import BinancePriceClient
from core.env_utils import env_float
from core.models import (
    BotSignal, CryptoMarketInfo, CryptoPriceDirection, Market,
    PortfolioState, SignalAction, Side,
)
from core.risk.manager import RiskManager

_DEFAULT_VOLATILITY = {"BTC": 0.0006, "ETH": 0.0008}


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _skip(condition_id: str, question: str, reason: str) -> BotSignal:
    return BotSignal(action=SignalAction.SKIP, condition_id=condition_id, question=question, reason=reason)


class CryptoStrategy:
    def __init__(
        self,
        price_client: BinancePriceClient,
        parser: CryptoMarketParser,
        risk_manager: RiskManager,
    ) -> None:
        self._price = price_client
        self._parser = parser
        self._risk = risk_manager

    def evaluate_market(self, market: Market, portfolio: PortfolioState) -> BotSignal:
        info = self._parser.parse(
            market.condition_id, market.question,
            reference_datetime=datetime.now(timezone.utc),
        )
        if info is None:
            return _skip(market.condition_id, market.question, "could not parse market question")

        try:
            true_prob = self._calculate_true_prob(info)
        except RuntimeError as exc:
            return _skip(market.condition_id, market.question, f"Binance fetch failed: {exc}")

        if true_prob is None:
            return _skip(market.condition_id, market.question, "too close to resolution")

        # For UP/DOWN markets: YES=up, NO=down. true_prob = P(up), so compare against yes_price.
        # For price-level markets: same logic — true_prob = P(above/below), side determined by edge.
        market_price = market.yes_price
        # Hold to resolution = only 1 fee (entry). No exit fee on binary resolution.
        edge, side = self._risk.calculate_edge(true_prob, market_price, is_hold_strategy=True)

        price_for_sizing = market.yes_price if side == Side.YES else market.no_price
        position_usd, kelly_used = self._risk.calculate_position_size(
            portfolio_value=portfolio.total_value_usd,
            true_prob=true_prob,
            price=price_for_sizing,
            open_positions_value=portfolio.open_positions_value_usd,
        )

        signal = BotSignal(
            action=SignalAction.ENTER,
            condition_id=market.condition_id,
            question=market.question,
            side=side,
            market_price=market_price,
            true_probability=true_prob,
            edge=edge,
            kelly_fraction=kelly_used,
            position_size_usd=position_usd,
            reason=f"p={true_prob:.2%} edge={edge:.2%} T={info.minutes_to_resolution:.0f}min",
        )
        if edge < self._risk.params.min_edge:
            return signal.model_copy(update={
                "action": SignalAction.SKIP,
                "reason": f"edge {edge:.1%} < min {self._risk.params.min_edge:.1%}",
            })
        return signal

    def evaluate_exit(
        self,
        position,
        market: Market,
    ) -> tuple[bool, str, Optional[float]]:
        info = self._parser.parse(
            market.condition_id, market.question,
            reference_datetime=datetime.now(timezone.utc),
        )
        new_true_prob: Optional[float] = None
        if info is not None:
            try:
                new_true_prob = self._calculate_true_prob(info)
            except Exception:
                pass

        should_exit, reason = self._risk.check_exit_signal(position, market, new_true_prob)
        return should_exit, reason, new_true_prob

    # ── Internal ──────────────────────────────────────────────────────────────

    def _calculate_true_prob(self, info: CryptoMarketInfo) -> Optional[float]:
        T = info.minutes_to_resolution
        if T < 1.0:
            return None

        is_up_or_down = info.direction in (CryptoPriceDirection.UP, CryptoPriceDirection.DOWN)

        if is_up_or_down:
            return self._prob_up_or_down(info, T)
        else:
            return self._prob_price_level(info, T)

    def _prob_up_or_down(self, info: CryptoMarketInfo, T: float) -> Optional[float]:
        """
        Signal-based model for directional (Up/Down) markets.

        Key insight: order book imbalance and trade flow are highly predictive
        at short horizons (T < 10min) but decay quickly at longer ones.
        time_factor scales signal strength by proximity to resolution.
        """
        spot = self._price.get_spot_price(info.asset)
        if spot <= 0:
            return None

        mom_3m     = self._price.get_short_momentum(info.asset, minutes=3)
        imbalance  = self._price.get_order_book_imbalance(info.asset)
        trade_flow = self._price.get_trade_flow(info.asset, count=30)

        is_up = info.direction == CryptoPriceDirection.UP

        # Time factor: signals are most predictive at short T.
        # At T=5min → 1.0, at T=15min → 1.0, at T=30min → 0.5, at T=60min → 0.25
        time_factor = min(1.0, 15.0 / max(T, 1.0))

        # 3-minute momentum: up to ±7% (not time-discounted — it's already short-term)
        mom_adj = max(-0.07, min(0.07, mom_3m / 100 * 14))

        # Order book imbalance: up to ±10%, scaled by time_factor
        imb_adj = max(-0.10, min(0.10, imbalance * 0.10)) * time_factor

        # Trade flow: up to ±6%, scaled by time_factor
        flow_adj = max(-0.06, min(0.06, trade_flow * 0.15)) * time_factor

        # Adjust sign for direction
        if not is_up:
            mom_adj  = -mom_adj
            imb_adj  = -imb_adj
            flow_adj = -flow_adj

        # Agreement bonus: book and flow agree → extra 2% (time-scaled)
        agree = 0.02 * time_factor if (imb_adj * flow_adj > 0) else 0.0

        total_adj = mom_adj + imb_adj + flow_adj + agree
        true_prob = max(0.01, min(0.99, 0.5 + total_adj))

        logger.debug(
            f"[CRYPTO] {info.asset} {info.direction.value} T={T:.0f}min tf={time_factor:.2f} "
            f"spot={spot:.2f} mom3m={mom_3m:+.3f}% imb={imbalance:+.3f} flow={trade_flow:+.3f} "
            f"adjs=[{mom_adj:+.3f},{imb_adj:+.3f},{flow_adj:+.3f}] agree={agree:.2f} → p={true_prob:.3f}"
        )
        return true_prob

    def _prob_price_level(self, info: CryptoMarketInfo, T: float) -> Optional[float]:
        """Lognormal model for price-level (above/below $X) markets."""
        spot = self._price.get_spot_price(info.asset)
        stats = self._price.get_price_stats(info.asset)
        imbalance = self._price.get_order_book_imbalance(info.asset)

        K = info.threshold_usd
        if K is None or K <= 0 or spot <= 0:
            return None

        pct_24h = float(stats.get("priceChangePercent", 0.0))
        default_vol = _DEFAULT_VOLATILITY.get(info.asset, 0.0007)
        sigma = env_float(f"{info.asset}_VOLATILITY_PER_MIN", default_vol)

        d2 = (math.log(spot / K) + (-sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
        is_above = info.direction == CryptoPriceDirection.ABOVE
        raw_prob = _norm_cdf(d2) if is_above else 1.0 - _norm_cdf(d2)

        price_rising = pct_24h > 0
        mom_adj = min(0.02, abs(pct_24h) / 100 * 0.4) * (1 if (is_above == price_rising) else -1)
        imb_adj = max(-0.03, min(0.03, imbalance * 0.03 * (1 if is_above else -1)))

        confidence = max(0.35, 1.0 - 0.04 * max(0, 10 - T))
        true_prob = max(0.01, min(0.99, 0.5 + (raw_prob + mom_adj + imb_adj - 0.5) * confidence))

        logger.debug(
            f"[CRYPTO] {info.asset} {info.direction.value} T={T:.0f}min "
            f"spot={spot:.2f} K={K:.2f} raw={raw_prob:.3f} "
            f"mom={mom_adj:+.3f} imb={imb_adj:+.3f} conf={confidence:.2f} → p={true_prob:.3f}"
        )
        return true_prob
