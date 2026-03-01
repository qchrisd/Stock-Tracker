"""
SQLAlchemy models for the shared cache database (cache.db).

StockCache  – cached financial data for all SEC-listed tradeable stocks.
CacheScheduler – settings for the background cache refresh scheduler.
"""
from datetime import datetime
import math

from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Index
from sqlalchemy.orm import declarative_base

CacheBase = declarative_base()


class StockCache(CacheBase):
    """Cached financial and Graham-value metrics for a tradeable stock."""
    __tablename__ = 'stock_cache'

    id = Column(Integer, primary_key=True)
    symbol = Column(String(10), unique=True, nullable=False)
    name = Column(String(255))
    sector = Column(String(100))
    market_cap = Column(Float)
    market_cap_billions = Column(Float)
    forward_pe = Column(Float)
    trailing_pe = Column(Float)
    dividend_yield = Column(Float)
    current_price = Column(Float)
    price_52w_low = Column(Float)
    price_52w_high = Column(Float)
    distance_from_low = Column(Float)
    eps = Column(Float)
    book_value_per_share = Column(Float)
    graham_number = Column(Float)

    # Graham Value metrics (from grahamvalue.com)
    size_in_sales = Column(Float)
    current_assets_to_2x_liabilities = Column(Float)
    net_current_assets_to_ltdebt = Column(Float)
    earnings_stability = Column(Float)
    dividend_record = Column(Float)
    earnings_growth = Column(Float)
    graham_number_percent = Column(Float)
    ncav_or_net_net = Column(Float)
    equity_to_debt = Column(Float)
    size_in_assets = Column(Float)
    rating_score = Column(Float)

    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index('ix_stock_cache_symbol', 'symbol'),
        Index('ix_stock_cache_timestamp', 'timestamp'),
    )

    def __repr__(self):
        return f'<StockCache {self.symbol} @ {self.current_price}>'

    def get_graham_number(self):
        if not self.eps or not self.book_value_per_share or self.eps <= 0 or self.book_value_per_share <= 0:
            return None
        try:
            return math.sqrt(22.5 * self.eps * self.book_value_per_share)
        except Exception:
            return None

    def to_dict(self):
        return {
            'symbol': self.symbol,
            'name': self.name,
            'sector': self.sector,
            'current_price': self.current_price,
            'price_52w_low': self.price_52w_low,
            'price_52w_high': self.price_52w_high,
            'distance_from_low': self.distance_from_low,
            'forward_pe': self.forward_pe,
            'trailing_pe': self.trailing_pe,
            'market_cap': self.market_cap,
            'market_cap_billions': self.market_cap_billions,
            'dividend_yield': self.dividend_yield,
            'eps': self.eps,
            'book_value_per_share': self.book_value_per_share,
            'graham_number': self.graham_number,
            'size_in_sales': self.size_in_sales,
            'current_assets_to_2x_liabilities': self.current_assets_to_2x_liabilities,
            'net_current_assets_to_ltdebt': self.net_current_assets_to_ltdebt,
            'earnings_stability': self.earnings_stability,
            'dividend_record': self.dividend_record,
            'earnings_growth': self.earnings_growth,
            'graham_number_percent': self.graham_number_percent,
            'ncav_or_net_net': self.ncav_or_net_net,
            'equity_to_debt': self.equity_to_debt,
            'size_in_assets': self.size_in_assets,
            'rating_score': self.rating_score,
        }


class CacheScheduler(CacheBase):
    """Settings for the background cache-refresh scheduler."""
    __tablename__ = 'cache_scheduler'

    id = Column(Integer, primary_key=True)
    enabled = Column(Boolean, default=False, nullable=False)
    day_of_week = Column(Integer, default=0, nullable=False)   # 0 = Monday … 6 = Sunday (ET)
    hour = Column(Integer, default=2, nullable=False)           # 0-23 ET
    minute = Column(Integer, default=0, nullable=False)         # 0-59
    last_run = Column(DateTime)
    next_run = Column(DateTime)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    def __repr__(self):
        days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        return (f'<CacheScheduler enabled={self.enabled} '
                f'{days[self.day_of_week]} {self.hour:02d}:{self.minute:02d} UTC>')
