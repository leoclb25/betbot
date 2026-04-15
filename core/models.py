"""
Shared data models for all bots.
All models are immutable (frozen) Pydantic v2 dataclasses.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, computed_field


# ─── Enums ───────────────────────────────────────────────────────────────────

class Side(str, Enum):
    YES = "YES"
    NO = "NO"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class BotMode(str, Enum):
    LIVE = "live"
    PAPER = "paper"


class WeatherCondition(str, Enum):
    RAIN = "rain"
    SNOW = "snow"
    TEMPERATURE_ABOVE = "temperature_above"
    TEMPERATURE_BELOW = "temperature_below"
    TEMPERATURE_EXACT = "temperature_exact"
    WIND_ABOVE = "wind_above"
    HURRICANE = "hurricane"
    STORM = "storm"
    SUNNY = "sunny"
    UNKNOWN = "unknown"


class SignalAction(str, Enum):
    ENTER = "ENTER"
    EXIT = "EXIT"
    HOLD = "HOLD"
    SKIP = "SKIP"  # not enough edge / risk limit hit


# ─── Market ──────────────────────────────────────────────────────────────────

class Market(BaseModel):
    """A Polymarket prediction market."""
    condition_id: str
    question: str
    yes_price: float = Field(ge=0.0, le=1.0)   # probability of YES (0-1)
    no_price: float = Field(ge=0.0, le=1.0)    # probability of NO (0-1)
    end_date: datetime
    volume_usd: float = 0.0
    liquidity_usd: float = 0.0
    active: bool = True
    closed: bool = False

    @computed_field
    @property
    def days_to_resolution(self) -> float:
        end = self.end_date
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        else:
            end = end.astimezone(timezone.utc)
        delta = end - datetime.now(timezone.utc)
        return max(0.0, delta.total_seconds() / 86400)


# ─── Weather parsing ─────────────────────────────────────────────────────────

class WeatherMarketInfo(BaseModel):
    """Parsed information extracted from a weather market question."""
    condition_id: str
    question: str
    location: str

    latitude: float
    longitude: float
    target_date: date
    condition: WeatherCondition
    threshold: Optional[float] = None      # lower bound (or exact value) in °C
    threshold_high: Optional[float] = None # upper bound for range markets (°C)
    threshold_unit: Optional[str] = None   # always "C" after normalization


# ─── Weather forecast ────────────────────────────────────────────────────────

class EnsembleForecast(BaseModel):
    """Raw ensemble forecast data for a specific location and date."""
    location: str
    latitude: float
    longitude: float
    target_date: date
    fetched_at: datetime
    # Per-member values (one per ensemble model)
    precipitation_mm: list[float] = Field(default_factory=list)
    temperature_max_c: list[float] = Field(default_factory=list)
    temperature_min_c: list[float] = Field(default_factory=list)
    wind_speed_max_kmh: list[float] = Field(default_factory=list)
    member_count: int = 0


class WeatherProbability(BaseModel):
    """Calculated probabilities from ensemble forecast."""
    condition: WeatherCondition
    true_probability: float = Field(ge=0.0, le=1.0)
    raw_probability: float = Field(ge=0.0, le=1.0)  # before confidence decay
    confidence: float = Field(ge=0.0, le=1.0)        # composite: days_out + model agreement
    days_out: float
    member_count: int
    model_agreement: float = Field(ge=0.0, le=1.0, default=1.0)  # 1=full agreement, 0=max disagreement
    models_used: list[str] = Field(default_factory=list)
    fetched_at: datetime


# ─── Trading ─────────────────────────────────────────────────────────────────

class Trade(BaseModel):
    """A single executed (or simulated) trade."""
    trade_id: str
    condition_id: str
    question: str
    timestamp: datetime
    side: Side          # YES or NO
    order_side: OrderSide   # BUY or SELL
    price: float        # price per share (0-1)
    amount_usd: float   # total USDC spent (before fee)
    fee_usd: float
    shares: float       # amount_usd / price
    mode: BotMode
    notes: str = ""


class Position(BaseModel):
    """An open or closed position."""
    position_id: str
    condition_id: str
    question: str
    side: Side
    status: PositionStatus = PositionStatus.OPEN
    entry_price: float
    entry_amount_usd: float   # net spent (after fee)
    entry_fee_usd: float
    shares: float
    entry_true_prob: float    # weather model probability at entry
    entry_edge: float
    opened_at: datetime
    market_end_date: Optional[datetime] = None   # when the market resolves
    closed_at: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_fee_usd: float = 0.0
    pnl_usd: Optional[float] = None
    pnl_pct: Optional[float] = None
    exit_reason: str = ""
    mode: BotMode

    @computed_field
    @property
    def current_value_usd(self) -> float:
        """Approximate value if exit_price is known."""
        if self.exit_price is not None:
            return self.shares * self.exit_price
        return self.shares * self.entry_price  # fallback to entry value


# ─── Bot signals ─────────────────────────────────────────────────────────────

class BotSignal(BaseModel):
    """Decision output from a bot strategy evaluation."""
    action: SignalAction
    condition_id: str
    question: str
    side: Optional[Side] = None
    market_price: Optional[float] = None
    true_probability: Optional[float] = None
    edge: Optional[float] = None
    kelly_fraction: Optional[float] = None
    position_size_usd: Optional[float] = None
    reason: str = ""


# ─── Portfolio ───────────────────────────────────────────────────────────────

class PortfolioState(BaseModel):
    """Snapshot of the portfolio at a point in time."""
    timestamp: datetime
    mode: BotMode
    cash_usd: float
    open_positions_value_usd: float
    total_value_usd: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    total_pnl_usd: float
    peak_value_usd: float
    drawdown_pct: float
    open_position_count: int
    total_trades: int
    winning_trades: int
    losing_trades: int

    @computed_field
    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.winning_trades / self.total_trades
