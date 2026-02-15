from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
import yfinance as yf

db = SQLAlchemy()


class Stock(db.Model):
    """Model for tracking stocks and their historical prices"""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(10), unique=True, nullable=False)
    add_date = db.Column(db.Date, nullable=False)
    initial_price = db.Column(db.Float, nullable=False)
    date_added = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    def __repr__(self):
        return f'<Stock {self.symbol}>'

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

    def get_price_change(self):
        """Calculate the change in price from add_date to today"""
        current_price = self.get_current_price()
        if current_price is None:
            return None
        price_change = current_price - self.initial_price
        return price_change

    def get_price_change_percent(self):
        """Calculate the percentage change in price"""
        if self.initial_price == 0:
            return None
        price_change = self.get_price_change()
        if price_change is None:
            return None
        percent_change = (price_change / self.initial_price) * 100
        return percent_change

    def to_dict(self):
        """Convert stock object to dictionary"""
        current_price = self.get_current_price()
        price_change = self.get_price_change()
        percent_change = self.get_price_change_percent()

        return {
            'id': self.id,
            'symbol': self.symbol,
            'add_date': self.add_date.isoformat(),
            'initial_price': self.initial_price,
            'current_price': current_price,
            'price_change': price_change,
            'percent_change': percent_change,
            'date_added': self.date_added.isoformat()
        }
