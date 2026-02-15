from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from datetime import datetime
from app.models import db, Stock

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def index():
    """Display all tracked stocks and their price changes"""
    stocks = Stock.query.all()
    portfolio_data = []

    for stock in stocks:
        try:
            portfolio_data.append({
                'stock': stock,
                'current_price': stock.get_current_price(),
                'price_change': stock.get_price_change(),
                'percent_change': stock.get_price_change_percent()
            })
        except Exception as e:
            flash(f"Error fetching data for {stock.symbol}: {str(e)}", 'error')

    return render_template('index.html', portfolio=portfolio_data)


@main_bp.route('/add', methods=['GET', 'POST'])
def add_stock():
    """Add a new stock to track"""
    if request.method == 'POST':
        symbol = request.form.get('symbol', '').upper().strip()
        date_str = request.form.get('date')
        
        if not symbol or not date_str:
            flash('Please provide both symbol and date', 'error')
            return redirect(url_for('main.add_stock'))

        # Check if stock already exists
        if Stock.query.filter_by(symbol=symbol).first():
            flash(f'Stock {symbol} is already being tracked', 'error')
            return redirect(url_for('main.add_stock'))

        try:
            # Parse the date
            add_date = datetime.strptime(date_str, '%Y-%m-%d').date()

            # Fetch the stock price on the specified date
            import yfinance as yf
            ticker = yf.Ticker(symbol)
            
            # Get historical data starting from the specified date
            hist = ticker.history(start=add_date, end=add_date)
            
            if hist.empty:
                # Try to get the price from the next available trading day
                hist = ticker.history(start=add_date, period='5d')
                if hist.empty:
                    flash(f'Could not find price data for {symbol} on or after {add_date}', 'error')
                    return redirect(url_for('main.add_stock'))

            initial_price = float(hist['Close'].iloc[0])

            # Create new stock record
            new_stock = Stock(
                symbol=symbol,
                add_date=add_date,
                initial_price=initial_price
            )
            
            db.session.add(new_stock)
            db.session.commit()

            flash(f'Successfully added {symbol} at ${initial_price:.2f} on {add_date}', 'success')
            return redirect(url_for('main.index'))

        except ValueError:
            flash('Invalid date format. Please use YYYY-MM-DD', 'error')
            return redirect(url_for('main.add_stock'))
        except Exception as e:
            db.session.rollback()
            flash(f'Error adding stock: {str(e)}', 'error')
            return redirect(url_for('main.add_stock'))

    return render_template('add_stock.html')


@main_bp.route('/stock/<int:stock_id>/delete', methods=['POST'])
def delete_stock(stock_id):
    """Delete a tracked stock"""
    stock = Stock.query.get(stock_id)
    
    if not stock:
        flash('Stock not found', 'error')
        return redirect(url_for('main.index'))

    try:
        symbol = stock.symbol
        db.session.delete(stock)
        db.session.commit()
        flash(f'Successfully removed {symbol} from tracking', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting stock: {str(e)}', 'error')

    return redirect(url_for('main.index'))


@main_bp.route('/api/stock/<symbol>')
def get_stock_data(symbol):
    """API endpoint to get real-time data for a stock"""
    stock = Stock.query.filter_by(symbol=symbol.upper()).first()
    
    if not stock:
        return jsonify({'error': 'Stock not found'}), 404

    try:
        return jsonify(stock.to_dict())
    except Exception as e:
        return jsonify({'error': str(e)}), 500
