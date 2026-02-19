from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
import yfinance as yf

db = SQLAlchemy()


class Account(db.Model):
    """Model for tracking account settings and starting values"""
    id = db.Column(db.Integer, primary_key=True)
    initial_value = db.Column(db.Float, nullable=False, default=0)
    start_date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<Account initial_value={self.initial_value}>'


class Transaction(db.Model):
    """Model for tracking individual stock transactions (purchases, sales, dividends, reinvestments)"""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(10), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # 'purchase', 'sale', 'dividend', 'reinvestment'
    date = db.Column(db.Date, nullable=False)
    shares = db.Column(db.Float)  # For purchase, sale, reinvestment
    price_per_share = db.Column(db.Float)  # For purchase, sale
    amount = db.Column(db.Float)  # For dividend amount received
    is_watchlist = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    
    def __repr__(self):
        return f'<Transaction {self.symbol} {self.type} {self.date}>'


class Stock(db.Model):
    """Model for tracking stocks and their historical prices"""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(10), nullable=False)
    add_date = db.Column(db.Date, nullable=False)
    shares = db.Column(db.Float, nullable=False)
    initial_price = db.Column(db.Float, nullable=False)
    date_added = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    is_watchlist = db.Column(db.Boolean, default=False, nullable=False)
    
    __table_args__ = (db.UniqueConstraint('symbol', 'is_watchlist', name='unique_symbol_type'),)

    def __repr__(self):
        return f'<Stock {self.symbol}>'

    def get_transactions(self):
        """Get all transactions for this stock"""
        return Transaction.query.filter_by(symbol=self.symbol, is_watchlist=self.is_watchlist).order_by(Transaction.date).all()
    
    def get_current_shares_from_transactions(self):
        """Calculate current shares from all transactions (purchases - sales + reinvestments)"""
        transactions = self.get_transactions()
        current_shares = 0
        for txn in transactions:
            if txn.type in ('purchase', 'reinvestment'):
                current_shares += txn.shares
            elif txn.type == 'sale':
                current_shares -= txn.shares
        return current_shares
    
    def get_cost_basis_from_transactions(self):
        """Calculate total cost basis from all purchases and reinvestments"""
        transactions = self.get_transactions()
        cost_basis = 0
        for txn in transactions:
            if txn.type == 'purchase':
                cost_basis += txn.shares * txn.price_per_share
            elif txn.type == 'reinvestment':
                cost_basis += txn.shares * txn.price_per_share
        return cost_basis
    
    def get_proceeds_from_sales(self):
        """Calculate total proceeds from all sales"""
        transactions = self.get_transactions()
        proceeds = 0
        for txn in transactions:
            if txn.type == 'sale':
                proceeds += txn.shares * txn.price_per_share
        return proceeds
    
    def get_realized_gains_from_transactions(self):
        """Calculate realized gains from sales"""
        transactions = self.get_transactions()
        total_shares_purchased = 0
        total_cost_of_purchases = 0
        realized_gains = 0
        
        for txn in transactions:
            if txn.type in ('purchase', 'reinvestment'):
                total_shares_purchased += txn.shares
                total_cost_of_purchases += txn.shares * txn.price_per_share
            elif txn.type == 'sale':
                # Calculate average cost per share
                if total_shares_purchased > 0:
                    avg_cost = total_cost_of_purchases / total_shares_purchased
                    sale_proceeds = txn.shares * txn.price_per_share
                    sale_cost = txn.shares * avg_cost
                    realized_gains += sale_proceeds - sale_cost
                    # Reduce shares and cost basis for next calculation
                    total_shares_purchased -= txn.shares
                    total_cost_of_purchases -= txn.shares * avg_cost
        
        return realized_gains
    
    def get_current_cost_basis_from_transactions(self):
        """Calculate cost basis only for currently held shares (FIFO method)"""
        transactions = self.get_transactions()
        total_shares_purchased = 0
        total_cost_of_purchases = 0
        
        for txn in transactions:
            if txn.type in ('purchase', 'reinvestment'):
                total_shares_purchased += txn.shares
                total_cost_of_purchases += txn.shares * txn.price_per_share
            elif txn.type == 'sale':
                # Reduce shares and cost basis for next calculation (FIFO)
                if total_shares_purchased > 0:
                    avg_cost = total_cost_of_purchases / total_shares_purchased
                    total_shares_purchased -= txn.shares
                    total_cost_of_purchases -= txn.shares * avg_cost
        
        return total_cost_of_purchases
    
    def get_dividends_received(self):
        """Calculate total dividends received"""
        transactions = self.get_transactions()
        total_dividends = 0
        for txn in transactions:
            if txn.type == 'dividend':
                total_dividends += txn.amount
        return total_dividends
    
    def get_unreinvested_dividends(self):
        """Calculate dividends received that were NOT reinvested (received as cash)"""
        transactions = self.get_transactions()
        total_unreinvested = 0
        
        for txn in transactions:
            if txn.type == 'dividend':
                # Check if there's a corresponding reinvestment transaction on the same date
                reinvestment = Transaction.query.filter_by(
                    symbol=self.symbol,
                    type='reinvestment',
                    date=txn.date,
                    is_watchlist=self.is_watchlist
                ).first()
                
                # Only count this dividend if it wasn't reinvested
                if not reinvestment:
                    total_unreinvested += txn.amount
        
        return total_unreinvested

    def get_current_price(self):
        """Fetch the current price of the stock from yfinance"""
        try:
            ticker = yf.Ticker(self.symbol)
            data = ticker.history(period='1d')
            if not data.empty:
                return float(data['Close'].iloc[-1])
            return None
        except Exception as e:
            print(f"Error fetching price for {self.symbol}: {e}")
            return None

    def get_initial_value(self):
        """Calculate the total cost basis (money invested)"""
        return self.get_cost_basis_from_transactions()

    def get_current_value(self):
        """Calculate the current total market value"""
        current_price = self.get_current_price()
        if current_price is None:
            return None
        current_shares = self.get_current_shares_from_transactions()
        return current_shares * current_price

    def get_value_change(self):
        """Calculate the total gain/loss including unrealized and realized gains"""
        current_value = self.get_current_value()
        if current_value is None:
            return None
        
        cost_basis = self.get_cost_basis_from_transactions()
        proceeds_from_sales = self.get_proceeds_from_sales()
        realized_gains = self.get_realized_gains_from_transactions()
        
        # Total gain/loss = unrealized gains + realized gains
        # Unrealized gains = current value - (cost basis - proceeds from sales)
        # Total = current value + proceeds - cost basis
        total_gain_loss = current_value + proceeds_from_sales - cost_basis
        
        return total_gain_loss

    def get_value_change_percent(self):
        """Calculate the total percentage return"""
        cost_basis = self.get_cost_basis_from_transactions()
        if cost_basis == 0:
            return None
        value_change = self.get_value_change()
        if value_change is None:
            return None
        percent_change = (value_change / cost_basis) * 100
        return percent_change

    def get_historical_data(self):
        """Fetch historical price data from add_date to today"""
        try:
            ticker = yf.Ticker(self.symbol)
            # Get data from add_date to today
            hist = ticker.history(start=self.add_date)
            if hist.empty:
                return None
            
            # Calculate normalized percentage returns
            normalized_data = []
            initial_price = hist['Close'].iloc[0]
            
            for date, row in hist.iterrows():
                percent_change = ((row['Close'] - initial_price) / initial_price) * 100
                normalized_data.append({
                    'date': date.strftime('%Y-%m-%d'),
                    'price': float(row['Close']),
                    'percent_change': percent_change
                })
            
            return normalized_data
        except Exception as e:
            print(f"Error fetching historical data for {self.symbol}: {e}")
            return None

    @staticmethod
    def get_sp500_historical_data(start_date):
        """Fetch S&P 500 historical data from start_date to today"""
        try:
            ticker = yf.Ticker('^GSPC')
            hist = ticker.history(start=start_date)
            if hist.empty:
                return None
            
            # Calculate normalized percentage returns
            normalized_data = []
            initial_price = hist['Close'].iloc[0]
            
            for date, row in hist.iterrows():
                percent_change = ((row['Close'] - initial_price) / initial_price) * 100
                normalized_data.append({
                    'date': date.strftime('%Y-%m-%d'),
                    'price': float(row['Close']),
                    'percent_change': percent_change
                })
            
            return normalized_data
        except Exception as e:
            print(f"Error fetching S&P 500 historical data: {e}")
            return None

    def to_dict(self):
        """Convert stock object to dictionary"""
        current_price = self.get_current_price()
        current_value = self.get_current_value()
        value_change = self.get_value_change()
        percent_change = self.get_value_change_percent()

        return {
            'id': self.id,
            'symbol': self.symbol,
            'shares': self.shares,
            'add_date': self.add_date.isoformat(),
            'initial_price': self.initial_price,
            'initial_value': self.get_initial_value(),
            'current_price': current_price,
            'current_value': current_value,
            'value_change': value_change,
            'percent_change': percent_change,
            'date_added': self.date_added.isoformat()
        }

class StockCache(db.Model):
    """Model for caching financial data of all tradeable stocks"""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(10), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255))
    sector = db.Column(db.String(100))
    market_cap = db.Column(db.Float)  # In dollars
    market_cap_billions = db.Column(db.Float)  # In billions (for convenience)
    forward_pe = db.Column(db.Float)
    trailing_pe = db.Column(db.Float)
    dividend_yield = db.Column(db.Float)
    current_price = db.Column(db.Float)
    price_52w_low = db.Column(db.Float)
    price_52w_high = db.Column(db.Float)
    distance_from_low = db.Column(db.Float)  # Percentage
    eps = db.Column(db.Float)  # Earnings per share
    book_value_per_share = db.Column(db.Float)  # Tangible book value per share
    graham_number = db.Column(db.Float)  # Graham Number from GrahamValue
    
    # Graham Value Metrics (from GrahamValue.com)
    size_in_sales = db.Column(db.Float)  # Percentage
    current_assets_to_2x_liabilities = db.Column(db.Float)  # Percentage
    net_current_assets_to_ltdebt = db.Column(db.Float)  # Percentage
    earnings_stability = db.Column(db.Float)  # Percentage
    dividend_record = db.Column(db.Float)  # Percentage
    earnings_growth = db.Column(db.Float)  # Percentage
    graham_number_percent = db.Column(db.Float)  # Percentage
    ncav_or_net_net = db.Column(db.Float)  # Percentage (Net-Net)
    equity_to_debt = db.Column(db.Float)  # 2 x Equity / Debt, Percentage
    size_in_assets = db.Column(db.Float)  # Percentage
    rating_score = db.Column(db.Float)  # Overall rating score
    
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)
    
    def __repr__(self):
        return f'<StockCache {self.symbol} @ {self.current_price}>'
    
    def get_graham_number(self):
        """Calculate Graham Number: √(22.5 × EPS × Book Value Per Share)"""
        if not self.eps or not self.book_value_per_share or self.eps <= 0 or self.book_value_per_share <= 0:
            return None
        try:
            import math
            graham_num = math.sqrt(22.5 * self.eps * self.book_value_per_share)
            return graham_num
        except:
            return None
    
    def to_dict(self):
        """Convert to dictionary for JSON serialization"""
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
            'rating_score': self.rating_score
        }


class CacheScheduler(db.Model):
    """Model for storing cache scheduler settings"""
    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False, nullable=False)  # Is the scheduler enabled?
    day_of_week = db.Column(db.Integer, default=0, nullable=False)  # 0=Monday, 6=Sunday
    hour = db.Column(db.Integer, default=2, nullable=False)  # Hour (0-23, in UTC)
    minute = db.Column(db.Integer, default=0, nullable=False)  # Minute (0-59)
    last_run = db.Column(db.DateTime)  # Last time cache was updated
    next_run = db.Column(db.DateTime)  # Next scheduled run
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        return f'<CacheScheduler enabled={self.enabled} {day_names[self.day_of_week]} {self.hour:02d}:{self.minute:02d}>'
    
    @staticmethod
    def get_or_create():
        """Get existing scheduler config or create default"""
        scheduler = CacheScheduler.query.first()
        if not scheduler:
            scheduler = CacheScheduler(
                enabled=False,
                day_of_week=0,  # Monday
                hour=2,  # 2 AM
                minute=0
            )
            db.session.add(scheduler)
            db.session.commit()
        return scheduler
