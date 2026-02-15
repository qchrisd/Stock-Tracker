# Stock Tracker

A small Python web application for tracking stock prices from a specified date to today. Monitor your stock investments and see exactly how much they've gained or lost!

## Features

- **Add Stocks with Share Count**: Specify a stock symbol, number of shares, and a date to start tracking from
- **Track Market Value Changes**: Automatically calculates total market value (shares × price) instead of just per-share changes
- **View Portfolio Dashboard**: See all your tracked stocks with comprehensive value metrics
- **Calculate Returns**: View both absolute value changes and percentage returns on your total investment
- **Portfolio Summary**: See your total investment value, current portfolio value, and overall gains/losses
- **Easy Management**: Remove stocks from your portfolio with a single click

## Requirements

- Python 3.14+
- Flask 3.0.0+
- Flask-SQLAlchemy 3.1.0+
- yfinance 0.2.0+

## Installation

1. **Clone or navigate to the project directory:**
   ```bash
   cd /home/chris/Stock-Tracker
   ```

2. **Activate the virtual environment:**
   ```bash
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install flask flask-sqlalchemy yfinance python-dotenv
   ```

## Usage

1. **Start the application:**
   ```bash
   python run.py
   ```

2. **Open your browser:**
   Navigate to `http://localhost:5000`

3. **Add a stock:**
   - Click the "+ Add Stock" button
   - Enter a stock symbol (e.g., AAPL, GOOGL, MSFT)
   - Enter the number of shares you own
   - Choose the date you want to track from
   - The app will fetch the price on that date and calculate value changes

4. **View your portfolio:**
   - See all tracked stocks with their:
     - Number of shares owned
     - Per-share price on your reference date
     - Total market value on your reference date
     - Current per-share price
     - Current total market value
     - Absolute value change ($)
     - Percentage change (%)
   - View portfolio totals showing:
     - Total initial investment value
     - Current total portfolio value
     - Overall portfolio gain/loss ($)
     - Overall portfolio gain/loss (%)

5. **Remove a stock:**
   - Click the "Remove" button on any stock card

## Project Structure

```
Stock-Tracker/
├── app/
│   ├── __init__.py           # Flask app factory
│   ├── config.py             # Configuration settings
│   ├── models.py             # Database models (Stock model)
│   ├── routes.py             # Flask routes and views
│   ├── static/
│   │   └── style.css         # Responsive styling
│   └── templates/
│       ├── base.html         # Base template with navigation
│       ├── index.html        # Portfolio dashboard
│       └── add_stock.html    # Add stock form
├── run.py                    # Entry point
├── pyproject.toml            # Project configuration
└── README.md                 # This file
```

## How It Works

1. **Database**: Uses SQLite (via SQLAlchemy) to store stock symbols, dates, share counts, and initial prices
2. **Stock Data**: Retrieves historical and current prices from Yahoo Finance via yfinance
3. **Value Calculations**: 
   - **Initial Market Value**: Shares × Initial Price (per-share price on add_date)
   - **Current Market Value**: Shares × Current Price (today's price)
   - **Value Change**: Current Market Value - Initial Market Value
   - **Percentage Change**: (Value Change / Initial Market Value) × 100%

## Database

The app automatically creates a SQLite database (`stock_tracker.db`) on first run with the following structure:

**Stock Table:**
- `id`: Unique identifier
- `symbol`: Stock ticker symbol (e.g., AAPL)
- `add_date`: The date you're tracking from
- `shares`: Number of shares owned (supports decimals)
- `initial_price`: Stock price per share on the add_date
- `date_added`: When the stock was added to your portfolio

## Configuration

The app supports different environments via `app/config.py`:
- **Development**: Debug mode enabled, SQLite database
- **Production**: Debug mode disabled, can use external database
- **Testing**: In-memory database for testing

Set the environment:
```bash
export FLASK_ENV=production  # or development/testing
```

## API Endpoints

In addition to the web interface, the app provides an API endpoint:

**GET** `/api/stock/<symbol>` - Get real-time data for a stock

Example:
```bash
curl http://localhost:5000/api/stock/AAPL
```

Response:
```json
{
  "id": 1,
  "symbol": "AAPL",
  "shares": 50,
  "add_date": "2025-01-01",
  "initial_price": 150.00,
  "initial_value": 7500.00,
  "current_price": 160.00,
  "current_value": 8000.00,
  "value_change": 500.00,
  "percent_change": 6.67,
  "date_added": "2026-02-15T10:30:00"
}
```

## Troubleshooting

**Issue**: "Could not find price data for [SYMBOL]"
- Solution: Verify the stock symbol is correct and that the date is a trading day

**Issue**: Application won't start
- Solution: Make sure all dependencies are installed: `pip install -r requirements.txt`

**Issue**: Database errors
- Solution: Delete `stock_tracker.db` and restart the app to recreate the database

## Future Enhancements

- Add user authentication for personal portfolios
- Portfolio performance charts and visualizations
- Historical value tracking over time
- Price alerts and notifications for significant changes
- Export portfolio data to CSV/PDF
- Support for multiple portfolio tracking
- Dividend tracking and yield calculations
- Tax lot accounting for more detailed analysis
- Mobile app version
- Real-time market data with WebSocket updates

## License

This project is open source and available under the MIT License.