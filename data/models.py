"""
SQLAlchemy ORM models and Pydantic schemas for the trading system.

Tables:
  - signals: Scored options flow signals from Unusual Whales
  - positions: Open/closed options positions
  - order_intents: Idempotent pre-trade records (prevents duplicates)
  - broker_orders: Actual broker order tracking
  - trade_log: Completed trade P&L ledger
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SignalAction(str, enum.Enum):
    CALL = "CALL"
    PUT = "PUT"


class PositionStatus(str, enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    ROLLING = "ROLLING"


class OrderSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class IntentStatus(str, enum.Enum):
    PENDING = "PENDING"
    EXECUTED = "EXECUTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class RiskLevel(str, enum.Enum):
    HEALTHY = "HEALTHY"
    CAUTIOUS = "CAUTIOUS"
    ELEVATED = "ELEVATED"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# SQLAlchemy Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------

class SignalRecord(Base):
    __tablename__ = "signals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(String(64), unique=True, nullable=False)
    ticker = Column(String(10), nullable=False, index=True)
    action = Column(Enum(SignalAction), nullable=False)
    strike = Column(Float, nullable=False)
    expiration = Column(String(10), nullable=False)
    premium = Column(Float, nullable=False)
    volume = Column(Integer, nullable=False)
    open_interest = Column(Integer, nullable=False)
    vol_oi_ratio = Column(Float, nullable=False)
    option_type = Column(String(10), nullable=False)
    order_type = Column(String(20), default="")
    score = Column(Integer, nullable=False)
    score_breakdown = Column(Text, default="")
    underlying_price = Column(Float, nullable=True)
    iv_rank = Column(Float, nullable=True)
    dte = Column(Integer, nullable=True)
    accepted = Column(Boolean, default=False)
    reject_reason = Column(String(200), default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class PositionRecord(Base):
    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(String(64), unique=True, nullable=False)
    signal_id = Column(String(64), nullable=False, index=True)
    ticker = Column(String(10), nullable=False, index=True)
    option_symbol = Column(String(30), nullable=False)
    action = Column(Enum(SignalAction), nullable=False)
    strike = Column(Float, nullable=False)
    expiration = Column(String(10), nullable=False)
    quantity = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    current_price = Column(Float, nullable=True)
    entry_value = Column(Float, nullable=False)
    current_value = Column(Float, nullable=True)
    pnl_pct = Column(Float, default=0.0)
    pnl_dollars = Column(Float, default=0.0)
    status = Column(Enum(PositionStatus), default=PositionStatus.OPEN, index=True)
    # Greeks
    delta = Column(Float, nullable=True)
    gamma = Column(Float, nullable=True)
    theta = Column(Float, nullable=True)
    vega = Column(Float, nullable=True)
    iv = Column(Float, nullable=True)
    # Thesis
    entry_thesis = Column(Text, default="")
    conviction = Column(Integer, default=0)
    # Timestamps
    opened_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime, nullable=True)
    last_checked = Column(DateTime, nullable=True)


class OrderIntent(Base):
    __tablename__ = "order_intents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String(64), unique=True, nullable=False)
    signal_id = Column(String(64), nullable=False, index=True)
    ticker = Column(String(10), nullable=False)
    option_symbol = Column(String(30), nullable=False)
    side = Column(Enum(OrderSide), nullable=False)
    quantity = Column(Integer, nullable=False)
    limit_price = Column(Float, nullable=True)
    status = Column(Enum(IntentStatus), default=IntentStatus.PENDING)
    broker_order_id = Column(String(64), nullable=True)
    reason = Column(Text, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    executed_at = Column(DateTime, nullable=True)


class BrokerOrder(Base):
    __tablename__ = "broker_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    broker_order_id = Column(String(64), unique=True, nullable=False)
    intent_id = Column(String(64), nullable=False, index=True)
    ticker = Column(String(10), nullable=False)
    option_symbol = Column(String(30), nullable=False)
    side = Column(Enum(OrderSide), nullable=False)
    quantity = Column(Integer, nullable=False)
    order_type = Column(String(10), default="limit")
    limit_price = Column(Float, nullable=True)
    filled_price = Column(Float, nullable=True)
    filled_qty = Column(Integer, default=0)
    status = Column(Enum(OrderStatus), default=OrderStatus.PENDING)
    submitted_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    filled_at = Column(DateTime, nullable=True)
    error_msg = Column(Text, default="")


class TradeLog(Base):
    __tablename__ = "trade_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    position_id = Column(String(64), nullable=False, index=True)
    ticker = Column(String(10), nullable=False)
    action = Column(Enum(SignalAction), nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    quantity = Column(Integer, nullable=False)
    pnl_dollars = Column(Float, nullable=False)
    pnl_pct = Column(Float, nullable=False)
    hold_duration_hours = Column(Float, nullable=False)
    entry_thesis = Column(Text, default="")
    exit_reason = Column(String(200), default="")
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Pydantic Schemas (runtime DTOs)
# ---------------------------------------------------------------------------

class FlowSignal(BaseModel):
    """Parsed signal from Unusual Whales API."""
    signal_id: str = Field(default_factory=lambda: uuid4().hex[:16])
    ticker: str
    action: SignalAction
    strike: float
    expiration: str
    premium: float
    volume: int
    open_interest: int
    vol_oi_ratio: float
    option_type: str
    option_symbol: str = ""
    order_type: str = ""
    underlying_price: float = 0.0
    iv_rank: float = 0.0
    dte: int = 0
    score: int = 0
    score_breakdown: str = ""

    # Directional conviction fields
    ask_side_volume: float = 0.0
    bid_side_volume: float = 0.0
    directional_pct: float = 0.0
    directional_side: str = ""

    # Trade structure fields
    has_singleleg: bool = False
    has_multileg: bool = False
    trade_count: int = 0
    next_earnings_date: str = ""


class PositionSnapshot(BaseModel):
    """Current state of an open position."""
    position_id: str
    ticker: str
    option_symbol: str
    action: SignalAction
    strike: float
    expiration: str
    quantity: int
    entry_price: float
    current_price: float = 0.0
    pnl_pct: float = 0.0
    pnl_dollars: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    iv: float = 0.0
    conviction: int = 0
    dte_remaining: int = 0


class TradeRequest(BaseModel):
    """Instruction to execute a trade."""
    signal_id: str
    ticker: str
    option_symbol: str
    side: OrderSide
    quantity: int
    limit_price: Optional[float] = None
    reason: str = ""
    thesis: str = ""
    conviction: int = 0


class RiskAssessment(BaseModel):
    """Portfolio risk evaluation."""
    risk_score: int = 0
    risk_level: RiskLevel = RiskLevel.HEALTHY
    delta_exposure: float = 0.0
    gamma_exposure: float = 0.0
    theta_daily_pct: float = 0.0
    max_concentration_pct: float = 0.0
    position_count: int = 0
    risk_capacity_pct: float = 1.0
    can_add_position: bool = True
    warnings: list[str] = Field(default_factory=list)


class TradeResult(BaseModel):
    """Result of a trade execution."""
    success: bool
    broker_order_id: str = ""
    filled_price: float = 0.0
    filled_qty: int = 0
    error: str = ""
    message: str = ""


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_engine = None
_SessionLocal = None


def init_db(db_path: str) -> None:
    """Initialize the database engine and create tables."""
    global _engine, _SessionLocal
    if db_path == ":memory:":
        url = "sqlite:///:memory:"
    else:
        url = f"sqlite:///{db_path}"
    _engine = create_engine(url, echo=False)
    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine)


def get_session() -> Session:
    """Get a new database session."""
    if _SessionLocal is None:
        raise RuntimeError("Database not initialized — call init_db() first")
    return _SessionLocal()
