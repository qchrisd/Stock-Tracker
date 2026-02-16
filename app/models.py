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
    
    def get_dividends_received(self):
        """Calculate total dividends received"""
        transactions = self.get_transactions()
        total_dividends = 0
        for txn in transactions:
            if txn.type == 'dividend':
                total_dividends += txn.amount
        return total_dividends

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
