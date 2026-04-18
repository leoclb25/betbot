"""
Abstract base class for all bots.

Every bot must implement:
  - scan_markets() → find markets relevant to this bot's niche
  - evaluate_market(market) → BotSignal
  - manage_open_positions() → check exits for already-open positions

The base class handles the run loop, portfolio snapshotting, and logging.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from loguru import logger

from typing import Optional

from core.models import BotMode, BotSignal, Market, PortfolioState, SignalAction
from core.portfolio.logger import OperationsLogger
from core.portfolio.tracker import PortfolioTracker
from core.risk.manager import RiskManager


class BaseBot(ABC):
    """
    Base class for all Polymarket bots.

    Subclasses implement the strategy; the base class handles
    the scan loop, risk gating, and logging.
    """

    name: str = "base"

    def __init__(
        self,
        client,            # PolymarketClient or PaperClient
        risk_manager: RiskManager,
        tracker: PortfolioTracker,
        ops_logger: OperationsLogger,
        scan_interval_seconds: int = 3600,
    ) -> None:
        self.client = client
        self.risk = risk_manager
        self.tracker = tracker
        self.logger = ops_logger
        self.scan_interval = scan_interval_seconds
        self.mode: BotMode = client.mode
        self._trading_paused = False
        self._pause_reason = ""

    # ── Abstract interface ────────────────────────────────────────────────────

    @abstractmethod
    def scan_markets(self) -> list[Market]:
        """Return candidate markets for this bot to evaluate."""
        ...

    @abstractmethod
    def evaluate_market(self, market: Market) -> BotSignal:
        """Evaluate a single market and return an entry signal."""
        ...

    @abstractmethod
    def manage_open_positions(self) -> None:
        """Check and act on existing open positions."""
        ...

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self, run_once: bool = False) -> None:
        """
        Start the bot loop.

        run_once=True runs a single scan then returns (useful for testing).
        """
        mode_label = f"[{self.mode.value.upper()}]"
        logger.info(f"{mode_label} {self.name} bot starting.")
        self.logger.log_bot_event("bot_start", {"bot": self.name, "mode": self.mode.value})

        try:
            while True:
                self._run_cycle()
                if run_once:
                    break
                logger.info(f"{mode_label} Sleeping {self.scan_interval}s until next scan...")
                time.sleep(self.scan_interval)
        except KeyboardInterrupt:
            logger.info(f"{mode_label} Bot stopped by user.")
            self.logger.log_bot_event("bot_stop", {"bot": self.name, "reason": "keyboard_interrupt"})

    def _run_cycle(self) -> None:
        """Execute one full scan-evaluate-manage cycle."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        logger.debug(f"[{self.mode.value.upper()}] Scan cycle {now}")

        # 1. Portfolio health check
        state = self.tracker.get_state()
        self.logger.update_balance_summary(state)
        self._check_trading_pause(state)

        # 2. Manage existing positions (exits)
        self.manage_open_positions()

        # 3. Scan for new opportunities (if not paused)
        if not self._trading_paused:
            markets = self.scan_markets()
            entries_this_cycle = 0
            max_entries = self.risk.params.max_entries_per_cycle

            for market in markets:
                if max_entries > 0 and entries_this_cycle >= max_entries:
                    logger.debug(
                        f"[{self.mode.value.upper()}] entry cap reached "
                        f"({entries_this_cycle}/{max_entries}) — skipping remaining candidates"
                    )
                    break
                try:
                    signal = self.evaluate_market(market)
                    # Pass market directly to avoid a redundant re-fetch
                    entered = self._act_on_signal(signal, state, market=market)
                    if entered:
                        entries_this_cycle += 1
                except Exception as exc:
                    logger.error(f"Error evaluating market {market.condition_id}: {exc}")
                    self.logger.log_bot_event(
                        "error",
                        {"condition_id": market.condition_id, "error": str(exc)},
                    )
        else:
            logger.warning(f"Trading paused: {self._pause_reason}")

        # 4. Final portfolio snapshot
        final_state = self.tracker.get_state()
        self.logger.update_balance_summary(final_state)

        # Only log summary if there are open positions or portfolio changed
        if final_state.open_position_count > 0 or final_state.total_trades > 0:
            pnl_sign = "+" if final_state.total_pnl_usd >= 0 else ""
            logger.info(
                f"[{self.mode.value.upper()}] Cycle | "
                f"open={final_state.open_position_count} | "
                f"cash=${final_state.cash_usd:.2f} | "
                f"total=${final_state.total_value_usd:.2f} | "
                f"P&L={pnl_sign}${final_state.total_pnl_usd:.2f}"
            )

    def _act_on_signal(
        self,
        signal: BotSignal,
        state: PortfolioState,
        market: Optional[Market] = None,
    ) -> bool:
        """
        Execute an ENTER signal (SKIP and HOLD are no-ops here).

        Returns True si se ejecutó la entrada, False en cualquier otro caso.
        """
        if signal.action != SignalAction.ENTER:
            return False

        # Use the market passed in (avoids a redundant API call)
        resolved_market = market or self._get_market_for_signal(signal)

        # Final risk gate
        allowed, reason = self.risk.check_entry_allowed(
            market=resolved_market,
            edge=signal.edge or 0.0,
            side=signal.side,
            position_size_usd=signal.position_size_usd or 0.0,
            portfolio=state,
            is_trading_paused=self._trading_paused,
        )
        if not allowed:
            return False

        self._execute_entry(signal, market=resolved_market)
        return True

    def _execute_entry(self, signal: BotSignal, market: Optional[Market] = None) -> None:
        """To be implemented by subclasses that need custom entry logic."""
        raise NotImplementedError("Subclass must implement _execute_entry")

    def _get_market_for_signal(self, signal: BotSignal) -> Market:
        """Fetch current market data for a signal's condition_id."""
        market = self.client.get_market(signal.condition_id)
        if market is None:
            raise ValueError(f"Market {signal.condition_id} not found")
        return market

    def _check_trading_pause(self, state: PortfolioState) -> None:
        pause, reason = self.risk.check_trading_pause(state, self.tracker.daily_pnl)
        if pause and not self._trading_paused:
            self._trading_paused = True
            self._pause_reason = reason
            logger.warning(f"TRADING PAUSED: {reason}")
            self.logger.log_bot_event("trading_paused", {"reason": reason})
        elif not pause and self._trading_paused:
            self._trading_paused = False
            self._pause_reason = ""
            logger.info("Trading resumed.")
            self.logger.log_bot_event("trading_resumed", {})
