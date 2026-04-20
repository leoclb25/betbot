"""
Risk management module.

Implements:
  - Kelly Criterion (fractional) for optimal position sizing
  - Pre-trade risk checks (portfolio limits, exposure caps)
  - In-trade exit signals (stop-loss, take-profit, thesis invalidation)
  - Fee-aware edge calculation

Kelly Criterion for binary markets:
  f* = (p * b - q) / b
  where:
    p = probability of winning
    q = 1 - p
    b = net payout per dollar bet = (1 / price) - 1

We use KELLY_FRACTION * f* (default 25%) to be conservative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

import os

from core.env_utils import env_float as _env_float, env_int as _env_int
from core.models import BotMode, Market, Position, PortfolioState, Side

# ── Fee constants ─────────────────────────────────────────────────────────────
POLYMARKET_FEE_RATE = 0.02   # 2% per trade (taker)
GAS_USD = 0.05               # approximate Polygon gas per tx


@dataclass
class RiskParams:
    min_edge: float              # minimum edge after fees to enter
    kelly_fraction: float        # fraction of full Kelly to use
    max_position_pct: float      # max % of portfolio per position
    max_open_positions: int      # max number of concurrent positions
    max_portfolio_risk: float    # max total % of portfolio exposed
    daily_loss_limit: float      # daily loss % that triggers pause
    drawdown_limit: float        # drawdown % from peak that triggers pause
    stop_loss_pct: float         # exit when position loses this fraction (0 disables)
    take_profit_pct: float       # exit early when profit hits this fraction
    min_position_usd: float      # minimum position size (below this, fees not worth it)
    min_liquidity_usd: float     # minimum market liquidity to trade
    min_market_price: float      # minimum price on either side (filters broken/dead markets)
    max_market_price: float      # maximum price on either side (= 1 - min_market_price)
    max_entries_per_cycle: int   # cap nuevas entradas por ciclo (evita ráfagas en arranque)
    # Asimetría YES/NO en mercados de banda angosta (rangos exactos de temperatura)
    narrow_range_c: float        # banda ≤ X°C se considera "angosta" (ruido ensemble > señal)
    narrow_range_yes_min_edge: float  # YES en banda angosta requiere edge mucho mayor
    narrow_range_no_min_edge: float   # NO en banda angosta: edge mínimo bajo (bet estructural)
    # Exit timing
    min_position_age_hours: float     # no permitir exit por precio antes de esto (deja que resuelva)
    thesis_flip_min_edge: float       # edge requerido en lado contrario para invalidar tesis


def load_risk_params(prefix: str = "") -> RiskParams:
    def _f(base: str, default: float) -> float:
        if prefix:
            key = f"{prefix}_{base}"
            if os.environ.get(key) is not None:
                return _env_float(key, default)
        return _env_float(base, default)

    def _i(base: str, default: int) -> int:
        if prefix:
            key = f"{prefix}_{base}"
            if os.environ.get(key) is not None:
                return _env_int(key, default)
        return _env_int(base, default)

    return RiskParams(
        min_edge=_f("MIN_EDGE", 0.08),
        kelly_fraction=_f("KELLY_FRACTION", 0.25),
        max_position_pct=_f("MAX_POSITION_PCT", 0.05),
        max_open_positions=_i("MAX_OPEN_POSITIONS", 6),
        max_portfolio_risk=_f("MAX_PORTFOLIO_RISK", 0.35),
        daily_loss_limit=_f("DAILY_LOSS_LIMIT", 0.10),
        drawdown_limit=_f("DRAWDOWN_LIMIT", 0.25),
        # Stop-loss default DESHABILITADO (0.0): en mercados que resuelven binariamente
        # cristalizar -40% por ruido de precio es peor que esperar a resolución.
        stop_loss_pct=_f("STOP_LOSS_PCT", 0.0),
        take_profit_pct=_f("TAKE_PROFIT_PCT", 0.40),
        min_position_usd=_f("MIN_POSITION_USD", 5.0),
        min_liquidity_usd=_f("MIN_LIQUIDITY_USD", 1000.0),
        min_market_price=_f("MIN_MARKET_PRICE", 0.05),
        max_market_price=_f("MAX_MARKET_PRICE", 0.95),
        max_entries_per_cycle=_i("MAX_ENTRIES_PER_CYCLE", 2),
        narrow_range_c=_f("NARROW_RANGE_C", 1.5),
        narrow_range_yes_min_edge=_f("NARROW_RANGE_YES_MIN_EDGE", 0.12),
        narrow_range_no_min_edge=_f("NARROW_RANGE_NO_MIN_EDGE", 0.03),
        min_position_age_hours=_f("MIN_POSITION_AGE_HOURS", 12.0),
        thesis_flip_min_edge=_f("THESIS_FLIP_MIN_EDGE", 0.07),
    )


class RiskManager:
    """
    Evaluates trades against risk parameters and returns pass/fail with reason.
    """

    def __init__(self, params: RiskParams | None = None) -> None:
        self.params = params or load_risk_params()

    # ── Asymmetric min edge by market shape ──────────────────────────────────

    def effective_min_edge(self, side: Side, range_c: Optional[float]) -> float:
        """
        MIN_EDGE ajustado por forma del mercado.

        Cuando el mercado es una banda de temperatura angosta (≤ narrow_range_c),
        el ruido del ensemble (spread ~1-2°C entre miembros) supera el ancho de la
        banda, así que:
          - YES requiere edge MUCHO mayor (el riesgo de que el forecast deje la banda es alto)
          - NO requiere edge BAJO (es una apuesta estructural a que no cae exactamente ahí)

        range_c=None → usar el MIN_EDGE por defecto (no es mercado de banda).
        """
        if range_c is not None and range_c <= self.params.narrow_range_c:
            if side == Side.YES:
                return self.params.narrow_range_yes_min_edge
            return self.params.narrow_range_no_min_edge
        return self.params.min_edge

    # ── Edge calculation ─────────────────────────────────────────────────────

    def calculate_edge(
        self,
        true_prob: float,
        market_price: float,
        is_hold_strategy: bool = True,
    ) -> tuple[float, Side]:
        """
        Calculate the edge for entering a position.

        Returns (edge, side_to_bet).
        edge > 0 means we have an advantage; edge > min_edge means we should bet.

        For hold strategy: only 1 fee (entry), so fee_cost = FEE_RATE.
        For trade strategy: 2 fees (entry + exit), so fee_cost = 2 * FEE_RATE.
        """
        fee_cost = POLYMARKET_FEE_RATE if is_hold_strategy else 2 * POLYMARKET_FEE_RATE

        yes_edge = true_prob - market_price - fee_cost
        no_edge = (1 - true_prob) - (1 - market_price) - fee_cost

        if yes_edge >= no_edge:
            return yes_edge, Side.YES
        return no_edge, Side.NO

    # ── Position sizing ──────────────────────────────────────────────────────

    def calculate_kelly_fraction(self, p: float, price: float) -> float:
        """
        Full Kelly fraction for a binary outcome at given price.

        f* = (p * b - q) / b
        b = (1 / price) - 1  (net payout per dollar)
        """
        if price <= 0 or price >= 1:
            return 0.0
        b = (1.0 / price) - 1.0
        q = 1.0 - p
        f_star = (p * b - q) / b
        return max(0.0, f_star)

    def calculate_position_size(
        self,
        portfolio_value: float,
        true_prob: float,
        price: float,
        open_positions_value: float,
    ) -> tuple[float, float]:
        """
        Calculate position size in USD.

        Returns (position_usd, kelly_fraction_used).
        """
        full_kelly = self.calculate_kelly_fraction(true_prob, price)
        fractional_kelly = full_kelly * self.params.kelly_fraction

        # Cap at max_position_pct
        capped_pct = min(fractional_kelly, self.params.max_position_pct)

        # Also cap by remaining risk budget
        already_exposed_pct = open_positions_value / portfolio_value if portfolio_value > 0 else 0
        remaining_risk = max(0.0, self.params.max_portfolio_risk - already_exposed_pct)
        final_pct = min(capped_pct, remaining_risk)

        position_usd = portfolio_value * final_pct
        return position_usd, final_pct

    # ── Pre-trade checks ─────────────────────────────────────────────────────

    def check_entry_allowed(
        self,
        market: Market,
        edge: float,
        side: Side,
        position_size_usd: float,
        portfolio: PortfolioState,
        is_trading_paused: bool = False,
    ) -> tuple[bool, str]:
        """
        Run all pre-trade checks. Returns (allowed, reason).
        """
        if is_trading_paused:
            return False, "trading paused (loss/drawdown limit hit)"

        if market.closed or not market.active:
            return False, "market is closed or inactive"

        if market.days_to_resolution <= 0:
            return False, "market resolves in the past"

        if market.liquidity_usd < self.params.min_liquidity_usd:
            return False, (
                f"insufficient liquidity (${market.liquidity_usd:.0f} < "
                f"${self.params.min_liquidity_usd:.0f})"
            )

        # Precio fuera de rango → mercado roto o sin liquidez real
        # (ej. YES=0.0005 o YES=0.999 indica precio muerto, nadie opera ahí)
        if market.yes_price < self.params.min_market_price or market.yes_price > self.params.max_market_price:
            return False, (
                f"market price out of range (yes={market.yes_price:.4f}, "
                f"valid range [{self.params.min_market_price:.2f}, {self.params.max_market_price:.2f}])"
            )

        if edge < self.params.min_edge:
            return False, f"edge {edge:.1%} below minimum {self.params.min_edge:.1%}"

        if position_size_usd < self.params.min_position_usd:
            return False, (
                f"position ${position_size_usd:.2f} below minimum ${self.params.min_position_usd:.2f}"
            )

        if portfolio.open_position_count >= self.params.max_open_positions:
            return False, (
                f"max open positions reached ({self.params.max_open_positions})"
            )

        if portfolio.cash_usd < position_size_usd:
            return False, (
                f"insufficient cash (${portfolio.cash_usd:.2f} < ${position_size_usd:.2f})"
            )

        return True, "all checks passed"

    # ── In-position exit checks ──────────────────────────────────────────────

    def check_exit_signal(
        self,
        position: Position,
        current_market: Market,
        new_true_prob: float | None = None,
    ) -> tuple[bool, str]:
        """
        Check whether an open position should be exited early.

        Returns (should_exit, reason). If False, hold.

        Política:
          - No hay exit alguno antes de MIN_POSITION_AGE_HOURS (evita cristalizar ruido).
          - Stop-loss deshabilitado por defecto (STOP_LOSS_PCT=0).
          - Take-profit solo si capturó >= TAKE_PROFIT_PCT del gain teórico.
          - Thesis-flip: nueva probabilidad apoya el lado contrario con edge >= THESIS_FLIP_MIN_EDGE.
        """
        current_price = (
            current_market.yes_price
            if position.side == Side.YES
            else current_market.no_price
        )
        current_value = position.shares * current_price
        pnl_pct = (current_value - position.entry_amount_usd) / position.entry_amount_usd

        # ── Edad mínima: durante las primeras N horas no se hace exit alguno ─
        opened_at = position.opened_at
        if opened_at is not None:
            if opened_at.tzinfo is None:
                opened_at = opened_at.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - opened_at).total_seconds() / 3600.0
        else:
            age_hours = float("inf")

        if age_hours < self.params.min_position_age_hours:
            return False, (
                f"hold (position age {age_hours:.1f}h < "
                f"{self.params.min_position_age_hours:.1f}h min)"
            )

        # ── Stop loss (0 = deshabilitado) ────────────────────────────────────
        if self.params.stop_loss_pct > 0 and pnl_pct <= -self.params.stop_loss_pct:
            return True, f"stop-loss hit ({pnl_pct:.1%})"

        # ── Take profit (early exit) ─────────────────────────────────────────
        max_value = position.shares * 1.0
        max_profit = max_value - position.entry_amount_usd
        if max_profit > 0:
            realized_fraction = (current_value - position.entry_amount_usd) / max_profit
            if realized_fraction >= self.params.take_profit_pct:
                return True, f"take-profit hit (captured {realized_fraction:.0%} of theoretical max)"

        # ── Thesis invalidation ──────────────────────────────────────────────
        if new_true_prob is not None:
            new_edge, best_side = self.calculate_edge(new_true_prob, current_price)
            if best_side != position.side and new_edge >= self.params.thesis_flip_min_edge:
                return True, (
                    f"thesis invalidated: new weather model supports opposite side "
                    f"(new_prob={new_true_prob:.2%}, edge={new_edge:.2%})"
                )

        return False, "hold"

    # ── Portfolio health ─────────────────────────────────────────────────────

    def check_trading_pause(
        self,
        portfolio: PortfolioState,
        daily_pnl_usd: float,
    ) -> tuple[bool, str]:
        """
        Returns (should_pause, reason) if trading should be halted.
        """
        # Drawdown check
        if portfolio.drawdown_pct >= self.params.drawdown_limit:
            return True, (
                f"drawdown limit hit ({portfolio.drawdown_pct:.1%} >= "
                f"{self.params.drawdown_limit:.1%})"
            )

        # Daily loss check
        if portfolio.total_value_usd > 0:
            daily_loss_pct = abs(daily_pnl_usd) / portfolio.total_value_usd
            if daily_pnl_usd < 0 and daily_loss_pct >= self.params.daily_loss_limit:
                return True, (
                    f"daily loss limit hit (${daily_pnl_usd:.2f}, "
                    f"{daily_loss_pct:.1%} >= {self.params.daily_loss_limit:.1%})"
                )

        return False, ""

    # ── Fee utilities ─────────────────────────────────────────────────────────

    @staticmethod
    def calculate_fee(amount_usd: float) -> float:
        return amount_usd * POLYMARKET_FEE_RATE

    @staticmethod
    def calculate_breakeven_price(entry_price: float, side: Side) -> float:
        """
        Minimum price at resolution needed to break even (including entry fee).
        For YES: we paid entry_price, paid FEE_RATE on that → need the share to be worth more.
        Simplified: breakeven at entry_price * (1 + FEE_RATE).
        """
        return min(1.0, entry_price * (1 + POLYMARKET_FEE_RATE))
