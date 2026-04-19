"""Crypto price prediction strategy using lognormal model + Binance data."""

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

        market_price = market.yes_price if info.direction == CryptoPriceDirection.ABOVE else market.no_price
        edge, side = self._risk.calculate_edge(true_prob, market_price, is_hold_strategy=False)

        if edge < self._risk.params.min_edge:
            return _skip(
                market.condition_id, market.question,
                f"edge {edge:.1%} < min {self._risk.params.min_edge:.1%}",
            )

        price_for_sizing = market.yes_price if side == Side.YES else market.no_price
        position_usd, kelly_used = self._risk.calculate_position_size(
            portfolio_value=portfolio.total_value_usd,
            true_prob=true_prob,
            price=price_for_sizing,
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
            kelly_fraction=kelly_used,
            position_size_usd=position_usd,
            reason=f"lognormal p={true_prob:.2%} edge={edge:.2%} T={info.minutes_to_resolution:.0f}min",
        )

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

        spot = self._price.get_spot_price(info.asset)
        stats = self._price.get_price_stats(info.asset)
        imbalance = self._price.get_order_book_imbalance(info.asset)

        pct_24h = float(stats.get("priceChangePercent", 0.0))

        default_vol = _DEFAULT_VOLATILITY.get(info.asset, 0.0007)
        sigma = env_float(f"{info.asset}_VOLATILITY_PER_MIN", default_vol)

        S, K = spot, info.threshold_usd
        if S <= 0 or K <= 0 or sigma <= 0:
            return None

        d2 = (math.log(S / K) + (-sigma ** 2 / 2) * T) / (sigma * math.sqrt(T))
        raw_prob = _norm_cdf(d2) if info.direction == CryptoPriceDirection.ABOVE else 1.0 - _norm_cdf(d2)

        # Momentum adjustment (max ±2%)
        price_rising = pct_24h > 0
        if info.direction == CryptoPriceDirection.ABOVE:
            mom_adj = min(0.02, abs(pct_24h) / 100 * 0.4) * (1 if price_rising else -1)
        else:
            mom_adj = min(0.02, abs(pct_24h) / 100 * 0.4) * (-1 if price_rising else 1)

        # Imbalance adjustment (max ±3%)
        if info.direction == CryptoPriceDirection.ABOVE:
            imb_adj = max(-0.03, min(0.03, imbalance * 0.03))
        else:
            imb_adj = max(-0.03, min(0.03, -imbalance * 0.03))

        # Confidence shrinkage (pulls toward 0.5 when T < 10 min)
        confidence = max(0.35, 1.0 - 0.04 * max(0, 10 - T))
        true_prob = max(0.01, min(0.99, 0.5 + (raw_prob + mom_adj + imb_adj - 0.5) * confidence))

        logger.debug(
            f"[CRYPTO] {info.asset} T={T:.0f}min spot={S:.2f} K={K:.2f} "
            f"raw={raw_prob:.3f} mom={mom_adj:+.3f} imb={imb_adj:+.3f} "
            f"conf={confidence:.2f} → p={true_prob:.3f}"
        )
        return true_prob
