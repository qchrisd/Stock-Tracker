from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
import yfinance as yf

db = SQLAlchemy()


class Stock(db.Model):
    """Model for tracking stocks and their historical prices"""
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(10), unique=True, nullable=False)
    add_date = db.Column(db.Date, nullable=False)
    shares = db.Column(db.Float, nullable=False)
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

    def get_initial_value(self):
        """Calculate the total market value at the addition date"""
        return self.shares * self.initial_price

    def get_current_value(self):
        """Calculate the current total market value"""
        current_price = self.get_current_price()
        if current_price is None:
            return None
        return self.shares * current_price

    def get_value_change(self):
        """Calculate the change in total market value from add_date to today"""
        current_value = self.get_current_value()
        if current_value is None:
            return None
        value_change = current_value - self.get_initial_value()
        return value_change

    def get_value_change_percent(self):
        """Calculate the percentage change in total market value"""
        initial_value = self.get_initial_value()
        if initial_value == 0:
            return None
        value_change = self.get_value_change()
        if value_change is None:
            return None
        percent_change = (value_change / initial_value) * 100
        return percent_change

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
