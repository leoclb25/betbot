"""
Operations logger.

Writes two files:
  data/logs/operations.jsonl   – append-only log of every trade
  data/logs/balance_summary.json – latest snapshot of the portfolio state

Both are human-readable and machine-parseable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from core.models import BotMode, PortfolioState, Trade

LOGS_DIR = Path("data/logs")
OPERATIONS_FILE = LOGS_DIR / "operations.jsonl"
BALANCE_SUMMARY_FILE = LOGS_DIR / "balance_summary.json"


def _ensure_logs_dir() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)


class OperationsLogger:
    """Writes trade and portfolio events to the log files."""

    def __init__(self, mode: BotMode) -> None:
        self.mode = mode
        _ensure_logs_dir()

    # ── Operations log ───────────────────────────────────────────────────────

    def log_trade(self, trade: Trade, notes: str = "") -> None:
        """Append a trade record to operations.jsonl."""
        record = {
            "timestamp": trade.timestamp.isoformat(),
            "mode": trade.mode.value,
            "event": "trade",
            "trade_id": trade.trade_id,
            "condition_id": trade.condition_id,
            "question": trade.question,
            "side": trade.side.value,
            "order_side": trade.order_side.value,
            "price": round(trade.price, 6),
            "amount_usd": round(trade.amount_usd, 4),
            "fee_usd": round(trade.fee_usd, 4),
            "shares": round(trade.shares, 6),
            "notes": notes or trade.notes,
        }
        self._append(record)

    def log_position_open(
        self,
        condition_id: str,
        question: str,
        side: str,
        entry_price: float,
        amount_usd: float,
        true_prob: float,
        edge: float,
    ) -> None:
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "mode": self.mode.value,
            "event": "position_open",
            "condition_id": condition_id,
            "question": question,
            "side": side,
            "entry_price": round(entry_price, 6),
            "amount_usd": round(amount_usd, 4),
            "true_prob": round(true_prob, 4),
            "edge": round(edge, 4),
        }
        self._append(record)

    def log_position_close(
        self,
        condition_id: str,
        question: str,
        side: str,
        exit_price: float,
        pnl_usd: float,
        pnl_pct: float,
        reason: str,
    ) -> None:
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "mode": self.mode.value,
            "event": "position_close",
            "condition_id": condition_id,
            "question": question,
            "side": side,
            "exit_price": round(exit_price, 6),
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 4),
            "reason": reason,
        }
        self._append(record)

    def log_signal_skipped(
        self,
        condition_id: str,
        question: str,
        reason: str,
        edge: float | None = None,
    ) -> None:
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "mode": self.mode.value,
            "event": "signal_skipped",
            "condition_id": condition_id,
            "question": question[:80],
            "reason": reason,
            "edge": round(edge, 4) if edge is not None else None,
        }
        self._append(record)

    def log_bot_event(self, event: str, details: dict) -> None:
        """Generic bot lifecycle event (scan_start, pause, resume, error)."""
        record = {
            "timestamp": datetime.utcnow().isoformat(),
            "mode": self.mode.value,
            "event": event,
            **details,
        }
        self._append(record)

    # ── Balance summary ──────────────────────────────────────────────────────

    def update_balance_summary(self, state: PortfolioState) -> None:
        """Overwrite balance_summary.json with the latest portfolio state."""
        summary = {
            "updated_at": state.timestamp.isoformat(),
            "mode": state.mode.value,
            "cash_usd": round(state.cash_usd, 4),
            "open_positions_value_usd": round(state.open_positions_value_usd, 4),
            "total_value_usd": round(state.total_value_usd, 4),
            "realized_pnl_usd": round(state.realized_pnl_usd, 4),
            "unrealized_pnl_usd": round(state.unrealized_pnl_usd, 4),
            "total_pnl_usd": round(state.total_pnl_usd, 4),
            "peak_value_usd": round(state.peak_value_usd, 4),
            "drawdown_pct": round(state.drawdown_pct, 4),
            "open_position_count": state.open_position_count,
            "total_trades": state.total_trades,
            "winning_trades": state.winning_trades,
            "losing_trades": state.losing_trades,
            "win_rate": round(state.win_rate, 4),
        }
        with BALANCE_SUMMARY_FILE.open("w") as f:
            json.dump(summary, f, indent=2)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _append(self, record: dict) -> None:
        with OPERATIONS_FILE.open("a") as f:
            f.write(json.dumps(record) + "\n")
