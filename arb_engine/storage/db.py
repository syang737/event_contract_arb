"""SQLAlchemy ORM models and a thin :class:`Database` helper.

Tables (per the spec):

* ``markets`` – static cross-venue mapping + metadata.
* ``quotes``  – sampled top-of-book snapshots.
* ``arbs``    – every detected opportunity (executed or not).
* ``trades``  – simulated paper trades and their settlement.
* ``fills``   – per-level fills backing each trade leg.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Iterator, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    select,
)
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    selectinload,
    sessionmaker,
)

from ..config import AppConfig
from ..core.models import ArbOpportunity, Exchange, MarketBook, Quote, Side, utcnow

if TYPE_CHECKING:
    from ..execution.simulator import SimulatedTrade
    from ..mapping.models import VenueMarket


class Base(DeclarativeBase):
    pass


class MarketRow(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pm_market_id: Mapped[str] = mapped_column(String)
    pm_yes_token: Mapped[str] = mapped_column(String)
    pm_no_token: Mapped[str] = mapped_column(String)
    ka_ticker: Mapped[str] = mapped_column(String)
    close_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class QuoteRow(Base):
    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    exchange: Mapped[str] = mapped_column(String, index=True)
    market_id: Mapped[str] = mapped_column(String, index=True)
    venue_market_id: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    best_bid: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    best_ask: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    bid_size: Mapped[float] = mapped_column(Float, default=0.0)
    ask_size: Mapped[float] = mapped_column(Float, default=0.0)


class ArbRow(Base):
    __tablename__ = "arbs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    market_id: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    size: Mapped[float] = mapped_column(Float)
    gross_edge_per_contract: Mapped[float] = mapped_column(Float)
    net_edge_per_contract: Mapped[float] = mapped_column(Float)
    net_edge_per_contract_adj: Mapped[float] = mapped_column(Float)
    total_net_profit: Mapped[float] = mapped_column(Float)
    total_cost: Mapped[float] = mapped_column(Float)
    total_fee: Mapped[float] = mapped_column(Float)
    executed: Mapped[bool] = mapped_column(Boolean, default=False)
    trade_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("trades.id"), nullable=True
    )


class TradeRow(Base):
    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    market_id: Mapped[str] = mapped_column(String, index=True)
    label: Mapped[str] = mapped_column(String)
    direction: Mapped[str] = mapped_column(String)
    size: Mapped[float] = mapped_column(Float)

    leg1_exchange: Mapped[str] = mapped_column(String)
    leg1_side: Mapped[str] = mapped_column(String)
    leg1_venue_market_id: Mapped[str] = mapped_column(String)
    leg1_avg_price: Mapped[float] = mapped_column(Float)
    leg1_fee: Mapped[float] = mapped_column(Float)

    leg2_exchange: Mapped[str] = mapped_column(String)
    leg2_side: Mapped[str] = mapped_column(String)
    leg2_venue_market_id: Mapped[str] = mapped_column(String)
    leg2_avg_price: Mapped[float] = mapped_column(Float)
    leg2_fee: Mapped[float] = mapped_column(Float)

    gross_edge_per_contract: Mapped[float] = mapped_column(Float)
    net_edge_per_contract: Mapped[float] = mapped_column(Float)

    status: Mapped[str] = mapped_column(String, default="OPEN", index=True)
    settlement_price_yes: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    payout_total: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    realized_pnl: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    fills: Mapped[list["FillRow"]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )

    @property
    def total_cost(self) -> float:
        return (
            self.leg1_avg_price * self.size
            + self.leg2_avg_price * self.size
            + self.leg1_fee
            + self.leg2_fee
        )


class FillRow(Base):
    __tablename__ = "fills"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trades.id"), index=True)
    exchange: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String)
    venue_market_id: Mapped[str] = mapped_column(String)
    price: Mapped[float] = mapped_column(Float)
    size: Mapped[float] = mapped_column(Float)

    trade: Mapped[TradeRow] = relationship(back_populates="fills")


class VenueMarketRow(Base):
    """Cached catalog entry from a venue's discovery endpoint."""

    __tablename__ = "venue_markets"
    __table_args__ = (UniqueConstraint("exchange", "venue_id", name="uq_venue_market"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    exchange: Mapped[str] = mapped_column(String, index=True)
    venue_id: Mapped[str] = mapped_column(String, index=True)
    title: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String, default="")
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    close_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active", index=True)
    yes_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    no_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    strike_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    strike: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    strike_cap: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    event_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    series_key: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class MarketMappingRow(Base):
    """A discovered cross-venue mapping with status + provenance."""

    __tablename__ = "market_mappings"
    __table_args__ = (
        UniqueConstraint("pm_condition_id", "ka_ticker", name="uq_market_mapping"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pm_condition_id: Mapped[str] = mapped_column(String, index=True)
    pm_yes_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    pm_no_token: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    ka_ticker: Mapped[str] = mapped_column(String, index=True)
    pm_yes_equals_kalshi_yes: Mapped[bool] = mapped_column(Boolean, default=True)

    label: Mapped[str] = mapped_column(String, default="")
    category: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    close_time: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String, default="proposed", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    score_json: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    reason: Mapped[str] = mapped_column(String, default="")
    method: Mapped[str] = mapped_column(String, default="rule")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    retired_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------- #
class Database:
    """Owns the SQLAlchemy engine and hands out sessions."""

    def __init__(self, url: str = "sqlite:///data/arb.db", echo: bool = False):
        is_sqlite = url.startswith("sqlite")
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        engine_kwargs: dict = {"echo": echo, "future": True, "connect_args": connect_args}
        # A shared in-memory DB must reuse one connection or each session gets a
        # fresh, empty database.
        if is_sqlite and ":memory:" in url:
            engine_kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **engine_kwargs)
        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        self.create_all()

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- writes --------------------------------------------------------- #
    def sync_markets(self, config: AppConfig) -> None:
        """Upsert the static market mapping into the ``markets`` table."""
        with self.session() as s:
            for m in config.markets:
                row = s.get(MarketRow, m.id)
                if row is None:
                    row = MarketRow(id=m.id)
                    s.add(row)
                row.label = m.label
                row.category = m.category
                row.pm_market_id = m.polymarket.market_id
                row.pm_yes_token = m.polymarket.yes_token
                row.pm_no_token = m.polymarket.no_token
                row.ka_ticker = m.kalshi.ticker
                row.close_time = m.close_time

    def record_quote_snapshot(self, book: MarketBook) -> None:
        with self.session() as s:
            for side in (Side.YES, Side.NO):
                q: Quote = Quote.from_book(book, side)
                s.add(
                    QuoteRow(
                        ts=q.ts,
                        exchange=q.exchange.value,
                        market_id=q.market_id,
                        venue_market_id=q.venue_market_id,
                        side=q.side.value,
                        best_bid=q.best_bid,
                        best_ask=q.best_ask,
                        bid_size=q.bid_size,
                        ask_size=q.ask_size,
                    )
                )

    def record_arb(self, opp: ArbOpportunity, *, executed: bool, trade_id: Optional[int] = None) -> int:
        with self.session() as s:
            row = ArbRow(
                ts=opp.ts,
                market_id=opp.market_id,
                label=opp.label,
                direction=opp.direction.value,
                size=opp.size,
                gross_edge_per_contract=opp.gross_edge_per_contract,
                net_edge_per_contract=opp.net_edge_per_contract,
                net_edge_per_contract_adj=opp.net_edge_per_contract_adj,
                total_net_profit=opp.total_net_profit,
                total_cost=opp.total_cost,
                total_fee=opp.total_fee,
                executed=executed,
                trade_id=trade_id,
            )
            s.add(row)
            s.flush()
            return row.id

    def insert_trade(self, sim: "SimulatedTrade") -> int:
        """Persist a simulated trade plus its per-level fills; returns trade id."""
        with self.session() as s:
            row = TradeRow(
                created_at=sim.created_at,
                market_id=sim.market_id,
                label=sim.label,
                direction=sim.direction.value,
                size=sim.size,
                leg1_exchange=sim.leg1.exchange.value,
                leg1_side=sim.leg1.side.value,
                leg1_venue_market_id=sim.leg1.venue_market_id,
                leg1_avg_price=sim.leg1.avg_price,
                leg1_fee=sim.leg1.fee,
                leg2_exchange=sim.leg2.exchange.value,
                leg2_side=sim.leg2.side.value,
                leg2_venue_market_id=sim.leg2.venue_market_id,
                leg2_avg_price=sim.leg2.avg_price,
                leg2_fee=sim.leg2.fee,
                gross_edge_per_contract=sim.gross_edge_per_contract,
                net_edge_per_contract=sim.net_edge_per_contract,
                status="OPEN",
            )
            for f in sim.fills:
                row.fills.append(
                    FillRow(
                        exchange=f.exchange.value,
                        side=f.side.value,
                        venue_market_id=f.venue_market_id,
                        price=f.price,
                        size=f.size,
                    )
                )
            s.add(row)
            s.flush()
            return row.id

    def upsert_venue_markets(self, markets: "list[VenueMarket]") -> int:
        """Insert-or-update catalog rows keyed by (exchange, venue_id)."""
        count = 0
        with self.session() as s:
            for vm in markets:
                row = s.scalar(
                    select(VenueMarketRow).where(
                        VenueMarketRow.exchange == vm.exchange.value,
                        VenueMarketRow.venue_id == vm.venue_id,
                    )
                )
                if row is None:
                    row = VenueMarketRow(exchange=vm.exchange.value, venue_id=vm.venue_id)
                    s.add(row)
                row.title = vm.title
                row.description = vm.description
                row.category = vm.category
                row.close_time = vm.close_time
                row.status = vm.status
                row.yes_token = vm.yes_token
                row.no_token = vm.no_token
                row.strike_type = vm.strike_type
                row.strike = vm.strike
                row.strike_cap = vm.strike_cap
                row.event_key = vm.event_key
                row.series_key = vm.series_key
                row.fetched_at = vm.fetched_at
                count += 1
        return count

    def venue_markets(self, exchange: Optional[Exchange] = None) -> list[VenueMarketRow]:
        with self.session() as s:
            stmt = select(VenueMarketRow)
            if exchange is not None:
                stmt = stmt.where(VenueMarketRow.exchange == exchange.value)
            return list(s.scalars(stmt))

    def upsert_mapping(self, fields: dict) -> int:
        """Insert-or-update a mapping keyed by (pm_condition_id, ka_ticker).

        Preserves ``created_at`` on updates and stamps ``updated_at``. Returns id.
        """
        with self.session() as s:
            row = s.scalar(
                select(MarketMappingRow).where(
                    MarketMappingRow.pm_condition_id == fields["pm_condition_id"],
                    MarketMappingRow.ka_ticker == fields["ka_ticker"],
                )
            )
            if row is None:
                row = MarketMappingRow(
                    pm_condition_id=fields["pm_condition_id"],
                    ka_ticker=fields["ka_ticker"],
                )
                s.add(row)
            for key, value in fields.items():
                if key in ("pm_condition_id", "ka_ticker", "created_at"):
                    continue
                setattr(row, key, value)
            row.updated_at = utcnow()
            s.flush()
            return row.id

    def get_mapping(self, mapping_id: int) -> Optional[MarketMappingRow]:
        with self.session() as s:
            return s.get(MarketMappingRow, mapping_id)

    def get_mapping_by_pair(
        self, pm_condition_id: str, ka_ticker: str
    ) -> Optional[MarketMappingRow]:
        with self.session() as s:
            return s.scalar(
                select(MarketMappingRow).where(
                    MarketMappingRow.pm_condition_id == pm_condition_id,
                    MarketMappingRow.ka_ticker == ka_ticker,
                )
            )

    def mappings(self, status: Optional[str] = None) -> list[MarketMappingRow]:
        with self.session() as s:
            stmt = select(MarketMappingRow)
            if status is not None:
                stmt = stmt.where(MarketMappingRow.status == status)
            return list(s.scalars(stmt.order_by(MarketMappingRow.confidence.desc())))

    def set_mapping_status(self, mapping_id: int, status: str) -> bool:
        with self.session() as s:
            row = s.get(MarketMappingRow, mapping_id)
            if row is None:
                return False
            row.status = status
            row.updated_at = utcnow()
            if status == "retired":
                row.retired_at = utcnow()
            return True

    # -- reads ---------------------------------------------------------- #
    def open_trades(self) -> list[TradeRow]:
        with self.session() as s:
            return list(s.scalars(select(TradeRow).where(TradeRow.status == "OPEN")))

    def open_trade_count(self, market_id: str) -> int:
        with self.session() as s:
            return len(
                list(
                    s.scalars(
                        select(TradeRow).where(
                            TradeRow.status == "OPEN",
                            TradeRow.market_id == market_id,
                        )
                    )
                )
            )

    def all_trades(self) -> list[TradeRow]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(TradeRow)
                    .options(selectinload(TradeRow.fills))
                    .order_by(TradeRow.created_at)
                )
            )
