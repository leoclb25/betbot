"""
CryptoBot – orchestrator for crypto price prediction market trading.

Wires together:
  - Market scanning (Gamma API, crypto keyword filter)
  - Strategy evaluation (lognormal model + Binance data + Kelly)
  - Position management (open/close via paper or live client)
  - Risk gating (RiskManager)
  - Logging (OperationsLogger)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Union

from loguru import logger

from bots.base import BaseBot
from bots.crypto.parser import CryptoMarketParser
from bots.crypto.strategy import CryptoStrategy
from core.env_utils import env_float, env_int
from core.models import BotMode, BotSignal, Market, OrderSide, SignalAction, Position
from core.portfolio.logger import OperationsLogger
from core.portfolio.tracker import PortfolioTracker
from core.polymarket.client import PolymarketClient
from core.polymarket.paper_client import PaperClient
from core.risk.manager import RiskManager, load_risk_params
from core.crypto.price_client import BinancePriceClient

CRYPTO_KEYWORDS = ["btc", "bitcoin", "eth", "ethereum", "price", "above", "below", "crypto"]


class CryptoBot(BaseBot):
    name = "crypto"

    def __init__(
        self,
        client: Union[PolymarketClient, PaperClient],
        strategy: CryptoStrategy,
        risk_manager: RiskManager,
        tracker: PortfolioTracker,
        ops_logger: OperationsLogger,
        scan_interval_seconds: int = 300,
    ) -> None:
        super().__init__(client, risk_manager, tracker, ops_logger, scan_interval_seconds)
        self._strategy = strategy
        self._open_condition_ids: set[str] = self._load_open_condition_ids()

    # ── Factory ───────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, mode: BotMode) -> "CryptoBot":
        from dotenv import load_dotenv
        load_dotenv()

        if mode == BotMode.PAPER:
            client: Union[PolymarketClient, PaperClient] = PaperClient(
                state_file=Path("data/crypto_paper_portfolio.json"),
                initial_balance_key="CRYPTO_PAPER_INITIAL_BALANCE",
            )
        else:
            client = PolymarketClient()

        risk_manager = RiskManager(load_risk_params(prefix="CRYPTO"))
        price_client = BinancePriceClient()
        parser = CryptoMarketParser()
        strategy = CryptoStrategy(price_client, parser, risk_manager)
        tracker = PortfolioTracker(client)
        ops_logger = OperationsLogger(mode, bot_name="crypto")
        scan_interval = env_int("CRYPTO_SCAN_INTERVAL_SECONDS", 300)

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
        max_min = env_float("CRYPTO_MAX_MINUTES_TO_RESOLUTION", 60.0)
        min_liq = env_float("CRYPTO_MIN_LIQUIDITY_USD", 500.0)

        all_markets = self.client.get_markets(keywords=CRYPTO_KEYWORDS, limit=200)

        open_questions = {
            p.question.strip().lower()
            for p in self.client.get_positions()
            if p.status.value == "OPEN"
        }

        min_price = self.risk.params.min_market_price
        max_price = self.risk.params.max_market_price

        filtered = [
            m for m in all_markets
            if 0 < m.days_to_resolution * 1440 <= max_min
            and m.liquidity_usd >= min_liq
            and min_price <= m.yes_price <= max_price
            and m.condition_id not in self._open_condition_ids
            and m.question.strip().lower() not in open_questions
        ]

        logger.debug(
            f"[CRYPTO] {len(all_markets)} raw → {len(filtered)} after filters "
            f"(≤{max_min:.0f}min, liq≥${min_liq:.0f})"
        )
        return filtered

    def evaluate_market(self, market: Market) -> BotSignal:
        state = self.tracker.get_state()
        logger.debug(f"[CRYPTO] EVAL '{market.question[:80]}' | yes={market.yes_price:.3f} liq=${market.liquidity_usd:.0f} days={market.days_to_resolution:.4f}")
        signal = self._strategy.evaluate_market(market, state)
        if signal.action.value == "SKIP":
            logger.debug(f"[CRYPTO] SKIP → {signal.reason}")
        return signal

    def manage_open_positions(self) -> None:
        positions = self.client.get_positions()
        open_positions = [p for p in positions if p.status.value == "OPEN"]

        if not open_positions:
            return

        for position in open_positions:
            market = self.client.get_market(position.condition_id)
            if market is None:
                if isinstance(self.client, PaperClient):
                    logger.warning(
                        f"[CRYPTO] Market {position.condition_id[:16]}… not on Gamma — "
                        f"closing paper position at 50¢"
                    )
                    synthetic = self._synthetic_market(position)
                    self._close_position(position, synthetic, "market not found (closed at 50¢)")
                else:
                    logger.warning(f"[CRYPTO] Market {position.condition_id[:16]}… not found – skipping")
                continue

            minutes_left = market.days_to_resolution * 1440

            # Use parser to verify resolution time independently of Gamma API lag
            parsed = self._strategy._parser.parse(
                market.condition_id, market.question,
                reference_datetime=datetime.now(timezone.utc),
            )
            parser_minutes = parsed.minutes_to_resolution if parsed else None

            expired = (
                market.closed
                or minutes_left <= 0
                or (parser_minutes is not None and parser_minutes < -1)
            )
            if expired:
                self._close_position(position, market, "market resolved")
                continue

            should_exit, reason, _ = self._strategy.evaluate_exit(position, market)
            if should_exit:
                self._close_position(position, market, reason)

    # ── Cycle override with forced-bet fallback ───────────────────────────────

    def _run_cycle(self) -> None:
        state_before = self.tracker.get_state()
        open_before = state_before.open_position_count
        super()._run_cycle()
        if self._trading_paused:
            return
        state_after = self.tracker.get_state()
        if state_after.open_position_count == open_before:
            self._try_forced_bet()

    def _try_forced_bet(self) -> None:
        state = self.tracker.get_state()
        if state.open_position_count >= self.risk.params.max_open_positions:
            logger.debug(
                f"[CRYPTO] forced-bet: skipped — already at max positions "
                f"({state.open_position_count}/{self.risk.params.max_open_positions})"
            )
            return

        floor = env_float("CRYPTO_FORCE_BET_FLOOR", -0.02)
        min_minutes = env_float("CRYPTO_FORCE_BET_MIN_MINUTES", 3.0)
        min_pos_usd = self.risk.params.min_position_usd

        markets = self.scan_markets()
        if not markets:
            logger.debug("[CRYPTO] forced-bet: no markets available")
            return

        best_signal = None
        best_market = None

        for market in markets:
            minutes_left = market.days_to_resolution * 1440
            if minutes_left < min_minutes:
                continue
            try:
                signal = self._strategy.evaluate_market(market, state)
            except Exception:
                continue
            if signal.edge is None or signal.side is None:
                continue
            if signal.edge < floor:
                continue
            if best_signal is None or (signal.edge or 0) > (best_signal.edge or 0):
                best_signal = signal
                best_market = market

        if best_signal is None or best_market is None:
            logger.debug(f"[CRYPTO] forced-bet: no candidate above floor {floor:.1%}")
            return

        logger.info(
            f"[CRYPTO] forced-bet: entering best candidate edge={best_signal.edge:.1%} "
            f"T={best_market.days_to_resolution*1440:.0f}min '{best_market.question[:60]}'"
        )
        forced = best_signal.model_copy(update={
            "action": SignalAction.ENTER,
            "position_size_usd": max(min_pos_usd, best_signal.position_size_usd or min_pos_usd),
            "reason": f"[forced] {best_signal.reason}",
        })
        self._execute_entry(forced, market=best_market)

    # ── Entry / Exit execution ────────────────────────────────────────────────

    def _execute_entry(self, signal: BotSignal, market=None) -> None:
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
                    f"[CRYPTO][PAPER] OPENED {signal.side.value} on '{market.question[:60]}' | "
                    f"${signal.position_size_usd:.2f} | edge={signal.edge:.1%}"
                )
            except ValueError as exc:
                logger.warning(f"[CRYPTO] Could not open position: {exc}")
        else:
            trade = self.client.place_order(
                market=market,
                side=signal.side,
                order_side=OrderSide.BUY,
                amount_usd=signal.position_size_usd,
            )
            self._open_condition_ids.add(market.condition_id)
            self.logger.log_trade(trade, notes=signal.reason)

    def _close_position(self, position: Position, market: Market, reason: str) -> None:
        if isinstance(self.client, PaperClient):
            try:
                trade, closed = self.client.close_position(position=position, market=market, reason=reason)
                self._open_condition_ids.discard(market.condition_id)
                self.tracker.record_close(closed.pnl_usd or 0.0)

                pnl = closed.pnl_usd or 0.0
                pnl_pct = closed.pnl_pct or 0.0
                pnl_sign = "+" if pnl >= 0 else ""
                pnl_color = "WIN" if pnl >= 0 else "LOSS"
                logger.info(
                    f"[CRYPTO][PAPER] ═══ {pnl_color} ═══ CLOSED {position.side.value} | "
                    f"'{market.question[:55]}' | "
                    f"P&L={pnl_sign}${pnl:.2f} ({pnl_sign}{pnl_pct*100:.1f}%) | reason={reason}"
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
                logger.error(f"[CRYPTO][PAPER] Error closing position {position.position_id}: {exc}")
        else:
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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _load_open_condition_ids(self) -> set[str]:
        try:
            positions = self.client.get_positions()
            return {p.condition_id for p in positions if p.status.value == "OPEN"}
        except Exception:
            return set()

    @staticmethod
    def _synthetic_market(position: Position) -> Market:
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
