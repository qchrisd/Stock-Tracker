"""
SQLAlchemy models for per-portfolio and per-watchlist databases.

These are plain SQLAlchemy models (not Flask-SQLAlchemy) so they can be used
with dynamically-created per-portfolio and per-watchlist database engines.
All methods that query the database accept a `session` parameter explicitly.
"""
from datetime import datetime
import yfinance as yf

from sqlalchemy import Column, Integer, String, Float, Date, DateTime, UniqueConstraint
from sqlalchemy.orm import declarative_base

PortfolioBase = declarative_base()


class Account(PortfolioBase):
    """Account settings and starting values for a single portfolio."""
    __tablename__ = 'account'

    id = Column(Integer, primary_key=True)
    initial_value = Column(Float, nullable=False, default=0)
    start_date = Column(Date, nullable=False, default=datetime.utcnow)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow,
                        onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<Account initial_value={self.initial_value}>'


class Transaction(PortfolioBase):
    """Individual stock transactions (purchases, sales, dividends, reinvestments)."""
    __tablename__ = 'transaction'

    id = Column(Integer, primary_key=True)
    symbol = Column(String(10), nullable=False)
    type = Column(String(20), nullable=False)   # purchase | sale | dividend | reinvestment
    date = Column(Date, nullable=False)
    shares = Column(Float)                       # purchase, sale, reinvestment
    price_per_share = Column(Float)              # purchase, sale, reinvestment
    amount = Column(Float)                       # dividend cash amount
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<Transaction {self.symbol} {self.type} {self.date}>'


class Stock(PortfolioBase):
    """A tracked stock within a single portfolio or watchlist database."""
    __tablename__ = 'stock'

    id = Column(Integer, primary_key=True)
    symbol = Column(String(10), nullable=False, unique=True)
    add_date = Column(Date, nullable=False)
    shares = Column(Float, nullable=False)
    initial_price = Column(Float, nullable=False)
    date_added = Column(DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<Stock {self.symbol}>'

    # ── Transaction helpers ──────────────────────────────────────────────────

    def get_transactions(self, session):
        return (session.query(Transaction)
                .filter_by(symbol=self.symbol)
                .order_by(Transaction.date)
                .all())

    def get_current_shares_from_transactions(self, session):
        current_shares = 0
        for txn in self.get_transactions(session):
            if txn.type in ('purchase', 'reinvestment'):
                current_shares += txn.shares
            elif txn.type == 'sale':
                current_shares -= txn.shares
        return current_shares

    def get_cost_basis_from_transactions(self, session):
        cost_basis = 0
        for txn in self.get_transactions(session):
            if txn.type in ('purchase', 'reinvestment'):
                cost_basis += txn.shares * txn.price_per_share
        return cost_basis

    def get_proceeds_from_sales(self, session):
        proceeds = 0
        for txn in self.get_transactions(session):
            if txn.type == 'sale':
                proceeds += txn.shares * txn.price_per_share
        return proceeds

    def get_realized_gains_from_transactions(self, session):
        total_shares = 0
        total_cost = 0
        realized = 0
        for txn in self.get_transactions(session):
            if txn.type in ('purchase', 'reinvestment'):
                total_shares += txn.shares
                total_cost += txn.shares * txn.price_per_share
            elif txn.type == 'sale' and total_shares > 0:
                avg_cost = total_cost / total_shares
                realized += txn.shares * txn.price_per_share - txn.shares * avg_cost
                total_shares -= txn.shares
                total_cost -= txn.shares * avg_cost
        return realized

    def get_current_cost_basis_from_transactions(self, session):
        """Cost basis for currently held shares only (avg-cost FIFO)."""
        total_shares = 0
        total_cost = 0
        for txn in self.get_transactions(session):
            if txn.type in ('purchase', 'reinvestment'):
                total_shares += txn.shares
                total_cost += txn.shares * txn.price_per_share
            elif txn.type == 'sale' and total_shares > 0:
                avg_cost = total_cost / total_shares
                total_shares -= txn.shares
                total_cost -= txn.shares * avg_cost
        return total_cost

    def get_dividends_received(self, session):
        return sum(t.amount for t in self.get_transactions(session) if t.type == 'dividend')

    def get_unreinvested_dividends(self, session):
        total = 0
        for txn in self.get_transactions(session):
            if txn.type == 'dividend':
                reinvested = (session.query(Transaction)
                              .filter_by(symbol=self.symbol, type='reinvestment', date=txn.date)
                              .first())
                if not reinvested:
                    total += txn.amount
        return total

    # ── Market data ──────────────────────────────────────────────────────────

    def get_current_price(self):
        try:
            ticker = yf.Ticker(self.symbol)
            data = ticker.history(period='1d')
            if not data.empty:
                return float(data['Close'].iloc[-1])
            return None
        except Exception as e:
            print(f"Error fetching price for {self.symbol}: {e}")
            return None

    def get_initial_value(self, session):
        return self.get_cost_basis_from_transactions(session)

    def get_current_value(self, session):
        price = self.get_current_price()
        if price is None:
            return None
        return self.get_current_shares_from_transactions(session) * price

    def get_value_change(self, session):
        current_value = self.get_current_value(session)
        if current_value is None:
            return None
        cost_basis = self.get_cost_basis_from_transactions(session)
        proceeds = self.get_proceeds_from_sales(session)
        return current_value + proceeds - cost_basis

    def get_value_change_percent(self, session):
        cost_basis = self.get_cost_basis_from_transactions(session)
        if cost_basis == 0:
            return None
        change = self.get_value_change(session)
        if change is None:
            return None
        return (change / cost_basis) * 100

    def get_historical_data(self):
        try:
            ticker = yf.Ticker(self.symbol)
            hist = ticker.history(start=self.add_date)
            if hist.empty:
                return None
            initial_price = hist['Close'].iloc[0]
            return [
                {
                    'date': date.strftime('%Y-%m-%d'),
                    'price': float(row['Close']),
                    'percent_change': ((row['Close'] - initial_price) / initial_price) * 100,
                }
                for date, row in hist.iterrows()
            ]
        except Exception as e:
            print(f"Error fetching historical data for {self.symbol}: {e}")
            return None

    @staticmethod
    def get_sp500_historical_data(start_date):
        try:
            ticker = yf.Ticker('^GSPC')
            hist = ticker.history(start=start_date)
            if hist.empty:
                return None
            initial_price = hist['Close'].iloc[0]
            return [
                {
                    'date': date.strftime('%Y-%m-%d'),
                    'price': float(row['Close']),
                    'percent_change': ((row['Close'] - initial_price) / initial_price) * 100,
                }
                for date, row in hist.iterrows()
            ]
        except Exception as e:
            print(f"Error fetching S&P 500 historical data: {e}")
            return None

    def to_dict(self, session):
        current_price = self.get_current_price()
        return {
            'id': self.id,
            'symbol': self.symbol,
            'shares': self.shares,
            'add_date': self.add_date.isoformat(),
            'initial_price': self.initial_price,
            'initial_value': self.get_initial_value(session),
            'current_price': current_price,
            'current_value': self.get_current_value(session),
            'value_change': self.get_value_change(session),
            'percent_change': self.get_value_change_percent(session),
            'date_added': self.date_added.isoformat(),
        }
